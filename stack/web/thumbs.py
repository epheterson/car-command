"""A route as a small inline SVG, from the positions already in hand.

The Timeline shows one drive a row, and a row that says "Home → Oak Avenue" reads
better with the shape of the route beside it. A tile map per
row would be fifty Leaflet instances on one page; this is a polyline in a box, drawn
server-side from the same downsampled points the Calendar's month map already fetches,
so a row costs a few hundred bytes and no request.
"""

from __future__ import annotations

from math import cos, hypot, radians
from typing import Sequence


def route_svg(points: Sequence[Sequence[float]], width: int = 96, height: int = 48, pad: int = 3) -> str:
    """An <svg> of the route through `points` ([lat, lon] pairs), or '' for fewer than two.

    Longitude is scaled by cos(latitude) so a north-south mile and an east-west mile are
    the same length on the page; the route is then fitted to the box on its longer side
    and centred on the other. Start is a hollow dot, end a filled one, as on the maps.
    """
    pts = [(float(la), float(lo)) for la, lo in points if la is not None and lo is not None]
    if len(pts) < 2:
        return ""
    k = cos(radians(sum(la for la, _ in pts) / len(pts)))
    xs = [lo * k for _, lo in pts]
    ys = [la for la, _ in pts]
    span_x, span_y = (max(xs) - min(xs)) or 1e-9, (max(ys) - min(ys)) or 1e-9
    scale = min((width - 2 * pad) / span_x, (height - 2 * pad) / span_y)
    off_x = (width - span_x * scale) / 2
    off_y = (height - span_y * scale) / 2
    xy = [(off_x + (x - min(xs)) * scale, height - off_y - (y - min(ys)) * scale) for x, y in zip(xs, ys)]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    (x0, y0), (x1, y1) = xy[0], xy[-1]
    return (
        f'<svg class="route" viewBox="0 0 {width} {height}" width="{width}" height="{height}" aria-hidden="true">'
        f'<polyline points="{path}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{x0:.1f}" cy="{y0:.1f}" r="2.2" fill="var(--panel, #fff)" stroke="currentColor" stroke-width="1.2"/>'
        f'<circle cx="{x1:.1f}" cy="{y1:.1f}" r="2.4" fill="currentColor"/></svg>'
    )


def thin(points: Sequence[Sequence[float]], limit: int = 40) -> list:
    """At most `limit` points, evenly spaced, always keeping both ends."""
    if len(points) <= limit:
        return list(points)
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


# A parked fix this far from the route's end is not where this drive began or ended: the
# positions between are missing, or it is another day's spot. Drawing to it would be a
# straight line across the map.
PARKED_MAX_M = 500


def meters(a: Sequence[float], b: Sequence[float]) -> float:
    """Distance between two [lat, lon] points, flat-earth; plenty at a few hundred metres."""
    k = 111_320.0
    return hypot((a[0] - b[0]) * k, (a[1] - b[1]) * k * cos(radians((a[0] + b[0]) / 2)))


def anchored(route: list, before: Sequence[float] | None, after: Sequence[float] | None, max_m: float = PARKED_MAX_M) -> list:
    """The route from where the car was parked to where it parked. The car reports every ~45 s
    while it drives, so the first and last driving fixes can be short of either spot."""
    if not route:
        return route
    out = list(route)
    if before is not None and None not in before[:2] and 0 < meters(before, out[0]) <= max_m:
        out.insert(0, [before[0], before[1]])
    if after is not None and None not in after[:2] and 0 < meters(after, out[-1]) <= max_m:
        out.append([after[0], after[1]])
    return out


def routes_for(trips: Sequence[dict], rows: Sequence[tuple], limit: int = 40) -> dict:
    """{trip id: thinned [lat, lon] points} from one month's positions.

    `rows` are (at, lat, lon) sorted by time for one car, every fix whatever the car's state;
    each trip takes the positions between its start and destination dates, plus the fix
    either side, where it was parked. One pass over both, since both are sorted.
    """
    out: dict = {}
    order = sorted((t for t in trips if t.get("start_date") and t.get("destination_date")), key=lambda t: t["start_date"])
    i = 0
    for t in order:
        while i < len(rows) and rows[i][0] < t["start_date"]:
            i += 1
        j = i
        pts = []
        while j < len(rows) and rows[j][0] <= t["destination_date"]:
            if rows[j][1] is not None and rows[j][2] is not None:
                pts.append([rows[j][1], rows[j][2]])
            j += 1
        if len(pts) >= 2:
            before = rows[i - 1][1:3] if i > 0 else None
            after = rows[j][1:3] if j < len(rows) else None
            out[t["id"]] = thin(anchored(pts, before, after), limit)
        i = j
    return out
