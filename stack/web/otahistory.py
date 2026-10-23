"""The versions a car has installed, as its connector lists them (Lucid's OTA history), written
into the software table so the Software page starts at the car's first firmware instead of the
day logging began.

The connector publishes JSON text, oldest first: [{"version": "3.1.51", "at": "2025-12-19T10:00:00Z"}].
Rows written here carry SOURCE as their import id, so a rewrite touches only them: what the
logger saw, an import or a hand entry stays as it was. The page shows a version once, at its
earliest row, so a live row for a version the history also lists is hidden, not deleted."""

from __future__ import annotations

import json
from datetime import datetime, timezone

SOURCE = "connector-ota"
VERSION_MAX = 40


def parse(raw) -> list[tuple[str, datetime]]:
    """(version, install time) pairs from the connector's text, oldest first; [] for anything
    that is not that list. An entry without a version or a readable time is skipped."""
    try:
        entries = json.loads(raw) if isinstance(raw, str) else []
    except ValueError:
        return []
    out: dict[tuple[str, datetime], None] = {}
    for e in entries if isinstance(entries, list) else []:
        version = e.get("version") if isinstance(e, dict) else None
        try:
            at = datetime.fromisoformat(e["at"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, TypeError, ValueError):
            continue
        if isinstance(version, str) and version.strip():
            out[(version.strip()[:VERSION_MAX], at if at.tzinfo else at.replace(tzinfo=timezone.utc))] = None
    return sorted(out, key=lambda r: r[1])


def plan(stored: list[tuple[str, datetime]], incoming: list[tuple[str, datetime]]) -> tuple[list[tuple[str, datetime]], list[tuple[str, datetime]]]:
    """(rows to delete, rows to insert) to make the stored rows equal the incoming ones."""
    have, want = set(stored), set(incoming)
    return sorted(have - want, key=lambda r: r[1]), sorted(want - have, key=lambda r: r[1])


def store(conn, vin: str, incoming: list[tuple[str, datetime]]) -> int:
    """Bring this car's connector-history rows to `incoming`; returns how many changed. An
    empty list changes nothing: a failed read must not empty the page."""
    if not incoming:
        return 0
    stored = [(r["version"], r["seen_at"]) for r in conn.execute("SELECT version, seen_at FROM cc_software WHERE vin = %s AND import_id = %s", (vin, SOURCE)).fetchall()]
    drop, add = plan(stored, incoming)
    for version, at in drop:
        conn.execute("DELETE FROM cc_software WHERE vin = %s AND import_id = %s AND version = %s AND seen_at = %s", (vin, SOURCE, version, at))
    for version, at in add:
        conn.execute("INSERT INTO cc_software (vin, version, seen_at, import_id) VALUES (%s, %s, %s, %s)", (vin, version, at, SOURCE))
    if drop or add:
        conn.commit()
    return len(drop) + len(add)
