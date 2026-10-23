"""The Powerwall, read from Tesla's cloud through the account Car Command already has.

Two jobs, one worker. `live_status`, asked every five minutes while a site is switched on, is the
house now (kept in memory for Now) and a row of cc_energy. And a history backfill, only when someone
starts it: five-minute power, one day a request, newest first, throttled, kept place by place in
cc_state so a restart or a refusal resumes where it was.

Nothing here touches the network on its own: the Fleet client, the token and the time are
handed in.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone

import energy
import fleet_usage
from tesla_fleet import TeslaError

LOG = logging.getLogger("energy_cloud")
BACKFILL_KEY = "energy_backfill"
LIVE_EVERY_S = 300
STEP_GAP_S = 2.0  # between history requests
BACKOFF_FIRST_S, BACKOFF_MAX_S = 60, 1800  # after a rate limit or a failed write, doubling
EMPTY_STOP = 30  # days with nothing in a row, where no install date is known: that is the start
DAY_ROWS = 200  # a day with this many readings held is not asked for
PER_REQUEST = 0.002  # dollars, Tesla's Fleet API data-request price at the time of writing: only an estimate
SCOPE_WORDS = "Tesla would not read the Powerwall. Sign in with Tesla again (Connect Tesla) to grant energy access."


def _watts(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def reading_from_live(body: dict) -> dict:
    """A live_status as a reading. Tesla's signs are a Powerwall's: grid positive importing,
    battery positive discharging. The house's load, when it is not given, is what the sources
    add up to."""
    solar, battery, grid = _watts(body.get("solar_power")), _watts(body.get("battery_power")), _watts(body.get("grid_power"))
    home = _watts(body.get("load_power"))
    return {
        "solar_w": solar,
        "home_w": home if home is not None else energy.home_from(solar, battery, grid),
        "grid_w": grid,
        "battery_w": battery,
        "soe_pct": _watts(body.get("percentage_charged")),
        "grid_status": body.get("grid_status"),
    }


def rows_from_series(series: list) -> list[dict]:
    """calendar_history samples as cc_energy rows, UTC; a sample without a readable time or
    with nothing to say is left out."""
    out = []
    for p in series:
        if not isinstance(p, dict):
            continue
        try:
            at = datetime.fromisoformat(str(p.get("timestamp")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if at.tzinfo is None:
            continue
        solar, battery, grid = _watts(p.get("solar_power")), _watts(p.get("battery_power")), _watts(p.get("grid_power"))
        if solar is None and battery is None and grid is None:
            continue
        out.append({"at": at.astimezone(timezone.utc), "solar_w": solar, "battery_w": battery, "grid_w": grid, "home_w": energy.home_from(solar or 0.0, battery or 0.0, grid or 0.0), "soe_pct": None})
    return out


def days_between(first: str, last: date) -> int:
    """Days from `first` to `last`, both counted."""
    return (last - date.fromisoformat(first)).days + 1


def cost_note(days: int) -> str:
    return (
        f"One request a day, {days:,} in all. Tesla bills Fleet API requests to your developer account beyond a free allowance; at about "
        f"${PER_REQUEST:.3f} each that is an estimate of ${days * PER_REQUEST:,.2f}. It runs once, newest first, and can be stopped and resumed."
    )


def progress(get) -> dict:
    """The backfill's state as kept: idle, running, paused, done or error, and where it is."""
    try:
        got = json.loads(get(BACKFILL_KEY) or "{}")
    except ValueError:
        got = {}
    return {"state": "idle", "next": None, "stop": None, "total": 0, "done": 0, "empty_run": 0, "error": None, "updated": None, "fails": 0, "retry_at": None, **got}


class CloudWorker(threading.Thread):
    def __init__(self, *, fleet, access, site, tz, get, put, write, have_day, live, sleep=time.sleep, now=lambda: datetime.now(timezone.utc)):
        super().__init__(daemon=True, name="energy-cloud")
        self.fleet, self.access, self.site, self.tz = fleet, access, site, tz
        self.get, self.put, self.write, self.have_day, self.live = get, put, write, have_day, live
        self.sleep, self.now = sleep, now
        self.last_live: datetime | None = None
        self.status: dict | None = None
        self._stop_event = threading.Event()

    # ---- the live reading
    def fallback(self, now: datetime) -> None:
        if self.last_live is not None and (now - self.last_live).total_seconds() < fleet_usage.slowed(LIVE_EVERY_S):
            return
        site, got, fleet = self.site(), self.access(), self.fleet()
        if not (site and got and fleet):
            return
        self.last_live = now
        try:
            reading = reading_from_live(fleet.energy_live(got[0], got[1], site))
        except TeslaError as err:
            self.status = {"ok": False, "at": now, "scope": err.status in (401, 403), "error": SCOPE_WORDS if err.status in (401, 403) else err.public}
            return
        self.status = {"ok": True, "at": now, "scope": False, "error": None}
        self.live.set(reading, "cloud", now)
        try:
            self.write([{"at": now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0), **{f: reading[f] for f in energy.FIELDS}}])  # on the five-minute grid: history upserts over it
        except Exception as err:  # noqa: BLE001
            LOG.warning("energy cloud: could not write a reading (%s)", type(err).__name__)

    # ---- the backfill
    def _keep(self, prog: dict) -> dict:
        prog["updated"] = datetime.now(timezone.utc).isoformat()
        self.put(BACKFILL_KEY, json.dumps(prog))
        return prog

    def start_backfill(self, first: date) -> dict:
        """Begin at `first` (the app passes yesterday) and go back, or resume where a stopped or failed run left off."""
        prog = progress(self.get)
        if prog["state"] in ("paused", "error", "running") and prog["next"]:
            prog.update(state="running", error=None)
            return self._keep(prog)
        installed = None
        site, got, fleet = self.site(), self.access(), self.fleet()
        if site and got and fleet:
            try:
                installed = fleet.energy_installed(got[0], got[1], site)
            except TeslaError:
                installed = None
        prog = {**progress(lambda _k: None), "state": "running", "next": first.isoformat(), "stop": installed, "total": days_between(installed, first) if installed else 0}
        return self._keep(prog)

    def wait_s(self) -> float:
        """How long the backfill has to wait before its next request: the time left on a back-off
        (from the kept retry_at), else 0. Lets the thread sleep it out instead of looking each second."""
        prog = progress(self.get)
        if prog["state"] != "running" or not prog.get("retry_at"):
            return 0.0
        return max(0.0, (datetime.fromisoformat(prog["retry_at"]) - self.now()).total_seconds())

    def stop_backfill(self) -> dict:
        prog = progress(self.get)
        if prog["state"] == "running":
            prog["state"] = "paused"
            self._keep(prog)
        return prog

    def _back_off(self, prog: dict) -> str:
        """Wait before the next request, 60 s doubling to 30 minutes; nothing is asked sooner."""
        wait = min(BACKOFF_MAX_S, BACKOFF_FIRST_S * 2 ** prog.get("fails", 0))
        prog["fails"] = prog.get("fails", 0) + 1
        prog["retry_at"] = (self.now() + timedelta(seconds=wait)).isoformat()
        self._keep(prog)
        return "running"

    def backfill_step(self) -> str:
        """One day of history; returns the state afterwards (running, done, error). Nothing it can
        meet escapes: a refusal or anything unexpected is the error state, a rate limit or a failed
        write waits out a growing back-off with the same day still next."""
        prog = progress(self.get)
        if prog["state"] != "running" or not prog["next"]:
            return prog["state"]
        if prog.get("retry_at") and self.now() < datetime.fromisoformat(prog["retry_at"]):
            return "running"
        day = date.fromisoformat(prog["next"])
        if prog["stop"] and day < date.fromisoformat(prog["stop"]):
            prog["state"] = "done"
            self._keep(prog)
            return "done"
        asked = False
        try:
            if self.have_day(day):
                rows: list[dict] = []
            else:
                site, got, fleet = self.site(), self.access(), self.fleet()
                if not (site and got and fleet):
                    prog.update(state="error", error="Tesla is not connected.")
                    self._keep(prog)
                    return "error"
                asked = True
                rows = rows_from_series(fleet.energy_power_day(got[0], got[1], site, day, self.tz))
        except TeslaError as err:
            if err.status == 429:
                return self._back_off(prog)
            prog.update(state="error", error=SCOPE_WORDS if err.status in (401, 403) else err.public)
            self._keep(prog)
            return "error"
        except Exception as err:  # noqa: BLE001 - not a hot loop, and not a crash
            LOG.warning("energy cloud backfill: %s", type(err).__name__)
            prog.update(state="error", error="The history could not be read; resume to try again.")
            self._keep(prog)
            return "error"
        if rows:
            try:
                self.write(rows)
            except Exception as err:  # noqa: BLE001
                LOG.warning("energy cloud backfill: could not write a day (%s)", type(err).__name__)
                return self._back_off(prog)
        prog["empty_run"] = 0 if rows or not asked else prog["empty_run"] + 1
        prog["done"] += 1
        prog["next"] = (day - timedelta(days=1)).isoformat()
        prog["fails"], prog["retry_at"] = 0, None
        if (not prog["stop"] and prog["empty_run"] >= EMPTY_STOP) or (prog["stop"] and day <= date.fromisoformat(prog["stop"])):
            prog["state"] = "done"
        self._keep(prog)
        if asked:
            self.sleep(fleet_usage.slowed(STEP_GAP_S))
        return prog["state"]

    # ---- the thread
    def run(self) -> None:
        while not self._stop_event.wait(1.0):
            try:
                now = datetime.now(timezone.utc)
                if self.site():
                    self.fallback(now)
                    if progress(self.get)["state"] == "running":
                        wait = self.wait_s()
                        if wait > 0:
                            self._stop_event.wait(min(wait + 0.5, 60.0))  # until the back-off is over
                            continue
                        self.backfill_step()
                        continue
                self._stop_event.wait(14.0)
            except Exception as err:  # noqa: BLE001 - the worker outlives anything one pass throws
                LOG.warning("energy cloud: %s", type(err).__name__)

    def stop(self) -> None:
        self._stop_event.set()
