"""A parked stretch's page: what the battery lost while the car sat, what was running, the
cabin against the outside, what it cost, and the readings, from series the route fetched
for the stretch alone. Pure."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from day import asleep_share, awake_hours, level_lookup, overlap_hours

JUST = timedelta(seconds=1)


def moment(ref: int) -> datetime | None:
    """The last instant of the whole second a parked link names. Links carry the start's
    whole second, and a stretch that began at 12:00:00.6 is not yet under way at 12:00:00,
    so the lookup reads the end of that second. None for a number no clock reaches."""
    try:
        return datetime.fromtimestamp(ref + 1, tz=timezone.utc) - timedelta(microseconds=1)
    except (ValueError, OverflowError, OSError):
        return None


def figures(start: datetime, end: datetime, levels, ranges, inside, outside, states, modes, kwh_per_rated_mi: float | None, home_rate: float | None, car_on=()) -> dict:
    """The stretch's figures. Series are (time, value) steps that may start before `start`
    (the reading that held when the car parked); `states` (first, last, STATE); `modes`
    (first, last, (sentry, climate)). Battery and range are read at both ends; kWh lost is
    the rated range lost × kWh a rated mile, priced at the home rate a kWh when known."""
    lv, rg, cab, out = level_lookup(levels), level_lookup(ranges), level_lookup(inside), level_lookup(outside)
    hours = (end - start).total_seconds() / 3600
    last = end - JUST  # just before the next drive or charge: its first reading is not the stretch's
    l0, l1, r0, r1 = lv(start), lv(last), rg(start), rg(last)
    range_lost = (r0 - r1) if r0 is not None and r1 is not None else None
    kwh = range_lost * kwh_per_rated_mi if range_lost is not None and kwh_per_rated_mi else None
    sentry = awake_hours(modes, states, start, end, lambda m: m[0])
    climate = awake_hours(modes, states, start, end, lambda m: m[1])
    on = overlap_hours(list(car_on), start, end, lambda v: True)  # the car on with no drive; the states say on, not that anyone sat in it
    return {
        "hours": hours,
        "duration": end - start,
        "level_start": l0,
        "level_end": l1,
        "drop": (l0 - l1) if l0 is not None and l1 is not None else None,
        "range_start": r0,
        "range_end": r1,
        "range_lost": range_lost,
        "per_hour": (range_lost / hours) if range_lost is not None and hours >= 0.5 else None,
        "kwh_lost": kwh,
        "cost": (kwh * home_rate) if kwh is not None and kwh > 0 and home_rate else None,
        "asleep_share": asleep_share(states, start, end),
        "sentry_h": sentry,
        "climate_h": climate,
        "sentry": timedelta(hours=sentry),
        "climate": timedelta(hours=climate),
        "on_h": on,
        "on": timedelta(hours=on),
        # what ran most decides which of the car's usual rates this one is compared with
        "ran": "sentry" if sentry >= 0.5 * hours and sentry > 0 else "climate" if climate >= 0.25 * hours and climate > 0 else "idle",
        "inside": (cab(start), cab(last)),
        "outside": (out(start), out(last)),
    }


def window(series, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """A step series for a chart of [start, end): the value that held at `start`, placed at
    `start`, then the readings inside. Not every earlier point moved onto `start`: a chart
    thins a pile of points at one time down to the first, the oldest."""
    held = level_lookup(series)(start)
    return ([(start, held)] if held is not None else []) + [(t, v) for t, v in series if start < t < end]


def mode_bands(modes, start: datetime, end: datetime, guard: str) -> list[list]:
    """Sentry and climate runs inside the stretch as chart bands [t0, t1, kind, label]."""
    out = []
    for a, b, (sentry, climate) in modes:
        lo, hi = max(a, start), min(b, end)
        if hi <= lo:
            continue
        if sentry:
            out.append([lo, hi, "sentry", f"{guard} on"])
        if climate:
            out.append([lo, hi, "climate", "Climate on"])
    return out


def readings(start: datetime, end: datetime, levels, ranges, inside, outside, states) -> list[dict]:
    """A row each time the battery, range, cabin or state reading changed inside the
    stretch, with every series' value at that moment."""
    lv, rg, cab, out = level_lookup(levels), level_lookup(ranges), level_lookup(inside), level_lookup(outside)
    st = level_lookup([(a, s) for a, _b, s in states])
    times = sorted({t for series in (levels, ranges, inside, outside) for t, _v in series if start <= t < end} | {a for a, _b, _s in states if start <= a < end} | {start})
    rows, last = [], None
    for t in times:
        row = {"at": t, "level": lv(t), "rated": rg(t), "inside": cab(t), "outside": out(t), "asleep": st(t) == "OFFLINE"}
        key = (row["level"], row["rated"], row["inside"], row["outside"], row["asleep"])
        if key != last:
            rows.append(row)
            last = key
    return rows
