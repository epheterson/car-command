"""Each tire's pressure and the outside temperature, a day at a time, for the Battery page's
chart over the whole history.

The readings are runs (cc_tires keeps one until a tire moves 0.3 psi, outside_temperatures
one per reading held), and a run can last days: a car parked a week at 41 psi is one row.
So a day's figure is every run that overlaps it, weighted by how long it did, never the
runs that happened to start that day. Finished days never change, so they are kept in
cc_tire_days, filled by the facts tick a few hundred days at a time; a page works out only
today from the runs."""

from __future__ import annotations

from datetime import date, datetime, timedelta

DDL = """
CREATE TABLE IF NOT EXISTS cc_tire_days (
    vin       VARCHAR NOT NULL,
    day       DATE NOT NULL,
    fl        DOUBLE PRECISION,
    fr        DOUBLE PRECISION,
    rl        DOUBLE PRECISION,
    rr        DOUBLE PRECISION,
    outside_f DOUBLE PRECISION,
    PRIMARY KEY (vin, day)
);
"""

BATCH = 300  # days a tick
TIRES = ("fl", "fr", "rl", "rr")
NO_READING = -39.5  # -40 is the sensor's "no reading"


def spread(runs: list[tuple], first: date, last: date, tz) -> dict[date, list[float | None]]:
    """The time-weighted mean of each value column for every local day from `first` to `last`.
    `runs` are (first, last, v1, v2, ...); a run counts on each day it covers for the part it
    covers, a gap in the runs counts for nothing, a lone reading counts as a second, and a
    column with no run on a day is None. One pass over the runs, each split at midnights."""
    n = max((len(r) - 2 for r in runs), default=0)
    acc: dict[date, list[list[float]]] = {}
    lo, hi = _local(first, tz), _local(last + timedelta(days=1), tz)
    for r in runs:
        a, b = max(r[0], lo), min(r[1], hi)
        if b < a or (b == a and not lo <= a < hi):
            continue
        d = a.astimezone(tz).date()
        while True:
            end = min(b, _local(d + timedelta(days=1), tz))
            s = max((end - a).total_seconds(), 1.0 if r[1] == r[0] else 0.0)
            if s > 0:
                t, w = acc.setdefault(d, [[0.0] * n, [0.0] * n])
                for i, v in enumerate(r[2:]):
                    if v is not None:
                        t[i] += float(v) * s
                        w[i] += s
            if end >= b:
                break
            a, d = end, d + timedelta(days=1)
    out = {}
    d = first
    while d <= last:
        t, w = acc.get(d, ([0.0] * n, [0.0] * n))
        out[d] = [t[i] / w[i] if w[i] else None for i in range(n)]
        d += timedelta(days=1)
    return out


def day_means(runs: list[tuple], day: date, tz) -> list[float | None]:
    """spread() for one day."""
    return spread(runs, day, day, tz)[day]


def days(tire_runs: list[tuple], temp_runs: list[tuple], first: date, last: date, tz) -> list[dict]:
    """A row a day from `first` to `last`: the four tires and the outside temperature."""
    tires, temps = spread(tire_runs, first, last, tz), spread(temp_runs, first, last, tz)
    return [{"day": d, **dict(zip(TIRES, tires[d] or [None] * 4)), "outside_f": temps[d][0] if temps[d] else None} for d in tires]


def runs_sql(table: str, cols: str, extra: str = "") -> str:
    """The runs of one car that cover [%(a)s, %(b)s): the one in force at the start and those
    starting inside, both ranges on the table's (vin, first_date) index."""
    sel = f"SELECT first_date, last_date, {cols} FROM {table} WHERE vin = %(vin)s{extra}"
    return f"({sel} AND first_date < %(a)s ORDER BY first_date DESC LIMIT 1) UNION ALL ({sel} AND first_date >= %(a)s AND first_date < %(b)s) ORDER BY first_date"


def read_runs(conn, vin: str, a: datetime, b: datetime) -> tuple[list[tuple], list[tuple]]:
    p = {"vin": vin, "a": a, "b": b}
    tires = [(r["first_date"], r["last_date"], r["fl"], r["fr"], r["rl"], r["rr"]) for r in conn.execute(runs_sql("cc_tires", "fl, fr, rl, rr"), p).fetchall()]
    temps = [
        (r["first_date"], r["last_date"], r["outside_temperature"])
        for r in conn.execute(runs_sql("outside_temperatures", "outside_temperature", f" AND outside_temperature > {NO_READING}"), p).fetchall()
    ]
    return tires, temps


def _local(d: date, tz) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=tz)


def fill(conn, vin: str, tz, today: date, batch: int = BATCH) -> int:
    """Keep the next `batch` finished days of a car that has tire runs; returns how many.
    Starts after the newest kept day, or at the first run; stops at yesterday."""
    row = conn.execute("SELECT MAX(day) AS d FROM cc_tire_days WHERE vin = %s", (vin,)).fetchone()
    if row and row["d"]:
        first = row["d"] + timedelta(days=1)
    else:
        start = conn.execute("SELECT MIN(first_date) AS t FROM cc_tires WHERE vin = %s", (vin,)).fetchone()
        if not start or start["t"] is None:
            return 0
        first = start["t"].astimezone(tz).date()
    last = min(today - timedelta(days=1), first + timedelta(days=batch - 1))
    if last < first:
        return 0
    tires, temps = read_runs(conn, vin, _local(first, tz), _local(last + timedelta(days=1), tz))
    rows = days(tires, temps, first, last, tz)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO cc_tire_days (vin, day, fl, fr, rl, rr, outside_f) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (vin, day) DO NOTHING",
            [(vin, r["day"], r["fl"], r["fr"], r["rl"], r["rr"], r["outside_f"]) for r in rows],
        )
    return len(rows)


def history(conn, vin: str, tz, today: date) -> list[dict]:
    """Every kept day of a car, then today from the runs: what the chart draws."""
    kept = conn.execute("SELECT day, fl, fr, rl, rr, outside_f FROM cc_tire_days WHERE vin = %s ORDER BY day", (vin,)).fetchall()
    if not kept:
        return []
    tires, temps = read_runs(conn, vin, _local(today, tz), _local(today + timedelta(days=1), tz))
    now = days(tires, temps, today, today, tz) if kept[-1]["day"] < today else []
    return [dict(r) for r in kept] + now
