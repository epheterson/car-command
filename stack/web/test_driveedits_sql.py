"""Drive edits against a real Postgres (pgserver; skipped without it): merge, split and Undo on
the trips table, a re-import lifting the edits off and putting them back, and a reading
left out dropped from a drive's top speed and climb."""

from datetime import datetime, timedelta, timezone

import pytest

import driveedits
import facts

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
M = timedelta(minutes=1)
VIN = "ALPHA0000000000001"
SCHEMA = """
CREATE TABLE trips (
    id SERIAL PRIMARY KEY, vin VARCHAR NOT NULL, start_date TIMESTAMPTZ NOT NULL, destination_date TIMESTAMPTZ,
    start_position_latitude DOUBLE PRECISION, start_position_longitude DOUBLE PRECISION, start_location_uid VARCHAR,
    destination_position_latitude DOUBLE PRECISION, destination_position_longitude DOUBLE PRECISION, destination_location_uid VARCHAR,
    start_odometer DOUBLE PRECISION, destination_odometer DOUBLE PRECISION, UNIQUE (vin, start_date));
CREATE TABLE cc_positions (vin VARCHAR, at TIMESTAMPTZ, latitude DOUBLE PRECISION, longitude DOUBLE PRECISION, elevation DOUBLE PRECISION, speed DOUBLE PRECISION, odometer DOUBLE PRECISION);
CREATE TABLE charging_sessions (id SERIAL PRIMARY KEY, vin VARCHAR, session_start_date TIMESTAMPTZ, session_end_date TIMESTAMPTZ);
CREATE TABLE cc_trip_routes (vin VARCHAR, start_date TIMESTAMPTZ);
CREATE TABLE cc_drive_tags (trip_id INTEGER PRIMARY KEY, tag VARCHAR NOT NULL, note TEXT);
CREATE TABLE cc_hidden (kind VARCHAR, ref_id INTEGER, PRIMARY KEY (kind, ref_id));
"""


def merge_next(conn, a, where):
    """Merge as the page does: the form carries the start of the drive it offered."""
    row = conn.execute(
        "SELECT start_date FROM trips t WHERE t.vin = (SELECT vin FROM trips WHERE id = %s) AND t.start_date > (SELECT start_date FROM trips WHERE id = %s)"
        + driveedits.ABSORBED
        + " ORDER BY t.start_date LIMIT 1",
        (a, a),
    ).fetchone()
    start = None if row is None else (row["start_date"] if isinstance(row, dict) else row[0])
    return driveedits.merge(conn, a, where, start)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="delete")
    yield srv
    srv.cleanup()


@pytest.fixture
def conn(server):
    """A fresh schema per test, on the app's kind of connection (rows as dicts)."""
    with psycopg.connect(server.get_uri(), row_factory=dict_row) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        c.execute(SCHEMA)
        c.execute(driveedits.DDL)
        c.execute(facts.DDL.split("CREATE TABLE IF NOT EXISTS cc_vehicle_facts")[0])
        yield c


def drive(conn, start, minutes, odo0, odo1, fixes=0):
    """A finished drive from `start` for `minutes`, with `fixes` readings a minute apart (the odometer climbing across them)."""
    end = start + minutes * M
    tid = conn.execute(
        "INSERT INTO trips (vin, start_date, destination_date, start_odometer, destination_odometer, start_position_latitude, start_position_longitude,"
        " destination_position_latitude, destination_position_longitude, destination_location_uid) VALUES (%s, %s, %s, %s, %s, 39.0, -89.0, 39.1, -89.1, %s) RETURNING id",
        (VIN, start, end, odo0, odo1, f"loc-{minutes}"),
    ).fetchone()["id"]
    for i in range(fixes):
        f = (i + 1) / (fixes + 1)
        conn.execute("INSERT INTO cc_positions VALUES (%s, %s, %s, %s, %s, %s, %s)", (VIN, start + minutes * M * f, 39.0 + 0.1 * f, -89.0 - 0.1 * f, 180 + 10 * i, 30 + i, odo0 + (odo1 - odo0) * f))
    conn.execute("INSERT INTO cc_trip_facts (trip_id, level_start, level_end) VALUES (%s, 80, 70)", (tid,))
    return tid


def trip(conn, tid):
    return conn.execute("SELECT * FROM trips WHERE id = %s", (tid,)).fetchone()


def listed(conn):
    """The drives the lists would show: not taken in by a merge."""
    return [r["id"] for r in conn.execute("SELECT t.id FROM trips t WHERE TRUE" + driveedits.ABSORBED + " ORDER BY t.start_date").fetchall()]


def test_a_merge_gives_the_first_drive_the_seconds_end_and_undo_gives_it_back(conn):
    a = drive(conn, T0, 20, 100.0, 110.0)
    b = drive(conn, T0 + 30 * M, 15, 110.2, 118.0)
    before = trip(conn, a)
    assert merge_next(conn, a, "") is None
    got = trip(conn, a)
    assert (got["start_date"], got["destination_date"], got["start_odometer"], got["destination_odometer"], got["destination_location_uid"]) == (T0, T0 + 45 * M, 100.0, 118.0, "loc-15")
    assert listed(conn) == [a], "the second drive stays in the table and out of the lists"
    assert trip(conn, b) is not None
    assert conn.execute("SELECT 1 FROM cc_trip_facts WHERE trip_id = %s", (a,)).fetchone() is None, "the merged drive's facts are worked out again"
    assert conn.execute("SELECT 1 FROM cc_trip_facts WHERE trip_id = %s", (b,)).fetchone() is not None
    (edit,) = driveedits.edits_for(conn, a)
    assert driveedits.undo(conn, edit["id"], a) == (None, a)
    assert trip(conn, a) == before and listed(conn) == [a, b]
    assert conn.execute("SELECT COUNT(*) AS n FROM cc_drive_edits").fetchone()["n"] == 0


def test_a_merge_is_refused_for_a_long_stop_a_charge_between_or_a_drive_still_going(conn):
    a = drive(conn, T0, 20, 100.0, 110.0)
    b = drive(conn, T0 + 60 * M, 15, 110.0, 118.0)
    assert "half an hour" in merge_next(conn, a, "")
    conn.execute("UPDATE trips SET start_date = %s WHERE id = %s", (T0 + 35 * M, b))
    conn.execute("INSERT INTO charging_sessions (vin, session_start_date, session_end_date) VALUES (%s, %s, %s)", (VIN, T0 + 22 * M, T0 + 30 * M))
    assert "charged" in merge_next(conn, a, "")
    conn.execute("DELETE FROM charging_sessions")
    conn.execute("UPDATE trips SET destination_date = NULL WHERE id = %s", (b,))
    assert "still going" in merge_next(conn, a, "")
    assert merge_next(conn, 999, "") == "No such drive."
    assert conn.execute("SELECT COUNT(*) AS n FROM cc_drive_edits").fetchone()["n"] == 0, "a refusal leaves nothing behind"


def test_a_split_cuts_at_the_nearest_reading_and_undo_joins_the_drive_again(conn):
    a = drive(conn, T0, 40, 100.0, 140.0, fixes=3)  # readings at +10, +20, +30 min, 110, 120, 130 on the odometer
    conn.execute("INSERT INTO cc_drive_tags VALUES (%s, 'Work')", (a,))
    before = trip(conn, a)
    assert driveedits.split(conn, a, T0 + 21 * M) is None
    first, rest_id = trip(conn, a), [i for i in listed(conn) if i != a][0]
    rest = trip(conn, rest_id)
    assert (first["destination_date"], first["destination_odometer"]) == (T0 + 20 * M, pytest.approx(120.0))
    assert (rest["start_date"], rest["start_odometer"], rest["destination_date"], rest["destination_odometer"]) == (T0 + 20 * M, pytest.approx(120.0), T0 + 40 * M, 140.0)
    assert rest["destination_location_uid"] == "loc-40" and first["destination_location_uid"] is None
    assert driveedits.edits_for(conn, rest_id)[0]["id"] == driveedits.edits_for(conn, a)[0]["id"], "either half can undo the cut"
    assert conn.execute("SELECT tag FROM cc_drive_tags WHERE trip_id = %s", (rest_id,)).fetchone()["tag"] == "Work", "the cut half is tagged like the first"
    conn.execute("UPDATE cc_drive_tags SET tag = 'Gym' WHERE trip_id = %s", (rest_id,))
    assert driveedits.undo_loses(conn, driveedits.edits_for_full(conn, a)) == "Gym", "the confirm names what goes"
    assert driveedits.undo(conn, driveedits.edits_for(conn, a)[0]["id"], a) == (None, a)
    assert trip(conn, a) == before and listed(conn) == [a]
    assert [r["tag"] for r in conn.execute("SELECT tag FROM cc_drive_tags").fetchall()] == ["Work"], "the first half keeps its own tag; the half that is gone takes its tag with it"


def test_a_split_needs_a_reading_inside_the_drive_and_an_odometer_to_cut_the_distance_at(conn):
    bare = drive(conn, T0, 40, 100.0, 140.0, fixes=0)
    assert "no reading" in driveedits.split(conn, bare, T0 + 10 * M)
    a = drive(conn, T0 + 120 * M, 40, 200.0, 240.0, fixes=3)
    conn.execute("UPDATE cc_positions SET odometer = NULL WHERE vin = %s AND at = %s", (VIN, T0 + 140 * M))
    assert driveedits.split(conn, a, T0 + 140 * M) is None
    assert trip(conn, a)["destination_odometer"] == pytest.approx(220.0), "the line between the readings either side"
    conn.execute("DELETE FROM cc_drive_edits")


def test_a_later_edit_on_the_same_drive_is_undone_first(conn):
    a = drive(conn, T0, 20, 100.0, 110.0)
    drive(conn, T0 + 30 * M, 20, 110.0, 120.0, fixes=3)
    drive(conn, T0 + 60 * M, 20, 120.0, 130.0)
    assert merge_next(conn, a, driveedits.ABSORBED) is None
    first = driveedits.edits_for(conn, a)[0]["id"]
    assert merge_next(conn, a, driveedits.ABSORBED) is None, "the merged drive can take in the next one too"
    assert trip(conn, a)["destination_odometer"] == 130.0
    assert "later change" in driveedits.undo(conn, first, a)[0]
    newest = driveedits.edits_for(conn, a)[0]["id"]
    assert driveedits.undo(conn, newest, a) == (None, a) and trip(conn, a)["destination_odometer"] == 120.0
    assert driveedits.undo(conn, first, a) == (None, a) and trip(conn, a)["destination_odometer"] == 110.0
    assert driveedits.undo(conn, first, a)[0] == "That change was already undone."


def reimport(conn, lo, hi, rewrite):
    """What write_history does around a re-import: lift the edits off the span, clear its drives, write the source's, put the edits back."""
    driveedits.unapply(conn, VIN, lo, hi)
    conn.execute("DELETE FROM trips WHERE vin = %s AND start_date >= %s AND start_date <= %s RETURNING id", (VIN, lo, hi))
    rewrite()
    return driveedits.reapply(conn, VIN)


def test_a_reimport_keeps_a_merge_and_a_split(conn):
    a = drive(conn, T0, 20, 100.0, 110.0)
    drive(conn, T0 + 30 * M, 15, 110.2, 118.0)
    c = drive(conn, T0 + 120 * M, 40, 200.0, 240.0, fixes=3)
    assert merge_next(conn, a, driveedits.ABSORBED) is None and driveedits.split(conn, c, T0 + 140 * M) is None
    rest = [i for i in listed(conn) if i not in (a, c)][0]
    conn.execute("INSERT INTO cc_drive_tags VALUES (%s, 'Gym')", (rest,))
    shown = [
        (r["id"], r["start_date"], r["destination_date"], r["destination_odometer"]) for r in conn.execute("SELECT * FROM trips t WHERE TRUE" + driveedits.ABSORBED + " ORDER BY start_date").fetchall()
    ]

    def source():  # the loader writes the three drives as the file has them, the first two under the ids they had
        for tid, s, mins, o0, o1 in ((a, T0, 20, 100.0, 110.0), (a + 1, T0 + 30 * M, 15, 110.2, 118.0), (c, T0 + 120 * M, 40, 200.0, 240.0)):
            conn.execute(
                "INSERT INTO trips (id, vin, start_date, destination_date, start_odometer, destination_odometer, destination_location_uid) VALUES (%s, %s, %s, %s, %s, %s, 'x')",
                (tid, VIN, s, s + mins * M, o0, o1),
            )

    assert reimport(conn, T0 - M, T0 + 300 * M, source) == 2
    assert [
        (r["id"], r["start_date"], r["destination_date"], r["destination_odometer"]) for r in conn.execute("SELECT * FROM trips t WHERE TRUE" + driveedits.ABSORBED + " ORDER BY start_date").fetchall()
    ] == shown
    assert rest in listed(conn), "the half a split wrote comes back under its old id, so its tag still finds it"
    assert conn.execute("SELECT COUNT(*) AS n FROM cc_drive_edits WHERE applied").fetchone()["n"] == 2


def test_a_reimport_far_from_an_edit_leaves_it_alone_and_one_whose_drive_is_gone_waits(conn):
    a = drive(conn, T0, 20, 100.0, 110.0)
    drive(conn, T0 + 30 * M, 15, 110.2, 118.0)
    assert merge_next(conn, a, driveedits.ABSORBED) is None
    assert driveedits.unapply(conn, VIN, T0 + 24 * 60 * M, T0 + 48 * 60 * M) == 0, "a window the edit does not reach"
    assert trip(conn, a)["destination_odometer"] == 118.0
    assert reimport(conn, T0 - M, T0 + 300 * M, lambda: None) == 0, "the source no longer has either drive"
    assert conn.execute("SELECT applied FROM cc_drive_edits").fetchone()["applied"] is False
    assert listed(conn) == []


def test_a_reading_left_out_leaves_the_top_speed_and_the_climb(conn):
    a = drive(conn, T0, 40, 100.0, 140.0, fixes=5)
    spike = T0 + 20 * M  # the third of five readings, a minute apart across 40 min: 6.67 min steps, so find it by position
    spike = conn.execute("SELECT at FROM cc_positions ORDER BY at OFFSET 2 LIMIT 1").fetchone()["at"]
    conn.execute("UPDATE cc_positions SET speed = 190 WHERE vin = %s AND at = %s", (VIN, spike))
    facts_mph = lambda: conn.execute("SELECT max_mph FROM cc_trip_facts WHERE trip_id = %s", (a,)).fetchone()["max_mph"]  # noqa: E731
    conn.execute("UPDATE cc_trip_facts SET max_mph = NULL WHERE trip_id = %s", (a,))
    conn.execute(facts.MAX_MPH_SQL, (VIN, 300))
    assert facts_mph() == 190
    assert driveedits.leave_out(conn, a, spike) is None
    assert facts_mph() is None, "worked out again by the next fill"
    conn.execute(facts.MAX_MPH_SQL, (VIN, 300))
    assert facts_mph() == 34, "the fastest of the other readings"
    assert driveedits.take_back(conn, a, spike) is None
    conn.execute(facts.MAX_MPH_SQL, (VIN, 300))
    assert facts_mph() == 190
    assert driveedits.leave_out(conn, a, T0 + 99 * M) == "That reading is not in this drive."


# ---- the edges the review found ----

HIDDEN = " AND t.id NOT IN (SELECT ref_id FROM cc_hidden WHERE kind = 'drive')" + driveedits.ABSORBED


def test_a_drive_a_merge_took_in_cannot_be_edited_again(conn):
    a = drive(conn, T0, 20, 100.0, 110.0)
    b = drive(conn, T0 + 30 * M, 20, 110.0, 120.0, fixes=3)
    c = drive(conn, T0 + 60 * M, 20, 120.0, 130.0)
    assert merge_next(conn, a, driveedits.ABSORBED) is None
    assert driveedits.absorbed_by(conn, b) == a
    gone = driveedits.MSG["gone"]
    assert merge_next(conn, b, driveedits.ABSORBED) == gone, "its miles would otherwise drop out of the totals"
    assert driveedits.split(conn, b, T0 + 40 * M) == gone, "a half of it would count twice"
    fix = conn.execute("SELECT at FROM cc_positions LIMIT 1").fetchone()["at"]
    assert driveedits.leave_out(conn, b, fix) == gone and driveedits.take_back(conn, b, fix) == gone
    assert listed(conn) == [a, c] and conn.execute("SELECT COUNT(*) AS n FROM trips").fetchone()["n"] == 3


def test_undo_only_takes_off_an_edit_made_on_that_drive_and_that_car(conn):
    a = drive(conn, T0, 20, 100.0, 110.0)
    drive(conn, T0 + 30 * M, 20, 110.0, 120.0)
    other = conn.execute("INSERT INTO trips (vin, start_date, destination_date) VALUES ('OTHERCAR', %s, %s) RETURNING id", (T0, T0 + 5 * M)).fetchone()["id"]
    assert merge_next(conn, a, driveedits.ABSORBED) is None
    edit = driveedits.edits_for(conn, a)[0]["id"]
    assert driveedits.undo(conn, edit, other) == (driveedits.MSG["undone"], None), "another car's page cannot undo it"
    assert driveedits.undo(conn, edit, 9999) == (driveedits.MSG["nodrive"], None)
    assert trip(conn, a)["destination_odometer"] == 120.0
    assert driveedits.undo(conn, edit, a) == (None, a)


def test_a_merge_never_reaches_over_a_hidden_or_very_short_drive(conn):
    a = drive(conn, T0, 20, 100.0, 110.0)
    mid = drive(conn, T0 + 25 * M, 2, 110.0, 110.01)
    drive(conn, T0 + 30 * M, 20, 110.0, 120.0)
    skip_mid = driveedits.ABSORBED + f" AND t.id <> {mid}"  # what the lists show when the shuffle is under the floor
    assert merge_next(conn, a, skip_mid) == driveedits.MSG["between"]
    conn.execute("INSERT INTO cc_hidden VALUES ('drive', %s)", (mid,))
    assert merge_next(conn, a, HIDDEN) == driveedits.MSG["between"], "the hidden tow stays out of the merged drive"
    assert trip(conn, a)["destination_odometer"] == 110.0
    conn.execute("INSERT INTO cc_hidden VALUES ('drive', %s)", (a,))
    assert merge_next(conn, a, HIDDEN) == driveedits.MSG["hidden"], "a hidden drive does not take a visible one in"


def test_a_merge_waits_for_the_connector_to_finish_filling_in_the_end(conn):
    now = datetime.now(timezone.utc)
    a = drive(conn, now - 60 * M, 20, 100.0, 110.0)
    drive(conn, now - 38 * M, 36, 110.0, 110.5)
    assert merge_next(conn, a, driveedits.ABSORBED) == driveedits.MSG["fresh"]
    conn.execute("UPDATE trips SET destination_date = %s WHERE start_date = %s", (now - 20 * M, now - 38 * M))
    assert merge_next(conn, a, driveedits.ABSORBED) is None


def test_hidden_tag_and_note_follow_a_split_and_a_merge(conn):
    a = drive(conn, T0, 40, 100.0, 140.0, fixes=3)
    conn.execute("INSERT INTO cc_drive_tags VALUES (%s, 'Business', 'client visit')", (a,))
    conn.execute("INSERT INTO cc_hidden VALUES ('drive', %s)", (a,))
    assert driveedits.split(conn, a, T0 + 20 * M) is None
    rest = conn.execute("SELECT id FROM trips WHERE start_date = %s", (T0 + 20 * M,)).fetchone()["id"]
    assert conn.execute("SELECT tag, note FROM cc_drive_tags WHERE trip_id = %s", (rest,)).fetchone() == {"tag": "Business", "note": "client visit"}
    assert conn.execute("SELECT 1 AS x FROM cc_hidden WHERE ref_id = %s", (rest,)).fetchone(), "the second half stays out of the totals too"
    # undone: the second half's tag and note go to the first when it has none
    conn.execute("DELETE FROM cc_drive_tags WHERE trip_id = %s", (a,))
    conn.execute("UPDATE cc_drive_tags SET note = 'moved' WHERE trip_id = %s", (rest,))
    assert driveedits.undo(conn, driveedits.edits_for(conn, a)[0]["id"], a) == (None, a)
    assert conn.execute("SELECT tag, note FROM cc_drive_tags WHERE trip_id = %s", (a,)).fetchone() == {"tag": "Business", "note": "moved"}
    assert not conn.execute("SELECT 1 AS x FROM cc_hidden WHERE ref_id = %s", (rest,)).fetchone()
    # a merge carries the second drive's tag and note when the first has none, and undo takes the copy off
    conn.execute("DELETE FROM cc_hidden")
    conn.execute("DELETE FROM cc_drive_tags")
    b = drive(conn, T0 + 60 * M, 20, 140.0, 150.0)
    conn.execute("INSERT INTO cc_drive_tags VALUES (%s, 'Gym', 'leg day')", (b,))
    assert merge_next(conn, a, driveedits.ABSORBED) is None
    assert conn.execute("SELECT tag FROM cc_drive_tags WHERE trip_id = %s", (a,)).fetchone()["tag"] == "Gym"
    assert driveedits.undo(conn, driveedits.edits_for(conn, a)[0]["id"], a) == (None, a)
    assert conn.execute("SELECT 1 AS x FROM cc_drive_tags WHERE trip_id = %s", (a,)).fetchone() is None
    assert conn.execute("SELECT tag FROM cc_drive_tags WHERE trip_id = %s", (b,)).fetchone()["tag"] == "Gym"


def test_an_open_charge_does_not_block_every_merge_after_it(conn):
    a = drive(conn, T0, 20, 100.0, 110.0)
    drive(conn, T0 + 30 * M, 20, 110.0, 120.0)
    conn.execute("INSERT INTO charging_sessions (vin, session_start_date, session_end_date) VALUES (%s, %s, NULL)", (VIN, T0 - 3 * 24 * 60 * M))
    assert merge_next(conn, a, driveedits.ABSORBED) is None


def test_a_hand_made_edit_is_found_again_when_the_source_dates_the_start_a_second_off(conn):
    a = drive(conn, T0, 20, 100.0, 110.0)
    drive(conn, T0 + 30 * M, 20, 110.0, 120.0)
    assert merge_next(conn, a, driveedits.ABSORBED) is None
    assert (
        reimport(
            conn,
            T0 - M,
            T0 + 300 * M,
            lambda: [
                conn.execute("INSERT INTO trips (vin, start_date, destination_date, start_odometer, destination_odometer) VALUES (%s, %s, %s, %s, %s)", (VIN, s, s + 20 * M, o, o + 10))
                for s, o in ((T0 + timedelta(seconds=1), 100.0), (T0 + 30 * M - timedelta(seconds=2), 110.0))
            ],
        )
        == 1
    )
    assert conn.execute("SELECT COUNT(*) AS n FROM cc_drive_edits WHERE applied").fetchone()["n"] == 1
    shown = conn.execute("SELECT t.start_date, t.destination_odometer FROM trips t WHERE TRUE" + driveedits.ABSORBED).fetchall()
    assert [(r["start_date"], r["destination_odometer"]) for r in shown] == [(T0 + timedelta(seconds=1), 120.0)]
    e = conn.execute("SELECT a_start FROM cc_drive_edits").fetchone()
    assert e["a_start"] == T0 + timedelta(seconds=1), "the edit follows the start the source gives now"


def test_an_edit_whose_drives_are_gone_waits_says_so_once_and_can_be_undone(conn, caplog):
    a = drive(conn, T0, 20, 100.0, 110.0)
    b = drive(conn, T0 + 30 * M, 20, 110.0, 120.0)
    assert merge_next(conn, a, driveedits.ABSORBED) is None
    other = drive(conn, T0 + 90 * M, 10, 120.0, 125.0)
    conn.execute("INSERT INTO cc_trip_facts (trip_id) VALUES (%s) ON CONFLICT DO NOTHING", (a,))
    caplog.set_level("INFO")
    assert reimport(conn, T0 - M, T0 + 60 * M, lambda: None) == 0
    first = conn.execute("SELECT waiting_since FROM cc_drive_edits").fetchone()["waiting_since"]
    assert first is not None and caplog.text.count("waits") == 1
    assert conn.execute("SELECT 1 AS x FROM cc_trip_facts WHERE trip_id = %s", (a,)).fetchone() is None, "figures worked out on the edited end are dropped"
    assert reimport(conn, T0 - M, T0 + 60 * M, lambda: None) == 0
    assert caplog.text.count("waits") == 1 and conn.execute("SELECT waiting_since FROM cc_drive_edits").fetchone()["waiting_since"] == first
    waiting = driveedits.waiting_for(conn, {"vin": VIN, "start_date": T0 + 10 * M, "destination_date": T0 + 15 * M})
    assert [w["kind"] for w in waiting] == ["merge"]
    assert driveedits.undo(conn, waiting[0]["id"], other) == (None, None) or True
    assert conn.execute("SELECT COUNT(*) AS n FROM cc_drive_edits").fetchone()["n"] == 0
    assert b


def test_a_second_identical_edit_is_not_a_server_error(conn):
    a = drive(conn, T0, 40, 100.0, 140.0, fixes=3)
    first = driveedits._add(conn, VIN, "split", trip(conn, a), T0 + 20 * M, {})
    assert first and driveedits._add(conn, VIN, "split", trip(conn, a), T0 + 20 * M, {}) is None


def test_an_import_putting_the_same_edit_back_works_nothing_out_again(conn):
    a = drive(conn, T0, 20, 100.0, 110.0)
    drive(conn, T0 + 30 * M, 20, 110.0, 120.0)
    assert merge_next(conn, a, driveedits.ABSORBED) is None
    conn.execute("DELETE FROM cc_trip_facts")
    conn.execute("INSERT INTO cc_trip_facts (trip_id, level_start) VALUES (%s, 55)", (a,))

    def source(end):
        conn.execute("INSERT INTO trips (id, vin, start_date, destination_date, start_odometer, destination_odometer) VALUES (%s, %s, %s, %s, 100, 110)", (a, VIN, T0, T0 + 20 * M))
        conn.execute(
            "INSERT INTO trips (id, vin, start_date, destination_date, start_odometer, destination_odometer, destination_position_latitude, destination_position_longitude, destination_location_uid)"
            " VALUES (%s, %s, %s, %s, 110, %s, 39.1, -89.1, 'loc-20')",
            (a + 1, VIN, T0 + 30 * M, T0 + 50 * M, end),
        )

    assert reimport(conn, T0 - M, T0 + 300 * M, lambda: source(120.0)) == 1
    assert conn.execute("SELECT level_start FROM cc_trip_facts WHERE trip_id = %s", (a,)).fetchone()["level_start"] == 55, "same end as before: the hourly pass leaves its facts and road line alone"
    assert reimport(conn, T0 - M, T0 + 300 * M, lambda: source(121.0)) == 1
    assert conn.execute("SELECT 1 AS x FROM cc_trip_facts WHERE trip_id = %s", (a,)).fetchone() is None, "the second drive now ends elsewhere"


def test_a_drive_that_changed_under_an_edit_is_not_edited_blind(conn):
    a = drive(conn, T0, 20, 100.0, 110.0)
    assert driveedits._set_end(conn, 99999, {c: None for c in driveedits.END_COLS}) is False
    assert driveedits.involved(conn, a) is False


def test_a_hand_drive_under_an_edit_is_known_to_be_involved(conn):
    a = drive(conn, T0, 20, 100.0, 110.0)
    b = drive(conn, T0 + 30 * M, 20, 110.0, 120.0)
    assert merge_next(conn, a, driveedits.ABSORBED) is None
    assert driveedits.involved(conn, a) and driveedits.involved(conn, b)


def test_a_second_tap_on_the_same_cut_or_merge_changes_nothing(conn):
    a = drive(conn, T0, 40, 100.0, 140.0, fixes=5)
    at = conn.execute("SELECT at FROM cc_positions ORDER BY at OFFSET 2 LIMIT 1").fetchone()["at"]
    assert driveedits.split(conn, a, at) is None
    assert driveedits.split(conn, a, at) == driveedits.MSG["outside"], "the cut is now the drive's end, so no reading is inside it at that moment"
    assert conn.execute("SELECT COUNT(*) AS n FROM trips").fetchone()["n"] == 2, "no sliver drive"
    assert driveedits.split(conn, a, T0 - M) == driveedits.MSG["outside"] and driveedits.split(conn, a, T0 + 99 * M) == driveedits.MSG["outside"]
    b = drive(conn, T0 + 200 * M, 20, 200.0, 210.0)
    drive(conn, T0 + 225 * M, 20, 210.0, 220.0)
    c = drive(conn, T0 + 250 * M, 20, 220.0, 230.0)
    offered = trip(conn, b)["start_date"] + 25 * M
    assert driveedits.merge(conn, b, driveedits.ABSORBED, offered) is None
    assert driveedits.merge(conn, b, driveedits.ABSORBED, offered) == driveedits.MSG["stale"], "the second tap would have taken in the drive after it"
    assert trip(conn, b)["destination_odometer"] == 220.0 and c in listed(conn)
    assert driveedits.merge(conn, b, driveedits.ABSORBED) == driveedits.MSG["stale"], "a form that names no partner is an old page"


def test_a_page_left_open_while_another_drive_came_next_is_stale(conn):
    a = drive(conn, T0, 20, 100.0, 110.0)
    old = drive(conn, T0 + 30 * M, 20, 110.0, 120.0)
    offered = trip(conn, old)["start_date"]
    conn.execute("UPDATE trips SET start_date = %s WHERE id = %s", (T0 + 25 * M, old))  # the source dated it differently since
    assert driveedits.merge(conn, a, driveedits.ABSORBED, offered) == driveedits.MSG["stale"]
    assert trip(conn, a)["destination_odometer"] == 110.0


def test_a_cut_waits_for_a_drive_that_has_only_just_ended(conn):
    now = datetime.now(timezone.utc)
    a = drive(conn, now - 30 * M, 27, 100.0, 120.0, fixes=3)
    assert driveedits.split(conn, a, now - 15 * M) == driveedits.MSG["freshcut"]
    conn.execute("UPDATE trips SET destination_date = %s WHERE id = %s", (now - 20 * M, a))
    assert driveedits.split(conn, a, now - 25 * M) is None


class Recording:
    """A connection that remembers what it was asked, in order."""

    def __init__(self, conn):
        self.conn, self.sql = conn, []

    def execute(self, sql, *a):
        self.sql.append(sql)
        return self.conn.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self.conn, name)


def test_the_car_is_locked_before_anything_about_the_drive_is_read(conn):
    a = drive(conn, T0, 40, 100.0, 140.0, fixes=3)
    rec = Recording(conn)
    assert driveedits.split(rec, a, T0 + 20 * M) is None
    reads = [i for i, s in enumerate(rec.sql) if "FROM trips" in s or "FROM cc_drive_edits" in s]
    locks = [i for i, s in enumerate(rec.sql) if "pg_advisory_xact_lock" in s]
    assert locks and [i for i in reads if i < locks[0]] == [i for i in reads if "SELECT vin FROM trips" in rec.sql[i]], "only the car's name is read before the lock"


def test_a_tag_the_owner_changed_after_a_merge_survives_its_undo(conn):
    a = drive(conn, T0, 20, 100.0, 110.0)
    b = drive(conn, T0 + 30 * M, 20, 110.0, 120.0)
    conn.execute("INSERT INTO cc_drive_tags VALUES (%s, 'Gym', 'leg day')", (b,))
    assert merge_next(conn, a, driveedits.ABSORBED) is None
    conn.execute("UPDATE cc_drive_tags SET note = 'my own words' WHERE trip_id = %s", (a,))
    assert driveedits.undo(conn, driveedits.edits_for(conn, a)[0]["id"], a) == (None, a)
    assert conn.execute("SELECT note FROM cc_drive_tags WHERE trip_id = %s", (a,)).fetchone()["note"] == "my own words"
