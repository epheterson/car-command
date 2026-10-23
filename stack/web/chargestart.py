"""A connector car's charge the database plugin closed without ever opening.

The plugin opens a session when the car's charging state turns CHARGING, but it looks the
session up newest-start first with a missing start sorting first, so a plug-in that made a
start-less session can leave a real charge with an end, a start level and an end level, and
no start (seen on one car: 23 to 90 %, about 78 kWh). Every page counts a charge from its
start, so that charge was nowhere. This puts the start back from the car's own record: the
first CHARGING state after the cable was plugged in (locked, if it says) and before the end.

A session is filled only when the battery rose at least a point, the car's states say it was
charging, and no other session of the car with a start overlaps it (a fragment of a charge
that is already counted stays as it is). Sessions with no end, or no rise, are left alone.

Run in the background by app.py beside the drive rebuild, and by hand as
`make charge-starts FROM=2026-09-04 [VIN=...] [ARGS=--dry-run]`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LOG = logging.getLogger("chargestart")
TZ = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))
MIN_RISE = 1.0  # percent: less than this is not a charge to count


def plan(sessions: list[dict], charging: list[dict]) -> list[dict]:
    """The starts to fill: [{id, start}]. `sessions` are one car's rows (id, session_start_date,
    session_end_date, plug_connected_date, plug_locked_date, start_level, end_level);
    `charging` its CHARGING state rows (first, last), in time order."""
    started = [(s["session_start_date"], s["session_end_date"]) for s in sessions if s["session_start_date"] is not None]
    out = []
    for s in sessions:
        end, plugged = s["session_end_date"], s["plug_locked_date"] or s["plug_connected_date"]
        if s["session_start_date"] is not None or end is None or plugged is None:
            continue
        if s["start_level"] is None or s["end_level"] is None or s["end_level"] - s["start_level"] < MIN_RISE:
            continue
        first = next((c for c in charging if c["last"] >= plugged and c["first"] < end), None)
        if first is None:
            continue
        start = max(first["first"], plugged)
        if any(a < end and (b is None or b > start) for a, b in started):
            continue  # a fragment of a charge already counted
        out.append({"id": s["id"], "start": start, "end": end, "from": s["start_level"], "to": s["end_level"]})
    return out


def _rows(conn, sql: str, args) -> list[dict]:
    cur = conn.execute(sql, args)
    names = [d.name for d in cur.description]
    return [r if isinstance(r, dict) else dict(zip(names, r)) for r in cur.fetchall()]


def run(conn, vin: str, since: datetime, dry_run: bool = False) -> list[dict]:
    """Fill one car's missing starts since `since`; returns what was (or would be) filled."""
    sessions = _rows(
        conn,
        "SELECT id, session_start_date, session_end_date, plug_connected_date, plug_locked_date, start_level, end_level FROM charging_sessions"
        " WHERE vin = %s AND COALESCE(session_end_date, session_start_date, plug_connected_date) >= %s ORDER BY id",
        (vin, since - timedelta(days=1)),
    )
    if not any(s["session_start_date"] is None and s["session_end_date"] is not None for s in sessions):
        return []
    charging = _rows(
        conn,
        "SELECT first_date AS first, last_date AS last FROM charging_states WHERE vin = %s AND state::text = 'CHARGING' AND last_date >= %s ORDER BY first_date",
        (vin, since - timedelta(days=1)),
    )
    fills = plan(sessions, charging)
    if not dry_run:
        for f in fills:
            conn.execute("UPDATE charging_sessions SET session_start_date = %s WHERE id = %s AND session_start_date IS NULL", (f["start"], f["id"]))
        conn.commit()
    return fills


def line(f: dict, tz=TZ) -> str:
    return f"charge {f['id']}: started {f['start'].astimezone(tz):%Y-%m-%d %H:%M}, to {f['end'].astimezone(tz):%H:%M}, {f['from']:.0f} to {f['to']:.0f} %"


def main(argv: list[str] | None = None) -> int:
    import psycopg

    from driverebuild import connector_cars

    ap = argparse.ArgumentParser(description="fill a connector car's charges that have an end and no start")
    ap.add_argument("since", help="ISO date or time")
    ap.add_argument("--vin", help="one car (default: every connector car)")
    ap.add_argument("--dry-run", action="store_true", help="list what would be filled and write nothing")
    args = ap.parse_args(argv)
    since = datetime.fromisoformat(args.since)
    if since.tzinfo is None:
        since = since.replace(tzinfo=TZ)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        if args.dry_run:
            conn.execute("SET TRANSACTION READ ONLY")
        for vin, name in connector_cars(conn, since, args.vin):
            for f in run(conn, vin, since, dry_run=args.dry_run):
                print(f"{name}: {line(f)}")
        if args.dry_run:
            conn.rollback()
    print("dry run: nothing changed" if args.dry_run else "done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
