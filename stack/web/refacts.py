"""Work a car's drive and charge facts out again where they disagree with its readings.

Run as `make refacts FROM=2026-09-04 [VIN=...] [ARGS=--dry-run]`. With --dry-run it prints
what would change and touches nothing. Otherwise the stale facts go (facts.repair_ends)
and the fill the importers run puts them back. A second run finds nothing."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import facts
from history import fill_after_import

TZ = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))


def _n(v) -> str:
    return "-" if v is None or v <= -999 else f"{v:g}"


def lines(name: str, trips: list[dict], charges: list[dict], tz=TZ) -> list[str]:
    """What a run found for one car, a line a session."""
    out = [f"{name}: {len(trips)} drive(s), {len(charges)} charge(s)"]
    for t in trips:
        miles = f"{t['miles']:.1f} mi" if t.get("miles") is not None else "? mi"
        out.append(
            f"  drive {t['id']} {t['start_date'].astimezone(tz):%Y-%m-%d %H:%M} {miles}: level {_n(t['level_start'])}->{_n(t['level_end'])} now {_n(t['now_level_start'])}->{_n(t['now_level_end'])},"
            f" range {_n(t['range_start'])}->{_n(t['range_end'])} now {_n(t['now_range_start'])}->{_n(t['now_range_end'])}"
        )
    for c in charges:
        out.append(f"  charge {c['id']} {c['start_date'].astimezone(tz):%Y-%m-%d %H:%M}: range added {_n(c['range_added'])} now {_n(c['now_range_added'])}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="work stale drive and charge facts out again from a date")
    ap.add_argument("since", help="ISO date or time")
    ap.add_argument("--vin", help="one car (default: every car)")
    ap.add_argument("--dry-run", action="store_true", help="print what would change and touch nothing")
    args = ap.parse_args(argv)
    since = datetime.fromisoformat(args.since)
    if since.tzinfo is None:
        since = since.replace(tzinfo=TZ)
    import psycopg

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        cars = conn.execute("SELECT vin, COALESCE(name, vin) FROM vehicles WHERE %s::text IS NULL OR vin = %s ORDER BY vin", (args.vin, args.vin)).fetchall()
        for vin, name in cars:
            if args.dry_run:
                conn.execute("SET TRANSACTION READ ONLY")
            trips, charges = facts.repair_ends(conn, vin, since, dry_run=args.dry_run)
            if args.dry_run:
                conn.rollback()
            elif trips or charges:
                fill_after_import(conn, vin, TZ)
            print("\n".join(lines(name, trips, charges)))
    print("dry run: nothing changed" if args.dry_run else "done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
