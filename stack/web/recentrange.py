"""Range at the car's recent efficiency: what the charge in the pack now would cover at the
energy per mile of its last stretch of driving.

The efficiency is worked out once, beside the other vehicle facts (facts.fill_recent_facts),
from drives already logged; a page only divides the energy in the pack by it.
"""

from __future__ import annotations

from units import drive_kwh, rated_mile_kwh

WINDOW_MI = 50.0  # drives are taken newest first until they add up to this many miles
MIN_MI = 20.0  # fewer miles than this and the figure is a guess: it is not shown
MIN_DRIVE_MI = 0.5  # a shorter drive is a shuffle: its rounded odometer would swing the average
MAX_DRIVES = 60  # a window of short hops stops here
SANE_WH_PER_MI = (80.0, 1200.0)  # outside this a drive's energy came from a bad reading


def drive_energy(row: dict, capacity_kwh: float | None, by_year: dict, all_time: float | None, tz=None) -> float | None:
    """kWh a stored drive took: the source's own sum, else rated range used, else level drop times capacity."""
    if row.get("summary_kwh") is not None:
        return row["summary_kwh"]
    k = rated_mile_kwh(by_year, row.get("vin"), row.get("start_date"), all_time, tz)
    return drive_kwh(row.get("level_start"), row.get("level_end"), capacity_kwh, row.get("range_start"), row.get("range_end"), k)


def recent_efficiency(drives: list[tuple[float | None, float | None]], window_mi: float = WINDOW_MI) -> dict | None:
    """(miles, kWh) per drive, newest first -> {"wh_per_mi", "miles", "drives"} over the drives that
    add up to `window_mi` (the drive that crosses the line is kept whole), or None when the
    usable ones add up to less than MIN_MI."""
    miles = kwh = 0.0
    used = 0
    for mi, e in drives[:MAX_DRIVES]:
        if mi is None or e is None or mi < MIN_DRIVE_MI or e <= 0:
            continue
        miles += mi
        kwh += e
        used += 1
        if miles >= window_mi:
            break
    if miles < MIN_MI:
        return None
    wh = kwh * 1000 / miles
    if not SANE_WH_PER_MI[0] <= wh <= SANE_WH_PER_MI[1]:
        return None
    return {"wh_per_mi": wh, "miles": miles, "drives": used}


def range_at(level_pct: float | None, capacity_kwh: float | None, wh_per_mi: float | None) -> float | None:
    """Miles the charge now covers at `wh_per_mi`: level of the usable capacity, over the rate. None when any is missing."""
    if level_pct is None or not capacity_kwh or capacity_kwh <= 0 or not wh_per_mi or wh_per_mi <= 0:
        return None
    return capacity_kwh * max(level_pct, 0.0) / 100 * 1000 / wh_per_mi
