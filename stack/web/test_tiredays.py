"""Tire pressures a day at a time: every run that covers a day, weighted by how long it did."""

from datetime import date, datetime, timedelta, timezone

import pytest

import tiredays

UTC = timezone.utc
D = date(2026, 1, 10)
T = datetime(2026, 1, 10, tzinfo=UTC)


def test_a_run_counts_on_every_day_it_covers_not_only_the_day_it_started():
    runs = [(T - timedelta(days=3), T + timedelta(hours=12), 40.0, 41.0, 42.0, 43.0)]
    assert tiredays.day_means(runs, D, UTC) == [40.0, 41.0, 42.0, 43.0], "a car parked for days at one pressure is one run"
    assert tiredays.day_means(runs, D - timedelta(days=2), UTC) == [40.0, 41.0, 42.0, 43.0]
    assert tiredays.day_means(runs, D + timedelta(days=1), UTC) == [None, None, None, None]


def test_a_day_is_weighted_by_how_long_each_run_lasted_and_gaps_count_for_nothing():
    runs = [(T, T + timedelta(hours=18), 40.0), (T + timedelta(hours=18), T + timedelta(hours=20), 44.0)]
    assert tiredays.day_means(runs, D, UTC) == [pytest.approx((40 * 18 + 44 * 2) / 20)], "the four unlogged hours are not a reading"
    assert tiredays.day_means([(T + timedelta(hours=5), T + timedelta(hours=5), 33.0)], D, UTC) == [33.0], "a single reading still counts"


def test_days_are_one_row_each_with_the_temperature():
    tires = [(T, T + timedelta(days=2), 40.0, 41.0, None, 43.0)]
    temps = [(T, T + timedelta(hours=6), 30.0), (T + timedelta(hours=6), T + timedelta(days=1), 50.0)]
    rows = tiredays.days(tires, temps, D, D + timedelta(days=2), UTC)
    assert [r["day"] for r in rows] == [D, D + timedelta(days=1), D + timedelta(days=2)]
    assert rows[0]["fl"] == 40.0 and rows[0]["rl"] is None and rows[0]["outside_f"] == pytest.approx(45.0)
    assert rows[2]["fl"] is None and rows[2]["outside_f"] is None


def test_the_runs_query_reads_the_run_in_force_and_those_inside_on_the_index():
    sql = tiredays.runs_sql("cc_tires", "fl")
    assert "first_date < %(a)s ORDER BY first_date DESC LIMIT 1" in sql and "first_date >= %(a)s AND first_date < %(b)s" in sql and "last_date >=" not in sql


class Conn:
    """A connection that answers the fill's three reads and records its writes."""

    def __init__(self, kept, first, tires):
        self.kept, self.first, self.tires, self.wrote = kept, first, tires, []

    def execute(self, q, p=None):
        self.q = q
        return self

    def fetchone(self):
        return {"d": self.kept} if "MAX(day)" in self.q else {"t": self.first}

    def fetchall(self):
        if "cc_tires" in self.q:
            return [{"first_date": a, "last_date": b, "fl": v, "fr": v, "rl": v, "rr": v} for a, b, v in self.tires]
        return []

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def executemany(self, q, rows):
                conn.wrote.extend(rows)

        return Cur()


def test_the_fill_keeps_finished_days_in_batches_and_never_today():
    c = Conn(None, T - timedelta(days=5), [(T - timedelta(days=5), T + timedelta(days=5), 41.0)])
    assert tiredays.fill(c, "V1", UTC, D, batch=3) == 3 and [r[1] for r in c.wrote] == [D - timedelta(days=5), D - timedelta(days=4), D - timedelta(days=3)]
    c = Conn(D - timedelta(days=2), T, [])
    assert tiredays.fill(c, "V1", UTC, D) == 1 and c.wrote[0][1] == D - timedelta(days=1), "up to yesterday"
    assert tiredays.fill(Conn(D - timedelta(days=1), T, []), "V1", UTC, D) == 0 and tiredays.fill(Conn(None, None, []), "V1", UTC, D) == 0
