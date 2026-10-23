"""The car's own meter for a live charge (connector 0.4.0's charge_session_energy), run for real on
a throwaway Postgres when pgserver is installed (skipped otherwise): kept against the session the
database plugin has open, adding up across a meter restart, and a charge's energy and price the
larger of it and the estimate. A session with an export's real_charged, a Tesla's, is untouched."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import billed
import facts

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

NOW = datetime.now(timezone.utc).replace(microsecond=0)
TZ = ZoneInfo("America/Chicago")
SCHEMA = """
CREATE TABLE charging_sessions (id INTEGER PRIMARY KEY, vin VARCHAR, session_start_date TIMESTAMPTZ, session_end_date TIMESTAMPTZ,
    session_position_latitude DOUBLE PRECISION, session_position_longitude DOUBLE PRECISION, real_charged DOUBLE PRECISION,
    charging_type VARCHAR, real_cost DOUBLE PRECISION);
CREATE TABLE charging_powers (vin VARCHAR, first_date TIMESTAMPTZ, last_date TIMESTAMPTZ, power DOUBLE PRECISION);
CREATE TABLE cc_charging_electrics (vin VARCHAR, first_date TIMESTAMPTZ, last_date TIMESTAMPTZ, volts DOUBLE PRECISION, amps DOUBLE PRECISION);
"""


@pytest.fixture
def conn(tmp_path):
    srv = pgserver.get_server(str(tmp_path / "pg"), cleanup_mode="delete")
    with psycopg.connect(srv.get_uri()) as c:
        c.execute(SCHEMA)
        c.execute(facts.DDL[facts.DDL.index("CREATE TABLE IF NOT EXISTS cc_charge_facts") :])
        c.execute(billed.DDL)
        yield c
    srv.cleanup()


def kwh_and_cost(conn, sid):
    return conn.execute("SELECT kwh, cost FROM cc_charge_facts WHERE session_id = %s", (sid,)).fetchone()


def fill(conn, vin):
    return facts.fill_charge_facts(conn, vin, TZ, lambda lat, lon: False, dc_price=0.5)


def test_the_last_reading_is_kept_against_the_open_session_and_the_same_one_writes_nothing(conn):
    conn.execute(
        "INSERT INTO charging_sessions (id, vin, session_start_date, session_end_date) VALUES (1, 'L1', %s, %s), (2, 'L1', %s, NULL)",
        (NOW - timedelta(days=2), NOW - timedelta(days=2) + timedelta(hours=1), NOW - timedelta(minutes=40)),
    )
    assert facts.take_meter(conn, "L1", 5.0, NOW - timedelta(minutes=30)) == 2
    assert facts.take_meter(conn, "L1", 18.42, NOW) == 2
    assert conn.execute("SELECT session_id, kwh FROM cc_charge_meter").fetchall() == [(2, 18.42)]
    before = conn.execute("SELECT at FROM cc_charge_meter").fetchone()
    facts.take_meter(conn, "L1", 18.42, NOW + timedelta(minutes=1))
    assert conn.execute("SELECT at FROM cc_charge_meter").fetchone() == before, "the same reading is not written again"
    assert facts.take_meter(conn, "L2", 3.0, NOW) is None, "no session yet: the next tick tries again"


def test_a_finished_session_never_takes_a_reading(conn):
    """Unplugged and plugged back in within minutes: the old session is closed, the new one not
    started yet. The new charge's first reading must not land on the old one."""
    conn.execute("INSERT INTO charging_sessions (id, vin, session_start_date, session_end_date) VALUES (1, 'L1', %s, %s)", (NOW - timedelta(hours=3), NOW - timedelta(minutes=3)))
    conn.execute("INSERT INTO cc_charge_meter (session_id, kwh, at) VALUES (1, 40.0, %s)", (NOW - timedelta(minutes=4),))
    conn.execute("INSERT INTO cc_charge_facts (session_id, kwh) VALUES (1, 40.0)")
    conn.execute("INSERT INTO charging_sessions (id, vin, session_start_date, session_end_date) VALUES (2, 'L1', NULL, NULL)")
    assert facts.take_meter(conn, "L1", 0.4, NOW) is None, "the new session has no start yet: wait"
    assert conn.execute("SELECT kwh FROM cc_charge_meter WHERE session_id = 1").fetchone() == (40.0,)
    assert kwh_and_cost(conn, 1) == (40.0, None), "the old charge keeps its 40 kWh"
    conn.execute("UPDATE charging_sessions SET session_start_date = %s WHERE id = 2", (NOW - timedelta(seconds=30),))
    assert facts.take_meter(conn, "L1", 0.4, NOW) == 2
    assert kwh_and_cost(conn, 1) == (40.0, None)


def test_a_start_stamped_a_little_ahead_of_now_still_counts(conn):
    conn.execute("INSERT INTO charging_sessions (id, vin, session_start_date, session_end_date) VALUES (3, 'L1', %s, NULL)", (NOW + timedelta(seconds=40),))
    assert facts.take_meter(conn, "L1", 0.3, NOW) == 3
    conn.execute("UPDATE charging_sessions SET session_start_date = %s WHERE id = 3", (NOW + timedelta(minutes=5),))
    assert facts.take_meter(conn, "L1", 0.3, NOW) is None, "more than the skew allowed: not this charge"


def meter(conn, sid):
    return conn.execute("SELECT kwh, last, carried FROM cc_charge_meter WHERE session_id = %s", (sid,)).fetchone()


def test_a_meter_that_restarts_after_a_pause_adds_up_across_it(conn):
    conn.execute("INSERT INTO charging_sessions (id, vin, session_start_date, session_end_date) VALUES (4, 'L1', %s, NULL)", (NOW - timedelta(hours=2),))
    for i, r in enumerate((5.0, 12.0, 20.0, 1.0, 6.0)):
        facts.take_meter(conn, "L1", r, NOW - timedelta(minutes=50 - i))
    assert meter(conn, 4) == (26.0, 6.0, 20.0), "20 before the pause, 6 since"
    facts.take_meter(conn, "L1", 0.5, NOW)
    assert meter(conn, 4) == (26.5, 0.5, 26.0), "a second restart carries the second stretch too"


def test_a_meter_that_keeps_counting_is_taken_as_it_reads(conn):
    conn.execute("INSERT INTO charging_sessions (id, vin, session_start_date, session_end_date) VALUES (4, 'L1', %s, NULL)", (NOW - timedelta(hours=2),))
    for i, r in enumerate((5.0, 12.0, 20.0, 19.98, 25.0)):
        facts.take_meter(conn, "L1", r, NOW - timedelta(minutes=50 - i))
    assert meter(conn, 4) == (25.0, 25.0, 0.0), "a dip of hundredths is rounding, not a restart"


def test_a_resumed_sessions_facts_are_never_dragged_down_by_its_first_reading(conn):
    """The plugin closes the session at the pause (its facts are taken), then reopens it."""
    start = charge(conn, 8, "L1")
    facts.take_meter(conn, "L1", 20.0, start + timedelta(minutes=30))
    close(conn, 8, start)
    fill(conn, "L1")
    assert kwh_and_cost(conn, 8) == (30.0, 15.0), "the estimate, above the meter's 20"
    conn.execute("UPDATE charging_sessions SET session_end_date = NULL WHERE id = 8")
    facts.take_meter(conn, "L1", 0.4, NOW)
    assert kwh_and_cost(conn, 8) == (30.0, 15.0) and meter(conn, 8)[0] == 20.4, "the meter carries on; the facts stand"
    close(conn, 8, start)
    assert fill(conn, "L1") == 0 and kwh_and_cost(conn, 8) == (30.0, 15.0)


def test_a_closed_charge_keeps_the_larger_of_the_meter_and_the_estimate(conn):
    """The meter's last reading may be a poll before the end: it never pulls the estimate down."""
    start = charge(conn, 9, "L1", kw=30.0)
    facts.take_meter(conn, "L1", 29.2, start + timedelta(minutes=57))
    close(conn, 9, start)
    assert fill(conn, "L1") == 1 and kwh_and_cost(conn, 9) == (30.0, 15.0)


def charge(conn, sid, vin, kw=30.0, paid=None):
    """A DC charge three hours ago, an hour long at `kw` (the estimate), still open."""
    start = NOW - timedelta(hours=3)
    conn.execute("INSERT INTO charging_sessions VALUES (%s, %s, %s, NULL, 39.78, -89.65, NULL, 'DC', %s)", (sid, vin, start, paid))
    conn.execute("INSERT INTO charging_powers VALUES (%s, %s, %s, %s)", (vin, start, start + timedelta(hours=1), kw))
    return start


def close(conn, sid, start):
    conn.execute("UPDATE charging_sessions SET session_end_date = %s WHERE id = %s", (start + timedelta(hours=1), sid))


def test_a_charges_energy_and_price_come_from_the_meter_when_it_says_more(conn):
    start = charge(conn, 5, "L1")
    facts.take_meter(conn, "L1", 31.0, start + timedelta(minutes=58))
    close(conn, 5, start)
    assert fill(conn, "L1") == 1 and kwh_and_cost(conn, 5) == (31.0, 15.5)
    assert fill(conn, "L1") == 0, "converges"


def test_a_session_with_an_exports_meter_is_untouched(conn):
    """A Tesla's real_charged, from TeslaFi or TeslaMate: no cc_charge_meter row, the same facts as before."""
    start = NOW - timedelta(days=3)
    conn.execute("INSERT INTO charging_sessions VALUES (7, 'T1', %s, %s, 39.78, -89.65, 40.0, 'DC', NULL)", (start, start + timedelta(hours=1)))
    conn.execute("INSERT INTO charging_powers VALUES ('T1', %s, %s, 30.0)", (start, start + timedelta(hours=1)))
    assert fill(conn, "T1") == 1 and kwh_and_cost(conn, 7) == (40.0, 20.0)
    assert fill(conn, "T1") == 0
