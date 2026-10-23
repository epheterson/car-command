"""Previous and next: the drive, charge or parked stretch either side of the one on screen,
for the same car (templates/_prevnext.html draws the two arrows).

Every lookup is one row off an index: trips and charging_sessions are unique on (vin,
start_date) and (vin, session_start_date), so "the newest before" and "the oldest after" are
an index step each, whatever the car's history. A parked stretch has no row of its own: it is
the time between the end of one drive or charge and the start of the next, at least
day.MIN_PARKED long (the Day page's rule), named by the epoch second it begins."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from day import MIN_PARKED
from places import place_for

# What every list leaves out by default: rows hidden by hand. app.py passes its own clauses
# (moved_sql, charged_sql) so the arrows step through what the lists show.
DRIVE_WHERE = " AND t.id NOT IN (SELECT ref_id FROM cc_hidden WHERE kind = 'drive')"
CHARGE_WHERE = " AND c.id NOT IN (SELECT ref_id FROM cc_hidden WHERE kind = 'charge')"
STEPS = 25  # parked stretches shorter than MIN_PARKED skipped before giving up

_DRIVE = """SELECT t.id, t.start_date AS start, t.destination_date AS "end", t.start_odometer, t.destination_odometer,
                   t.destination_position_latitude AS lat, t.destination_position_longitude AS lon
            FROM trips t WHERE t.vin = %s AND t.start_date {op} %s{where} ORDER BY t.start_date {order} LIMIT 1"""
_CHARGE = """SELECT c.id, c.session_start_date AS start, c.session_end_date AS "end", c.start_level, c.end_level,
                    c.session_position_latitude AS lat, c.session_position_longitude AS lon
             FROM charging_sessions c LEFT JOIN cc_charge_facts f ON f.session_id = c.id
             WHERE c.vin = %s AND c.session_start_date {op} %s{where} ORDER BY c.session_start_date {order} LIMIT 1"""


def _one(conn, kind: str, vin: str, op: str, at: datetime, where: str) -> dict | None:
    sql = (_DRIVE if kind == "drive" else _CHARGE).format(op=op, order="DESC" if op.startswith("<") else "ASC", where=where)
    r = conn.execute(sql, (vin, at)).fetchone()
    return dict(r, kind=kind) if r else None


def _event(conn, vin: str, op: str, at: datetime, drive_where: str, charge_where: str) -> dict | None:
    """The drive or charge nearest `at` on the `op` side ("<" before, ">=" from), whichever is nearer."""
    d, c = _one(conn, "drive", vin, op, at, drive_where), _one(conn, "charge", vin, op, at, charge_where)
    if not d or not c:
        return d or c
    later = d if d["start"] > c["start"] else c
    return later if op.startswith("<") else (c if later is d else d)


def parked_before(before, at: datetime) -> tuple | None:
    """The parked stretch that ends at or before `at`, as (start, end, the event it followed).
    `before(t)` is the event {start, end} that started last before t. A gap shorter than
    MIN_PARKED, or one that events overlap, is passed over."""
    nxt = before(at)
    for _ in range(STEPS):
        if nxt is None:
            return None
        prev = before(nxt["start"])
        if prev is None or prev["end"] is None:
            return None
        if nxt["start"] - prev["end"] >= MIN_PARKED:
            return prev["end"], nxt["start"], prev
        nxt = prev
    return None


def parked_after(after, at: datetime) -> tuple | None:
    """The parked stretch that starts after `at`, as (start, end, the event it followed); end
    None while the car is still parked there. `after(t)` is the first event {start, end}
    starting at or after t."""
    ev = after(at)
    for _ in range(STEPS):
        if ev is None or ev["end"] is None:
            return None
        nxt = after(ev["end"])
        if nxt is not None and nxt["start"] == ev["start"] and nxt["end"] == ev["end"]:  # a zero-length event finds itself
            nxt = after(ev["end"] + timedelta(microseconds=1))
        if nxt is None:
            return ev["end"], None, ev
        if nxt["start"] - ev["end"] >= MIN_PARKED:
            return ev["end"], nxt["start"], ev
        ev = nxt
    return None


def stretch_at(conn, vin: str, at: datetime, drive_where: str = DRIVE_WHERE, charge_where: str = CHARGE_WHERE) -> tuple | None:
    """The parked stretch that `at` falls in, as (start, end, the event it followed): end None
    while the car is still parked. None when `at` is inside a drive or charge, before the
    first one, or in a gap shorter than MIN_PARKED. Two indexed rows a table."""
    before = _event(conn, vin, "<", at + timedelta(seconds=1), drive_where, charge_where)
    if before is None or before["end"] is None or before["end"] > at:
        return None
    after = _event(conn, vin, ">", at, drive_where, charge_where)
    end = after["start"] if after else None
    if end is not None and end - before["end"] < MIN_PARKED:
        return None
    return before["end"], end, before


def _item(kind: str, r: dict | None, known) -> dict | None:
    if r is None:
        return None
    at = place_for(r.get("lat"), r.get("lon"), known) if known else None
    out = {"kind": kind, "id": r["id"], "start": r["start"], "end": r["end"], "where": at["name"] if at else None}
    if kind == "drive":
        a, b = r.get("start_odometer"), r.get("destination_odometer")
        out["href"], out["miles"] = f"/drives/{r['id']}", (b - a) if a is not None and b is not None else None
    elif kind == "charge":
        a, b = r.get("start_level"), r.get("end_level")
        out["href"], out["gained"] = f"/charges/{r['id']}", (b - a) if a is not None and b is not None else None
    else:
        out["href"] = f"/parked/{r['id']}"
        out["duration"] = (r["end"] - r["start"]) if r["end"] else None
    return out


def neighbours(conn, kind: str, vin: str, ref, known=None, drive_where: str = DRIVE_WHERE, charge_where: str = CHARGE_WHERE) -> dict:
    """{"prev": item | None, "next": item | None} for the drive (ref a trips.id), charge (a
    charging_sessions.id) or parked stretch (ref the epoch second it starts) of car `vin`.
    An item is {kind, id, start, end, href (under the site root), where (a place name from
    `known`, or None)} plus a figure for its label: miles, gained (battery points) or
    duration. A ref that is not this car's gives no neighbours."""
    if kind in ("drive", "charge"):
        table, col = ("trips", "start_date") if kind == "drive" else ("charging_sessions", "session_start_date")
        row = conn.execute(f"SELECT {col} AS start FROM {table} WHERE id = %s AND vin = %s", (ref, vin)).fetchone()
        if not row or row["start"] is None:
            return {"prev": None, "next": None}
        where = drive_where if kind == "drive" else charge_where
        return {
            "prev": _item(kind, _one(conn, kind, vin, "<", row["start"], where), known),
            "next": _item(kind, _one(conn, kind, vin, ">", row["start"], where), known),
        }
    if kind != "parked":
        raise ValueError(f"no neighbours for {kind!r}")
    at = datetime.fromtimestamp(int(ref), tz=timezone.utc)
    before = lambda t: _event(conn, vin, "<", t, drive_where, charge_where)
    after = lambda t: _event(conn, vin, ">=", t, drive_where, charge_where)
    out = {}
    for side, span in (("prev", parked_before(before, at)), ("next", parked_after(after, at))):
        # where the car sat: where the drive before it ended, or the charge's spot
        out[side] = span and _item("parked", {"id": int(span[0].timestamp()), "start": span[0], "end": span[1], "lat": span[2].get("lat"), "lon": span[2].get("lon")}, known)
    return out
