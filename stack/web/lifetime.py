"""The battery's lifetime report: range at 100 % over the car's whole history, from one
point a charge (cc_charge_facts: the range the pack estimated as the charge left it, the
level, the odometer), never from the raw readings.

A single charge's estimate wobbles a few miles either way, so the line is drawn through
buckets of charges along the odometer, the median of each, and a trend through those; each
bucket says how many charges it rests on. Loss is read three ways: from the best the pack
has shown, from where the record starts, and from a starting range the owner knows (the
window sticker, a reading from before logging began). Pure."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from statistics import median

import chartdata

# A charge that stops at 20 % says little about 100 %: the estimate is extrapolated five
# times over. Charges that end at this level or above are the points.
MIN_LEVEL = 60
# The most points the line is drawn through, and the fewest miles a bucket spans.
MAX_POINTS = 120
MIN_BUCKET_MI = 100
# "Now" and "first" are the median of this many charges at either end, so one odd charge
# does not set either.
ENDS = 10
# How much of the line each point of the trend leans on (LOWESS, tricube weights).
SPAN = 0.35


def clean(rows: list[tuple], min_level: float = MIN_LEVEL) -> list[tuple]:
    """(at, odometer, full) for charges that ended at `min_level` or above, in odometer
    order, without the readings that cannot be a pack's range (a third off the median
    either way: a unit mix-up, a level read as zero)."""
    pts = [(at, float(odo), float(full)) for at, odo, full, level in rows if odo is not None and full is not None and level is not None and level >= min_level and odo > 0 and full > 0]
    if not pts:
        return []
    mid = median(p[2] for p in pts)
    return sorted((p for p in pts if mid * 2 / 3 <= p[2] <= mid * 1.5), key=lambda p: (p[1], p[0]))


def bucket_size(span_mi: float, most: int = MAX_POINTS) -> float:
    """A round width in miles (100, 200, 250, 500, 1,000…) giving at most `most` buckets."""
    raw = max(span_mi / most, MIN_BUCKET_MI)
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)


def buckets(pts: list[tuple]) -> list[dict]:
    """[{odo, at, full, n}] a bucket: the median odometer, time and range of its charges."""
    if not pts:
        return []
    size = bucket_size(pts[-1][1] - pts[0][1])
    groups: dict[int, list[tuple]] = {}
    for p in pts:
        groups.setdefault(int(p[1] // size), []).append(p)
    out = []
    for k in sorted(groups):
        g = groups[k]
        out.append({"odo": median(p[1] for p in g), "at": datetime.fromtimestamp(median(p[0].timestamp() for p in g), tz=g[0][0].tzinfo), "full": median(p[2] for p in g), "n": len(g)})
    return out


def lowess(xs: list[float], ys: list[float], ws: list[float] | None = None, span: float = SPAN) -> list[float]:
    """A locally weighted straight line through (xs, ys), evaluated at each x: for each, a
    least-squares line through its nearest `span` of the points, nearer ones counting more
    (tricube), each point also weighted by `ws` (how many charges it rests on)."""
    n = len(xs)
    if n < 3:
        return list(ys)
    ws = ws or [1.0] * n
    k = max(3, min(n, int(math.ceil(span * n))))
    out = []
    for x0 in xs:
        d = sorted(abs(x - x0) for x in xs)[k - 1] or 1.0
        sw = swx = swy = swxx = swxy = 0.0
        for x, y, w0 in zip(xs, ys, ws):
            u = abs(x - x0) / d
            if u >= 1:
                continue
            w = w0 * (1 - u**3) ** 3
            sw, swx, swy, swxx, swxy = sw + w, swx + w * x, swy + w * y, swxx + w * x * x, swxy + w * x * y
        den = sw * swxx - swx * swx
        if sw <= 0:
            out.append(ys[xs.index(x0)])
        elif abs(den) < 1e-9:
            out.append(swy / sw)
        else:
            b = (sw * swxy - swx * swy) / den
            out.append((swy - b * swx) / sw + b * x0)
    return out


def loss(a: float | None, b: float | None) -> dict | None:
    """From a to b: both figures, the miles lost and the share of a."""
    if a is None or b is None or a <= 0:
        return None
    return {"from": a, "to": b, "lost": a - b, "pct": (a - b) / a}


def report(rows: list[tuple], start: float | None = None, min_level: float = MIN_LEVEL) -> dict | None:
    """Everything the Battery page draws: `rows` are (at, odometer, full, level) a charge.
    None when fewer than three charges qualify. `start` is a starting range the owner
    typed, in miles."""
    pts = clean(rows, min_level)
    if len(pts) < 3:
        return None
    bs = buckets(pts)
    trend = lowess([b["odo"] for b in bs], [b["full"] for b in bs], [math.sqrt(b["n"]) for b in bs])
    for b, t in zip(bs, trend):
        b["trend"] = t
    by_time = sorted(pts, key=lambda p: p[0])
    # Best, first and now are read off the trend, which has already weighed the wobble; a
    # median of ten charges at either end, or the best stretch, still moves by a few miles
    # with the weather and would put that noise into the headline. The raw figures stay
    # beside them for the tooltip.
    first, last = bs[0], bs[-1]
    best = max(bs, key=lambda b: b["trend"])
    raw_best = max([b for b in bs if b["n"] >= 3] or bs, key=lambda b: b["full"])
    now = last["trend"]
    return {
        "points": bs,
        "charges": len(pts),
        "min_level": min_level,
        "now": now,
        "now_at": by_time[-1][0],
        "now_raw": median(p[2] for p in by_time[-ENDS:]),
        "first": first["trend"],
        "first_at": by_time[0][0],
        "first_raw": median(p[2] for p in by_time[:ENDS]),
        "best": {**best, "full": best["trend"]},
        "best_raw": raw_best,
        "losses": {
            "best": loss(best["trend"], now),
            "first": loss(first["trend"], now),
            "start": loss(start, now) if start else None,
        },
        "odo": (pts[0][1], pts[-1][1]),
    }


def odometer_at(pts: list[dict], when: datetime) -> float | None:
    """The odometer at a moment, interpolated between the report's points (by time); None
    outside them. For year and software marks on the odometer axis, with no query."""
    seq = sorted(pts, key=lambda b: b["at"])
    if not seq or when < seq[0]["at"] or when > seq[-1]["at"]:
        return None
    for a, b in zip(seq, seq[1:]):
        if a["at"] <= when <= b["at"]:
            span = (b["at"] - a["at"]).total_seconds()
            f = (when - a["at"]).total_seconds() / span if span else 0.0
            return a["odo"] + (b["odo"] - a["odo"]) * f
    return seq[-1]["odo"]


def year_starts(first: datetime, last: datetime, tz) -> list[datetime]:
    """New Year's midnights, local, strictly between two moments."""
    a, b = first.astimezone(tz), last.astimezone(tz)
    return [datetime(y, 1, 1, tzinfo=tz) for y in range(a.year + 1, b.year + 1)]


def round_axis(vals: list[float]) -> tuple[float, float]:
    """A scale whose three labels (bottom, middle, top: chart.js draws those) all land on
    round figures, 290 / 300 / 310 rather than 290 / 303 / 315: the smallest step of 1, 2,
    2.5 or 5 times a power of ten (one at least) for which two steps up from a round bottom reach the top."""
    lo, hi = min(vals), max(vals)
    # never under one: chart.js writes whole numbers
    step = max(1, 10 ** math.floor(math.log10(max((hi - lo) / 2, 1e-6))))
    while True:
        for m in (1, 2, 2.5, 5):
            s = m * step
            bottom = math.floor(lo / s) * s
            if bottom + 2 * s >= hi:
                return bottom, bottom + 2 * s
        step *= 10


def chart(rep: dict, units, tz, by: str = "odo", versions=()) -> dict:
    """The report as a chart.js payload: the bucket medians joined as a line (no fill), the
    trend on the same scale, the charges each point rests on in the hover card, along the
    odometer (`by` "odo") or the calendar ("date"). New years are marked; each software
    update (`versions`, (version, seen_at) pairs) is a tick on the top edge, named on hover.
    The card names each point by its date and odometer whichever the axis. Display units."""
    bs, d = rep["points"], units.distance
    unit = " " + units.labels["dist"]
    lo, hi = round_axis([d(b["full"]) for b in bs] + [d(b["trend"]) for b in bs])
    x = (lambda b: round(d(b["odo"]), 1)) if by == "odo" else (lambda b: b["at"].timestamp())
    seq = bs if by == "odo" else sorted(bs, key=lambda b: b["at"])
    names = [f"{b['at'].astimezone(tz):%b %-d, %Y} · {d(b['odo']):,.0f}{unit}" for b in seq]
    primary = {"key": "full", "label": "Range at 100%", "unit": unit, "lo": lo, "hi": hi, "points": [[x(b), round(d(b["full"]), 1)] for b in seq], "names": names}
    extras = [
        {"key": "trend", "label": "Trend", "unit": unit, "lo": lo, "hi": hi, "points": [[x(b), round(d(b["trend"]), 1)] for b in seq]},
        {"key": "n", "label": "Charges", "unit": "", "lo": 0, "hi": max(b["n"] for b in bs) * 4, "points": [[x(b), b["n"]] for b in seq]},
    ]
    first, last = min(b["at"] for b in bs), max(b["at"] for b in bs)

    def place(items):
        out = []
        for when, label in items:
            if by == "odo":
                o = odometer_at(bs, when)
                if o is not None:
                    out.append([round(d(o), 1), label])
            elif first <= when <= last:
                out.append([when.timestamp(), label])
        return out

    mk = place((y, str(y.year)) for y in year_starts(first, last, tz))
    rug = place((at, v) for v, at in versions)
    if by == "odo":
        x0, x1 = d(bs[0]["odo"]), d(bs[-1]["odo"])
        ticks = [[t[0], t[1] + unit, t[2]] for t in chartdata.dist_ticks(x0, x1)]
        out = chartdata.distance_payload(x0, x1, primary, extras, ticks, unit, marks=mk, on=["trend"])
    else:
        # one stretch of charges is one moment: give the axis a day so it has a width
        last = max(last, first + timedelta(days=1))
        out = chartdata.payload(first, last, primary, extras, [], [], chartdata.ticks(first, last, tz), marks=mk, on=["trend"])
    out["line"] = True  # estimates joined straight, no fill (chart.js)
    if rug:
        out["rug"] = sorted(rug)
    return out
