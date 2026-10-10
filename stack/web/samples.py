"""The readings behind a drive or a charge, as rows rather than as a chart.

The detail pages have always drawn these: speed, battery, rated range, elevation, power,
volts. What they could not do is show you the numbers. Nothing here is fetched or stored that
was not already recorded — the same series the charts use are lined up against a common
clock and handed over as rows.

The clock is the position track for a drive and the power runs for a charge, because in
each case that is the series that only exists while the thing is happening. Everything
else is a value that persists between readings, and is looked up rather than resampled.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence


def in_force(spine: Sequence[datetime], runs: Iterable[tuple[datetime, datetime, Any]]) -> list[Any]:
    """The value in force at each moment of `spine`.

    The interval tables keep one row per run of a value, first_date to last_date, so the
    reading at a moment is the run covering it — or, between runs, the last one before
    it, because a battery that was at 62 % a minute ago is still the latest thing anyone
    knows about it. A moment before the first run has no answer and gets None.

    Both sequences are sorted, so they are walked together once. A thousand samples
    against a thousand runs costs two thousand steps rather than a million, which is the
    same reason the facts tables exist: a per-row lookup on a page is how Statistics
    became a 35-second page.
    """
    runs = list(runs)
    out: list[Any] = []
    i, current = 0, None
    for t in spine:
        while i < len(runs) and runs[i][0] <= t:
            current = runs[i][2]
            i += 1
        out.append(current)
    return out


def align(spine: Sequence[datetime], **series: Iterable[tuple[datetime, datetime, Any]]) -> list[dict]:
    """One dict per moment of `spine`, holding each named series' value in force."""
    columns = {name: in_force(spine, runs) for name, runs in series.items()}
    return [{name: values[i] for name, values in columns.items()} for i in range(len(spine))]


def drive_samples(track: Sequence[dict], **series: Iterable[tuple[datetime, datetime, Any]]) -> list[dict]:
    """Every position fix in a drive, with the readings that were in force at it.

    A fix already carries what the recorder writes with it — where, how fast, the
    odometer, the battery, the elevation — so only the values kept in their own interval
    tables are looked up. A drive whose track was never recorded has no rows here, and
    the page says so rather than showing an empty table.
    """
    spine = [r["at"] for r in track]
    return [
        {
            "at": fix["at"],
            "speed": fix.get("speed"),
            "level": fix.get("level"),
            "odometer": fix.get("odometer"),
            "elevation": fix.get("elevation"),
            "power_kw": fix.get("power_kw"),
            "latitude": fix.get("latitude"),
            "longitude": fix.get("longitude"),
            **extra,
        }
        for fix, extra in zip(track, align(spine, **series))
    ]


def change_points(series: Iterable[Iterable[tuple[datetime, datetime, Any]]], start: datetime | None = None, end: datetime | None = None) -> list[datetime]:
    """Every moment at which any of these series takes a new value, inside the window.

    `start` is always one of them, so the first row of a table is the beginning of the
    thing being described rather than whenever the first reading happened to land.
    """
    ts = {first for runs in series for first, *_rest in runs}
    if start is not None:
        ts.add(start)
    return sorted(t for t in ts if (start is None or t >= start) and (end is None or t <= end))


def charge_samples(start: datetime, end: datetime, powers: Sequence[tuple[datetime, datetime, float]], **series: Iterable[tuple[datetime, datetime, Any]]) -> list[dict]:
    """One row per moment something changed during a session.

    The obvious clock, the charging-power readings, is the wrong one: charging_powers
    keeps a run per constant value, so a steady 6.6 kW charge overnight is a single run
    and a table built on it has exactly one row. A session's readings are the moments any
    of them moved — the battery ticking up a percent, the rated range with it, the
    voltage sagging — so that is the clock, and power is looked up like everything else.
    """
    named = {k: list(v) for k, v in series.items()}
    runs = list(powers)
    spine = change_points([runs, *named.values()], start, end)
    return [{"at": t, **row} for t, row in zip(spine, align(spine, kw=runs, **named))]


def thinned(rows: Sequence[dict], limit: int) -> tuple[list[dict], int]:
    """At most `limit` rows, evenly spaced, and how many moments one row stands for.

    A twenty-second sample rate turns a long road trip into thousands of rows, which is
    a slow page for something nobody scrolls to the bottom of. Taking every nth keeps
    the shape and the endpoints; the page says what n is, so a number that looks missing
    can be understood as skipped rather than absent.
    """
    n = len(rows)
    if limit <= 0 or n <= limit:
        return list(rows), 1
    step = -(-n // limit)  # ceiling, so the count never exceeds the limit
    out = [rows[i] for i in range(0, n, step)]
    # Taking every nth rarely lands on the last one, and the end of a drive is not a row
    # to drop: it is where you arrived. It replaces the final pick rather than being
    # appended to it, so the limit stays a limit.
    out[-1] = rows[-1]
    return out, step


# Below this the reading is not a charger delivering power: a parked car reports a few
# volts of residual bus voltage and an amp of noise, and multiplying those together
# would turn an idle night into energy that was never drawn.
CHARGING_VOLTS = 50.0
CHARGING_AMPS = 1.0

# How long a single reading is taken to describe. The electrics are sampled about once a
# minute; five minutes is generous enough to ride out a missed poll and short enough that
# a gap — a car unplugged and plugged back in — is treated as the gap it is rather than
# as energy nobody drew.
SAMPLE_HOLD = timedelta(minutes=5)


def as_runs(samples: Sequence[tuple[datetime, datetime, Any]], hold: timedelta = SAMPLE_HOLD) -> list[tuple[datetime, datetime, Any]]:
    """Instant readings as runs, each holding until the next one, up to `hold`.

    cc_charging_electrics keeps one row per reading with first_date equal to last_date,
    because a voltage that drifts a tenth of a volt a minute never repeats and so never
    forms a run. Integrating rows of zero length gives zero energy, which is how a
    four-hour charge at a steady 235 V and 48 A first came out as 0.9 kWh.
    """
    out = []
    for i, (first, last, value) in enumerate(samples):
        nxt = samples[i + 1][0] if i + 1 < len(samples) else None
        end = min(nxt, first + hold) if nxt is not None else first + hold
        out.append((first, max(last, end), value))
    return out


def with_measured_power(powers: Sequence[tuple[datetime, datetime, float]], electrics: Sequence[tuple[datetime, datetime, float, float]]) -> list[tuple[datetime, datetime, float]]:
    """Charging power runs, with volts times amps standing in where the car reported none.

    Both Teslas stopped reporting charger_power partway through their history, and a
    session whose power reads zero throughout integrates to zero energy. 918 of 4,143
    finished charges said "0.0 kWh added, 0.0 kW peak" on a page that also said the
    battery went from 19 % to 83 %, and those sessions cost nothing on every total that
    added them up.

    Volts and amps are still recorded for 897 of them, and their product tracks the
    reported power closely wherever both exist: over 183,372 overlapping readings the
    median ratio is 0.985 and the middle 80 % falls between 0.955 and 1.034. Close, but
    not the same number, so it stands in only where the reported power is zero. A
    session the car does report is left exactly as it was.

    Zero stretches stay in the result. charging_powers writes them on purpose, so that a
    run can never bridge two sessions, and dropping them would let one do so.
    """
    derived = as_runs([(f, l, v * a / 1000.0) for f, l, v, a in electrics if v is not None and a is not None and v > CHARGING_VOLTS and a > CHARGING_AMPS])
    # The reported rows get the same hold. A car whose power drifts a tenth of a kilowatt
    # a poll (the Lucid: 1,269 of its 2,275 rows in a month) never repeats a value, so
    # its rows are instants too, and a 17-hour charge at 10.4 kW integrated to 0.0 kWh.
    reported = as_runs(list(powers))
    if not derived:
        return reported
    edges = sorted({t for first, last, *_ in [*reported, *derived] for t in (first, last)})
    out: list[tuple[datetime, datetime, float]] = []
    pi = di = 0
    for a_t, b_t in zip(edges, edges[1:]):
        while pi < len(reported) and reported[pi][1] <= a_t:
            pi += 1
        while di < len(derived) and derived[di][1] <= a_t:
            di += 1
        kw = reported[pi][2] if pi < len(reported) and reported[pi][0] <= a_t else 0.0
        if not kw and di < len(derived) and derived[di][0] <= a_t:
            kw = derived[di][2]
        out.append((a_t, b_t, kw or 0.0))
    return out


def run_points(runs: Sequence[tuple[datetime, datetime, float]], start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """Chart points for runs of a value inside one window: clipped to it, and stepped.

    Two things a chart of runs gets wrong when it is fed their raw endpoints. A run that
    began long before the window drags the x-axis back to its first date: a three-hour
    charge on Aug 29 was drawn on an axis that started in July because a months-long
    0 kW run touched it. And a gap between one run's end and the next one's start is
    not a slope: the last thing the car said still stands until it says otherwise, so
    the point before the next run carries the previous value, and the line steps rather
    than ramps across eleven unreported hours.
    """
    out: list[tuple[datetime, float]] = []
    prev: float | None = None
    for first, last, value in runs:
        if value is None or last < start or first > end:
            continue
        a, b = max(first, start), min(last, end)
        if prev is not None and out and a > out[-1][0]:
            out.append((a, prev))
        out.append((a, value))
        if b > a:
            out.append((b, value))
        prev = value
    return out
