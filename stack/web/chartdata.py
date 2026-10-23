"""Chart payloads: what static/chart.js draws, built from series the page already has.

A chart is one primary series (drawn filled), any number of extra series the reader can
switch on together, a lane of what the car was doing, and the drives and charges the hover
card names. Optional on top: `marks` (a labelled vertical line: a software update, a year),
`bands` (a shaded span), `avg` (a labelled level across), and `on` (the extras shown at
first). Everything is in epoch seconds and display units, thinned to what a screen can
show, so the page carries it inline and the script only draws. A chart along the odometer
instead of the clock says `xkind: "dist"`: x is distance, ticks are given (dist_ticks)."""

from __future__ import annotations

import math

import clockfmt
from datetime import datetime, timedelta, timezone

_LANE = {"sleeping": "asleep", "idle": "parked", "caron": "caron", "driving": "driving", "charging": "charging"}


def thin(series, t0: datetime, t1: datetime, n: int = 400) -> list[list[float]]:
    return thin_x(((t.timestamp(), v) for t, v in series), t0.timestamp(), t1.timestamp(), n)


def thin_x(series, a: float, b: float, n: int = 400) -> list[list[float]]:
    """thin() on a plain number line: (x, value) pairs in x order, x in seconds or miles."""
    before = None
    inside = []
    for s, v in series:
        if v is None:
            continue
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


# Each extra line has its own colour, named by what it is so Range is the same teal on every
# page (style.css, --ov-*); anything else takes one of four spare tones in turn.
TONES = ("level", "range", "temp", "speed", "elevation", "power", "volts", "amps")
BAND_KINDS = ("driving", "charging", "asleep", "parked", "sentry", "climate", "note")


def _x(v) -> float | None:
    """An x position: a datetime as epoch seconds, a number as itself; None for anything else."""
    if isinstance(v, datetime):
        return v.timestamp()
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        return float(v)
    return None


def annotations(marks=None, bands=None, avg=None) -> dict:
    """The optional drawn-on extras, checked: marks [[x, label]], bands [[x0, x1, kind, label]]
    with kind one of BAND_KINDS, avg {value, label}. A malformed entry is dropped, not drawn
    wrong; nothing given, nothing in the payload."""
    rows = lambda v, n: [r for r in v or () if isinstance(r, (list, tuple)) and len(r) >= n]
    out: dict = {}
    m = [[_x(r[0]), str(r[1])] for r in rows(marks, 2) if _x(r[0]) is not None and r[1]]
    if m:
        out["marks"] = sorted(m)
    b = [[_x(r[0]), _x(r[1]), r[2], str(r[3]) if len(r) > 3 and r[3] else ""] for r in rows(bands, 3) if None not in (_x(r[0]), _x(r[1])) and _x(r[1]) > _x(r[0]) and r[2] in BAND_KINDS]
    if b:
        out["bands"] = b
    if isinstance(avg, dict) and _x(avg.get("value")) is not None:
        out["avg"] = {"value": _x(avg["value"]), "label": str(avg.get("label") or "Average")}
    return out


def toned(extras: list[dict]) -> list[dict]:
    """Each extra with its colour: its own for a known series, else a spare one in turn."""
    spare = 0
    out = []
    for x in extras:
        if x.get("key") in TONES:
            tone = x["key"]
        else:
            tone, spare = f"x{spare % 4}", spare + 1
        out.append(dict(x, tone=tone))
    return out


def _assemble(x0: float, x1: float, primary, extras, lane_rows, events, ticks, marks, bands, avg, on, tz=None) -> dict:
    extras = toned(extras)
    out = {"t0": x0, "t1": x1, "primary": primary, "extras": extras, "lane": lane_rows, "events": events, "ticks": ticks or []}
    if getattr(tz, "key", None):
        out["tz"] = tz.key  # the zone the ticks are in, so the hover card's time says the same clock
        out["h12"] = clockfmt.PREFS["clock"] != "24"  # and the same 12- or 24-hour clock
    out.update(annotations(marks, bands, avg))
    keys = {x["key"] for x in extras}
    shown = [k for k in on or () if k in keys]
    if shown:
        out["on"] = shown
    return out


def payload(t0: datetime, t1: datetime, primary: dict, extras: list[dict], lane_rows: list, events: list[dict], ticks=None, marks=None, bands=None, avg=None, on=None, tz=None) -> dict:
    """A chart over time. `tz`, the zone `ticks` were drawn in, goes in the payload so the
    hover card tells the time on the same clock as the axis, not the browser's."""
    return _assemble(t0.timestamp(), t1.timestamp(), primary, extras, lane_rows, events, ticks, marks, bands, avg, on, tz)


def distance_payload(x0: float, x1: float, primary: dict, extras: list[dict], ticks: list, xunit: str, marks=None, bands=None, avg=None, on=None) -> dict:
    """A chart along the odometer: points are [distance, value], `xunit` (" mi") names the
    axis in the hover card, and the ticks come from dist_ticks. No lane and no events: those
    are times, and this axis has none."""
    out = _assemble(float(x0), float(max(x1, x0 + 1)), primary, extras, [], [], ticks, marks, bands, avg, on)
    out.update({"xkind": "dist", "xunit": xunit})
    return out


def dist_ticks(x0: float, x1: float, most: int = 6) -> list[list]:
    """Round odometer labels between x0 and x1 ("40k" past ten thousand), at most `most`."""
    span = max(x1 - x0, 1)
    raw = span / most
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    out = []
    v = math.ceil(x0 / step) * step
    while v <= x1:
        label = f"{v / 1000:g}k" if step >= 1000 else f"{v:,.0f}"
        out.append([float(v), label, False])
        v += step
    return out


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
        fmt = "%a %-d" if days == 1 else "%b %Y" if days >= 60 else "%b %-d"  # steps of months span years: name the year
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
            out.append([at.timestamp(), at.strftime(clockfmt.adapt(fmt)), step >= 86400 and at > t0])
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
    if round((b - a) / step) % 2:  # an even count of steps, so the middle label lands on a round value too
        b += step
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


def peaks(series, t0: datetime, t1: datetime, n: int = 400) -> list[list[float]]:
    """thin() for a sampled series drawn as a line: each slice keeps its first, lowest and
    highest reading in time order, so a drive's top speed is on the chart, not thinned away.
    No reading before t0 is carried in: a line starts at its first reading."""
    a, b = t0.timestamp(), t1.timestamp()
    step = (b - a) / max(1, n) or 1
    slices: dict[int, list] = {}
    for t, v in series:
        s = t.timestamp()
        if v is not None and a <= s <= b:
            slices.setdefault(int((s - a) / step), []).append((s, float(v)))
    out = []
    for k in sorted(slices):
        got = slices[k]
        keep = {got[0], min(got, key=lambda p: p[1]), max(got, key=lambda p: p[1]), got[-1]}
        out.extend([s, v] for s, v in sorted(keep))
    return out


def _line(key, label, unit, series, t0, t1, scale, draw=None, gap=None):
    """One series for the chart; `draw="line"` for a sampled one (speed, power, height): a
    line through its readings, peaks kept, rather than steps. The line breaks where two
    readings are more than `gap` seconds apart (chart.js takes 300 when none is given)."""
    pts = peaks(series, t0, t1) if draw == "line" else thin(series, t0, t1)
    if not pts:
        return None
    lo, hi = scale([v for _, v in pts])
    out = {"key": key, "label": label, "unit": unit, "lo": lo, "hi": hi, "points": pts}
    if draw:
        out["draw"] = draw
    if gap:
        out["gap"] = gap
    return out


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


def snug_level_scale(vals: list[float]) -> tuple[float, float]:
    """A battery axis for a parked stretch, where a point or two is the whole story: whole
    points just outside the readings, at least 2 tall and an even count, so the middle label
    is a whole point too (77.0 -> 76.2 % draws on 76-78, not 75-85). A stretch that moved 5
    points or more takes level_scale's window."""
    lo, hi = min(vals), max(vals)
    if hi - lo >= 5:
        return level_scale(vals)
    a, b = max(0, math.floor(lo - 0.2)), min(100, math.ceil(hi + 0.2))
    while b - a < 2 or (b - a) % 2:
        if b < 100:
            b += 1
        else:
            a -= 1
    return a, b


def drive_chart(t0, t1, levels, speeds, elevations, powers, ranges, outside, event, units, tz, on=None) -> dict:
    """A drive's chart: battery leads, or speed, or elevation when there is no battery
    reading; the rest are offered as extra lines, `on` the ones shown at first. Everything
    in the active units."""
    lb = units.labels
    lines = [
        _line("level", "Battery", "%", levels, t0, t1, level_scale),
        _line("speed", "Speed", " " + lb["speed"], [(t, units.speed(v)) for t, v in speeds if v is not None], t0, t1, nice, "line"),
        _line("elevation", "Elevation", " " + lb["elev"], [(t, units.elevation(v)) for t, v in elevations if v is not None], t0, t1, lambda v: nice(v, zero=False), "line"),
        _line("power", "Power", " kW", powers, t0, t1, nice, "line"),
        _line("range", "Range", " " + lb["dist"], [(t, units.distance(v)) for t, v in ranges if v is not None], t0, t1, lambda v: nice(v, zero=False)),
        _line("temp", "Outside", lb["temp"], [(t, units.temperature(v)) for t, v in outside if v is not None and v > NO_READING], t0, t1, lambda v: nice(v, zero=False)),
    ]
    present = [x for x in lines if x]
    lead = next((x for x in present if x["key"] in ("level", "speed", "elevation")), None)
    primary = lead or {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": []}
    # height under the lane on the same clock, unless it is the chart's own line
    strip = next((x for x in present if x["key"] == "elevation" and x is not lead), None)
    out = payload(t0, t1, primary, [x for x in present if x is not lead and x is not strip], [], [event], ticks(t0, t1, tz), on=on, tz=tz)
    if strip:
        out["strip"] = dict(strip, tone="data")  # neutral: green is charging
    return out


def charge_chart(t0, t1, powers, levels, ranges, volts, amps, event, units, tz, outside=(), rate=(), on=None) -> dict:
    """A charge's chart: power leads; battery, rated range, the range it was adding an hour
    (`rate`, miles an hour), volts, amps and the outside temperature are offered as extra
    lines, `on` the ones shown at first."""
    lb = units.labels
    # a charge's readings come as runs, a value held from one reading to the next, so an hour at
    # 11 kW is two points an hour apart: that is no gap, and the line must not break there
    held = (t1 - t0).total_seconds() + 1
    power = _line("power", "Power", " kW", powers, t0, t1, nice, "line", held)
    rest = [
        _line("level", "Battery", "%", levels, t0, t1, level_scale),  # the session's own window, so 65→80% is a climb, not a flat line
        _line("range", "Range", " " + lb["dist"], [(t, units.distance(v)) for t, v in ranges if v is not None], t0, t1, nice),
        _line("rate", "Adding", " " + lb["dist"] + "/h", [(t, units.distance(v)) for t, v in rate if v is not None], t0, t1, nice, "line", held),
        _line("volts", "Volts", " V", volts, t0, t1, lambda v: nice(v, zero=False), "line", held),
        _line("amps", "Amps", " A", amps, t0, t1, nice, "line", held),
        _line("temp", "Outside", lb["temp"], [(t, units.temperature(v)) for t, v in outside if v is not None and v > NO_READING], t0, t1, lambda v: nice(v, zero=False)),
    ]
    primary = power or {"key": "power", "label": "Power", "unit": " kW", "lo": 0, "hi": 10, "points": []}
    return payload(t0, t1, primary, [x for x in rest if x], [], [event], ticks(t0, t1, tz), on=on, tz=tz)


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
    return payload(t0, t1, lead, [x for x in (make(e) for e in extras) if x], [], [], ticks(t0, t1, tz), tz=tz)
