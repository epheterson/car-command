"""Every reading, as it arrived, kept for good.

Drives, charges and facts are worked out from readings, and the working-out changes:
a threshold moves, a bug is found, a new figure is wanted for old days. With the raw
readings kept, any of it can be run again over the whole history (`make replay`).
Without them, what was derived wrongly is derived wrongly
for ever — one car's erased weeks came back on 2026-09-28 only because the feed's raw
rows were still there.

Three sources, three stores:
- the TeslaFi feed: `cc_teslafi_rows`, one row a poll, written by `teslafi_live`; the
  30-day trim it used to have is gone;
- the live connector: `cc_raw`, the whole vehicle tree (about 8 KB, 167 leaves) each
  time a value in it changes, written from the recorder's snapshot hook; every leaf
  carries an `upd` stamp that moves on every poll, so change is judged on values alone;
- TeslaMate live: TeslaMate's own database is the raw store; nothing to keep twice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone

DDL = """CREATE TABLE IF NOT EXISTS cc_raw (
    vin    VARCHAR NOT NULL,
    at     TIMESTAMPTZ NOT NULL,
    source VARCHAR NOT NULL,
    data   JSONB NOT NULL,
    PRIMARY KEY (vin, at)
);
"""


def values_only(tree):
    """The tree without its `upd` stamps: what is compared, not what is stored."""
    if isinstance(tree, dict):
        return {k: values_only(v) for k, v in tree.items() if k != "upd"}
    if isinstance(tree, list):
        return [values_only(v) for v in tree]
    return tree


def digest(tree) -> str:
    return hashlib.sha1(json.dumps(values_only(tree), sort_keys=True, default=str).encode()).hexdigest()


class RawKeeper:
    """Stores a car's tree when a value in it changed since the last one stored."""

    def __init__(self, connect, source: str = "connector") -> None:
        self.connect, self.source = connect, source
        self.last: dict[str, str] = {}
        self.ready = False

    def keep(self, vin: str, tree: dict, now: datetime | None = None) -> bool:
        if not tree:
            return False
        d = digest(tree)
        if self.last.get(vin) == d:
            return False
        now = now or datetime.now(timezone.utc)
        with self.connect() as conn:
            if not self.ready:
                conn.execute(DDL)
                self.ready = True
            conn.execute("INSERT INTO cc_raw (vin, at, source, data) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", (vin, now, self.source, json.dumps(tree, default=str)))
            conn.commit()
        self.last[vin] = d
        return True


def state_at(conn, vin: str, at: datetime) -> dict | None:
    """The tree as it stood at `at`: the newest one stored at or before then."""
    row = conn.execute("SELECT data FROM cc_raw WHERE vin = %s AND at <= %s ORDER BY at DESC LIMIT 1", (vin, at)).fetchone()
    if row is None:
        return None
    data = row["data"] if isinstance(row, dict) else row[0]
    return json.loads(data) if isinstance(data, str) else data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="print a car's raw connector tree as it stood at a moment")
    ap.add_argument("vin")
    ap.add_argument("at", nargs="?", help="ISO time, default now")
    args = ap.parse_args(argv)
    import app  # the site's connection

    at = datetime.fromisoformat(args.at) if args.at else datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=app.TZ)
    with app.db() as conn:
        tree = state_at(conn, args.vin, at)
    if tree is None:
        print("nothing stored for that car by then", file=sys.stderr)
        return 1
    json.dump(tree, sys.stdout, indent=1, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
