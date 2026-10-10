"""Pure helpers: unit conversion, status classification, the 24-hour state ribbon."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

KM_PER_MI = 1.609344
FT_PER_M = 0.3048  # feet to metres, for elevation gain and loss


def km_to_mi(km: float | None) -> float | None:
    return None if km is None else km / KM_PER_MI


def c_to_f(c: float | None) -> float | None:
    return None if c is None else c * 9 / 5 + 32


class Units:
    """How to say a quantity, in the system the operator picked.

    Everything is stored in one canonical system — miles, mph, °F, Wh/mi — because the
    imported histories are imperial and a database that mixes systems is a database that
    lies. Conversion happens here, once, on the way to the page. `metric` is the only
    thing that changes; nothing upstream of a template knows it exists.
    """

    def __init__(self, metric: bool = False):
        self.metric = metric

    @property
    def labels(self) -> dict[str, str]:
        return (
            {"dist": "km", "speed": "km/h", "temp": "°C", "econ": "Wh/km", "elev": "m", "distname": "kilometres", "energy": "kWh", "power": "kW"}
            if self.metric
            else {"dist": "mi", "speed": "mph", "temp": "°F", "econ": "Wh/mi", "elev": "ft", "distname": "miles", "energy": "kWh", "power": "kW"}
        )

    def distance(self, mi: float | None) -> float | None:
        return None if mi is None else (mi * KM_PER_MI if self.metric else mi)

    def speed(self, mph: float | None) -> float | None:
        return None if mph is None else (mph * KM_PER_MI if self.metric else mph)

    def temperature(self, f: float | None) -> float | None:
        return None if f is None else ((f - 32) * 5 / 9 if self.metric else f)

    def per_distance(self, x: float | None) -> float | None:
        """Anything measured *per* unit distance — Wh/mi, cents/mi. Dividing where
        distance multiplies: a car using 300 Wh per mile uses 186 Wh per kilometre."""
        return None if x is None else (x / KM_PER_MI if self.metric else x)

    def to_stored_distance(self, shown: float | None) -> float | None:
        """The inverse of distance(), for a number a person typed. A metric operator
        entering an odometer reading types kilometres; the column holds miles."""
        return None if shown is None else (shown / KM_PER_MI if self.metric else shown)

    def elevation(self, ft: float | None) -> float | None:
        return None if ft is None else (ft * FT_PER_M if self.metric else ft)


# The active system, set once at startup and again when Settings is saved. Module state
# for the same reason cost.use_plan is: a Jinja filter is registered once and cannot be
# handed a per-request argument.
ACTIVE = Units()


def use_units(metric: bool) -> None:
    ACTIVE.metric = bool(metric)


MM_PER_32ND = 25.4 / 32


def tread_mm(value, unit: str) -> float | None:
    """A tread depth as entered, in millimetres. Tyre shops in the US read 32nds of an
    inch off the gauge; most of the world reads millimetres. Stored as one thing."""
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if v < 0 or v > 30:
        return None
    return v * MM_PER_32ND if unit == "32nds" else v


def tread_display(mm: float | None, metric: bool) -> str:
    """How a stored depth reads on the page: 6.4 mm, or 8/32 under imperial units."""
    if mm is None:
        return ""
    if metric:
        return f"{mm:.1f} mm"
    # Gauges read in half-32nds, and round() would turn 8.5 into 8 by banker's rounding —
    # a tire measured at 8.5/32 must not read 8/32 on the page it was entered on.
    return f"{mm / MM_PER_32ND:.1f}".rstrip("0").rstrip(".") + "/32"


def climate_active(state: str | None) -> bool | None:
    """Whether the climate system is doing anything, from CarConnectivity's
    ClimatizationState: heating, cooling and ventilation are yes, off is no, and
    anything the car has not actually said stays None rather than becoming "off"."""
    if state in ("heating", "cooling", "ventilation"):
        return True
    if state == "off":
        return False
    return None


def classify_status(state: str | None, connection_state: str | None, charging_state: str | None) -> str:
    """One word for the car right now, in the driver's vocabulary.

    Charging beats everything; driving beats parked; a car whose connection is
    offline is asleep; otherwise it is awake and idle.
    """
    if charging_state == "charging":
        return "Charging"
    if state == "driving":
        return "Driving"
    if state == "ignition_on":
        return "Ready"
    if connection_state in ("offline", None):
        return "Sleeping"
    return "Idle"


def ribbon_segments(states: Iterable[tuple[datetime, datetime, str]], charging: Iterable[tuple[datetime, datetime, str]], start: datetime, end: datetime) -> list[dict]:
    """Cut [start, end) into contiguous segments labelled sleeping/idle/driving/charging.

    `states` rows are (first_date, last_date, STATE) with STATE from the vehicle
    state table (OFFLINE, PARKED, IGNITION_ON, DRIVING); `charging` rows are the
    same shape from charging_states. A charging interval overrides whatever the
    vehicle state says. Each returned segment carries its width as a fraction
    of the window so a template can lay it out without doing arithmetic.
    """
    total = (end - start).total_seconds()
    if total <= 0:
        return []
    cuts = {start, end}
    for a, b, _ in list(states) + list(charging):
        for t in (a, b):
            if start < t < end:
                cuts.add(t)
    points = sorted(cuts)
    states = list(states)
    charging = list(charging)

    def label_at(t: datetime) -> str:
        for a, b, s in charging:
            if a <= t < b and s == "CHARGING":
                return "charging"
        for a, b, s in states:
            if a <= t < b:
                return {"DRIVING": "driving", "IGNITION_ON": "idle", "PARKED": "idle"}.get(s, "sleeping")
        return "unknown"

    segments: list[dict] = []
    for a, b in zip(points, points[1:]):
        label = label_at(a)
        width = (b - a).total_seconds() / total
        if segments and segments[-1]["label"] == label:
            segments[-1]["width"] += width
            segments[-1]["end"] = b
        else:
            segments.append({"label": label, "start": a, "end": b, "width": width})
    return segments


def humanize_age(delta: timedelta) -> str:
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        h = s // 3600
        return f"{h} h ago"
    return f"{s // 86400} d ago"


def humanize_duration(delta: timedelta | None) -> str:
    if delta is None:
        return ""
    s = int(delta.total_seconds())
    if s < 3600:
        return f"{max(1, s // 60)} min"
    if s >= 48 * 3600:  # "7 days 8 h", not "176 h 0 min"
        d, h = divmod(s // 3600, 24)
        return f"{d} days" + (f" {h} h" if h else "")
    h, m = divmod(s // 60, 60)
    return f"{h} h" + (f" {m} min" if m else "")


def daily_breakdown(states: list[tuple[datetime, datetime, str]], charging: list[tuple[datetime, datetime, str]], days: int, tz, now: datetime) -> list[dict]:
    """The last `days` local days, today included and ending at `now`."""
    today = now.astimezone(tz).date()
    return days_between(states, charging, today - timedelta(days=days - 1), today, tz, now)


def days_between(states: list[tuple[datetime, datetime, str]], charging: list[tuple[datetime, datetime, str]], first_day, last_day, tz, now: datetime) -> list[dict]:
    """Per local day from first_day to last_day inclusive, hours spent in each
    label and how many times the car woke. Days run midnight to midnight in
    `tz`; a day that is still running ends at `now`; days after `now` are
    skipped. A wake-up is a transition out of `sleeping` into any other known
    label. Rows are the same (first_date, last_date, LABEL) shape the ribbon takes.
    """
    out: list[dict] = []
    local_now = now.astimezone(tz)
    d = first_day
    while d <= last_day:
        day_start = datetime(d.year, d.month, d.day, tzinfo=tz)
        d += timedelta(days=1)
        if day_start >= local_now:
            break
        day_end = min(day_start + timedelta(days=1), local_now)
        segs = ribbon_segments(states, charging, day_start, day_end)
        span_h = (day_end - day_start).total_seconds() / 3600
        hours = {"sleeping": 0.0, "idle": 0.0, "driving": 0.0, "charging": 0.0, "unknown": 0.0}
        wakeups = 0
        prev = None
        for s in segs:
            hours[s["label"]] += s["width"] * span_h
            if prev == "sleeping" and s["label"] not in ("sleeping", "unknown"):
                wakeups += 1
            prev = s["label"]
        known = span_h - hours["unknown"]
        out.append(
            {"date": day_start.date(), "hours": hours, "span_h": span_h, "known_h": known, "segments": segs, "asleep_pct": (hours["sleeping"] / known * 100) if known > 0 else None, "wakeups": wakeups}
        )
    return out


def current_streak(states: list[tuple[datetime, datetime, str]], now: datetime) -> tuple[str, timedelta] | None:
    """The label the car is in right now and how long it has been there."""
    open_rows = [r for r in states if r[1] >= now - timedelta(minutes=3)]
    if not open_rows:
        return None
    first, last, s = max(open_rows, key=lambda r: r[0])
    label = {"DRIVING": "driving", "IGNITION_ON": "idle", "PARKED": "idle"}.get(s, "sleeping")
    return label, now - first


def rated_mile_kwh(by_year: dict, vin: str | None, when, all_time: float | None, tz=None) -> float | None:
    """What a rated mile cost this car around `when`.

    A rated mile is a definition, and the definition moves: the Model 3's own charges
    measure 0.2524 kWh per rated mile in 2018 and 0.2427 in 2023, about 3 % end to end
    and not in one direction, which is firmware recomputing rated range as much as it is
    a battery ageing. Pricing a 2018 drive with the 2026 figure is the one systematic
    error left in the energy numbers, so a drive is priced by its own year where that
    year measured enough charges to have an answer, and by the all-time figure where it
    did not — which is also what a car with a few months of history gets.

    The year is the local one, to match how every page groups a date.
    """
    if when is not None and vin is not None:
        local = when.astimezone(tz) if tz is not None and when.tzinfo is not None else when
        k = by_year.get((vin, local.year))
        if k is not None:
            return k
    return all_time


def drive_kwh(level_start, level_end, capacity_kwh, range_start=None, range_end=None, kwh_per_rated_mi=None) -> float | None:
    """Energy a drive took. Rated range used x what a rated mile costs this car
    (measured from its own charges) when both are known; else capacity x SoC
    drop. The first is the honest one; the second over-reads on an aged
    pack because the nominal capacity is not the real one."""
    if range_start is not None and range_end is not None and kwh_per_rated_mi and range_start >= range_end:
        return (range_start - range_end) * kwh_per_rated_mi
    if level_start is not None and level_end is not None and capacity_kwh and level_start >= level_end:
        return capacity_kwh * (level_start - level_end) / 100
    return None


def trip_stats(
    miles: float | None,
    duration: timedelta | None,
    level_start: float | None,
    level_end: float | None,
    capacity_kwh: float | None,
    range_start: float | None = None,
    range_end: float | None = None,
    kwh_per_rated_mi: float | None = None,
) -> dict:
    """Derived numbers for one trip: average speed, battery used, energy and efficiency."""
    hours = duration.total_seconds() / 3600 if duration else None
    used_pct = (level_start - level_end) if level_start is not None and level_end is not None else None
    kwh = drive_kwh(level_start, level_end, capacity_kwh, range_start, range_end, kwh_per_rated_mi)
    return {
        "avg_mph": (miles / hours) if miles is not None and hours and hours > 0 else None,
        "used_pct": used_pct,
        "kwh": kwh,
        "wh_per_mi": (kwh * 1000 / miles) if kwh is not None and miles and miles > 0.5 else None,
        "rated_used": (range_start - range_end) if range_start is not None and range_end is not None else None,
        "rated_efficiency": (miles / (range_start - range_end)) if range_start is not None and range_end is not None and range_start - range_end > 0.5 and miles else None,
    }


def charge_stats(powers: list[tuple[datetime, datetime, float]], start: datetime, end: datetime | None, level_start: float | None, level_end: float | None) -> dict:
    """Energy, peak and average power for one charging session from the charging_powers rows."""
    end = end or datetime.now(timezone.utc)
    kwh = 0.0
    peak = 0.0
    delivering = 0.0
    for a, b, kw in powers:
        if kw is None or kw <= 0:
            continue
        overlap = (min(b, end) - max(a, start)).total_seconds()
        if overlap > 0:
            kwh += kw * overlap / 3600
            peak = max(peak, kw)
            delivering += overlap
    # Averaged over the time power was flowing, else the wall clock. A car that charged
    # four hours, paused for the night and finished in the morning averaged 3.5 kW over a
    # 17-hour plug-in against a 10.8 kW peak, which describes the schedule, not the charger.
    hours = (delivering or (end - start).total_seconds()) / 3600
    gained = (level_end - level_start) if level_start is not None and level_end is not None else None
    return {"kwh": kwh, "peak_kw": peak, "avg_kw": (kwh / hours) if hours > 0 else None, "gained_pct": gained, "kwh_per_100": (kwh / gained * 100) if gained and gained > 0 and kwh > 0 else None}


def idle_periods(
    states: list[tuple[datetime, datetime, str]], levels: list[tuple[datetime, float]], charging: list[tuple[datetime, datetime, str]], now: datetime, min_hours: float = 1.0
) -> list[dict]:
    """Stretches the car sat still (not driving, not charging) with the battery drop across each.

    Built from the state rows alone, so it works before any trip exists. A
    period ends at a DRIVING row or at a CHARGING row; the drop is the last
    level reading at each end. Asleep share comes from OFFLINE state rows.
    """
    states = sorted(states)
    charging_windows = [(a, b) for a, b, s in charging if s == "CHARGING"]

    def level_at(t: datetime) -> float | None:
        best = None
        for ts, lv in levels:
            if ts <= t:
                best = lv
            else:
                break
        return best

    def busy(t: datetime) -> bool:
        return any(a <= t < b for a, b in charging_windows)

    periods: list[dict] = []
    cur_start: datetime | None = None
    asleep = 0.0
    for a, b, s in states:
        b = min(b, now)
        if a >= b:
            continue
        if s == "DRIVING" or busy(a):
            if cur_start is not None and (a - cur_start).total_seconds() >= min_hours * 3600:
                periods.append({"start": cur_start, "end": a, "asleep_h": asleep})
            cur_start, asleep = None, 0.0
            continue
        if cur_start is None:
            cur_start, asleep = a, 0.0
        if s == "OFFLINE":
            asleep += (b - a).total_seconds() / 3600
    if cur_start is not None and (now - cur_start).total_seconds() >= min_hours * 3600:
        periods.append({"start": cur_start, "end": now, "asleep_h": asleep, "open": True})
    for p in periods:
        hours = (p["end"] - p["start"]).total_seconds() / 3600
        s0, s1 = level_at(p["start"]), level_at(p["end"])
        drop = (s0 - s1) if s0 is not None and s1 is not None else None
        p.update(
            {
                "hours": hours,
                "soc_start": s0,
                "soc_end": s1,
                "drop_pct": drop,
                "drop_per_day": (drop / hours * 24) if drop is not None and hours > 0 else None,
                "asleep_share": (p["asleep_h"] / hours) if hours > 0 else None,
            }
        )
    periods.reverse()
    return periods


def electrics_stats(rows: list[tuple[datetime, datetime, float, float]], start: datetime, end: datetime) -> dict:
    """Time-weighted average and peak volts and amps over [start, end) from (first, last, volts, amps) runs."""
    secs = 0.0
    v_sum = a_sum = 0.0
    v_max = a_max = None
    for a, b, volts, amps in rows:
        lo, hi = max(a, start), min(b, end)
        if hi <= lo:
            continue
        w = (hi - lo).total_seconds()
        if volts:
            v_sum += volts * w
            v_max = volts if v_max is None else max(v_max, volts)
        if amps:
            a_sum += amps * w
            a_max = amps if a_max is None else max(a_max, amps)
        secs += w
    return {"avg_volts": (v_sum / secs) if secs and v_sum else None, "max_volts": v_max, "avg_amps": (a_sum / secs) if secs and a_sum else None, "max_amps": a_max}


def idle_details(idles: list[dict], drives: list[dict], sentry: list[tuple[datetime, datetime, bool]]) -> list[dict]:
    """Where each idle was, and how much of it Sentry watched. The place is the destination
    of the last drive that ended at or before the idle began; Sentry is the share of the
    idle covered by mode rows with it on. Both feed the Sleep page's filters."""
    ends = sorted((d["destination_date"], d.get("to")) for d in drives if d.get("destination_date"))
    out = []
    for p in idles:
        q = dict(p)
        place = None
        for at, name in ends:
            if at <= p["start"]:
                place = name
            else:
                break
        q["place"] = place
        span = (p["end"] - p["start"]).total_seconds()
        watched = 0.0
        for a, b, on in sentry:
            if on and b > p["start"] and a < p["end"]:
                watched += (min(b, p["end"]) - max(a, p["start"])).total_seconds()
        q["sentry_share"] = (watched / span) if span > 0 else None
        out.append(q)
    return out


def idles_by_day(days: list[dict], idles: list[dict], tz) -> list[dict]:
    """Each day of the Sleep page with the idle periods that began on it (local time), newest
    first, so a day's row opens onto its own stops. A period that began before the first day
    shown belongs to none; the days keep their order."""
    by: dict = {}
    for p in idles:
        by.setdefault(p["start"].astimezone(tz).date(), []).append(p)
    return [{**d, "idles": sorted(by.get(d["date"], []), key=lambda p: p["start"], reverse=True)} for d in days]


def filter_idles(idles: list[dict], min_h: float | None = None, max_h: float | None = None, place: str | None = None, sentry_only: bool = False) -> list[dict]:
    """The Sleep page's search: hours between, at a place (any part of its name), Sentry on for most of it."""
    want = (place or "").strip().lower()
    out = []
    for p in idles:
        if min_h is not None and p["hours"] < min_h or max_h is not None and p["hours"] > max_h:
            continue
        if want and want not in (p.get("place") or "").lower():
            continue
        if sentry_only and not ((p.get("sentry_share") or 0) >= 0.5):
            continue
        out.append(p)
    return out


def charge_minutes_left(status: str | None, reached, power_kw, soc, target, capacity_kwh, now: datetime) -> int | None:
    """Whole minutes until the charge reaches its target, or None when the car is not charging
    or the facts are not there. The connector's own estimate (`reached`, an ISO time) wins; without
    one it is the energy still to add over the power now, which runs a little short of a real
    charge because the power tapers near the top."""
    if status != "Charging":
        return None
    if reached:
        try:
            at = reached if isinstance(reached, datetime) else datetime.fromisoformat(str(reached))
            if at.tzinfo is not None:
                return max(0, round((at - now).total_seconds() / 60))
        except ValueError:
            pass
    try:
        left_pct = float(target) - float(soc)
        power, capacity = float(power_kw), float(capacity_kwh)
    except (TypeError, ValueError):
        return None
    if power <= 0 or capacity <= 0:
        return None
    return max(0, round(max(left_pct, 0.0) / 100 * capacity / power * 60))
