"""Filling a dead zone against a real Postgres (pgserver): the drive is added through history.fill_gaps, marked, never
over a real drive, once only, and a dry run writes nothing."""

from datetime import datetime, timedelta, timezone

import pytest

import history
import nosignal
import recorder

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

T0 = datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc)
M = timedelta(minutes=1)
DAY = timedelta(days=1)
VIN = "ALPHA0000000000001"
SCHEMA = """
CREATE TABLE trips (
    id SERIAL PRIMARY KEY, vin VARCHAR NOT NULL, start_date TIMESTAMPTZ NOT NULL, destination_date TIMESTAMPTZ,
    start_position_latitude DOUBLE PRECISION, start_position_longitude DOUBLE PRECISION, start_location_uid VARCHAR,
    destination_position_latitude DOUBLE PRECISION, destination_position_longitude DOUBLE PRECISION, destination_location_uid VARCHAR,
    start_odometer DOUBLE PRECISION, destination_odometer DOUBLE PRECISION, UNIQUE (vin, start_date));
-- the CarConnectivity database plugin's tables, with the live column lists (the plugin makes these; nothing here is invented)
CREATE TABLE drives (id SERIAL PRIMARY KEY, vin VARCHAR, drive_id VARCHAR, type VARCHAR, capacity DOUBLE PRECISION, capacity_total DOUBLE PRECISION, wltp_range DOUBLE PRECISION);
CREATE TABLE charging_sessions (
    id SERIAL PRIMARY KEY, vin VARCHAR, plug_connected_date TIMESTAMPTZ, plug_locked_date TIMESTAMPTZ, plug_unlocked_date TIMESTAMPTZ,
    session_start_date TIMESTAMPTZ, session_end_date TIMESTAMPTZ, plug_disconnected_date TIMESTAMPTZ, start_level DOUBLE PRECISION, end_level DOUBLE PRECISION,
    session_charge_type VARCHAR, session_position_latitude DOUBLE PRECISION, session_position_longitude DOUBLE PRECISION, session_odometer DOUBLE PRECISION,
    charging_type VARCHAR, location_uid VARCHAR, charging_station_uid VARCHAR, meter_start_kwh DOUBLE PRECISION, meter_end_kwh DOUBLE PRECISION,
    price_per_kwh DOUBLE PRECISION, price_per_min DOUBLE PRECISION, price_per_session DOUBLE PRECISION, real_charged DOUBLE PRECISION, real_cost DOUBLE PRECISION);
CREATE TABLE cc_state (key TEXT PRIMARY KEY, value TEXT);
"""


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="delete")
    yield srv
    srv.cleanup()


@pytest.fixture
def conn(server):
    with psycopg.connect(server.get_uri(), row_factory=dict_row) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        c.execute(SCHEMA)
        c.execute(recorder.DDL)  # cc_positions as the site makes it
        c.execute(history.IMPORT_DDL)  # cc_trip_extras and cc_gap_drives as the site makes them
        c.execute("INSERT INTO drives (vin, capacity) VALUES (%s, 100)", (VIN,))
        yield c


def drive(conn, a, b, odo0, odo1):
    return conn.execute(
        "INSERT INTO trips (vin, start_date, destination_date, start_odometer, destination_odometer, start_position_latitude, start_position_longitude,"
        " destination_position_latitude, destination_position_longitude) VALUES (%s, %s, %s, %s, %s, 39.0, -89.0, 39.1, -89.1) RETURNING id",
        (VIN, T0 + a * M, None if b is None else T0 + b * M, odo0, odo1),
    ).fetchone()["id"]


def lost_day(conn):
    drive(conn, 0, 60, 2380.0, 2395.57)
    drive(conn, 165, 180, 2402.47, 2410.0)
    conn.execute(
        "INSERT INTO cc_positions (vin, at, latitude, longitude, odometer, level) VALUES (%s, %s, 39.1, -89.1, 2395.5, 80), (%s, %s, 39.2, -89.2, 2402.5, 74)", (VIN, T0 + 59 * M, VIN, T0 + 166 * M)
    )


def trips(conn):
    return conn.execute(
        "SELECT t.id, t.start_date, t.destination_date, t.start_odometer, t.destination_odometer, (g.trip_id IS NOT NULL) AS gap FROM trips t LEFT JOIN cc_gap_drives g ON g.trip_id = t.id ORDER BY t.start_date"
    ).fetchall()


def test_the_dead_zone_becomes_one_marked_drive_with_its_energy(conn):
    lost_day(conn)
    assert len(history.fill_gaps(conn, VIN, T0 - DAY)) == 1
    rows = trips(conn)
    assert [r["gap"] for r in rows] == [False, True, False]
    mid = rows[1]
    assert (mid["start_date"], mid["destination_date"], mid["start_odometer"], mid["destination_odometer"]) == (T0 + 60 * M, T0 + 165 * M, 2395.57, 2402.47)
    assert conn.execute("SELECT kwh FROM cc_trip_extras WHERE vin = %s AND start_date = %s", (VIN, T0 + 60 * M)).fetchone()["kwh"] == pytest.approx(6.0)


def test_a_rerun_adds_nothing_and_the_real_drives_are_untouched(conn):
    lost_day(conn)
    before = trips(conn)
    history.fill_gaps(conn, VIN, T0 - DAY)
    assert history.fill_gaps(conn, VIN, T0 - DAY) == []
    rows = trips(conn)
    assert len(rows) == 3 and rows[0] == before[0] and rows[2] == before[1], "the two real drives are as they were"
    assert conn.execute("SELECT COUNT(*) AS n FROM cc_gap_drives").fetchone()["n"] == 1


def test_a_dry_run_reads_only(conn):
    lost_day(conn)
    found = history.fill_gaps(conn, VIN, T0 - DAY, dry_run=True)
    assert len(found) == 1 and len(trips(conn)) == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM cc_gap_drives").fetchone()["n"] == 0


def test_a_car_that_slept_with_no_gap_and_a_small_step_get_nothing(conn):
    drive(conn, 0, 60, 2380.0, 2395.57)
    drive(conn, 600, 620, 2395.57, 2400.0)
    drive(conn, 700, 720, 2400.3, 2405.0)
    assert history.fill_gaps(conn, VIN, T0 - DAY) == [] and len(trips(conn)) == 3


def test_a_stretch_with_a_drive_in_it_is_filled_around_the_drive_never_over_it(conn):
    lost_day(conn)
    real = drive(conn, 100, 110, 2398.0, 2399.0)
    added = history.fill_gaps(conn, VIN, T0 - DAY)
    assert [(g["start"], g["end"]) for g in added] == [(T0 + 60 * M, T0 + 100 * M), (T0 + 110 * M, T0 + 165 * M)]
    assert conn.execute("SELECT destination_odometer FROM trips WHERE id = %s", (real,)).fetchone()["destination_odometer"] == 2399.0
    assert history.fill_gaps(conn, VIN, T0 - DAY) == []


def test_a_charge_in_the_silence_splits_the_filled_drive_and_a_rerun_adds_nothing(conn):
    lost_day(conn)
    conn.execute(
        "INSERT INTO charging_sessions (vin, session_start_date, session_end_date, session_position_latitude, session_position_longitude) VALUES (%s, %s, %s, 39.15, -89.15)",
        (VIN, T0 + 100 * M, T0 + 130 * M),
    )
    assert len(history.fill_gaps(conn, VIN, T0 - DAY)) == 2
    assert [(r["start_date"], r["destination_date"]) for r in trips(conn) if r["gap"]] == [(T0 + 60 * M, T0 + 100 * M), (T0 + 130 * M, T0 + 165 * M)]
    assert history.fill_gaps(conn, VIN, T0 - DAY) == []


def test_a_charge_with_no_place_leaves_a_note_on_the_one_drive(conn):
    lost_day(conn)
    conn.execute("INSERT INTO charging_sessions (vin, session_start_date, session_end_date) VALUES (%s, %s, %s)", (VIN, T0 + 100 * M, T0 + 130 * M))
    assert len(history.fill_gaps(conn, VIN, T0 - DAY)) == 1
    assert "no place" in conn.execute("SELECT note FROM cc_gap_drives").fetchone()["note"]


def test_a_gap_row_whose_trip_a_reimport_replaced_is_dropped(conn):
    lost_day(conn)
    history.fill_gaps(conn, VIN, T0 - DAY)
    conn.execute("DELETE FROM trips WHERE id IN (SELECT trip_id FROM cc_gap_drives)")
    assert len(history.fill_gaps(conn, VIN, T0 - DAY)) == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM cc_gap_drives").fetchone()["n"] == 1


def test_a_slow_long_stretch_is_a_window_that_keeps_its_miles_and_energy(conn):
    drive(conn, 0, 60, 2380.0, 2395.57)
    drive(conn, 400, 420, 2404.66, 2410.0)  # 9.09 mi over six hours
    conn.execute(
        "INSERT INTO cc_positions (vin, at, latitude, longitude, odometer, level) VALUES (%s, %s, 39.1, -89.1, 2395.5, 80), (%s, %s, 39.2, -89.2, 2404.7, 75)", (VIN, T0 + 59 * M, VIN, T0 + 401 * M)
    )
    (g,) = history.fill_gaps(conn, VIN, T0 - DAY)
    assert g["window"] is True and g["miles"] == pytest.approx(9.09) and g["kwh"] == pytest.approx(5.0)
    assert conn.execute("SELECT window_only FROM cc_gap_drives").fetchone()["window_only"] is True
    lost_ok = nosignal.is_window(T0, T0 + 22 * M, 6.9)
    assert lost_ok is False, "6.9 miles in 22 minutes is a drive"


def test_the_automatic_fill_only_takes_silences_that_began_after_it_went_live(conn):
    lost_day(conn)  # the silence began at +60 min, in the past
    assert history.fill_gaps(conn, VIN, T0 - DAY, auto=True) == [], "first run: the floor is set to now, and this silence is older"
    floor = conn.execute("SELECT value FROM cc_state WHERE key = %s", (nosignal.LIVE_KEY,)).fetchone()["value"]
    assert history.fill_gaps(conn, VIN, T0 - DAY, auto=True) == []
    assert conn.execute("SELECT value FROM cc_state WHERE key = %s", (nosignal.LIVE_KEY,)).fetchone()["value"] == floor, "stored once"
    assert len(history.fill_gaps(conn, VIN, T0 - DAY)) == 1, "the hand-run fill still reaches back"


def test_a_dry_automatic_run_writes_no_floor(conn):
    lost_day(conn)
    history.fill_gaps(conn, VIN, T0 - DAY, dry_run=True, auto=True)
    assert conn.execute("SELECT COUNT(*) AS n FROM cc_state").fetchone()["n"] == 0


def test_the_site_ddl_makes_the_marker_table():
    assert "cc_gap_drives" in history.IMPORT_DDL


def test_a_drive_caught_already_moving_starts_where_the_car_was_parked(conn):
    """Parked overnight, the feed woke 0.6 mi into the morning drive at 40 mph: no window drive, the morning
    drive starts from the parked spot 54 s earlier and keeps what it had under its start time."""
    drive(conn, 0, 60, 2380.0, 2395.57)
    morning = drive(conn, 900, 920, 2396.17, 2405.0)
    conn.execute("INSERT INTO cc_positions (vin, at, latitude, longitude, odometer, speed) VALUES (%s, %s, 39.2, -89.2, 2396.17, 40)", (VIN, T0 + 900 * M))
    conn.execute("INSERT INTO cc_trip_extras (vin, start_date, kwh) VALUES (%s, %s, 2.5)", (VIN, T0 + 900 * M))
    (g,) = history.fill_gaps(conn, VIN, T0 - DAY)
    assert g["late_start"] == morning
    rows = trips(conn)
    assert len(rows) == 2 and not any(r["gap"] for r in rows), "no drive of its own"
    start = T0 + 900 * M - timedelta(seconds=54)
    assert (rows[1]["start_date"], rows[1]["start_odometer"]) == (start, 2395.57)
    assert conn.execute("SELECT start_position_latitude FROM trips WHERE id = %s", (morning,)).fetchone()["start_position_latitude"] == 39.1, "the parked spot"
    assert conn.execute("SELECT kwh FROM cc_trip_extras WHERE vin = %s AND start_date = %s", (VIN, start)).fetchone()["kwh"] == 2.5
    assert conn.execute("SELECT old_start, old_odometer FROM cc_late_starts").fetchone() == {"old_start": T0 + 900 * M, "old_odometer": 2396.17}
    assert history.fill_gaps(conn, VIN, T0 - DAY) == [], "a rerun finds nothing"


def test_a_drive_whose_first_reading_is_stopped_or_too_far_is_still_a_window(conn):
    drive(conn, 0, 60, 2380.0, 2395.57)
    drive(conn, 900, 920, 2396.17, 2405.0)
    conn.execute("INSERT INTO cc_positions (vin, at, latitude, longitude, odometer, speed) VALUES (%s, %s, 39.2, -89.2, 2396.17, 0)", (VIN, T0 + 900 * M))
    (g,) = history.fill_gaps(conn, VIN, T0 - DAY, dry_run=True)
    assert g.get("late_start") is None and g["window"] is True, "stopped at its first reading: when it moved is unknown"
    conn.execute("UPDATE cc_positions SET speed = 20")
    conn.execute("UPDATE trips SET start_odometer = 2405.0, destination_odometer = 2410.0 WHERE start_date = %s", (T0 + 900 * M,))
    (g,) = history.fill_gaps(conn, VIN, T0 - DAY, dry_run=True)
    assert g.get("late_start") is None, "9.4 mi at 20 mph is not a missed minute"
