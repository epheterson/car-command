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
import io
import os
import sys
import time
from pathlib import Path

import psycopg

from zoneinfo import ZoneInfo

from history import cut, fill_after_import, live_since, write_history
from teslafi_import import USABLE_KWH, iter_samples, replay

TZ = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))
PROGRESS_ROWS = 20000


class Counting(io.RawIOBase):
    """A file read from the outside: how many bytes of it have been consumed, for a progress line."""

    def __init__(self, path: Path) -> None:
        self.fh = open(path, "rb")
        self.n = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buf) -> int:
        got = self.fh.readinto(buf)
        self.n += got or 0
        return got or 0

    def close(self) -> None:
        self.fh.close()
        super().close()


def open_text(path: Path, counter: Counting | None = None):
    """The export as text lines, plain or gzip. With a `counter`, the bytes read are counted on it."""
    with open(path, "rb") as fh:
        magic = fh.read(2)
    raw = io.BufferedReader(counter or Counting(path))
    if magic == b"\x1f\x8b":
        return io.TextIOWrapper(gzip.GzipFile(fileobj=raw), encoding="utf-8", errors="replace", newline="")
    return io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def parse(path: Path, progress=None) -> dict:
    """Replay the export in one streaming pass. `progress(bytes_read, bytes_total, newest_sample_time)` is called every PROGRESS_ROWS rows."""
    size = max(path.stat().st_size, 1)
    counter = Counting(path)

    def watched(samples):
        for i, smp in enumerate(samples, 1):
            if progress and i % PROGRESS_ROWS == 0:
                progress(counter.n, size, smp["time"])
            yield smp

    with open_text(path, counter) as fh:
        return replay(watched(iter_samples(fh)), edges=True)


def store(path: Path, out: dict, vin: str, name: str, conn) -> dict:
    """Write a parsed export; the file's hash is its import id, so loading it again replaces what it loaded.
    It stops where a live source began, so the export never rewrites what that source logged."""
    veh = out["vehicle"]
    until = live_since(conn, vin)
    if until:
        out = cut(out, until)
    counts = write_history(conn, vin, name, "Tesla", veh.get("model"), USABLE_KWH.get(veh.get("model") or ""), out, file_hash(path), path.name, TZ)
    counts["trip_facts"], counts["charge_facts"] = fill_after_import(conn, vin, TZ)
    return counts


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--vin")
    ap.add_argument("--name")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    path = Path(args.csv)
    t0 = time.monotonic()
    out = parse(path)
    if not out["rows"]:
        print("no rows parsed; is this a TeslaFi export (a CSV with its header row)?", file=sys.stderr)
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
        counts = store(path, out, vin, name, conn)
    print("loaded: " + ", ".join(f"{k} {v}" for k, v in counts.items()) + f", total {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
