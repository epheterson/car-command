"""The backfills' SQL run for real, on a throwaway Postgres when pgserver is installed
(skipped otherwise): a drive's highest and lowest ground with GPS spikes left out, and a
charge's peak power, range at 100 % and odometer, each row marked once looked at."""

from datetime import datetime, timedelta, timezone

import pytest

import driveedits
import facts

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
SCHEMA = """
CREATE TABLE trips (id INTEGER PRIMARY KEY, vin VARCHAR, start_date TIMESTAMPTZ, destination_date TIMESTAMPTZ);
CREATE TABLE cc_positions (vin VARCHAR, at TIMESTAMPTZ, elevation DOUBLE PRECISION, odometer DOUBLE PRECISION);
CREATE TABLE drives (id INTEGER PRIMARY KEY, vin VARCHAR);
CREATE TABLE drive_levels (drive_id INTEGER, first_date TIMESTAMPTZ, last_date TIMESTAMPTZ, level DOUBLE PRECISION);
CREATE TABLE drive_ranges (drive_id INTEGER, first_date TIMESTAMPTZ, last_date TIMESTAMPTZ, range DOUBLE PRECISION);
CREATE TABLE drive_ranges_estimated_full (drive_id INTEGER, first_date TIMESTAMPTZ, last_date TIMESTAMPTZ, range_estimated_full DOUBLE PRECISION);
CREATE TABLE charging_sessions (id INTEGER PRIMARY KEY, vin VARCHAR, session_start_date TIMESTAMPTZ, session_end_date TIMESTAMPTZ, session_odometer DOUBLE PRECISION);
CREATE TABLE charging_powers (vin VARCHAR, first_date TIMESTAMPTZ, last_date TIMESTAMPTZ, power DOUBLE PRECISION);
CREATE TABLE cc_charging_electrics (vin VARCHAR, first_date TIMESTAMPTZ, last_date TIMESTAMPTZ, volts DOUBLE PRECISION, amps DOUBLE PRECISION);
"""


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="delete")
    with psycopg.connect(srv.get_uri()) as c:
        c.execute(SCHEMA)
        # the columns only the stale-facts and split-charge tests read, after the ones the inserts give by position
        c.execute("ALTER TABLE trips ADD COLUMN start_odometer DOUBLE PRECISION, ADD COLUMN destination_odometer DOUBLE PRECISION")
        c.execute(
            "ALTER TABLE charging_sessions ADD COLUMN start_level DOUBLE PRECISION, ADD COLUMN end_level DOUBLE PRECISION,"
            " ADD COLUMN plug_connected_date TIMESTAMPTZ, ADD COLUMN plug_disconnected_date TIMESTAMPTZ"
        )
        c.execute("CREATE TABLE cc_trip_routes (vin VARCHAR, start_date TIMESTAMPTZ)")
        c.execute(driveedits.DDL)  # the readings left out by hand, which the backfills skip
        c.execute(facts.DDL.split("CREATE TABLE IF NOT EXISTS cc_vehicle_facts")[0])  # cc_trip_facts and its columns
        c.execute(facts.DDL[facts.DDL.index("CREATE TABLE IF NOT EXISTS cc_charge_facts") : facts.DDL.index("CREATE TABLE IF NOT EXISTS cc_charge_extras")])
        yield c
    srv.cleanup()


def test_elevation_drops_spikes_and_marks_a_drive_with_no_fixes(conn):
    conn.execute("INSERT INTO trips VALUES (1, 'V1', %s, %s), (2, 'V1', %s, %s)", (T0, T0 + timedelta(minutes=10), T0 + timedelta(hours=2), T0 + timedelta(hours=2, minutes=5)))
    conn.execute("INSERT INTO cc_trip_facts (trip_id) VALUES (1), (2)")
    # a climb from 180 to 260 m, one fix 4 km under the sea and one 900 m up in the middle of it
    for i, e in enumerate([180, 200, -4000, 220, 240, 1140, 260]):
        conn.execute("INSERT INTO cc_positions VALUES ('V1', %s, %s, NULL)", (T0 + timedelta(minutes=i), e))
    n = conn.execute(facts.ELEVATION_SQL, ("V1", 300)).rowcount
    got = dict((r[0], (r[1], r[2])) for r in conn.execute("SELECT trip_id, elev_max_m, elev_min_m FROM cc_trip_facts").fetchall())
    assert n == 2 and got[1] == (260.0, 180.0), "the spikes are left out, the last fix beside one kept"
    assert got[2] == (-99999.0, -99999.0), "looked, nothing there"
    assert conn.execute(facts.ELEVATION_SQL, ("V1", 300)).rowcount == 0, "each drive is visited once"


def test_charge_points_wait_for_the_end_and_fall_back_in_order(conn):
    now = datetime.now(timezone.utc)
    conn.execute("INSERT INTO drives VALUES (7, 'V1')")
    conn.execute(
        "INSERT INTO charging_sessions VALUES (10, 'V1', %s, %s, 1200.0), (11, 'V1', %s, %s, NULL), (12, 'V1', %s, %s, NULL)",
        (T0, T0 + timedelta(hours=1), T0 + timedelta(days=1), T0 + timedelta(days=1, hours=1), now - timedelta(minutes=20), now - timedelta(minutes=5)),
    )
    conn.execute("INSERT INTO cc_charge_facts (session_id) VALUES (10), (11), (12)")
    conn.execute("INSERT INTO charging_powers VALUES ('V1', %s, %s, 11.0), ('V1', %s, %s, 7.0)", (T0, T0 + timedelta(minutes=30), T0 + timedelta(minutes=30), T0 + timedelta(hours=1)))
    conn.execute("INSERT INTO cc_charging_electrics VALUES ('V1', %s, %s, 400.0, 250.0)", (T0 + timedelta(days=1), T0 + timedelta(days=1, hours=1)))
    conn.execute("INSERT INTO drive_levels VALUES (7, %s, %s, 80.0), (7, %s, %s, 90.0)", (T0, T0 + timedelta(hours=1), T0 + timedelta(days=1), T0 + timedelta(days=1, hours=1)))
    conn.execute("INSERT INTO drive_ranges VALUES (7, %s, %s, 248.0), (7, %s, %s, 279.0)", (T0, T0 + timedelta(hours=1), T0 + timedelta(days=1), T0 + timedelta(days=1, hours=1)))
    conn.execute("INSERT INTO drive_ranges_estimated_full VALUES (7, %s, %s, 312.0)", (T0 + timedelta(days=1), T0 + timedelta(days=1, hours=1)))
    conn.execute("INSERT INTO cc_positions VALUES ('V1', %s, NULL, 1250.0)", (T0 + timedelta(hours=20),))
    assert conn.execute(facts.CHARGE_POINT_SQL, ("V1", 300)).rowcount == 2, "a charge that ended five minutes ago waits"
    got = {r[0]: r[1:] for r in conn.execute("SELECT session_id, peak_kw, full_range, full_level, odometer FROM cc_charge_facts ORDER BY 1").fetchall()}
    assert got[10] == (11.0, 310.0, 80.0, 1200.0), "power runs; rated range over the level; the session's own odometer"
    assert got[11] == (100.0, 312.0, 90.0, 1250.0), "volts times amps; the car's own estimate; the odometer before it"
    assert got[12] == (None, None, None, None)


def test_ground_facts_prefer_the_model_and_ask_for_the_tiles_a_drive_lacks(conn, tmp_path):
    """Climb and descent on the terrain model for a drive it covers, -1 and a wish for the
    missing square for one it does not, and a second look at the -1 once told to."""
    import struct
    from array import array

    import terrain
    from psycopg.rows import dict_row

    n = 101
    cells = array("h", [round(1000 * i / (n - 1)) for i in range(n) for _ in range(n)])  # 1000 m at the south edge, 0 at the north
    (tmp_path / "N39W090.cct").write_bytes(terrain.MAGIC + struct.pack("<HH", n, 0) + cells.tobytes())
    ter = terrain.Terrain(tmp_path)
    conn.execute("ALTER TABLE cc_positions ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION")
    conn.execute("ALTER TABLE cc_positions ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION")
    base = T0 + timedelta(days=30)
    conn.execute("INSERT INTO trips VALUES (21, 'V2', %s, %s), (22, 'V2', %s, %s)", (base, base + timedelta(minutes=30), base + timedelta(hours=3), base + timedelta(hours=3, minutes=30)))
    conn.execute("INSERT INTO cc_trip_facts (trip_id) VALUES (21), (22)")
    for i in range(31):  # driving south down the square: 600 m of descent
        conn.execute("INSERT INTO cc_positions (vin, at, latitude, longitude, elevation) VALUES ('V2', %s, %s, -89.5, 5)", (base + timedelta(minutes=i), 39.9 - 0.01 * i))
    for i in range(31):  # the next one is a square away
        conn.execute("INSERT INTO cc_positions (vin, at, latitude, longitude, elevation) VALUES ('V2', %s, %s, -89.5, 5)", (base + timedelta(hours=3, minutes=i), 41.5))
    conn.row_factory = dict_row
    try:
        wanted: set = set()
        assert facts.fill_ground(conn, "V2", ter, wanted) == 2
        got = {r["trip_id"]: r for r in conn.execute("SELECT * FROM cc_trip_facts WHERE trip_id IN (21, 22)").fetchall()}
        assert got[21]["ground_climb_m"] == pytest.approx(300, abs=2) and got[21]["ground_descent_m"] == 0, "south is up on this square"
        assert (got[21]["ground_hi_m"], got[21]["ground_lo_m"]) == (pytest.approx(400, abs=1), pytest.approx(100, abs=1))
        assert got[22]["ground_climb_m"] == -1 and got[22]["ground_hi_m"] is None and wanted == {"N41W090"}
        assert facts.fill_ground(conn, "V2", ter, wanted) == 0, "each drive is looked at once"
        assert facts.fill_ground(conn, "V2", terrain.Terrain(None), wanted) == 0, "no model, nothing marked"
        assert facts.reset_ground(conn) == 1
        assert facts.fill_ground(conn, "V2", ter, wanted) == 1
    finally:
        from psycopg.rows import tuple_row

        conn.row_factory = tuple_row


def test_records_and_lists_prefer_the_ground_figures_and_fall_through_for_an_uncovered_drive(conn):
    import re
    from pathlib import Path

    src = (Path(__file__).parent / "app.py").read_text()
    hi = re.search(r"COALESCE\(f\.ground_hi_m, NULLIF\(f\.elev_max_m, -99999\)\) AS elev_max_m", src)
    assert hi and "COALESCE(f.ground_lo_m, NULLIF(f.elev_min_m, -99999)) AS elev_min_m" in src, "the trip query's highest and lowest point"
    conn.execute("INSERT INTO cc_trip_facts (trip_id, elev_max_m, elev_min_m, ground_hi_m, ground_lo_m, ground_climb_m) VALUES (31, 500, 400, 300, 200, 90), (32, 500, 400, NULL, NULL, -1)")
    rows = conn.execute(
        "SELECT trip_id, COALESCE(f.ground_hi_m, NULLIF(f.elev_max_m, -99999)), COALESCE(f.ground_lo_m, NULLIF(f.elev_min_m, -99999)) FROM cc_trip_facts f WHERE trip_id IN (31, 32) ORDER BY 1"
    ).fetchall()
    assert rows == [(31, 300.0, 200.0), (32, 500.0, 400.0)]


def test_a_charge_split_in_two_takes_its_end_from_the_row_that_opened_inside_it(conn):
    h = lambda x: T0 + timedelta(hours=x)  # noqa: E731
    rows = [
        # id, start, end, start level, end level, plugged in, unplugged
        (30, h(1), None, 74.5, None, h(0.5), None),  # the charge, its end missing
        (31, None, h(3), None, 90.0, h(1.8), h(9)),  # the plug reading that took the end
        (32, None, None, None, None, h(20), h(21)),  # plugged in, never charged
        (33, h(30), None, 50.0, None, h(29.9), None),  # charging now
        (34, h(40), None, 40.0, None, h(39), None),  # another split, but its end-only row is still plugged in
        (35, None, h(42), None, 60.0, h(41), None),
        (36, h(50), None, 20.0, None, h(49), None),  # a charge started in between: not this one's end
        (37, h(51), h(52), 30.0, 40.0, h(49), h(53)),
        (38, None, h(54), None, 45.0, h(51.5), h(55)),
    ]
    for r in rows:
        conn.execute(
            "INSERT INTO charging_sessions (id, vin, session_start_date, session_end_date, start_level, end_level, plug_connected_date, plug_disconnected_date) VALUES (%s, 'V5', %s, %s, %s, %s, %s, %s)",
            r,
        )
    assert facts.heal_split_charges(conn, "V5") == 1
    got = {r[0]: r[1:] for r in conn.execute("SELECT id, session_end_date, end_level, plug_disconnected_date FROM charging_sessions WHERE vin = 'V5'").fetchall()}
    assert got[30] == (h(3), 90.0, h(9)), "one charge from 1 to 3, unplugged at 9"
    assert got[31] == (h(3), 90.0, h(9)), "the plugin's row is left as it was"
    assert got[33][0] is None and got[34][0] is None and got[36][0] is None
    assert facts.heal_split_charges(conn, "V5") == 0, "converges"


def test_stale_ends_finds_the_drive_whose_facts_stopped_mid_drive(conn):
    conn.execute("INSERT INTO drives VALUES (9, 'V6')")
    conn.execute(
        "INSERT INTO trips VALUES (41, 'V6', %s, %s, 100.0, 149.0), (42, 'V6', %s, %s, 200.0, 210.0)", (T0, T0 + timedelta(hours=1), T0 + timedelta(hours=5), T0 + timedelta(hours=5, minutes=20))
    )
    conn.execute(
        "INSERT INTO drive_levels VALUES (9, %s, %s, 83.0), (9, %s, %s, 81.0), (9, %s, %s, 63.0)",
        (T0, T0, T0 + timedelta(minutes=20), T0 + timedelta(minutes=20), T0 + timedelta(hours=1), T0 + timedelta(hours=6)),
    )
    conn.execute("INSERT INTO drive_ranges VALUES (9, %s, %s, 229.0), (9, %s, %s, 173.5)", (T0, T0, T0 + timedelta(hours=1), T0 + timedelta(hours=6)))
    # 41 was filled twenty minutes in; 42 is right
    conn.execute("INSERT INTO cc_trip_facts (trip_id, level_start, level_end, range_start, range_end, max_mph) VALUES (41, 83.0, 81.0, 229.0, 223.25, 40.0), (42, 63.0, 63.0, 173.5, 173.5, 30.0)")
    conn.execute("INSERT INTO cc_trip_routes VALUES ('V6', %s)", (T0,))
    trips, charges = facts.stale_ends(conn, "V6", T0 - timedelta(days=1), batch=1)
    assert [(t["id"], t["level_end"], t["now_level_end"], t["now_range_end"]) for t in trips] == [(41, 81.0, 63.0, 173.5)] and charges == []
    facts.repair_ends(conn, "V6", T0 - timedelta(days=1), dry_run=False)
    assert [r[0] for r in conn.execute("SELECT trip_id FROM cc_trip_facts WHERE trip_id IN (41, 42)").fetchall()] == [42], "41's facts go, for the fill to redo"
    assert conn.execute("SELECT count(*) FROM cc_trip_routes WHERE vin = 'V6'").fetchone()[0] == 0, "and its road line"
    assert facts.stale_ends(conn, "V6", T0 - timedelta(days=1)) == ([], []), "nothing left to find"


def test_finished_charge_reads_only_facts_worked_out_after_the_end(conn):
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=1)
    conn.execute("INSERT INTO charging_sessions VALUES (40, 'V9', %s, %s, NULL)", (now - timedelta(hours=2), end))
    conn.execute("INSERT INTO cc_charge_facts (session_id, kwh, cost, computed_at) VALUES (40, 3.1, 0.9, %s)", (end - timedelta(minutes=90),))
    since = now - timedelta(hours=2)
    assert conn.execute(facts.FINISHED_CHARGE_SQL, ("V9", since)).fetchone() is None, "taken partway: the first kWh of it"
    conn.execute("UPDATE cc_charge_facts SET kwh = 61.8, cost = 19.43, computed_at = %s WHERE session_id = 40", (now,))
    assert conn.execute(facts.FINISHED_CHARGE_SQL, ("V9", since)).fetchone() == (61.8, 19.43)
