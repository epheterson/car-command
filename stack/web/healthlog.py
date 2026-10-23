"""The car's own battery-health number, once a day (the Lucid connector's battery_health_level,
from 0.4.0). The proto does not say what it measures, so it is kept and shown as the car sends
it, never scaled or named as a state of health.

A row a car a day, the first reading of the day; the recorder writes it, and the whole series
is held in memory (a few hundred numbers a year), so the Battery page reads it without a query.
"""

from __future__ import annotations

from datetime import date

DDL = """
CREATE TABLE IF NOT EXISTS cc_battery_health (
    vin   VARCHAR NOT NULL,
    day   DATE NOT NULL,
    level DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (vin, day)
);
"""


class HealthLog:
    def __init__(self) -> None:
        self.series: dict[str, list[tuple[date, float]]] = {}  # vin -> (day, level), oldest first

    def load(self, conn) -> None:
        """Every logged day, once at startup."""
        series: dict[str, list[tuple[date, float]]] = {}
        for r in conn.execute("SELECT vin, day, level FROM cc_battery_health ORDER BY vin, day").fetchall():
            vin, day, level = (r["vin"], r["day"], r["level"]) if isinstance(r, dict) else r
            series.setdefault(vin, []).append((day, float(level)))
        self.series = series

    def due(self, vin: str, day: date) -> bool:
        """Whether `day` has no figure yet for this car: the recorder asks before it opens a connection."""
        held = self.series.get(vin)
        return not held or held[-1][0] < day

    def record(self, conn, vin: str, level: float | None, day: date) -> tuple[date, float] | None:
        """Write the day's figure if the day has none yet; the (day, level) to keep once the
        caller has committed, else None."""
        if level is None or not self.due(vin, day):
            return None
        conn.execute("INSERT INTO cc_battery_health (vin, day, level) VALUES (%s, %s, %s) ON CONFLICT (vin, day) DO NOTHING", (vin, day, level))
        return (day, float(level))

    def keep(self, vin: str, entry: tuple[date, float]) -> None:
        """Hold a committed day's figure for the page."""
        self.series.setdefault(vin, []).append(entry)


def worth_charting(series: list[tuple[date, float]]) -> bool:
    """A chart once the number has said something: a value other than 100, or two different
    values. Before that it is one flat line at 100, and the current value says as much."""
    levels = {v for _d, v in series}
    return len(levels) >= 2 or any(v != 100 for v in levels)
