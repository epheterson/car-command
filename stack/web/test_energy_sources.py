"""The per-charge split, stored beside the charge facts: which charges are picked, what is written, what is left alone."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import cost
import energy

NOT_NULL = {"cc_charge_sources": ("session_id", "status", "approx"), "cc_energy": ("site", "at", "source")}
PT = ZoneInfo("America/Los_Angeles")
START = datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)  # noon Pacific
END = START + timedelta(minutes=30)


class Conn:
    """Answers each query by what it reads from; records the writes."""

    def __init__(self, sessions, powers, rows, hours):
        self.sessions, self.powers, self.rows, self.hours = sessions, powers, rows, hours
        self.asked, self.written, self.deleted = [], [], []

    def execute(self, sql, params=()):
        self.asked.append((sql, params))
        flat = " ".join(sql.split())

        class R:
            def __init__(self, rows):
                self.rows = rows

            def fetchall(self):
                return self.rows

        if flat.startswith("DELETE"):
            self.deleted.append(params)
            return R([])
        if "FROM charging_sessions" in flat:
            return R(self.sessions)
        if "FROM charging_powers" in flat:
            return R(self.powers)
        if "cc_charging_electrics" in flat:
            return R([])
        if "date_trunc('hour'" in flat:
            return R(self.hours)
        if "FROM cc_energy e" in flat:
            return R(self.rows)
        raise AssertionError(flat)

    def cursor(self):
        conn = self

        class C:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def executemany(self, sql, rows):
                rows = list(rows)
                # as Postgres would: a NOT NULL column given None fails the whole batch
                for table, cols in NOT_NULL.items():
                    if f"INSERT INTO {table} " in sql:
                        names = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
                        for row in rows:
                            for name in cols:
                                if name in names:
                                    assert row[names.index(name)] is not None, f"{table}.{name} is NOT NULL but was given None"
                conn.written.append((" ".join(sql.split()), rows))

        return C()


def row(minute, **kw):
    base = {"at": START + timedelta(minutes=minute), "solar_w": 0.0, "home_w": 0.0, "grid_w": 0.0, "battery_w": 0.0, "soe_pct": 50.0, "source": "cloud"}
    return {**base, **kw}


def session(sid=1, kwh=3.6):
    return {"id": sid, "start": START, "end": END, "kwh": kwh}


@pytest.fixture
def plan():
    cost.use_plan(
        cost.compile_rows("Springfield", [{"from": "2026-01-01", "off_peak": 0.10, "part_peak": 0.20, "peak": 0.40}], [{"period": "peak", "days": "all", "start": 0, "end": 24, "months": None}])
    )
    yield
    cost.use_plan(None)


def test_a_midday_charge_is_stored_as_sun_with_its_costs(plan):
    sun = [row(i, solar_w=9000, home_w=8000, grid_w=-1000) for i in range(30)]
    hours = [{"hour": datetime(2026, 7, 10, 12), "from_sun_kwh": 5.0, "in_kwh": 5.0}]
    conn = Conn([session()], [{"first_date": START, "last_date": END, "power": 7.2}], sun, hours)
    assert energy.fill_charge_sources(conn, "5YJ3E1EA0SF000001", PT) == 1
    sql, rows = conn.written[0]
    assert "INSERT INTO cc_charge_sources" in sql and "ON CONFLICT (session_id) DO UPDATE" in sql
    sid, status, solar, battery, grid, frac, grid_cost, battery_cost, approx, coverage = rows[0]
    assert (
        (sid, status) == (1, "ok")
        and solar == pytest.approx(3.6)
        and battery == 0
        and grid == 0
        and frac == 1.0
        and grid_cost == 0
        and battery_cost == 0
        and approx is False
        and coverage == pytest.approx(1.0)
    )


def test_a_night_charge_is_grid_priced_at_the_plan(plan):
    night = [row(i, solar_w=0, home_w=8000, grid_w=8000) for i in range(30)]
    conn = Conn([session()], [{"first_date": START, "last_date": END, "power": 7.2}], night, [])
    energy.fill_charge_sources(conn, "V", PT)
    _sid, status, solar, battery, grid, frac, grid_cost, *_ = conn.written[0][1][0]
    assert status == "ok" and solar == 0 and grid == pytest.approx(3.6) and grid_cost == pytest.approx(3.6 * 0.40) and frac is None


def test_a_charge_without_readings_is_marked_none_so_it_is_looked_at_again_in_a_day():
    conn = Conn([session()], [{"first_date": START, "last_date": END, "power": 7.2}], [], [])
    assert energy.fill_charge_sources(conn, "V", PT) == 1
    _sid, status, *rest = conn.written[0][1][0]
    assert status == "none" and all(v is None for v in rest[:-2]) and rest[-2] is False


def test_the_candidate_query_takes_only_finished_home_charges_that_have_readings_and_no_split():
    conn = Conn([], [], [], [])
    assert energy.fill_charge_sources(conn, "V", PT, batch=17) == 0
    sql = " ".join(conn.asked[0][0].split())
    assert "f.at_home" in sql and "session_end_date < now() - interval '5 minutes'" in sql and "s.session_id IS NULL" in sql and "s.status = 'none'" in sql and "EXISTS (SELECT 1 FROM cc_energy" in sql
    assert "ORDER BY c.session_start_date DESC" in sql and "LIMIT %s" in sql and conn.asked[0][1] == ("V", "home", "home", "home", "home", 17) and "MIN(e0.at)" in sql
    assert conn.written == []


def test_orphans_are_dropped_with_one_statement():
    conn = Conn([], [], [], [])
    energy.drop_orphan_sources(conn)
    assert conn.deleted == [()]


def test_a_none_split_writes_a_false_approx_not_a_null_the_table_refuses():
    conn = Conn([session()], [{"first_date": START, "last_date": END, "power": 7.2}], [], [])
    energy.fill_charge_sources(conn, "V", PT)
    row = conn.written[0][1][0]
    assert row[1] == "none" and row[8] is False, "approx BOOLEAN NOT NULL"
    assert "COALESCE(%s, FALSE)" in conn.written[0][0], "and the SQL says so too"


def test_a_split_is_looked_at_again_when_readings_arrived_for_it_after_it_was_made():
    conn = Conn([], [], [], [])
    energy.fill_charge_sources(conn, "V", PT)
    sql = " ".join(conn.asked[0][0].split())
    assert "e1.updated_at > s.checked_at" in sql and "s.approx" in sql, "only a split with a gap, only when something newer is in its window"
    assert "updated_at" in energy.DDL and "ADD COLUMN IF NOT EXISTS updated_at" in energy.DDL


def test_a_stored_row_notes_when_it_was_written():
    class C:
        def __init__(self):
            self.sql = ""

        def cursor(self):
            outer = self

            class Cur:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return False

                def executemany(self, sql, rows):
                    outer.sql = sql

            return Cur()

    c = C()
    energy.store(c, [{"at": START}])
    assert "updated_at = now()" in c.sql
