"""Efficiency by outside temperature as a chart (Stats): a bar of the miles driven in each
5-degree band, with the energy a mile drawn over it as a line; with "compare with" on, the
second set's bars sit beside the first's and its line is drawn the same way.

Built from stats.efficiency_bins rows over finer bands, from the drives the page already has.
Everything is placed in a W by H box here, so the template only writes rectangles and paths;
its labels are HTML at a share of the width or height, so they keep their size when the SVG
stretches across its card."""

from __future__ import annotations

from chartdata import nice

W, H = 600, 160
STEP = 5  # degrees a band, in the unit the page shows
BAR_TOP = 0.62  # the tallest bar reaches this share of the height, under most of the line


def bands(metric: bool) -> list[tuple[float, float, float]]:
    """(lo °F, hi °F, lo shown) for each band, wide enough for any climate: -25 to 120 °F,
    or -30 to 50 °C (a 5 °C band is 9 °F). The first and last are open-ended."""
    if metric:
        shown = list(range(-30, 50, STEP))
        out = [(c * 9 / 5 + 32, (c + STEP) * 9 / 5 + 32, c) for c in shown]
    else:
        shown = list(range(-25, 120, STEP))
        out = [(f, f + STEP, f) for f in shown]
    lo0, hi0, s0 = out[0]
    lo1, hi1, s1 = out[-1]
    return [(-200.0, hi0, s0)] + out[1:-1] + [(lo1, 300.0, s1)]


def chart(sides: list[list[dict]], temp_bands: list[tuple[float, float, float]]) -> dict | None:
    """One side, or two to compare: each a list of efficiency_bins rows ({lo, hi, drives,
    miles, wh_per_mi}) over `temp_bands`. The bands run from the first to the last that any
    side drove in; one with nothing on a side is a gap in that side's line."""
    by = [{r["lo"]: r for r in side} for side in sides]
    used = [i for i, (lo, _hi, _s) in enumerate(temp_bands) if any(lo in b for b in by)]
    if len(used) < 2:
        return None
    span = temp_bands[used[0] : used[-1] + 1]
    n, k = len(span), len(sides)
    slot = W / n
    top = max(r["miles"] for b in by for r in b.values()) or 1.0
    lo, hi = nice([r["wh_per_mi"] for b in by for r in b.values()], zero=False)
    y = lambda v: round(H - 6 - (v - lo) / ((hi - lo) or 1) * (H - 12), 2)  # noqa: E731
    slots, bars, lines = [], [], [{"side": i, "d": "", "dots": []} for i in range(k)]
    bw = slot * 0.72 / k
    for j, (flo, _fhi, shown) in enumerate(span):
        cx = j * slot + slot / 2
        cells = [b.get(flo) for b in by]
        slots.append({"x": round(j * slot, 2), "w": round(slot, 2), "left": round((j + 0.5) / n * 100, 2), "shown": shown, "open": j == 0 and flo < -100 or j == n - 1 and _fhi > 200, "cells": cells})
        for i, c in enumerate(cells):
            if c:
                h = max(1.0, c["miles"] / top * H * BAR_TOP)
                bars.append({"side": i, "x": round(cx - slot * 0.36 + i * bw, 2), "w": round(bw * 0.9, 2), "y": round(H - h, 2), "h": round(h, 2)})
                gap = not lines[i]["dots"] or lines[i]["dots"][-1]["j"] != j - 1
                lines[i]["d"] += ("M" if gap else "L") + f"{cx:.1f},{y(c['wh_per_mi'])}"
                lines[i]["dots"].append({"j": j, "x": round(cx, 2), "y": y(c["wh_per_mi"])})
    ticks = [{"v": v, "top": round(y(v) / H * 100, 2)} for v in (lo, (lo + hi) / 2, hi)]
    return {"w": W, "h": H, "slots": slots, "bars": bars, "lines": lines, "ticks": ticks, "every": 1 if n <= 12 else 2}
