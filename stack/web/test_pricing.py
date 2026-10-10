"""Which price a charge gets: what was paid, the home plan, the fast-charging rate away,
the regional average for a home charge no plan covers, else none. And the privacy rule:
the region comes from the charge session's own position, and nothing on the way to it
reads a drive table."""

import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import cost
from averages import Averages
from facts import charge_facts, fill_charge_facts, fill_charge_regions
from test_rates_data import BERLIN, MID_ATLANTIC, SPRINGFIELD, averages_data

CHI = ZoneInfo("America/Chicago")
SEP = datetime(2026, 9, 12, 2, 0, tzinfo=timezone.utc)  # Sep 11, 9pm in Springfield
JUN = datetime(2026, 6, 12, 2, 0, tzinfo=timezone.utc)
RATES = Averages(averages_data())
HOME = lambda lat, lon: (lat, lon) in (SPRINGFIELD, BERLIN, MID_ATLANTIC)  # noqa: E731
AWAY = lambda lat, lon: False  # noqa: E731


@pytest.fixture(autouse=True)
def no_plan():
    cost.use_plan(None)
    yield
    cost.use_plan(None)


def session(at=SEP, where=SPRINGFIELD, kwh=30.0, kind="AC", **kw):
    return {
        "id": 7,
        "session_start_date": at,
        "session_end_date": at + timedelta(hours=3),
        "session_position_latitude": where[0],
        "session_position_longitude": where[1],
        "real_charged": kwh,
        "charging_type": kind,
        **kw,
    }


def priced(s, is_home=HOME, **kw):
    (row,) = charge_facts([s], [], CHI, is_home, averages=RATES, **kw)
    return dict(zip(("id", "kwh", "cost", "plan", "home", "off", "part", "peak", "split", "source", "region", "month"), row))


def test_a_home_charge_no_plan_covers_is_priced_at_its_regions_average():
    r = priced(session(at=JUN))
    assert r["source"] == "average" and r["region"] == "US-IL" and r["plan"] == "Illinois average" and r["month"] == "2026-06"
    assert r["cost"] == pytest.approx(30.0 * 0.1989) and r["split"] is None and r["off"] is None


def test_a_month_not_published_yet_is_priced_on_the_newest_figure_and_records_which():
    r = priced(session(at=SEP))
    assert r["month"] == "2026-07" and r["cost"] == pytest.approx(30.0 * 0.1922)


def test_berlin_is_priced_in_its_own_currency_from_eurostat():
    r = priced(session(at=SEP, where=BERLIN))
    assert r["region"] == "DE" and r["plan"] == "Germany average" and r["month"] == "2025-12" and r["cost"] == pytest.approx(30.0 * 0.3869)


def test_a_plan_always_beats_an_average():
    cost.use_plan(cost.compile_rows("Springfield EV", [{"from": "2020-01-01", "off_peak": 0.10, "part_peak": 0.20, "peak": 0.30}], []))
    r = priced(session(at=SEP))
    assert r["source"] == "plan" and r["cost"] == pytest.approx(3.0) and r["region"] == "US-IL" and r["month"] is None


def test_charging_away_is_never_priced_at_a_household_average():
    assert priced(session(kind="DC"), is_home=AWAY)["source"] == "none", "a fast charge with no DC rate stays unpriced"
    r = priced(session(kind="DC"), is_home=AWAY, dc_price=0.48)
    assert r["source"] == "dc" and r["cost"] == pytest.approx(30 * 0.48)
    r = priced(session(kind="AC"), is_home=AWAY)
    assert r["source"] == "none" and r["cost"] is None and r["region"] == "US-IL"


def test_paid_beats_everything_and_nowhere_is_unpriced():
    assert priced(session(real_cost=4.2))["source"] == "hand"
    r = priced(session(where=MID_ATLANTIC))
    assert r["source"] == "none" and r["cost"] is None and r["region"] == ""
    (row,) = charge_facts([session()], [], CHI, HOME)  # no averages handed in: nothing to estimate with
    assert row[9] == "none"


DRIVE_TABLES = re.compile(r"\b(trips|drives|drive_\w+|cc_positions|positions|cc_trip_facts|cc_modes|states)\b")


class Recorder:
    """Records every statement; answers the session query with one Springfield charge."""

    def __init__(self):
        self.sql, self.params, self.writes = [], [], []

    def execute(self, sql, args=()):
        self.sql.append(sql)
        self.params.append(args)
        rows = []
        if "FROM charging_sessions c LEFT JOIN cc_charge_facts" in sql:
            s = session()
            rows = [
                {k: s.get(k) for k in ("id", "session_start_date", "session_end_date", "session_position_latitude", "session_position_longitude", "real_charged", "charging_type")}
                | {"real_cost": None}
            ]
        elif "FROM cc_charge_facts f JOIN charging_sessions" in sql:
            rows = [{"session_id": 7, "session_position_latitude": BERLIN[0], "session_position_longitude": BERLIN[1]}]

        class R:
            def fetchall(self):
                return rows

        return R()

    def cursor(self):
        conn = self

        class C:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def executemany(self, sql, rows):
                conn.sql.append(sql)
                conn.writes.append(list(rows))

        return C()


def test_the_region_path_reads_only_charging_tables():
    conn = Recorder()
    assert fill_charge_regions(conn, "ALPHA0000000000001") == 1 and conn.writes[-1] == [("DE", 7)]
    assert fill_charge_facts(conn, "ALPHA0000000000001", CHI, HOME, averages=RATES) == 1
    (row,) = conn.writes[-1]
    assert row[9] == "average" and row[10] == "US-IL" and row[11] == "2026-07"
    for sql in conn.sql:
        assert not DRIVE_TABLES.search(sql), f"a rates query touches a drive table: {sql[:120]}"
    # The unpriced home charges re-read are those whose region now has a figure.
    covered = [p for p in conn.params if isinstance(p, tuple) and len(p) == 5][0][3]
    assert "US-IL" in covered and "DE" in covered


def test_the_split_columns_stay_empty_for_an_average_so_the_time_of_use_page_skips_it():
    from stats import tou_split

    r = priced(session(at=JUN))
    c = {"at_home": True, "session_start_date": JUN, "off_peak_kwh": r["off"], "part_peak_kwh": r["part"], "peak_kwh": r["peak"], "period_kwh": json.loads(r["split"]) if r["split"] else None}
    assert tou_split([c]) == {}
