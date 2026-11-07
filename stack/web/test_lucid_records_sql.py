"""Lucid's charge records, the Electrify America plan, drive modes and privacy stretches, run for real on a
throwaway Postgres when pgserver is installed (skipped otherwise): a record matched to a session by car and
start time sets its energy and marks it Electrify America; the plan prices it free; a record or plan that
arrives after the facts were taken puts the charge back to be priced; modes and privacy are read back by span."""

import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import facts
import lucidfields
import lucidrecords

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
SCHEMA = """
CREATE TABLE charging_sessions (id INTEGER PRIMARY KEY, vin VARCHAR, session_start_date TIMESTAMPTZ, session_end_date TIMESTAMPTZ,
    session_position_latitude DOUBLE PRECISION, session_position_longitude DOUBLE PRECISION, real_charged DOUBLE PRECISION,
    charging_type VARCHAR, real_cost DOUBLE PRECISION, plug_disconnected_date TIMESTAMPTZ);
CREATE TABLE charging_powers (vin VARCHAR, first_date TIMESTAMPTZ, last_date TIMESTAMPTZ, power DOUBLE PRECISION);
CREATE TABLE cc_charging_electrics (vin VARCHAR, first_date TIMESTAMPTZ, last_date TIMESTAMPTZ, volts DOUBLE PRECISION, amps DOUBLE PRECISION);
"""
RECORD = {"id": "r1", "start": NOW + timedelta(minutes=3), "stop": NOW + timedelta(minutes=43), "kwh": 31.5, "charger": "Electrify America Springfield"}


@pytest.fixture
def conn(tmp_path):
    import billed

    srv = pgserver.get_server(str(tmp_path / "pg"), cleanup_mode="delete")
    with psycopg.connect(srv.get_uri()) as c:
        c.execute(SCHEMA)
        c.execute(facts.DDL[facts.DDL.index("CREATE TABLE IF NOT EXISTS cc_charge_facts") :])
        c.execute(billed.DDL)
        c.execute(lucidrecords.DDL)
        c.execute("INSERT INTO charging_sessions (id, vin, session_start_date, session_end_date, charging_type) VALUES (1, 'L1', %s, %s, 'DC')", (NOW, NOW + timedelta(minutes=40)))
        c.execute("INSERT INTO charging_powers VALUES ('L1', %s, %s, 40.0)", (NOW, NOW + timedelta(minutes=40)))
        yield c
    srv.cleanup()


def fill(conn):
    return facts.fill_charge_facts(conn, "L1", TZ, lambda la, lo: False, dc_price=0.5)


def row(conn):
    return conn.execute("SELECT kwh, cost, cost_source, plan FROM cc_charge_facts WHERE session_id = 1").fetchone()


def test_a_record_and_a_plan_known_before_the_charge_price_it_free_at_the_records_energy(conn):
    assert lucidrecords.keep_plan(conn, "L1", "Complimentary charging", date(2027, 1, 1), date(2026, 9, 1)) is True
    assert lucidrecords.store(conn, "L1", [RECORD]) == ["r1"]
    fill(conn)
    assert row(conn) == (31.5, 0.0, "place", "Complimentary charging, free at Electrify America")


def test_without_a_plan_the_same_charge_is_the_fast_charging_rate(conn):
    lucidrecords.store(conn, "L1", [RECORD])
    fill(conn)
    assert row(conn)[:3] == (31.5, 15.75, "dc"), "the record still gives the energy"


def test_a_record_that_arrives_after_the_facts_puts_the_charge_back_to_be_priced(conn):
    lucidrecords.keep_plan(conn, "L1", "Complimentary charging", date(2027, 1, 1), date(2026, 9, 1))
    fill(conn)
    assert row(conn)[:3] == (26.666666666666668, 13.333333333333334, "dc")
    changed = lucidrecords.store(conn, "L1", [RECORD])
    ids = lucidrecords.redo_for(conn, changed)
    assert ids == [1] and facts.clear_prices(conn, ids) == 1
    assert row(conn)[0] == 31.5, "the energy is the record's now"
    fill(conn)
    assert row(conn)[:3] == (31.5, 0.0, "place")
    assert lucidrecords.store(conn, "L1", [RECORD]) == [], "the same record again changes nothing"


def test_a_plan_that_appears_later_reprices_the_charges_at_electrify_america_only(conn):
    conn.execute(
        "INSERT INTO charging_sessions (id, vin, session_start_date, session_end_date, charging_type) VALUES (2, 'L1', %s, %s, 'DC')",
        (NOW - timedelta(days=1), NOW - timedelta(days=1) + timedelta(minutes=30)),
    )
    lucidrecords.store(conn, "L1", [RECORD, {**RECORD, "id": "r0", "start": NOW - timedelta(days=1), "charger": "Wallbox"}])
    fill(conn)
    assert lucidrecords.keep_plan(conn, "L1", "Complimentary charging", date(2027, 1, 1), NOW.date() - timedelta(days=30))
    ids = lucidrecords.plan_sessions(conn, "L1")
    assert ids == [1]
    facts.clear_prices(conn, ids)
    fill(conn)
    assert row(conn)[2] == "place" and conn.execute("SELECT cost_source FROM cc_charge_facts WHERE session_id = 2").fetchone() == ("dc",)
    assert lucidrecords.keep_plan(conn, "L1", "Complimentary charging", date(2027, 1, 1), NOW.date()) is False, "the same plan again: nothing to redo"
    assert lucidrecords.keep_plan(conn, "L1", None, None, NOW.date()) is False, "a car that stops saying keeps what it had"
    assert lucidrecords.plan_for(conn, "L1")["since"] == NOW.date() - timedelta(days=30)


def test_the_car_meter_beats_the_record_and_a_wrong_unit_is_not_used(conn):
    conn.execute("UPDATE charging_sessions SET real_charged = 29.0 WHERE id = 1")
    lucidrecords.store(conn, "L1", [RECORD])
    fill(conn)
    assert row(conn)[0] == 29.0
    conn.execute("DELETE FROM cc_charge_facts")
    conn.execute("UPDATE charging_sessions SET real_charged = NULL WHERE id = 1")
    conn.execute("UPDATE cc_lucid_charges SET kwh = 31500")
    fill(conn)
    assert row(conn)[0] == pytest.approx(26.67, abs=0.01), "the power profile stands"


def test_modes_and_privacy_are_read_back_by_span(conn):
    from runs import extend_or_start

    for minutes in range(0, 40):  # the recorder ticks every minute or less
        mode = "comfort" if minutes < 8 or minutes >= 30 else "sport-plus"
        extend_or_start(conn, "cc_drive_modes", "L1", NOW + timedelta(minutes=minutes), ("mode",), (mode,))
        extend_or_start(conn, "cc_privacy", "L1", NOW + timedelta(minutes=minutes), ("private",), (minutes >= 20,))
    assert lucidrecords.modes_between(conn, "L1", NOW + timedelta(minutes=5), NOW + timedelta(minutes=25)) == ["comfort", "sport-plus"]
    assert lucidrecords.modes_between(conn, "L1", NOW - timedelta(hours=3), NOW - timedelta(hours=2)) == []
    assert lucidrecords.private_between(conn, "L1", NOW + timedelta(minutes=22), NOW + timedelta(minutes=24)) is True
    assert lucidrecords.private_between(conn, "L1", NOW, NOW + timedelta(minutes=15)) is False
    assert lucidrecords.private_hours(conn, "L1", NOW, NOW + timedelta(hours=1)) == pytest.approx(19 / 60), "minutes 20 to 39 of the hour"
    assert lucidrecords.private_hours(conn, "L1", NOW - timedelta(hours=5), NOW - timedelta(hours=4)) == 0.0


def test_the_worker_keeps_what_the_held_tree_says_and_asks_nothing_else(conn):
    from contextlib import contextmanager

    def leaf(node, key):
        return node.get(key, {}).get("val") if isinstance(node.get(key), dict) else None

    car = {
        "charging_records": {"val": json.dumps([{"id": "r1", "start": "2026-10-20T15:03:00Z", "stop": "2026-10-20T15:43:00Z", "energy": 31.5, "charger": "Electrify America Springfield"}])},
        "ea_plan_name": {"val": "Complimentary charging"},
        "ea_plan_expires": {"val": "2027-01-01T00:00:00+00:00"},
    }

    @contextmanager
    def connect():
        yield conn

    def read(tree):
        got = lucidfields.read(tree, leaf)
        return lucidfields.records_of(tree, leaf), got["ea_plan"], got["ea_plan_ends"]

    worker = lucidrecords.RecordsWorker(connect=connect, tree=lambda: {"L1": car, "T1": {}}, read=read, clear=facts.clear_prices, today=lambda: date(2026, 9, 1))
    fill(conn)
    assert worker.round() == 1 and row(conn)[2] is None, "the charge is cleared to be priced again"
    fill(conn)
    assert row(conn)[:3] == (31.5, 0.0, "place")
    assert worker.round() == 0, "nothing new: nothing redone"
