"""What unit the Lucid's raw GPS height is in: a straight-line fit from the raw number to metres.

The connector publishes gps_elevation_raw as the car sends it (one read 8932 where the Teslas
record 123 m at the same spot), and the recorder keeps it beside each position in
cc_positions.elevation_raw. Two references give metres for the same places: the Teslas' own
heights where the cars share a road (the commute), and the terrain model where its tiles are
on this box. Each is fitted on its own, so they can be compared: a slope and intercept, how far
the points sit from the line (rmse, metres), and how many points.

Pure apart from main(), which reads the database and prints both fits:
    python3 elevfit.py          (make elevation-fit runs it on the host)
"""

from __future__ import annotations

import math
import os

MATCH_M = 25.0  # a Tesla fix this close counts as the same place
CELL_DEG = 0.0005  # the matching grid, about 55 m north-south


def linear_fit(pairs: list[tuple[float, float]]) -> dict | None:
    """Least squares metres = slope * raw + intercept over (raw, metres) pairs, with the rmse of
    the residuals in metres and r². None under three pairs or when every raw value is the same."""
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    slope = sxy / sxx
    intercept = my - slope * mx
    sse = sum((y - (slope * x + intercept)) ** 2 for x, y in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    return {"slope": slope, "intercept": intercept, "n": n, "rmse_m": math.sqrt(sse / n), "r2": 1 - sse / syy if syy else None}


def _metres(lat1, lon1, lat2, lon2) -> float:
    dy = (lat2 - lat1) * 111_320.0
    dx = (lon2 - lon1) * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def match(raw: list[tuple[float, float, float]], ref: list[tuple[float, float, float]], within_m: float = MATCH_M) -> list[tuple[float, float]]:
    """(raw value, reference metres) for each raw (lat, lon, value) point with a reference (lat,
    lon, metres) point within `within_m`, the nearest one. A grid over the references, so a
    commute's thousands of fixes against years of another car's are matched in one pass."""
    grid: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    for p in ref:
        grid.setdefault((int(p[0] // CELL_DEG), int(p[1] // CELL_DEG)), []).append(p)
    out = []
    for lat, lon, value in raw:
        ci, cj = int(lat // CELL_DEG), int(lon // CELL_DEG)
        best = None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for rl, ro, m in grid.get((ci + di, cj + dj), ()):
                    d = _metres(lat, lon, rl, ro)
                    if d <= within_m and (best is None or d < best[0]):
                        best = (d, m)
        if best is not None:
            out.append((value, best[1]))
    return out


def fit_both(raw: list[tuple[float, float, float]], ref: list[tuple[float, float, float]], height=None) -> dict:
    """The fit against the other cars' heights and against the terrain model (`height(lat, lon)`
    in metres or None; None for no model), each None when it has too little to go on."""
    ground = [(v, h) for lat, lon, v in raw if height is not None and (h := height(lat, lon)) is not None]
    return {"cars": linear_fit(match(raw, ref)), "terrain": linear_fit(ground)}


def describe(name: str, fit: dict | None) -> str:
    if fit is None:
        return f"{name}: not enough matched points yet"
    r2 = f", r² {fit['r2']:.3f}" if fit["r2"] is not None else ""
    sign = "−" if fit["intercept"] < 0 else "+"
    return f"{name}: metres = {fit['slope']:.6g} × raw {sign} {abs(fit['intercept']):.4g}  (rmse {fit['rmse_m']:.1f} m over {fit['n']} points{r2})"


def main() -> None:
    import psycopg

    import identity
    import terrain

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        raw = conn.execute("SELECT latitude, longitude, elevation_raw FROM cc_positions WHERE elevation_raw IS NOT NULL").fetchall()
        if not raw:
            print("No raw heights recorded yet: they arrive with the Lucid connector 0.4.0.")
            return
        lats, lons = [r[0] for r in raw], [r[1] for r in raw]
        pad = 0.01
        teslas = [r[0] for r in conn.execute("SELECT DISTINCT vin FROM cc_positions WHERE elevation IS NOT NULL").fetchall() if identity.is_tesla(r[0])]
        ref = conn.execute(
            "SELECT latitude, longitude, elevation FROM cc_positions WHERE vin = ANY(%s) AND elevation IS NOT NULL AND latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s",
            (teslas, min(lats) - pad, max(lats) + pad, min(lons) - pad, max(lons) + pad),
        ).fetchall()
    model = terrain.Terrain(os.environ.get("TERRAIN_DIR"))
    fits = fit_both([tuple(r) for r in raw], [tuple(r) for r in ref], model.height if model else None)
    print(f"{len(raw)} raw heights; {len(ref)} Tesla fixes nearby")
    print(describe("Against the Teslas", fits["cars"]))
    print(describe("Against the terrain model", fits["terrain"]) if model else "Against the terrain model: no tiles on this box")


if __name__ == "__main__":
    main()
