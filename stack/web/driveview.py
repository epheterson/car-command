"""What a drive's map and rows draw beyond the line itself: the route given times and speeds
from the car's own readings (so the chart can move a marker along it and the line can be
coloured by speed), how the drive's time divides between speed bands, and a small elevation
profile for a drive's row. Pure: lists in, lists out."""

from __future__ import annotations

import math
from datetime import datetime

# Speed bands, in the units on screen: the same steps on every drive, so one drive's blue
# means what another's does. The last band is open-ended.
BANDS = {"mph": (0, 25, 45, 60, 75), "km/h": (0, 40, 70, 100, 120)}
GAP = 300  # seconds between two readings past which the time between them is not driving time


def band_of(v: float | None, steps) -> int | None:
    """The band a speed falls in: 0 for the slowest, len(steps) - 1 for the open top one."""
    if v is None:
        return None
    return max(0, sum(1 for s in steps if v >= s) - 1)


def band_labels(steps) -> list[str]:
    return [f"{a}–{b}" for a, b in zip(steps, steps[1:])] + [f"{steps[-1]}+"]


def _d2(a, b) -> float:
    """Squared distance between two [lat, lon] points, flat-earth: only ever compared."""
    k = math.cos(math.radians(a[0]))
    return (a[0] - b[0]) ** 2 + ((a[1] - b[1]) * k) ** 2


def timed_route(route: list, track: list[dict], speed=lambda v: v) -> list[list]:
    """The route's points as [lat, lon, epoch seconds, speed], times from the readings.

    The route may be the readings themselves or a line matched to the roads with many more
    (or fewer) points than readings and no times. Each point takes the nearest reading at or
    after the last one taken, looking a window ahead, so the times only ever go forward; a
    run of points that took the same reading shares out the time to the next one by distance.
    Speed comes through `speed` (the display units); None where the reading had none."""
    fixes = [r for r in track if r.get("latitude") is not None and r.get("longitude") is not None]
    if not route or not fixes:
        return []
    window = max(40, 3 * len(fixes) // max(1, len(route)))
    j, picks = 0, []
    for p in route:
        best, k = None, j
        for i in range(j, min(len(fixes), j + window)):
            d = _d2(p, (fixes[i]["latitude"], fixes[i]["longitude"]))
            if best is None or d < best:
                best, k = d, i
        j = k
        picks.append(k)
    ts = [fixes[k]["at"].timestamp() for k in picks]
    # spread each run of equal times towards the next reading's time, by distance along the line
    i = 0
    while i < len(route):
        e = i
        while e + 1 < len(route) and picks[e + 1] == picks[i]:
            e += 1
        nxt = fixes[picks[i] + 1]["at"].timestamp() if picks[i] + 1 < len(fixes) else None
        if e > i and nxt is not None and e + 1 < len(route):
            seg = [math.sqrt(_d2(route[x], route[x + 1])) for x in range(i, e + 1)]
            total = sum(seg) or 1.0
            run, t0 = 0.0, ts[i]
            for x in range(i + 1, e + 1):
                run += seg[x - i - 1]
                ts[x] = t0 + (nxt - t0) * run / total
        i = e + 1
    out = []
    for p, t, k in zip(route, ts, picks):
        v = fixes[k].get("speed")
        out.append([round(p[0], 6), round(p[1], 6), round(t, 1), None if v is None else round(speed(v), 1)])
    return out


def speed_shares(track: list[dict], steps, speed=lambda v: v) -> list[dict]:
    """How the drive's time divides between the speed bands: [{label, share, minutes}] per
    band, slowest first. Each reading's speed holds until the next one, and a gap longer than
    GAP counts for nothing. Empty when no reading had a speed."""
    secs = [0.0] * len(steps)
    for a, b in zip(track, track[1:]):
        v = a.get("speed")
        dt = (b["at"] - a["at"]).total_seconds()
        if v is None or not 0 < dt <= GAP:
            continue
        secs[band_of(speed(v), steps)] += dt
    total = sum(secs)
    if not total:
        return []
    return [{"label": lb, "share": s / total, "minutes": s / 60} for lb, s in zip(band_labels(steps), secs)]


def elevation_spark(series: list[tuple[datetime, float]], n: int = 48) -> dict | None:
    """A drive's elevation as a small filled profile: {d (an SVG path in a 100 × 20 box,
    time across, the lowest point at the bottom), lo, hi} in the series' units, or None when
    there is too little to draw. Resampled to `n` points, the highest in each slice, so a
    pass is not thinned away."""
    pts = [(t.timestamp(), v) for t, v in series if v is not None]
    if len(pts) < 3:
        return None
    t0, t1 = pts[0][0], pts[-1][0]
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    if t1 <= t0:
        return None
    slots: list[float | None] = [None] * n
    for t, v in pts:
        i = min(n - 1, int((t - t0) / (t1 - t0) * n))
        slots[i] = v if slots[i] is None else max(slots[i], v)
    last = pts[0][1]
    for i, v in enumerate(slots):  # an empty slice holds the one before it
        slots[i] = last = v if v is not None else last
    span = (hi - lo) or 1.0
    # a flat drive is a low strip, not a line pinned to the top; 20 units tall, 2 kept as floor
    ys = [18 - (v - lo) / span * 16 if hi - lo >= 1 else 16 for v in slots]
    step = 100 / (n - 1)
    d = "M0,20 " + " ".join(f"L{i * step:.1f},{y:.1f}" for i, y in enumerate(ys)) + " L100,20 Z"
    return {"d": d, "lo": lo, "hi": hi}
