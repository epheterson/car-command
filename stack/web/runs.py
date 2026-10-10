"""Runs of a value over time, written live.

The interval tables — cc_modes, cc_inside_temperatures — hold one row per stretch during
which a value held: first_date to last_date, and the value. The importer builds them
from a whole export at once. Nothing built them from the live car, so a car logged by
the connector rather than by an import had no cabin temperature series and no record
of when Sentry or the climate was on — which is why the parked-cost table on Sleep said
"not enough yet" for it and always would have.

This is the live half. Each tick either stretches the open run to now, if the value
has not changed, or closes it and starts another. One SELECT and one write a tick per
table; the tables are small and indexed on (vin, first_date).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Sequence


def extend_or_start(conn, table: str, vin: str, now: datetime, columns: Sequence[str], values: Sequence[Any], same: Callable[[Sequence[Any], Sequence[Any]], bool] | None = None) -> str:
    """Stretch the newest run for `vin` to `now` if its values match, else start one.

    Returns "extended", "started" or "skipped". Skipped when any value is None: a run
    that says "climate was off" for a stretch where the car had not said is a lie with
    a timestamp on it, and every table this writes is NOT NULL for that reason. `same`
    decides whether two value tuples continue one run; the default is equality, and a
    caller with a drifting float passes a tolerance instead.
    """
    if any(v is None for v in values):
        return "skipped"
    same = same or (lambda a, b: tuple(a) == tuple(b))
    cols = ", ".join(columns)
    last = conn.execute(f"SELECT id, {cols} FROM {table} WHERE vin = %s ORDER BY first_date DESC LIMIT 1", (vin,)).fetchone()
    if last is not None:
        have = tuple(last[c] for c in columns) if isinstance(last, dict) else tuple(last)[1:]
        if same(have, values):
            conn.execute(f"UPDATE {table} SET last_date = %s WHERE id = %s", (now, last["id"] if isinstance(last, dict) else last[0]))
            return "extended"
    marks = ", ".join(["%s"] * len(values))
    conn.execute(f"INSERT INTO {table} (vin, first_date, last_date, {cols}) VALUES (%s, %s, %s, {marks})", (vin, now, now, *values))
    return "started"


def within(tolerance: float) -> Callable[[Sequence[Any], Sequence[Any]], bool]:
    """A `same` for a single float that drifts: one run while it stays within `tolerance`."""
    return lambda a, b: a[0] is not None and b[0] is not None and abs(float(a[0]) - float(b[0])) <= tolerance
