"""Load a TeslaMate dump directory into Car Command.

    python3 teslamate_load.py /tmp/tm --vin 5YJ3E1EA0JF000001 --name Alpha [--until auto|2026-09-04T05:06:29Z] [--dry-run]

`--until auto` cuts at the first row the live connector wrote for this vin,
so the two loggers never overlap. Idempotent per directory.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from history import cut, fill_after_import, first_logged, write_history
from teslamate_import import replay_dir

TZ = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--vin", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--manufacturer", default="Lucid")
    ap.add_argument("--model", default=None)
    ap.add_argument("--capacity", type=float, default=None)
    ap.add_argument("--until", default="auto")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    d = Path(args.dir)
    out = replay_dir(d, args.vin, args.name)
    if not out["rows"]:
        print("no rows", file=sys.stderr)
        return 2
    # one import id per car, not per dump: the source keeps growing, and a re-import must replace the last one
    import_id = f"tm-{args.vin}"
    print(
        f"{d}: {out['rows']} samples, {out['first']:%Y-%m-%d} to {out['last']:%Y-%m-%d}, {len(out['trips'])} trips, "
        f"{len(out['charging_sessions'])} charges, {len(out['positions'])} positions, {len(out['software'])} software versions, import {import_id}"
    )
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        # imports written before the id was per car carry a content hash; adopt them so they get replaced, not duplicated
        for table, col in (("cc_imports", "id"), ("cc_positions", "import_id"), ("cc_software", "import_id")):
            conn.execute(f"UPDATE {table} SET {col} = %s WHERE vin = %s AND {col} LIKE 'tm-%%' AND {col} <> %s", (import_id, args.vin, import_id))
        conn.commit()
        until = None
        if args.until == "auto":
            until = first_logged(conn, args.vin)
        elif args.until:
            until = datetime.fromisoformat(args.until.replace("Z", "+00:00")).astimezone(timezone.utc)
        if until:
            out = cut(out, until)
            print(f"cut at {until:%Y-%m-%d %H:%M:%S%z}: {len(out['trips'])} trips, {len(out['charging_sessions'])} charges, {len(out['positions'])} positions remain")
        if args.dry_run:
            return 0
        counts = write_history(conn, args.vin, args.name, args.manufacturer, args.model, args.capacity, out, import_id, str(d), TZ)
        counts["trip_facts"], counts["charge_facts"] = fill_after_import(conn, args.vin, TZ)
    print("loaded: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
