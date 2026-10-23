"""Write a replayed history (the dict `teslafi_import.replay` returns) into the
database plugin's tables plus Car Command's own, idempotently per import id.

Shared by the TeslaFi export loader, the TeslaMate loader and the TeslaFi
live poller. Rows this import produced last time are removed first: interval
tables and sessions by the import's time window for the vin, positions and
software by import id. `until` cuts everything at a time, for a car another
logger took over (TeslaMate until Car Command's first row, say).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import averages
import cost
import driveedits
import settings as settings_mod
from facts import ensure_tables, fill_facts
from manual import DDL as MANUAL_DDL
from places import PlaceIndex, is_home as is_home_place, place_for, resolve_merged
from recorder import DDL as POSITIONS_DDL

IMPORT_DDL = """
CREATE TABLE IF NOT EXISTS cc_imports (
    id        VARCHAR PRIMARY KEY,
    vin       VARCHAR NOT NULL,
    filename  VARCHAR NOT NULL,
    rows      INTEGER NOT NULL,
    first_at  TIMESTAMPTZ,
    last_at   TIMESTAMPTZ,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS cc_software (
    id      SERIAL PRIMARY KEY,
    vin     VARCHAR NOT NULL,
    version VARCHAR NOT NULL,
    seen_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS cc_inside_temperatures (
    id         BIGSERIAL PRIMARY KEY,
    vin        VARCHAR NOT NULL,
    first_date TIMESTAMPTZ NOT NULL,
    last_date  TIMESTAMPTZ NOT NULL,
    inside_f   DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS cc_inside_temperatures_vin_first ON cc_inside_temperatures (vin, first_date);
CREATE TABLE IF NOT EXISTS cc_modes (
    id         BIGSERIAL PRIMARY KEY,
    vin        VARCHAR NOT NULL,
    first_date TIMESTAMPTZ NOT NULL,
    last_date  TIMESTAMPTZ NOT NULL,
    sentry     BOOLEAN NOT NULL,
    climate    BOOLEAN NOT NULL
);
CREATE INDEX IF NOT EXISTS cc_modes_vin_first ON cc_modes (vin, first_date);
-- Time the car was on and parked, no drive (driverebuild.car_on): the ignition-on states
-- no drive holds, where the readings say the car stood. Written by the drive rebuild, read by the
-- strips, the Day page and the Timeline.
CREATE TABLE IF NOT EXISTS cc_car_on (
    id         BIGSERIAL PRIMARY KEY,
    vin        VARCHAR NOT NULL,
    first_date TIMESTAMPTZ NOT NULL,
    last_date  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS cc_car_on_vin_first ON cc_car_on (vin, first_date);
CREATE TABLE IF NOT EXISTS cc_charging_electrics (
    id         BIGSERIAL PRIMARY KEY,
    vin        VARCHAR NOT NULL,
    first_date TIMESTAMPTZ NOT NULL,
    last_date  TIMESTAMPTZ NOT NULL,
    volts      DOUBLE PRECISION,
    amps       DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS cc_charging_electrics_vin_first ON cc_charging_electrics (vin, first_date);
CREATE TABLE IF NOT EXISTS cc_trip_extras (
    vin        VARCHAR NOT NULL,
    start_date TIMESTAMPTZ NOT NULL,
    fsd_miles  DOUBLE PRECISION,
    PRIMARY KEY (vin, start_date)
);
-- the energy a drive used, where the source summed it itself (TeslaFi's drive summaries): a
-- summary has no battery line inside the drive to work it out from
ALTER TABLE cc_trip_extras ADD COLUMN IF NOT EXISTS kwh DOUBLE PRECISION;
CREATE TABLE IF NOT EXISTS cc_charge_extras (
    vin        VARCHAR NOT NULL,
    start_date TIMESTAMPTZ NOT NULL,
    network    VARCHAR,
    PRIMARY KEY (vin, start_date)
);
ALTER TABLE cc_software ADD COLUMN IF NOT EXISTS import_id VARCHAR;
"""
INTERVAL_TABLES = {  # table -> (value column, keyed by vin or drive)
    "states": ("state", "vin"),
    "connection_states": ("connection_state", "vin"),
    "charging_states": ("state", "vin"),
    "outside_temperatures": ("outside_temperature", "vin"),
    "charging_powers": ("power", "vin"),
    "drive_levels": ("level", "drive"),
    "drive_ranges": ("range", "drive"),
}
# Every table of stretches a replay writes: (table, key in the replay's result, keyed by
# vin or drive, value columns). Values with two columns travel as a tuple.
RUN_TABLES = [(t, t, key, (col,)) for t, (col, key) in INTERVAL_TABLES.items()] + [
    ("cc_inside_temperatures", "inside_temperatures", "vin", ("inside_f",)),
    ("cc_modes", "modes", "vin", ("sentry", "climate")),
    ("cc_charging_electrics", "charging_electrics", "vin", ("volts", "amps")),
]


def _t(row):
    """A fetched row as a tuple, whether the connection hands back tuples (the loaders) or dicts (the app)."""
    if row is None:
        return None
    return tuple(row.values()) if isinstance(row, dict) else tuple(row)


def copy_rows(conn, table: str, cols: tuple[str, ...], rows) -> int:
    n = 0
    with conn.cursor() as cur, cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as copy:
        for r in rows:
            copy.write_row(r)
            n += 1
    return n


def real_software_changes(conn, vin: str, rows) -> list:
    """The (seen_at, until, version) rows of a replay that are real changes. Each replay window
    reports the version it first sees as an event, so a car that never changed would store one
    row per window. A row stays only if its version differs from the one before it in the batch
    and from the newest stored version at or before it (from any import)."""
    out, prev = [], None
    for row in sorted(rows, key=lambda r: r[0]):
        if row[2] == prev:
            continue
        prev = row[2]
        stored = _t(conn.execute("SELECT version FROM cc_software WHERE vin = %s AND seen_at <= %s ORDER BY seen_at DESC, id DESC LIMIT 1", (vin, row[0])).fetchone())
        if stored is None or stored[0] != row[2]:
            out.append(row)
    return out


def software_dedupe(conn, vin: str | None = None, dry_run: bool = True) -> list[dict]:
    """Rows of cc_software whose version equals the car's previous row (by seen_at, across
    imports): the first of each run stays. Returns them; with dry_run=False also deletes them,
    300 at a time. One-off cleanup, never run at startup."""
    cur = conn.execute(
        """SELECT id, vin, version, seen_at, import_id FROM (
               SELECT id, vin, version, seen_at, import_id, LAG(version) OVER (PARTITION BY vin ORDER BY seen_at, id) AS prev FROM cc_software"""
        + (" WHERE vin = %s" if vin else "")
        + """) x WHERE prev = version ORDER BY vin, seen_at, id""",
        (vin,) if vin else None,
    )
    dupes = [dict(zip(("id", "vin", "version", "seen_at", "import_id"), _t(r))) for r in cur.fetchall()]
    if not dry_run:
        for i in range(0, len(dupes), 300):
            conn.execute("DELETE FROM cc_software WHERE id = ANY(%s)", ([d["id"] for d in dupes[i : i + 300]],))
            conn.commit()
    return dupes


def first_logged(conn, vin: str) -> datetime | None:
    """When another source (the live connector) started writing this vin: the earliest state row that no import produced."""
    r = _t(
        conn.execute(
            """SELECT MIN(s.first_date) FROM states s WHERE s.vin = %s AND NOT EXISTS (
                            SELECT 1 FROM cc_imports i WHERE i.vin = s.vin AND s.first_date BETWEEN i.first_at AND i.last_at)""",
            (vin,),
        ).fetchone()
    )
    return r[0] if r else None


# The filenames the rolling writers give their imports: a live source, not a file someone loaded.
LIVE_IMPORTS = ("tesla stream", "teslafi feed", "teslafi history", "teslamate live")


def live_since(conn, vin: str) -> datetime | None:
    """When any live source began writing this vin: the connector's own rows (first_logged) or the
    first window of a rolling import (the TeslaFi feed, the Tesla stream, TeslaMate live). A file of
    history stops here, so loading one never rewrites what a live source logged."""
    r = _t(conn.execute("SELECT MIN(first_at) FROM cc_imports WHERE vin = %s AND filename = ANY(%s)", (vin, list(LIVE_IMPORTS))).fetchone())
    found = [t for t in (first_logged(conn, vin), r[0] if r else None) if t is not None]
    return min(found) if found else None


def cut(out: dict, until: datetime) -> dict:
    """Drop everything at or after `until` from a replay result; clip intervals that straddle it."""
    o = dict(out)
    for k in list(INTERVAL_TABLES) + ["inside_temperatures", "modes", "charging_electrics"]:
        o[k] = [(a, min(b, until), v) for a, b, v in out.get(k, []) if a < until]
    o["software"] = [(a, min(b, until), v) for a, b, v in out["software"] if a < until]
    o["positions"] = [p for p in out["positions"] if p[0] < until]
    o["trips"] = [t for t in out["trips"] if t["start_date"] < until and t["destination_date"] <= until]
    o["charging_sessions"] = [c for c in out["charging_sessions"] if c["session_start_date"] < until and c["session_end_date"] <= until]
    o["last"] = min(out["last"], until) if out.get("last") else out.get("last")
    return o


def replace_span(prev: tuple, a: datetime, b: datetime, rolling: bool) -> tuple[datetime, datetime, datetime]:
    """What a re-import clears before it writes: (from, to, the import's first date afterwards).

    A file import replaces everything it wrote last time, so its previous span goes.
    A rolling replay (the TeslaFi feed, TeslaMate live) rewrites a window that moves
    forward every hour; clearing its previous span would take rows the new window no
    longer covers, and they would never come back. One car lost every drive, charge and
    position between Sep 4 and Sep 27 2026 that way, an hour at a time, while the feed
    said "ok". So a rolling import clears only from the start of the window it is about
    to write, and its recorded first date stays where the first replay put it.
    """
    pa, pb = prev
    if rolling:
        return a, max(pb, b), min(pa, a)
    return pa, pb, a


def clear_import(conn, vin: str, drive_id, import_id: str, lo: datetime, hi: datetime, rolling: bool = False) -> None:
    """Remove what an import wrote between lo and hi.

    A file import takes the rows that start in its span and everything else under its id.
    A rolling replay takes only rows that touch the window, so a stretch crossing lo is
    rewritten from lo rather than left beside a copy of itself, and only the positions
    and software rows inside the window. Drives and charges added by hand (cc_manual)
    are the user's, not the import's, and stay either way."""
    span = "last_date >= %s AND first_date <= %s" if rolling else "first_date >= %s AND first_date <= %s"
    # The stretch a rolling window opens inside (the car asleep, mostly) began before lo:
    # its head is the import's earlier work, not this window's. It is read here and
    # handed back (keep_heads), or every hourly rebuild would cut a night down to the
    # part after its own start, and a night would be eaten an hour at a time.
    heads = {}
    if rolling:
        for table, key, by, cols in RUN_TABLES:
            rows = conn.execute(
                f"SELECT first_date, last_date, {', '.join(cols)} FROM {table} WHERE {'vin' if by == 'vin' else 'drive_id'} = %s AND first_date < %s AND last_date >= %s ORDER BY first_date",
                (vin if by == "vin" else drive_id, lo, lo),
            ).fetchall()
            heads[key] = [(r[0], r[1], r[2] if len(cols) == 1 else tuple(r[2:])) for r in map(_t, rows)]
    for table, (col, key) in INTERVAL_TABLES.items():
        if key == "vin":
            conn.execute(f"DELETE FROM {table} WHERE vin = %s AND {span}", (vin, lo, hi))
        else:
            conn.execute(f"DELETE FROM {table} WHERE drive_id = %s AND {span}", (drive_id, lo, hi))
    trip_span = "destination_date >= %s AND start_date <= %s" if rolling else "start_date >= %s AND start_date <= %s"
    charge_span = "session_end_date >= %s AND session_start_date <= %s" if rolling else "session_start_date >= %s AND session_start_date <= %s"
    # The ids of the drives and charges removed here go back to the rows that replace
    # them (write_history): a rewrite used to give every one a new id, so an open drive
    # page went 404 within the hour and a tag or a hide keyed by the id lost its drive.
    # Each id's end goes back too: a drive or charge the window first wrote while it was
    # still going comes back longer under the same id, and what was worked out from its
    # old end is not true of the new one (write_history drops it).
    kept = {"trips": {}, "charging_sessions": {}, "heads": heads, "ends": {"trips": {}, "charging_sessions": {}}}
    for i, at, end in map(
        _t,
        conn.execute(
            f"DELETE FROM trips WHERE vin = %s AND {trip_span} AND id NOT IN (SELECT ref_id FROM cc_manual WHERE kind = 'drive') RETURNING id, start_date, destination_date", (vin, lo, hi)
        ).fetchall(),
    ):
        kept["trips"].setdefault(at, i)
        kept["ends"]["trips"][i] = end
    for i, at, end in map(
        _t,
        conn.execute(
            f"DELETE FROM charging_sessions WHERE vin = %s AND {charge_span} AND id NOT IN (SELECT ref_id FROM cc_manual WHERE kind = 'charge') RETURNING id, session_start_date, session_end_date",
            (vin, lo, hi),
        ).fetchall(),
    ):
        kept["charging_sessions"].setdefault(at, i)
        kept["ends"]["charging_sessions"][i] = end
    if rolling:
        conn.execute("DELETE FROM cc_positions WHERE import_id = %s AND at >= %s AND at <= %s", (import_id, lo, hi))
        conn.execute("DELETE FROM cc_software WHERE import_id = %s AND seen_at >= %s AND seen_at <= %s", (import_id, lo, hi))
    else:
        conn.execute("DELETE FROM cc_positions WHERE import_id = %s", (import_id,))
        conn.execute("DELETE FROM cc_software WHERE import_id = %s", (import_id,))
    conn.execute(f"DELETE FROM cc_inside_temperatures WHERE vin = %s AND {span}", (vin, lo, hi))
    conn.execute(f"DELETE FROM cc_modes WHERE vin = %s AND {span}", (vin, lo, hi))
    conn.execute(f"DELETE FROM cc_charging_electrics WHERE vin = %s AND {span}", (vin, lo, hi))
    conn.execute("DELETE FROM cc_trip_extras WHERE vin = %s AND start_date >= %s AND start_date <= %s", (vin, lo, hi))
    conn.execute("DELETE FROM cc_charge_extras WHERE vin = %s AND start_date >= %s AND start_date <= %s", (vin, lo, hi))
    conn.execute("DELETE FROM cc_imports WHERE id = %s", (import_id,))
    return kept


def clear_overlap(conn, vin: str, import_id: str, lo: datetime, hi: datetime) -> None:
    """A file import's span is its own: the positions and software rows another import left
    inside it go too (clear_import takes only this import's), or a wider export loaded over
    a narrower one would hold every reading twice. Rows with no import id are the live
    recorder's and stay."""
    conn.execute("DELETE FROM cc_positions WHERE vin = %s AND at >= %s AND at <= %s AND import_id IS NOT NULL AND import_id <> %s", (vin, lo, hi, import_id))
    conn.execute("DELETE FROM cc_software WHERE vin = %s AND seen_at >= %s AND seen_at <= %s AND import_id IS NOT NULL AND import_id <> %s", (vin, lo, hi, import_id))


def keep_heads(rows, heads) -> list:
    """A replay's stretches for one table with the stretches it opened inside put back.
    A head the replay's first stretch carries on (same value, starting before the head
    ended) becomes one stretch from the head's start; any other head ends where the
    replay's first stretch begins; with nothing new for the table it stays whole."""
    rows = sorted(rows, key=lambda r: r[0])
    out = []
    for a, b, v in heads or ():
        if rows and rows[0][0] <= b and rows[0][2] == v and a < rows[0][0]:
            rows[0] = (a, rows[0][1], v)
        else:
            out.append((a, min(b, rows[0][0]) if rows else b, v))
    return out + rows


def with_kept_ids(rows, kept: dict, at: int) -> tuple[list, list]:
    """Split rows into those that take back the id of the row that started at the same
    moment before a rewrite (the id prepended) and the rest. Each id goes back once."""
    kept = dict(kept)
    reuse, fresh = [], []
    for r in rows:
        i = kept.pop(r[at], None)
        if i is None:
            fresh.append(r)
        else:
            reuse.append((i, *r))
    return reuse, fresh


def moved_ends(reuse: list, ends: dict) -> list:
    """The rows given back their old id (with_kept_ids: id, vin, start, end, ...) whose end
    is not where it was. Their facts were worked out to the old end: a drive the hourly
    rebuild first wrote twenty minutes in kept the battery level of minute twenty for good,
    and one 49-mile drive read 83 to 81 %. The rows go, and the next fill works them out again."""
    return [r for r in reuse if r[0] in ends and ends[r[0]] != r[3]]


def write_history(conn, vin: str, name: str, manufacturer: str, model: str | None, capacity: float | None, out: dict, import_id: str, filename: str, tz: ZoneInfo, rolling: bool = False) -> dict:
    # cc_positions belongs to the recorder, columns and all. An import can run before
    # the recorder ever has, on a database that has only just been created, so its
    # schema is applied here rather than assumed.
    conn.execute(POSITIONS_DDL)
    conn.execute(IMPORT_DDL)
    conn.execute(MANUAL_DDL)
    conn.execute(driveedits.DDL)
    driveedits.lock(conn, vin)  # a drive edit made now waits for this import, and the other way round
    conn.execute("INSERT INTO vehicles (vin, name, manufacturer, model, type) VALUES (%s, %s, %s, %s, 'ELECTRIC') ON CONFLICT (vin) DO NOTHING", (vin, name, manufacturer, model))
    drive = _t(conn.execute("SELECT id FROM drives WHERE vin = %s ORDER BY id LIMIT 1", (vin,)).fetchone())
    if drive is None:
        drive = _t(conn.execute("INSERT INTO drives (vin, drive_id, type, capacity) VALUES (%s, 'primary', 'ELECTRIC', %s) RETURNING id", (vin, capacity)).fetchone())
    drive_id = drive[0]
    if capacity:
        conn.execute("UPDATE drives SET capacity = %s WHERE id = %s AND capacity IS NULL", (capacity, drive_id))
    a, b = out["first"], out["last"]
    # Re-import: remove what this import produced last time.
    prev = _t(conn.execute("SELECT first_at, last_at FROM cc_imports WHERE id = %s", (import_id,)).fetchone())
    first_at = a
    kept = {"trips": {}, "charging_sessions": {}}
    if prev:
        lo, hi, first_at = replace_span(prev, a, b, rolling)
        driveedits.unapply(conn, vin, lo, hi)  # the drives go back as the source had them, and the edits are put on again below
        kept = clear_import(conn, vin, drive_id, import_id, lo, hi, rolling)
        if not rolling:
            clear_overlap(conn, vin, import_id, lo, hi)
    else:
        # a first write owns its span too: a rolling import its window (whatever another
        # source left there, the export's last rows at the same second, goes first), a file
        # its whole range. An export that overlaps one loaded before (a wider range of the
        # same car, a second file of the same months) replaces those rows; left in place
        # they collide with its own on (vin, start_date) and the load fails.
        driveedits.unapply(conn, vin, a, b)
        kept = clear_import(conn, vin, drive_id, import_id, a, b, rolling)
        if not rolling:
            clear_overlap(conn, vin, import_id, a, b)
    counts = {}
    out = {**out, **{k: keep_heads(out.get(k, []), kept.get("heads", {}).get(k)) for _, k, *_ in RUN_TABLES}}
    for table, (col, key) in INTERVAL_TABLES.items():
        rows = out[table]
        if key == "vin":
            counts[table] = copy_rows(conn, table, ("vin", "first_date", "last_date", col), ((vin, x, y, v) for x, y, v in rows))
        else:
            counts[table] = copy_rows(conn, table, ("drive_id", "first_date", "last_date", col), ((drive_id, x, y, v) for x, y, v in rows))
    trip_cols = (
        "vin",
        "start_date",
        "destination_date",
        "start_odometer",
        "destination_odometer",
        "start_position_latitude",
        "start_position_longitude",
        "destination_position_latitude",
        "destination_position_longitude",
    )
    trip_rows = [
        (
            vin,
            t["start_date"],
            t["destination_date"],
            t["start_odometer"],
            t["destination_odometer"],
            t["start_position_latitude"],
            t["start_position_longitude"],
            t["destination_position_latitude"],
            t["destination_position_longitude"],
        )
        for t in out["trips"]
    ]
    reuse, fresh = with_kept_ids(trip_rows, kept["trips"], at=1)
    counts["trips"] = copy_rows(conn, "trips", trip_cols, fresh) + (copy_rows(conn, "trips", ("id", *trip_cols), reuse) if reuse else 0)
    moved = moved_ends(reuse, kept.get("ends", {}).get("trips", {}))
    if moved:
        driveedits.forget(conn, vin, [r[0] for r in moved], [r[2] for r in moved])
    charge_cols = (
        "vin",
        "session_start_date",
        "session_end_date",
        "start_level",
        "end_level",
        "session_position_latitude",
        "session_position_longitude",
        "session_odometer",
        "real_charged",
        "charging_type",
    )
    charge_rows = [
        (
            vin,
            c["session_start_date"],
            c["session_end_date"],
            c["start_level"],
            c["end_level"],
            c["session_position_latitude"],
            c["session_position_longitude"],
            c["session_odometer"],
            c["real_charged"],
            c["charging_type"],
        )
        for c in out["charging_sessions"]
    ]
    reuse, fresh = with_kept_ids(charge_rows, kept["charging_sessions"], at=1)
    counts["charging_sessions"] = copy_rows(conn, "charging_sessions", charge_cols, fresh) + (copy_rows(conn, "charging_sessions", ("id", *charge_cols), reuse) if reuse else 0)
    moved = moved_ends(reuse, kept.get("ends", {}).get("charging_sessions", {}))
    if moved and driveedits._has(conn, "cc_charge_facts"):
        conn.execute("DELETE FROM cc_charge_facts WHERE session_id = ANY(%s)", ([r[0] for r in moved],))
    counts["cc_positions"] = copy_rows(
        conn,
        "cc_positions",
        ("vin", "at", "latitude", "longitude", "heading", "level", "odometer", "speed", "state", "elevation", "power_kw", "import_id"),
        # Trailing fields stay optional so a loader that does not have one (TeslaMate
        # carries no elevation and no power) keeps working without being changed.
        ((vin, *p[:8], (p[8] if len(p) > 8 else None), (p[9] if len(p) > 9 else None), import_id) for p in out["positions"]),
    )
    counts["cc_inside_temperatures"] = copy_rows(conn, "cc_inside_temperatures", ("vin", "first_date", "last_date", "inside_f"), ((vin, a, b, v) for a, b, v in out.get("inside_temperatures", [])))
    counts["cc_modes"] = copy_rows(conn, "cc_modes", ("vin", "first_date", "last_date", "sentry", "climate"), ((vin, a, b, v[0], v[1]) for a, b, v in out.get("modes", [])))
    counts["cc_trip_extras"] = copy_rows(
        conn,
        "cc_trip_extras",
        ("vin", "start_date", "fsd_miles", "kwh"),
        ((vin, t["start_date"], t.get("fsd_miles"), t.get("kwh")) for t in out["trips"] if t.get("fsd_miles") is not None or t.get("kwh") is not None),
    )
    counts["cc_charge_extras"] = copy_rows(
        conn, "cc_charge_extras", ("vin", "start_date", "network"), ((vin, c["session_start_date"], c["network"]) for c in out["charging_sessions"] if c.get("network"))
    )
    counts["cc_charging_electrics"] = copy_rows(
        conn, "cc_charging_electrics", ("vin", "first_date", "last_date", "volts", "amps"), ((vin, a, b, v[0], v[1]) for a, b, v in out.get("charging_electrics", []))
    )
    counts["cc_software"] = copy_rows(conn, "cc_software", ("vin", "version", "seen_at", "import_id"), ((vin, v, x, import_id) for x, _y, v in real_software_changes(conn, vin, out["software"])))
    counts["drive_edits"] = driveedits.reapply(conn, vin)
    conn.execute("INSERT INTO cc_imports (id, vin, filename, rows, first_at, last_at) VALUES (%s, %s, %s, %s, %s, %s)", (import_id, vin, filename, out["rows"], first_at, b))
    conn.commit()
    return counts


def apply_trip_plan(conn, vin: str, actions: list[dict]) -> dict:
    """Write what driverebuild.plan_changes decided: new drives, drives given a new end (and
    start), drives merged away. A drive keeps its id, so its tags, hides and open page stay.
    What was worked out for a drive that changed is dropped for the next fill (driveedits.forget)."""
    counts = {"create": 0, "change": 0, "merge": 0, "remove": 0}
    gone, changed, starts = [], [], []
    for a in actions:
        w = a.get("drive")
        if a["do"] == "create":
            conn.execute(
                "INSERT INTO trips (vin, start_date, destination_date, start_odometer, destination_odometer, start_position_latitude, start_position_longitude,"
                " destination_position_latitude, destination_position_longitude) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (vin, w["start"], w["end"], w["start_odometer"], w["destination_odometer"], w["start_lat"], w["start_lon"], w["end_lat"], w["end_lon"]),
            )
        elif a["do"] == "change":
            new_start = a["start"] == w["start"]  # otherwise the drive keeps the start it has
            conn.execute(
                "UPDATE trips SET start_date = %s, destination_date = %s, destination_odometer = %s, destination_position_latitude = %s, destination_position_longitude = %s"
                + (", start_odometer = %s, start_position_latitude = %s, start_position_longitude = %s" if new_start else "")
                + " WHERE id = %s",
                (a["start"], w["end"], w["destination_odometer"], w["end_lat"], w["end_lon"]) + ((w["start_odometer"], w["start_lat"], w["start_lon"]) if new_start else ()) + (a["id"],),
            )
            changed.append(a["id"])
            starts += [a["was"][0], a["start"]]
        elif a["do"] in ("merge", "remove"):  # a remove is a trip that never moved
            gone.append(a["id"])
            starts.append(a["start"])
        else:
            continue
        counts[a["do"]] += 1
    if gone:
        conn.execute("DELETE FROM trips WHERE id = ANY(%s) AND id NOT IN (SELECT ref_id FROM cc_manual WHERE kind = 'drive')", (gone,))
        if driveedits._has(conn, "cc_drive_tags"):
            conn.execute("DELETE FROM cc_drive_tags WHERE trip_id = ANY(%s)", (gone,))
    driveedits.forget(conn, vin, changed + gone, starts)
    return counts


def apply_car_on(conn, vin: str, since: datetime, now: datetime, stretches: list[dict]) -> bool:
    """Make the car's car-on stretches that began in [since, now) these. Rows already right are
    left alone (the rebuild runs every few minutes; most runs change nothing). True when it wrote."""
    want = sorted((a["start"], a["end"]) for a in stretches)
    have = sorted(_t(r) for r in conn.execute("SELECT first_date, last_date FROM cc_car_on WHERE vin = %s AND first_date >= %s AND first_date < %s", (vin, since, now)).fetchall())
    if want == have:
        return False
    conn.execute("DELETE FROM cc_car_on WHERE vin = %s AND first_date >= %s AND first_date < %s", (vin, since, now))
    for a, b in want:
        conn.execute("INSERT INTO cc_car_on (vin, first_date, last_date) VALUES (%s, %s, %s)", (vin, a, b))
    return True


def fill_after_import(conn, vin: str, tz: ZoneInfo) -> tuple[int, int]:
    # Every column, so an importer run against a site that has not yet added the price
    # column still reads its places (with no prices) rather than failing.
    cur = conn.execute("SELECT * FROM cc_places") if _t(conn.execute("SELECT to_regclass('cc_places')").fetchone())[0] else None
    names = [d.name for d in cur.description] if cur is not None and cur.description else []
    known = PlaceIndex(resolve_merged([dict(zip(names, _t(r))) for r in cur.fetchall()]) if cur is not None else [])
    # The site's own rules for home and for a charge's place, so a charge priced here is
    # priced as the site would price it.
    is_home = lambda lat, lon: is_home_place(place_for(lat, lon, known))  # noqa: E731
    place_of = lambda lat, lon: place_for(lat, lon, known)  # noqa: E731
    # The DDL and the backfill were one transaction, so the ACCESS EXCLUSIVE locks that a
    # CREATE or ALTER takes on the facts tables were held for as long as the backfill ran
    # — and every reader of those tables is the whole site. That only works while the
    # backfill has nothing to do; any real work inside the window blocks the site.
    #
    # A lock_timeout so the DDL gives up instead of queueing, and a commit so it lets go
    # before any work starts. The backfill's own locks are row-level and block nobody.
    conn.execute("SET LOCAL lock_timeout = '3s'")  # LOCAL: ends with the commit, never outlives it on a pooled connection
    ensure_tables(conn)
    conn.commit()
    # An importer run from the command line is its own process, which never read Settings:
    # it would price every home charge on no plan. The site's plan, then.
    st = settings_mod.load(conn)
    cost.use_plan(st.get("rate_plan"))
    # And the averages the site prices unplanned home charges at: its downloaded copy if
    # it has a newer one than this image's.
    try:
        rates = averages.newer(averages.bundled(), averages.stored(conn))
    except Exception:  # noqa: BLE001 - a site that has never started has no table yet
        conn.rollback()
        rates = averages.bundled()
    # The site's fast-charging prices too, or a live poller's fast charges would be
    # looked at with none and the site's tick would have to find them again.
    fast = {k: (float(st[k]) if st.get(k) else None) for k in ("dc_price", "dc_per_min", "sc_price", "sc_per_min")}
    return fill_facts(conn, vin, tz, is_home, averages=rates, place_of=place_of, **fast)
