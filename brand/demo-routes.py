"""Fetch the road geometry for the demo car's drives, once, into stack/web/demo_routes.json.

    python3 brand/demo-routes.py

The demo car (stack/web/demo.py) is fictional and its places are made-up points around
Springfield, Illinois, so one call to the public OSRM demo server at development time says
nothing about anyone. The generator itself never touches the network: it reads the file this
writes. Each place is snapped to the nearest road first (the printed PLACES go into demo.py),
then every leg the generator drives is routed between the snapped points and thinned to
within about 3 m of the full line.
"""

from __future__ import annotations

import json
import math
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "stack" / "web"))

import demo  # noqa: E402

OSRM = "https://router.project-osrm.org"
TOLERANCE_M = 3.0


def get(url: str) -> dict:
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "carcommand-demo-routes"}), timeout=30) as r:
                return json.load(r)
        except Exception as exc:  # the demo server rate-limits; wait and retry
            print("retrying", url, exc, file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    raise SystemExit(f"could not fetch {url}")


def simplify(pts: list[list[float]], tol_m: float) -> list[list[float]]:
    """Douglas-Peucker on a local flat projection, in metres."""
    lat0 = math.radians(pts[0][0])
    xy = [(p[1] * 111_320 * math.cos(lat0), p[0] * 110_540) for p in pts]

    def dist(p, a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        if dx == dy == 0:
            return math.hypot(p[0] - a[0], p[1] - a[1])
        t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
        return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)

    keep = {0, len(pts) - 1}
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        far, at = 0.0, None
        for k in range(i + 1, j):
            d = dist(xy[k], xy[i], xy[j])
            if d > far:
                far, at = d, k
        if at is not None and far > tol_m:
            keep.add(at)
            stack += [(i, at), (at, j)]
    return [pts[k] for k in sorted(keep)]


def main() -> None:
    places = {}
    for name, (la, lo) in demo.PLACES.items():
        loc = get(f"{OSRM}/nearest/v1/driving/{lo},{la}")["waypoints"][0]["location"]
        places[name] = (round(loc[1], 5), round(loc[0], 5))
        time.sleep(0.3)
    routes = {}
    for a, b in demo.LEGS:
        (la1, lo1), (la2, lo2) = places[a], places[b]
        data = get(f"{OSRM}/route/v1/driving/{lo1},{la1};{lo2},{la2}?overview=full&geometries=geojson")
        line = [[round(y, 5), round(x, 5)] for x, y in data["routes"][0]["geometry"]["coordinates"]]
        line[0], line[-1] = list(places[a]), list(places[b])
        routes[f"{a}>{b}"] = simplify(line, TOLERANCE_M)
        print(f"{a}>{b}: {data['routes'][0]['distance'] / 1609.34:.1f} mi, {len(routes[f'{a}>{b}'])} points", file=sys.stderr)
        time.sleep(0.3)
    out = HERE / "stack" / "web" / "demo_routes.json"
    out.write_text(json.dumps({"routes": routes}, separators=(",", ":")) + "\n")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    print("PLACES = {")
    for n, (la, lo) in places.items():
        print(f'    "{n}": ({la}, {lo}),')
    print("}")


if __name__ == "__main__":
    main()
