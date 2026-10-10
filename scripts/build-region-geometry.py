#!/usr/bin/env python3
"""Build stack/web/rates/regions.json: country and US state outlines for finding which
price region a charge was in. Standard library only.

    scripts/build-region-geometry.py [--src DIR] [--out PATH]

Reads Natural Earth 1:50m admin-0 countries and admin-1 states (public domain), pinned to
a release so a rebuild gives the same file. Keeps every country with an ISO code and the
fifty US states plus DC, simplifies each outline (Douglas-Peucker, 0.01 degrees, about
1 km), drops islands smaller than a few square kilometres unless they are a region's
only land, and stores each ring as integer hundredths of a degree, delta-encoded. The
result is under 600 KB.

--src reads the two .geojson files from a directory instead of downloading them.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

RELEASE = "v5.1.2"
BASE = f"https://raw.githubusercontent.com/nvkelso/natural-earth-vector/{RELEASE}/geojson/"
COUNTRIES = "ne_50m_admin_0_countries.geojson"
STATES = "ne_50m_admin_1_states_provinces.geojson"
OUT = Path(__file__).resolve().parents[1] / "stack/web/rates/regions.json"

TOLERANCE = 0.01  # degrees, for simplification
SCALE = 100  # stored coordinates are integer hundredths of a degree
MIN_AREA = 0.002  # square degrees; smaller islands go unless they are all a region has


def load(src: Path | None, name: str) -> dict:
    if src:
        return json.loads((src / name).read_text())
    with urllib.request.urlopen(BASE + name, timeout=120) as r:  # noqa: S310 - a fixed https URL
        return json.loads(r.read())


def _perp(p, a, b) -> float:
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    return ((x - x1 - t * dx) ** 2 + (y - y1 - t * dy) ** 2) ** 0.5


def simplify(pts: list, tol: float) -> list:
    """Douglas-Peucker, iterative so a long coastline cannot overflow the stack."""
    if len(pts) < 4:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        far, at = 0.0, None
        for k in range(i + 1, j):
            d = _perp(pts[k], pts[i], pts[j])
            if d > far:
                far, at = d, k
        if at is not None and far > tol:
            keep[at] = True
            stack += [(i, at), (at, j)]
    return [p for p, k in zip(pts, keep) if k]


def area(ring: list) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]))) / 2


def encode(ring: list) -> list[int] | None:
    """Integer hundredths, repeated points dropped, as [x0, y0, dx1, dy1, ...]."""
    q = []
    for x, y in ring:
        p = (round(x * SCALE), round(y * SCALE))
        if not q or q[-1] != p:
            q.append(p)
    if len(q) > 1 and q[0] == q[-1]:
        q.pop()
    if len(q) < 3:
        return None
    out = [q[0][0], q[0][1]]
    for (ax, ay), (bx, by) in zip(q, q[1:]):
        out += [bx - ax, by - ay]
    return out


def polygons(geom: dict) -> list[list[list]]:
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return geom["coordinates"]
    return []


def region(code: str, name: str, geom: dict) -> dict | None:
    polys = polygons(geom)
    biggest = max((area(p[0]) for p in polys), default=0)
    out, lo_x, lo_y, hi_x, hi_y = [], 999.0, 999.0, -999.0, -999.0
    for poly in polys:
        a = area(poly[0])
        if a < MIN_AREA and a < biggest:
            continue
        rings = []
        for i, ring in enumerate(poly):
            if i and area(ring) < MIN_AREA:
                continue  # a lake too small to matter
            enc = encode(simplify(ring, TOLERANCE))
            if enc:
                rings.append(enc)
        if rings:
            out.append(rings)
            for x, y in poly[0]:
                lo_x, lo_y, hi_x, hi_y = min(lo_x, x), min(lo_y, y), max(hi_x, x), max(hi_y, y)
    if not out:
        return None
    return {"name": name, "bbox": [round(lo_x, 2), round(lo_y, 2), round(hi_x, 2), round(hi_y, 2)], "polygons": out}


def build(src: Path | None) -> dict:
    regions = {}
    for f in load(src, COUNTRIES)["features"]:
        p = f["properties"]
        code = p.get("ISO_A2_EH") if p.get("ISO_A2_EH") not in (None, "-99") else p.get("ISO_A2")
        if not code or code == "-99" or code in regions:
            continue
        r = region(code, p.get("NAME") or code, f["geometry"])
        if r:
            regions[code] = r
    states = {}
    for f in load(src, STATES)["features"]:
        p = f["properties"]
        if p.get("iso_a2") != "US" or not str(p.get("iso_3166_2", "")).startswith("US-"):
            continue
        r = region(p["iso_3166_2"], p.get("name") or p["iso_3166_2"], f["geometry"])
        if r:
            states[p["iso_3166_2"]] = r
    return {
        "source": f"Natural Earth 1:50m {RELEASE} (public domain), simplified",
        "scale": SCALE,
        "countries": dict(sorted(regions.items())),
        "states": dict(sorted(states.items())),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, help="directory holding the two Natural Earth .geojson files")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    data = build(args.src)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, separators=(",", ":")))
    size = args.out.stat().st_size
    print(f"{args.out}: {len(data['countries'])} countries, {len(data['states'])} states, {size / 1024:.0f} KB")
    return 0 if size <= 600 * 1024 else 1


if __name__ == "__main__":
    sys.exit(main())
