"""Drive edits through the real write_history, with its clear_import and with_kept_ids, on the
database plugin's own tables (pgserver, and the plugin installed; skipped without them): a file
import and a rolling replay each clear the drives they rewrite, and the edits come back on
what was written, under the ids the drives had."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import driveedits
import history

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")
sqlalchemy = pytest.importorskip("sqlalchemy")
pytest.importorskip("carconnectivity_plugins.database")

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
M = timedelta(minutes=1)
VIN = "5YJ3E1EA0JF000001"
UTC = ZoneInfo("UTC")


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
    """The plugin's tables on a fresh schema, and a loader's kind of connection (tuples)."""
    sys.path.insert(0, str(Path(__file__).parent.parent / "cc"))
    import init_db

    with psycopg.connect(server.get_uri()) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        try:
            engine = sqlalchemy.create_engine(server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1))
            init_db.plugin_metadata().create_all(engine)
        except Exception as err:  # noqa: BLE001 - no psycopg driver for SQLAlchemy here
            pytest.skip(f"cannot build the plugin's tables: {err}")
        c.execute("CREATE TABLE cc_drive_tags (trip_id INTEGER PRIMARY KEY, tag VARCHAR NOT NULL, note TEXT)")
        c.execute("CREATE TABLE cc_hidden (kind VARCHAR, ref_id INTEGER, PRIMARY KEY (kind, ref_id))")
        c.commit()
        yield c


def trip(start, minutes, odo):
    return {
        "start_date": start,
        "destination_date": start + minutes * M,
        "start_odometer": odo,
        "destination_odometer": odo + minutes / 2,
        "start_position_latitude": 39.0,
        "start_position_longitude": -89.0,
        "destination_position_latitude": 39.1,
        "destination_position_longitude": -89.1,
        "fsd_miles": None,
    }


# a drive a stop split in two, and a long drive that ran on through a stop
TRIPS = [trip(T0, 20, 100.0), trip(T0 + 30 * M, 15, 110.0), trip(T0 + 120 * M, 60, 200.0)]


def replay(lo, hi, trips=TRIPS):
    """What a loader hands write_history for [lo, hi]: the drives touching it, and a reading a minute through each."""
    out = {t: [] for t in history.INTERVAL_TABLES}
    out.update(first=lo, last=hi, rows=1, charging_sessions=[], software=[], inside_temperatures=[], modes=[], charging_electrics=[])
    out["trips"] = [t for t in trips if t["destination_date"] >= lo and t["start_date"] <= hi]
    out["positions"] = [
        (t["start_date"] + i * M, 39.0 + 0.001 * i, -89.0, 0.0, 70.0, t["start_odometer"] + i / 2, 30.0, "Driving")
        for t in out["trips"]
        for i in range(int((t["destination_date"] - t["start_date"]) / M) + 1)
    ]
    return out


def write(conn, lo, hi, rolling, trips=TRIPS):
    history.write_history(conn, VIN, "Alpha", "Tesla", "model3", 75.0, replay(lo, hi, trips), "imp", "test", UTC, rolling=rolling)


def shown(conn):
    rows = conn.execute("SELECT t.id, t.start_date, t.destination_date, t.destination_odometer FROM trips t WHERE TRUE" + driveedits.ABSORBED + " ORDER BY t.start_date").fetchall()
    return [tuple(r) for r in rows]


def edit(conn):
    """Merge the first two drives and cut the long one at its middle reading."""
    first, long = (conn.execute("SELECT id FROM trips WHERE start_date = %s", (t["start_date"],)).fetchone()[0] for t in (TRIPS[0], TRIPS[2]))
    assert merge_next(conn, first, driveedits.ABSORBED) is None
    assert driveedits.split(conn, long, T0 + 150 * M) is None
    conn.commit()


@pytest.mark.parametrize("rolling", [False, True], ids=["a file import", "a rolling replay"])
def test_a_reimport_through_the_real_writer_keeps_a_merge_and_a_split(conn, rolling):
    write(conn, T0 - M, T0 + 300 * M, rolling)
    edit(conn)
    before = shown(conn)
    assert len(before) == 3, "the pair is one drive, the long one is two"
    ids = {r[0] for r in conn.execute("SELECT id FROM trips").fetchall()}
    write(conn, T0 - M, T0 + 300 * M, rolling)
    assert shown(conn) == before
    assert {r[0] for r in conn.execute("SELECT id FROM trips").fetchall()} == ids, "every drive, the cut half too, keeps its id through clear_import and with_kept_ids"
    assert conn.execute("SELECT COUNT(*) FROM cc_drive_edits WHERE applied").fetchone()[0] == 2


def test_a_rolling_window_that_opens_inside_a_cut_drive_does_not_collide_with_it(conn):
    """The window starts after the cut, inside the drive the source still has whole: without lifting the edit first
    the rewritten drive would meet the shortened one on (vin, start_date)."""
    write(conn, T0 - M, T0 + 300 * M, True)
    edit(conn)
    before = shown(conn)
    write(conn, T0 + 170 * M, T0 + 300 * M, True)
    assert shown(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 4


def test_a_rolling_window_far_after_the_edits_leaves_them_alone(conn):
    write(conn, T0 - M, T0 + 300 * M, True)
    edit(conn)
    before = shown(conn)
    write(conn, T0 + 990 * M, T0 + 1100 * M, True, [trip(T0 + 1000 * M, 20, 500.0)])
    assert shown(conn)[:3] == before and len(shown(conn)) == 4
