"""What Car Command asks of Tesla's Fleet API, counted on disk, and a budget that keeps it
inside Tesla's monthly allowance.

Tesla bills the Fleet API per use and gives every account a monthly credit: US$10 (as of
2026-10), https://developer.tesla.com/ ("Usage Based Pricing") and
https://developer.tesla.com/docs/fleet-api/billing-and-limits . The published rates: 500 data
requests, 1,000 commands, 50 wakes or 150,000 streamed signals per $1. Tesla changes both, so the
credit and the rates are settings (`fleet_credit`, `fleet_rates`) with these defaults. Tesla bills
a request that answers below 500, so only those are counted into the month; every attempt is still
logged. Billing months begin on the first of the month, taken here in UTC.

Every request goes through tesla_fleet.Fleet._send / charging_invoice or tesla_cmd.Proxy._post /
_get, and each asks `guard` first (the budget) and `count` after (the meter); the stream's records
are counted by `signals` in tesla_live. Counting is a dict increment under a lock: nothing waits on
the disk. `tick` writes the batch on a background thread, a few seconds apart, and puts it back if
the write fails. The signing proxy makes its own session handshakes with Tesla, which are not seen
here: about one extra command per car per session.

The budget, as a share of the month's credit spent: from SLOW_AT the background pollers wait
twice as long between asks; from TIGHT_AT four times as long and a background wake is refused;
at STOP_AT the background asks stop until the owner raises the allowance. A command (sent by
hand, or by the charging hold) always goes through, with a warning past the limit. Signing in
and refreshing a token are free and never held back. A credit of 0 turns the budget off."""

from __future__ import annotations

import contextlib
import contextvars
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import strips

CATEGORIES = {"data": "Data requests", "commands": "Commands", "wakes": "Wakes", "signals": "Streamed signals", "other": "Account and setup"}
BILLED = ("data", "commands", "wakes", "signals")  # "other" is free
DEFAULT_CREDIT = 10.0
DEFAULT_RATES = {"data": 0.002, "commands": 0.001, "wakes": 0.02, "signals": 1 / 150000}
SLOW_AT, TIGHT_AT, STOP_AT = 0.5, 0.8, 1.0
FACTORS = {"ok": 1, "slow": 2, "tight": 4, "stopped": 4}
KEEP_CALLS = 500
SHOWN_CALLS = 100
VIN = re.compile(r"/vehicles/([A-Za-z0-9]{11,17})(?:/|\?|$)")

DDL = """
CREATE TABLE IF NOT EXISTS cc_fleet_usage (
    month    VARCHAR(7) NOT NULL,
    vin      VARCHAR NOT NULL,
    category VARCHAR NOT NULL,
    calls    BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (month, vin, category)
);
CREATE TABLE IF NOT EXISTS cc_fleet_calls (
    id       BIGSERIAL PRIMARY KEY,
    at       TIMESTAMPTZ NOT NULL,
    vin      VARCHAR NOT NULL,
    category VARCHAR NOT NULL,
    endpoint VARCHAR NOT NULL,
    status   SMALLINT NOT NULL
);
CREATE TABLE IF NOT EXISTS cc_fleet_alerts (month VARCHAR PRIMARY KEY);
"""

MANUAL = contextvars.ContextVar("fleet_manual", default=False)


@contextlib.contextmanager
def by_hand():
    """Calls made inside are a command's (sent by the owner, or by a rule they set): never held back."""
    token = MANUAL.set(True)
    try:
        yield
    finally:
        MANUAL.reset(token)


class Overspent(Exception):
    """The budget refuses this call; `public` is the sentence to show."""

    def __init__(self, public: str):
        super().__init__(public)
        self.public = public


def classify(method: str, url: str, body=None) -> tuple[str, str, str]:
    """(category, endpoint, vin or "") for one request, from its method and URL."""
    parts = urlparse(url)
    path = parts.path
    found = VIN.search(path)
    vin = found.group(1) if found else ""
    if not vin and isinstance(body, dict) and isinstance(body.get("vins"), list) and len(body["vins"]) == 1:
        vin = str(body["vins"][0])
    if "oauth2" in path or "auth.tesla.com" in (parts.netloc or ""):
        return "other", "token", vin
    if path.endswith("/wake_up"):
        return "wakes", "wake_up", vin
    if "/command/" in path:
        return "commands", path.rsplit("/", 1)[-1], vin
    if path.endswith("/vehicle_data"):
        return "data", "vehicle_data", vin
    if "/dx/charging/" in path:
        return "data", "charging_" + path.rsplit("/", 2)[-2 if "invoice" in path else -1], vin
    if "/energy_sites/" in path:
        return "data", "energy_" + path.rsplit("/", 1)[-1], vin
    if vin and path.endswith(vin):
        return "data", "vehicle", vin
    return "other", path.rsplit("/", 1)[-1] or "request", vin  # the car list, fleet_status, the stream's config, the app's domain


def month_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")


def month_back(month: str, n: int) -> str:
    y, m = int(month[:4]), int(month[5:])
    i = y * 12 + (m - 1) - n
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def billable(status: int) -> bool:
    """Tesla bills a request that answered below 500; one that never got an answer is not billed."""
    return 0 < int(status or 0) < 500


class Meter:
    def __init__(self, clock=time.time):
        self.clock, self.lock = clock, threading.RLock()
        self.credit, self.rates, self.on = DEFAULT_CREDIT, dict(DEFAULT_RATES), True
        self.totals: dict[tuple[str, str, str], int] = {}  # (month, vin, category) -> billed calls, the last 13 months
        self.month_n: dict[str, dict[str, int]] = {}  # month -> category -> billed calls: step() reads this, O(1)
        self.recent: deque = deque(maxlen=SHOWN_CALLS)  # every attempt, newest first
        self.pending: list[tuple] = []  # attempts not yet on disk: (at, vin, category, endpoint, status)
        self.pending_n: dict[tuple[str, str, str], int] = {}  # billed counts not yet on disk
        self.alerted: set[str] = set()  # "YYYY-MM" (80%) and "YYYY-MM:stop" (100%) already sent
        self.new_alerts: list[str] = []
        self.outbox: list[str] = []

    # ---- settings
    def configure(self, st: dict) -> None:
        try:
            credit = float(st.get("fleet_credit"))
        except (TypeError, ValueError):
            credit = DEFAULT_CREDIT
        rates = dict(DEFAULT_RATES)
        given = st.get("fleet_rates")
        for k, v in given.items() if isinstance(given, dict) else ():
            try:
                if k in rates and float(v) >= 0:
                    rates[k] = float(v)
            except (TypeError, ValueError):
                pass
        with self.lock:
            self.credit, self.rates = max(0.0, credit), rates
            self.on = st.get("fleet_budget_on") is not False and self.credit > 0  # no allowance, nothing to hold to

    # ---- what the month has used
    def calls(self, month: str | None = None, vin: str | None = None) -> dict[str, int]:
        month = month or month_of(self.clock())
        with self.lock:
            if vin is None:
                return {c: self.month_n.get(month, {}).get(c, 0) for c in CATEGORIES}
            return {c: self.totals.get((month, vin, c), 0) for c in CATEGORIES}

    def spend(self, month: str | None = None) -> float:
        counts = self.calls(month)
        return sum(counts[c] * self.rates.get(c, 0.0) for c in BILLED)

    def share(self, month: str | None = None) -> float:
        return self.spend(month) / self.credit if self.credit > 0 else 0.0

    def step(self) -> str:
        """ok, slow, tight or stopped: where the budget stands this month."""
        if not self.on:
            return "ok"
        f = self.share()
        return "stopped" if f >= STOP_AT else "tight" if f >= TIGHT_AT else "slow" if f >= SLOW_AT else "ok"

    def factor(self) -> int:
        return FACTORS[self.step()]

    def warning(self) -> str | None:
        """The sentence that goes beside a command sent past the limit."""
        return "This month's Tesla API allowance is used up." if self.step() == "stopped" else None

    # ---- the hooks every call passes through
    def guard(self, method: str, url: str, body=None) -> tuple[str, str, str]:
        """Classify the call and refuse it when the budget says so (Overspent). Commands, and
        anything inside `by_hand`, always go; the free account calls too."""
        cat, endpoint, vin = classify(method, url, body)
        if cat in ("other", "commands") or MANUAL.get() or not self.on:
            return cat, endpoint, vin
        step = self.step()
        if cat == "wakes" and step in ("tight", "stopped"):
            raise Overspent("Waking the car is paused: this month's Tesla API allowance is nearly used. Settings › Commands.")
        if step == "stopped":
            raise Overspent("Tesla API use is paused: this month's allowance is used. Raise it in Settings › Commands.")
        return cat, endpoint, vin

    def _add(self, month: str, vin: str, cat: str, n: int) -> None:
        key = (month, vin, cat)
        self.totals[key] = self.totals.get(key, 0) + n
        self.pending_n[key] = self.pending_n.get(key, 0) + n
        by = self.month_n.setdefault(month, {})
        by[cat] = by.get(cat, 0) + n
        self._alert(month)

    def _alert(self, month: str) -> None:
        share = self.share(month)
        spent = f"${self.spend(month):.2f} of ${self.credit:.2f}"
        if share >= TIGHT_AT and month not in self.alerted:
            self.alerted.add(month)
            self.new_alerts.append(month)
            self.outbox.append(
                f"Tesla API use is at {share:.0%} of this month's allowance ({spent})." + (" Background polling is slowing, and it pauses at the limit." if self.on else "") + " Settings › Commands."
            )
        if share >= STOP_AT and f"{month}:stop" not in self.alerted:
            self.alerted.add(f"{month}:stop")
            self.new_alerts.append(f"{month}:stop")
            self.outbox.append(
                f"Tesla API allowance used up ({spent})."
                + (" Background calls are paused; commands still go. Raise the allowance in Settings › Commands to resume." if self.on else " The budget is off, so nothing is held back.")
            )

    def count(self, cat: str, endpoint: str, vin: str, status: int) -> None:
        """One attempt: logged always, added to the month only when Tesla would bill it."""
        now = self.clock()
        row = (datetime.fromtimestamp(now, timezone.utc), vin, cat, endpoint, int(status or 0))
        with self.lock:
            self.pending.append(row)
            self.recent.appendleft(row)
            if billable(status):
                self._add(month_of(now), vin, cat, 1)

    def signals(self, vin: str, n: int) -> None:
        """`n` signals streamed by a car, billed per signal (not an attempt: nothing is logged)."""
        if n > 0:
            with self.lock:
                self._add(month_of(self.clock()), vin, "signals", n)

    # ---- disk
    def load(self, conn) -> None:
        month = month_of(self.clock())
        floor = month_back(month, 12)
        rows = conn.execute("SELECT month, vin, category, calls FROM cc_fleet_usage WHERE month >= %s", (floor,)).fetchall()
        calls = conn.execute("SELECT at, vin, category, endpoint, status FROM cc_fleet_calls ORDER BY id DESC LIMIT %s", (SHOWN_CALLS,)).fetchall()
        done = conn.execute("SELECT month FROM cc_fleet_alerts").fetchall()
        with self.lock:
            for r in rows:
                if r["category"] in CATEGORIES:
                    n = int(r["calls"])
                    key = (r["month"], r["vin"], r["category"])
                    self.totals[key] = self.totals.get(key, 0) + n
                    by = self.month_n.setdefault(r["month"], {})
                    by[r["category"]] = by.get(r["category"], 0) + n
            self.recent.extend((r["at"], r["vin"], r["category"], r["endpoint"], r["status"]) for r in calls)
            self.alerted.update(r["month"] for r in done)

    def idle(self) -> bool:
        with self.lock:
            return not (self.pending or self.pending_n or self.new_alerts or self.outbox)

    def tick(self, conn, send=None) -> None:
        """Write what has been counted since the last tick, then send the alerts due. A write
        that fails puts everything back and raises, so the next tick has it."""
        with self.lock:
            rows, self.pending = self.pending, []
            added, self.pending_n = self.pending_n, {}
            months, self.new_alerts = self.new_alerts, []
            texts, self.outbox = self.outbox, []
        try:
            for (m, vin, cat), n in added.items():
                conn.execute(
                    "INSERT INTO cc_fleet_usage (month, vin, category, calls) VALUES (%s, %s, %s, %s) ON CONFLICT (month, vin, category) DO UPDATE SET calls = cc_fleet_usage.calls + EXCLUDED.calls",
                    (m, vin, cat, n),
                )
            for row in rows:
                conn.execute("INSERT INTO cc_fleet_calls (at, vin, category, endpoint, status) VALUES (%s, %s, %s, %s, %s)", row)
            if rows:
                conn.execute("DELETE FROM cc_fleet_calls WHERE id <= (SELECT max(id) FROM cc_fleet_calls) - %s", (KEEP_CALLS,))
            for m in months:
                conn.execute("INSERT INTO cc_fleet_alerts (month) VALUES (%s) ON CONFLICT DO NOTHING", (m,))
            conn.commit()
        except Exception:
            with self.lock:
                self.pending[:0] = rows
                for k, n in added.items():
                    self.pending_n[k] = self.pending_n.get(k, 0) + n
                self.new_alerts[:0] = months
                self.outbox[:0] = texts
            raise
        for text in texts if send else ():
            send(text)

    # ---- the Settings page
    def view(self, names: dict[str, str]) -> dict:
        """Everything Settings › Commands draws, from memory."""
        now = self.clock()
        month = month_of(now)
        with self.lock:
            counts = self.calls(month)
            cars = sorted({v for (m, v, _c) in self.totals if m == month and v})
            per = {(v, c): self.totals.get((month, v, c), 0) for v in [*cars, ""] for c in CATEGORIES}
            recent = list(self.recent)[:SHOWN_CALLS]
            history = [(month_back(month, i), self.spend(month_back(month, i)), sum(self.calls(month_back(month, i))[c] for c in BILLED)) for i in range(11, -1, -1)]
        rows = []
        for cat in CATEGORIES:
            n = counts[cat]
            cost = n * self.rates.get(cat, 0.0) if cat in BILLED else 0.0
            rows.append(
                {
                    "key": cat,
                    "label": CATEGORIES[cat],
                    "calls": n,
                    "cost": cost,
                    "rate": self.rates.get(cat) if cat in BILLED else None,
                    "cars": [{"name": names.get(v) or "Account", "calls": per[(v, cat)]} for v in [*cars, ""] if per[(v, cat)]],
                }
            )
        rows = [r for r in sorted(rows, key=lambda r: (r["key"] == "other", -r["cost"], -r["calls"])) if r["calls"] or r["key"] in ("data", "commands", "wakes")]
        bins = []
        for m, spend, n in history:
            label = datetime.strptime(m + "-01", "%Y-%m-%d").strftime("%b %Y")
            bins.append({"value": spend, "label": f"{label}: ${spend:.2f}, {n:,} calls", "href": None, "current": m == month, "future": False})
        first = datetime.strptime(history[0][0] + "-01", "%Y-%m-%d")
        strip = strips.strip(bins, [(0, first.strftime("%b")), (11, datetime.strptime(month + "-01", "%Y-%m-%d").strftime("%b"))], ref=self.credit if self.credit > 0 else None)
        start = datetime.strptime(month + "-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(month_back(month, -1) + "-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
        elapsed = (now - start.timestamp()) / (end - start).total_seconds()
        spend = self.spend(month)
        return {
            "month": start.strftime("%B %Y"),
            "credit": self.credit,
            "spend": spend,
            "pace": spend / elapsed if elapsed >= 0.07 and spend > 0 else None,  # projected month-end, once a couple of days say something
            "pct": min(100.0, self.share(month) * 100),
            "step": self.step(),
            "on": self.on,
            "rows": rows,
            "strip": strip,
            "recent": [
                {"at": at, "car": names.get(vin) or ("Account" if not vin else vin[-4:]), "category": CATEGORIES.get(cat, cat), "endpoint": endpoint, "status": status, "ok": 0 < status < 400}
                for at, vin, cat, endpoint, status in recent
            ],
            "slow_at": SLOW_AT * 100,
            "tight_at": TIGHT_AT * 100,
        }


METER = Meter()


def through(method: str, url: str, body, ask):
    """Run `ask()` (the request) behind the budget and count it. `ask` returns (status, ...) or
    raises; a request that never got an answer counts as status 0. Overspent is raised before
    anything is sent."""
    cat, endpoint, vin = METER.guard(method, url, body)
    try:
        out = ask()
    except Exception:
        METER.count(cat, endpoint, vin, 0)
        raise
    METER.count(cat, endpoint, vin, out[0])
    return out


def slowed(seconds: float) -> float:
    """A poller's wait, stretched by the budget."""
    return seconds * METER.factor()
