"""Point-in-polygon for GeoJSON (holes honoured), bounding boxes, and distance. No dependencies."""

from __future__ import annotations

import math


def _in_ring(ring, lat: float, lon: float) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _in_polygon(rings, lat: float, lon: float) -> bool:
    return bool(rings) and _in_ring(rings[0], lat, lon) and not any(_in_ring(h, lat, lon) for h in rings[1:])


def contains(geometry: dict, lat: float, lon: float) -> bool:
    if not geometry:
        return False
    if geometry["type"] == "Polygon":
        return _in_polygon(geometry["coordinates"], lat, lon)
    if geometry["type"] == "MultiPolygon":
        return any(_in_polygon(p, lat, lon) for p in geometry["coordinates"])
    return False


def _points(geometry: dict):
    """Every (lon, lat) leaf coordinate in any GeoJSON geometry -- Point through
    GeometryCollection -- found by walking the nesting until it bottoms out at a
    [lon, lat] (or [lon, lat, ele]) pair, rather than assuming Polygon's depth."""
    if not geometry:
        return
    if geometry.get("type") == "GeometryCollection":
        for g in geometry.get("geometries") or []:
            yield from _points(g)
        return
    stack = [geometry.get("coordinates")]
    while stack:
        c = stack.pop()
        if not c:
            continue
        if isinstance(c[0], (int, float)):
            yield c[0], c[1]
        else:
            stack.extend(c)


def bbox(geometry: dict) -> tuple[float, float, float, float]:
    """(min_lat, min_lon, max_lat, max_lon) over every coordinate in the geometry."""
    pts = list(_points(geometry))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(ys), min(xs), max(ys), max(xs)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(math.sqrt(a))
