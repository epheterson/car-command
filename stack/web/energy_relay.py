"""The Powerwall, read on the house's own network through a pypowerwall proxy (POWERWALL_RELAY_URL).

The live reading every EVERY_S, kept in memory for Now and, averaged over each minute, a row of
cc_energy with source 'relay'. A minute's average is the energy that minute held, which the charge
split integrates; a single sample at the minute would be a guess at it. While the relay answers,
Tesla's cloud is not asked for the live reading (`fresh`); its nightly refill passes over days the
relay filled, and still reads the days it missed.

The proxy's /soe is the gateway's own figure, which keeps a 5% reserve below the scale the Tesla app
shows; it is put on the app's scale here, so every row reads the same whichever source wrote it.

Nothing here touches the network on its own: the fetch and the time are handed in.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from datetime import datetime, timezone

import energy

LOG = logging.getLogger("energy_relay")
EVERY_S = 15.0  # the proxy keeps its readings 5 s; a fresher ask buys nothing
TIMEOUT_S = 5.0
FRESH_S = 90.0  # a reading older than this hands the live reading back to the cloud
RESERVE = 5.0  # the gateway's percent below the app's 0%


def soe_app(raw) -> float | None:
    """The gateway's state of energy on the Tesla app's scale: (raw − 5) / 0.95, held to 0–100."""
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    return round(min(100.0, max(0.0, (raw - RESERVE) / (1 - RESERVE / 100))), 1)


def _power(agg: dict, meter: str) -> float | None:
    v = (agg.get(meter) or {}).get("instant_power") if isinstance(agg.get(meter), dict) else None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def reading_from(agg: dict, soe: dict) -> dict | None:
    """/aggregates and /soe as a reading, or None when they say nothing. The proxy's signs are a
    Powerwall's (site positive importing, battery positive discharging); the panels' few watts of
    night draw read as no sun."""
    solar, battery, grid, home = _power(agg, "solar"), _power(agg, "battery"), _power(agg, "site"), _power(agg, "load")
    if solar is None and battery is None and grid is None:
        return None
    if solar is not None:
        solar = max(0.0, solar)
    return {
        "solar_w": solar,
        "home_w": max(0.0, home) if home is not None else energy.home_from(solar, battery, grid),
        "grid_w": grid,
        "battery_w": battery,
        "soe_pct": soe_app((soe or {}).get("percentage")),
    }


def minute_row(minute: datetime, samples: list[dict]) -> dict:
    """The minute's samples as one row: each power its mean over the samples that had it, the
    charge its last."""
    row: dict = {"at": minute}
    for f in ("solar_w", "home_w", "grid_w", "battery_w"):
        vals = [s[f] for s in samples if s.get(f) is not None]
        row[f] = round(sum(vals) / len(vals), 1) if vals else None
    row["soe_pct"] = next((s["soe_pct"] for s in reversed(samples) if s.get("soe_pct") is not None), None)
    return row


def address(text: str) -> str | None:
    """An address typed in Settings as a base URL, or None when it is not one: http or https, a host, an optional port."""
    from urllib.parse import urlsplit

    text = (text or "").strip()
    if text and "://" not in text:
        text = "http://" + text
    text = text.rstrip("/")
    try:
        u = urlsplit(text)
        u.port  # noqa: B018 - a bad port raises here
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname or u.query or u.fragment or u.username:
        return None
    return text


def http_get(base: str):
    """A fetch of a path under base, as JSON; raises on anything but a 200 with JSON."""

    def get(path: str) -> dict:
        with urllib.request.urlopen(base.rstrip("/") + path, timeout=TIMEOUT_S) as r:  # noqa: S310 - the owner's own LAN address
            return json.loads(r.read())

    return get


class RelayWorker(threading.Thread):
    def __init__(self, *, url, live, write, now=lambda: datetime.now(timezone.utc)):
        super().__init__(daemon=True, name="energy-relay")
        self.url, self.live, self.write, self.now = url, live, write, now
        self.last_ok: datetime | None = None
        self.status: dict | None = None
        self.minute: datetime | None = None
        self.samples: list[dict] = []
        self._stop_event = threading.Event()

    def fresh(self, now: datetime) -> bool:
        """Whether the relay answered lately enough that the cloud need not be asked."""
        return self.last_ok is not None and (now - self.last_ok).total_seconds() < FRESH_S

    def _flush(self) -> None:
        if self.minute is not None and self.samples:
            try:
                self.write([minute_row(self.minute, self.samples)])
            except Exception as err:  # noqa: BLE001 - the next minute tries again
                LOG.warning("energy relay: could not write a reading (%s)", type(err).__name__)
        self.samples = []

    def step(self, get) -> None:
        now = self.now()
        try:
            reading = reading_from(get("/aggregates"), get("/soe"))
        except Exception as err:  # noqa: BLE001 - unreachable, asleep, or not the proxy: the cloud takes over
            if self.status is None or self.status.get("ok"):
                LOG.info("energy relay: not answering (%s)", type(err).__name__)
            self.status = {"ok": False, "at": now, "error": type(err).__name__}
            return
        if reading is None:
            self.status = {"ok": False, "at": now, "error": "no readings"}
            return
        self.last_ok = now
        self.status = {"ok": True, "at": now, "error": None}
        self.live.set(reading, "relay", now)
        minute = now.replace(second=0, microsecond=0)
        if minute != self.minute:
            self._flush()
            self.minute = minute
        self.samples.append(reading)

    def run(self) -> None:
        """The address is read on every pass: set, changed or cleared in Settings, it takes effect at the next."""
        url, get = None, None
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                now_url = self.url()
                if now_url != url:
                    self._flush()
                    url, get = now_url, (http_get(now_url) if now_url else None)
                    self.status, self.last_ok = None, None
                if get:
                    self.step(get)
            except Exception as err:  # noqa: BLE001 - the worker outlives anything one pass throws
                LOG.warning("energy relay: %s", type(err).__name__)
            self._stop_event.wait(max(1.0, EVERY_S - (time.monotonic() - started)))
        self._flush()

    def stop(self) -> None:
        self._stop_event.set()
