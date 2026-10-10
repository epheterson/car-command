"""Which price region a point is in: a US state ('US-CA'), else a country ('DE'), else None.

Reads the outlines bundled at rates/regions.json (Natural Earth, simplified; built by
scripts/build-region-geometry.py) and answers locally: nothing about the point leaves the
process. Used only with a charge session's own position, to pick the regional average
price for that charge; drive positions are never passed here.

A point just off a simplified coastline (a charger on a pier, a ferry terminal) still
belongs to the nearest region within NEAR degrees; anything further out is None.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

FILE = Path(__file__).resolve().parent / "rates" / "regions.json"
NEAR = 0.1  # degrees (about 10 km): how far off an outline a point may sit and still count


def _decode(flat: list[int], scale: int) -> list[tuple[float, float]]:
    x, y = flat[0], flat[1]
    out = [(x / scale, y / scale)]
    for i in range(2, len(flat), 2):
        x += flat[i]
        y += flat[i + 1]
        out.append((x / scale, y / scale))
    return out


@lru_cache(maxsize=1)
def _load(path: str = str(FILE)) -> tuple[list, list, dict]:
    """(states, countries, names): each region as (code, bbox, [[ring, hole, ...], ...])."""
    data = json.loads(Path(path).read_text())
    scale = data.get("scale", 100)

    def regions(group: dict) -> list:
        return [(code, tuple(r["bbox"]), [[_decode(ring, scale) for ring in poly] for poly in r["polygons"]]) for code, r in group.items()]

    names = {code: r["name"] for group in (data["states"], data["countries"]) for code, r in group.items()}
    return regions(data["states"]), regions(data["countries"]), names


def _inside(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    hit = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def _edge_distance(x: float, y: float, ring: list[tuple[float, float]]) -> float:
    best = float("inf")
    j = len(ring) - 1
    for i in range(len(ring)):
        (x1, y1), (x2, y2) = ring[j], ring[i]
        dx, dy = x2 - x1, y2 - y1
        t = 0.0 if dx == 0 and dy == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
        best = min(best, ((x - x1 - t * dx) ** 2 + (y - y1 - t * dy) ** 2) ** 0.5)
        j = i
    return best


def _contains(x: float, y: float, polys: list) -> bool:
    return any(_inside(x, y, poly[0]) and not any(_inside(x, y, hole) for hole in poly[1:]) for poly in polys)


def _find(x: float, y: float, group: list) -> str | None:
    near, best = None, NEAR
    for code, (x0, y0, x1, y1), polys in group:
        if x0 - NEAR <= x <= x1 + NEAR and y0 - NEAR <= y <= y1 + NEAR:
            if x0 <= x <= x1 and y0 <= y <= y1 and _contains(x, y, polys):
                return code
            d = min(_edge_distance(x, y, poly[0]) for poly in polys)
            if d < best:
                near, best = code, d
    return near


def region_of(lat, lon) -> str | None:
    """'US-IL' for a point in Illinois, 'DE' for one in Germany, 'US' for one in the US
    that no state outline holds, None for open sea or no position."""
    if lat is None or lon is None:
        return None
    try:
        y, x = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= y <= 90 and -180 <= x <= 180):
        return None
    states, countries, _ = _load()
    country = _find(x, y, countries)
    if country == "US" or country is None:
        state = _find(x, y, states)
        if state:
            return state
    return country


def name_of(code: str | None) -> str | None:
    """'Illinois' for 'US-IL', 'Germany' for 'DE'."""
    return _load()[2].get(code) if code else None
