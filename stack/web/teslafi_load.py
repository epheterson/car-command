"""Load a TeslaFi CSV export (plain or .gz) into Car Command's database.

    python3 teslafi_load.py /tmp/TeslaFiData.csv.gz [--vin 5YJ3...] [--name Alpha] [--dry-run]

Streams the file: a full-history export (2.9M rows, 55 MB gz) never sits in
memory as rows. Idempotent per file: rows this file produced last time are
removed before it loads again (interval tables and sessions by the file's time
window for the vin; positions and software by import id).

Never run this for a vehicle the connector logs live; the two would fight.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import sys
import time
from pathlib import Path

import psycopg

from zoneinfo import ZoneInfo

from history import fill_after_import, write_history
from teslafi_import import USABLE_KWH, iter_samples, replay

TZ = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))


def open_text(path: Path):
    with open(path, "rb") as fh:
        magic = fh.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="")
    return open(path, "r", encoding="utf-8", errors="replace", newline="")


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--vin")
    ap.add_argument("--name")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    path = Path(args.csv)
    t0 = time.monotonic()
    with open_text(path) as fh:
        out = replay(iter_samples(fh))
    if not out["rows"]:
        print("no rows parsed; check the column names against docs/teslafi-fields.md", file=sys.stderr)
        return 2
    veh = out["vehicle"]
    vin = args.vin or veh.get("vin")
    name = args.name or veh.get("name") or vin
    if not vin:
        print("no VIN in the file; pass --vin", file=sys.stderr)
        return 2
    import_id = file_hash(path)
    print(
        f"{path.name}: {out['rows']} rows, {out['first']:%Y-%m-%d} to {out['last']:%Y-%m-%d}, vin {vin} ({name}, {veh.get('model')}), "
        f"{len(out['trips'])} trips, {len(out['charging_sessions'])} charges, {len(out['positions'])} positions, "
        f"{len(out['software'])} software versions, parsed in {time.monotonic() - t0:.0f}s, import {import_id}"
    )
    if args.dry_run:
        return 0
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        counts = write_history(conn, vin, name, "Tesla", veh.get("model"), USABLE_KWH.get(veh.get("model") or ""), out, import_id, path.name, TZ)
        counts["trip_facts"], counts["charge_facts"] = fill_after_import(conn, vin, TZ)
    print("loaded: " + ", ".join(f"{k} {v}" for k, v in counts.items()) + f", total {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
