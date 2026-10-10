"""Chart payloads: what static/chart.js draws, built from series the page already has.

A chart is one primary series (drawn filled), any number of extra series the reader can
add one at a time, a lane of what the car was doing, and the drives and charges the hover
card names. Everything is in epoch seconds and display units, thinned to what a screen
can show, so the page carries it inline and the script only draws."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

_LANE = {"sleeping": "asleep", "idle": "parked", "driving": "driving", "charging": "charging"}


def thin(series, t0: datetime, t1: datetime, n: int = 400) -> list[list[float]]:
    a, b = t0.timestamp(), t1.timestamp()
    before = None
    inside = []
    for t, v in series:
        if v is None:
            continue
        s = t.timestamp()
        if s < a:
            before = v
        elif s <= b:
            inside.append((s, float(v)))
    out: list[list[float]] = []
    if before is not None and (not inside or inside[0][0] > a):
        out.append([a, float(before)])
    if inside:
        step = (b - a) / max(1, n)
        last_bucket = None
        for s, v in inside:
            bucket = int((s - a) / step) if step else 0
            if bucket != last_bucket:
                out.append([s, v])
                last_bucket = bucket
        if out[-1][0] != inside[-1][0]:
            out.append([inside[-1][0], inside[-1][1]])
    return out


def lane(segments: list[dict]) -> list[list]:
    return [[s["start"].timestamp(), s["end"].timestamp(), _LANE.get(s["label"], "unknown")] for s in segments]


def payload(t0: datetime, t1: datetime, primary: dict, extras: list[dict], lane_rows: list, events: list[dict], ticks=None) -> dict:
    return {"t0": t0.timestamp(), "t1": t1.timestamp(), "primary": primary, "extras": extras, "lane": lane_rows, "events": events, "ticks": ticks or []}


STEPS = (300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800, 1209600, 2592000)


def ticks(t0: datetime, t1: datetime, tz, most: int = 7) -> list[list]:
    """Time labels for a chart from t0 to t1: the smallest round step that gives at most
    `most` of them, on the local wall clock (a 25-hour day still says 6 AM at 6 AM).
    Each is [epoch seconds, label, whether to draw a day line]."""
    span = (t1 - t0).total_seconds()
    step = next((s for s in STEPS if span / s <= most), None)
    if step is None:  # past seven 30-day steps: whole blocks of them, not a label a day
        step = math.ceil(span / most / 2592000) * 2592000
    local = t0.astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    secs = (local.replace(tzinfo=None) - midnight).total_seconds()
    if step >= 86400:
        days = step // 86400
        first = midnight + timedelta(days=math.ceil(secs / 86400))  # the first midnight at or after t0
        fmt = "%a %-d" if days == 1 else "%b %Y" if days >= 180 else "%b %-d"
        wall = (first + timedelta(days=n * days) for n in range(most * 4 + 2))
    else:
        k = math.ceil(secs / step)
        fmt = "%-I:%M" if step < 3600 else "%-I %p"
        wall = (midnight + timedelta(seconds=n * step) for n in range(k, k + most * 4 + 4))
    out = []
    for naive in wall:
        # wall-clock arithmetic on the naive time, then zoned: steps land on round local times
        at = naive.replace(tzinfo=tz)
        if at.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != naive:
            continue  # the spring-forward hour: this wall time never happened
        if at >= t1:
            break
        if at >= t0:
            out.append([at.timestamp(), at.strftime(fmt), step >= 86400 and at > t0])
    return out


def nice(vals: list[float], zero: bool = True) -> tuple[float, float]:
    """A round scale around `vals`, from zero when `zero`: about four steps of 1, 2, 2.5 or 5."""
    lo, hi = min(vals), max(vals)
    if zero:
        lo, hi = min(lo, 0), max(hi, 0)
    span = (hi - lo) or abs(hi) or 1
    raw = span / 4
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    a, b = math.floor(lo / step) * step, math.ceil(hi / step) * step
    if b == a:
        b = a + step
    return (int(a) if a == int(a) else a), (int(b) if b == int(b) else b)


NO_READING = -39.5  # -40 is the sensor's "no reading" in either scale, the one temperature where they agree


def extras(ranges, temps, t0: datetime, t1: datetime, units) -> list[dict]:
    """The second lines a reader can add: rated range and outside temperature, each on its
    own scale in the active units. Range tops out a little above its highest reading, on a
    round 50, so a car at 450 miles is drawn at 450 rather than pinned to a fixed ceiling."""
    out = []
    rng = thin([(t, units.distance(v)) for t, v in ranges if v is not None], t0, t1)
    if rng:
        top = max(v for _, v in rng)
        out.append({"key": "range", "label": "Range", "unit": " " + units.labels["dist"], "lo": 0, "hi": max(100, math.ceil(top * 1.05 / 50) * 50), "points": rng})
    tmp = thin([(t, units.temperature(v)) for t, v in temps if v is not None and v > NO_READING], t0, t1)
    if tmp:
        vals = [v for _, v in tmp]
        lo, hi = math.floor((min(vals) - 2) / 10) * 10, math.ceil((max(vals) + 2) / 10) * 10
        out.append({"key": "temp", "label": "Outside", "unit": units.labels["temp"], "lo": lo, "hi": max(hi, lo + 10), "points": tmp})
    return out


def run_series(runs) -> list[tuple[datetime, float]]:
    """(first, last, value, ...) runs as the two ends of each, so steps draw as steps."""
    out = []
    for r in runs:
        out.append((r[0], r[2]))
        out.append((r[1], r[2]))
    return out


def _line(key, label, unit, series, t0, t1, scale):
    pts = thin(series, t0, t1)
    if not pts:
        return None
    lo, hi = scale([v for _, v in pts])
    return {"key": key, "label": label, "unit": unit, "lo": lo, "hi": hi, "points": pts}


def level_scale(vals: list[float]) -> tuple[float, float]:
    """A battery axis from the readings, not 0–100: a drive from 86 % to 85 % is a line
    across a 10-point window, not a flat stroke on a slab. Multiples of 5, at least 10
    points tall, never outside 0–100."""
    lo, hi = min(vals), max(vals)
    lo = max(0.0, math.floor((lo - 1) / 5) * 5)
    hi = min(100.0, math.ceil((hi + 1) / 5) * 5)
    if hi - lo < 10:
        hi = min(100.0, lo + 10)
        lo = max(0.0, hi - 10)
    return lo, hi


def drive_chart(t0, t1, levels, speeds, elevations, powers, ranges, outside, event, units, tz) -> dict:
    """A drive's chart: battery leads, or speed, or elevation when there is no battery
    reading; the rest are offered as the second line. Everything in the active units."""
    lb = units.labels
    lines = [
        _line("level", "Battery", "%", levels, t0, t1, level_scale),
        _line("speed", "Speed", " " + lb["speed"], [(t, units.speed(v)) for t, v in speeds if v is not None], t0, t1, nice),
        _line("elevation", "Elevation", " " + lb["elev"], [(t, units.elevation(v)) for t, v in elevations if v is not None], t0, t1, lambda v: nice(v, zero=False)),
        _line("power", "Power", " kW", powers, t0, t1, nice),
        _line("range", "Range", " " + lb["dist"], [(t, units.distance(v)) for t, v in ranges if v is not None], t0, t1, lambda v: nice(v, zero=False)),
        _line("temp", "Outside", lb["temp"], [(t, units.temperature(v)) for t, v in outside if v is not None and v > NO_READING], t0, t1, lambda v: nice(v, zero=False)),
    ]
    present = [x for x in lines if x]
    lead = next((x for x in present if x["key"] in ("level", "speed", "elevation")), None)
    primary = lead or {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": []}
    return payload(t0, t1, primary, [x for x in present if x is not lead], [[t0.timestamp(), t1.timestamp(), "driving"]], [event], ticks(t0, t1, tz))


def charge_chart(t0, t1, powers, levels, ranges, volts, amps, event, units, tz) -> dict:
    """A charge's chart: power leads; battery, rated range, volts and amps are offered as
    the second line."""
    lb = units.labels
    power = _line("power", "Power", " kW", powers, t0, t1, nice)
    rest = [
        _line("level", "Battery", "%", levels, t0, t1, lambda v: (0, 100)),
        _line("range", "Range", " " + lb["dist"], [(t, units.distance(v)) for t, v in ranges if v is not None], t0, t1, nice),
        _line("volts", "Volts", " V", volts, t0, t1, lambda v: nice(v, zero=False)),
        _line("amps", "Amps", " A", amps, t0, t1, nice),
    ]
    primary = power or {"key": "power", "label": "Power", "unit": " kW", "lo": 0, "hi": 10, "points": []}
    return payload(t0, t1, primary, [x for x in rest if x], [[t0.timestamp(), t1.timestamp(), "charging"]], [event], ticks(t0, t1, tz))


def steady(vals: list[float], share: float = 0.05) -> tuple[float, float]:
    """An axis for a figure that should barely move (range at 100%): at least ±`share` of
    its middle, so 2 mi of noise on 386 mi is a near-flat line, not a seismograph."""
    lo, hi = min(vals), max(vals)
    mid = (lo + hi) / 2
    half = max((hi - lo) / 2, abs(mid) * share)
    return nice([mid - half, mid + half], zero=False)


def series_chart(t0, t1, primary: dict, extras: list[dict], tz) -> dict:
    """A chart of readings over days or months with no lane of states and no events:
    each line given as {key, label, unit, series, scale}, scale "pct" (0–100), "zero",
    "tight" or "steady" (at least ±5% of its middle)."""
    scales = {"pct": lambda v: (0, 100), "zero": nice, "tight": lambda v: nice(v, zero=False), "steady": steady}
    make = lambda x: _line(x["key"], x["label"], x["unit"], x["series"], t0, t1, scales[x["scale"]])
    lead = make(primary) or {"key": primary["key"], "label": primary["label"], "unit": primary["unit"], "lo": 0, "hi": 100, "points": []}
    return payload(t0, t1, lead, [x for x in (make(e) for e in extras) if x], [], [], ticks(t0, t1, tz))
