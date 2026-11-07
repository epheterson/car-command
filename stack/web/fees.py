"""Session and idle fees beyond the energy price, for a charge the network did not bill.

A rule is {"session": fee per charge, "idle": price per minute, "grace": minutes free}, any
part None. It comes from the place a charge was at (placeprice: fee, idle, grace) or from the
network's line on Settings › Money (sc_ for Superchargers, dc_ for any other fast charger).
A bill from the network (billed.py) always outranks a rule.

Idle minutes run from when the charge finished (the last stretch of at least IDLE_KW in the
power profile) to when the car left: the plug coming out (charging_sessions.plug_disconnected_date,
written by the live connector's database plugin) else the start of the next drive within
MAX_DEPARTURE_WAIT (an upper bound: the cable may have come out earlier). The imports end a session
when charging stops and record no plug, so a parked charge with no later drive has no known end.
Without a finish or a departure idle is None (unknown): no idle fee is charged and the charge page says so. Pure."""

from __future__ import annotations

from datetime import datetime, timedelta

MAX_DEPARTURE_WAIT = timedelta(days=3)  # how long after a charge the next drive still counts as the car leaving it
IDLE_KW = 1.0  # a charge still drawing less than this is taken to be done
MAX_IDLE_PER_MIN = 5.0
MAX_SESSION_FEE = 100.0
MAX_GRACE_MIN = 240.0

# Settings keys per network: (session fee, idle per minute, grace minutes)
KEYS = {"sc": ("sc_session_fee", "sc_idle_per_min", "sc_idle_grace"), "dc": ("dc_session_fee", "dc_idle_per_min", "dc_idle_grace")}
FEE_KEYS = tuple(k for keys in KEYS.values() for k in keys)
BOUNDS = {"session_fee": (0.01, MAX_SESSION_FEE), "idle_per_min": (0.01, MAX_IDLE_PER_MIN), "idle_grace": (0.0, MAX_GRACE_MIN)}


def bounds_of(key: str) -> tuple[float, float]:
    """The lowest and highest value Settings accepts for one of FEE_KEYS."""
    return BOUNDS[key.split("_", 1)[1]]


def rules(st: dict) -> dict[str, dict]:
    """{"sc": rule, "dc": rule} from the settings, blank parts None."""
    return {net: {part: (float(st[k]) if st.get(k) else None) for part, k in zip(("session", "idle", "grace"), keys)} for net, keys in KEYS.items()}


def idle_minutes(finished: datetime | None, left: datetime | None) -> float | None:
    """Minutes between the charge finishing and the car leaving or unplugging; None unless both are known."""
    if finished is None or left is None:
        return None
    return max(0.0, (left - finished).total_seconds() / 60)


def idle_fee(minutes: float | None, per_min: float | None, grace: float | None) -> float:
    """What idling costs: every minute past the grace at the per-minute price; 0 when unknown or unpriced."""
    if not minutes or not per_min:
        return 0.0
    return max(0.0, minutes - (grace or 0.0)) * per_min


def charge_finished(runs: list[tuple[datetime, datetime, float]], end: datetime) -> datetime | None:
    """When the last run drawing at least IDLE_KW ended, never after the session's end; None with no such run."""
    return max((min(b, end) for _a, b, kw in runs if kw >= IDLE_KW), default=None)
