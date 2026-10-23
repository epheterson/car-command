"""The garage door's own actions, kept for good (the Settings list in cc_state keeps 30), and the
drives they belong to: a close as a car left, an open as it turned in."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta

DDL = """
CREATE TABLE IF NOT EXISTS cc_garage_events (
    id     SERIAL PRIMARY KEY,
    vin    VARCHAR NOT NULL,
    at     TIMESTAMPTZ NOT NULL,
    kind   VARCHAR NOT NULL,
    mode   VARCHAR,
    detail JSONB NOT NULL,
    UNIQUE (vin, at, kind)
);
CREATE INDEX IF NOT EXISTS cc_garage_events_at ON cc_garage_events (at);
"""

# What the Timeline shows: the door actions, kept apart by `did`. Anything else (left alone, left open,
# failed, a door someone opened by hand) is not Car Command's doing.
LABELS = {
    ("start", "started the door"): ("in", "Garage opened"),
    ("start", "would start the door"): ("in", "Garage would have opened"),
    ("close", "closed the door"): ("out", "Garage closed"),
    ("close", "would close the door"): ("out", "Garage would have closed"),
}
SHOWN_KINDS = sorted({k for k, _ in LABELS})

BEFORE_ARRIVAL = timedelta(minutes=10)  # the door starts as the car comes up the street
AFTER_ARRIVAL = timedelta(minutes=3)
BEFORE_DEPARTURE = timedelta(minutes=3)  # the camera sees the car out as it pulls away
AFTER_DEPARTURE = timedelta(minutes=5)


def label(kind: str, detail: dict) -> tuple[str, str] | None:
    """(direction, line) for an event worth a line, else None."""
    return LABELS.get((kind, detail.get("did")))


def parse_at(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return None


def row_of(entry: dict, vin: str) -> tuple | None:
    """(vin, at, kind, mode, detail) for a cc_state log entry, or None without a time or kind."""
    at, kind = parse_at(entry.get("at")), entry.get("kind")
    if at is None or not kind or not vin:
        return None
    return (vin, at, kind, entry.get("mode"), {k: v for k, v in entry.items() if k not in ("at", "kind", "mode")})


def attach(drives: list[dict], events: list[dict]) -> dict[int, list[str]]:
    """{drive id: lines}: each action goes to the one drive it is nearest, an open to a drive's end
    and a close to a drive's start, same car, a direction once a drive. `events` are {vin, at, kind, detail}."""
    anchors = {"in": (BEFORE_ARRIVAL, AFTER_ARRIVAL, "destination_date"), "out": (BEFORE_DEPARTURE, AFTER_DEPARTURE, "start_date")}
    index: dict[tuple[str, str], tuple[list, list]] = {}  # (direction, vin) -> sorted anchor times, drives; bisected, a month is hundreds
    for direction, (_, _, field) in anchors.items():
        by_vin: dict[str, list] = {}
        for d in drives:
            if d.get(field) is not None:
                by_vin.setdefault(d.get("vin"), []).append((d[field], d))
        for vin, pairs in by_vin.items():
            pairs.sort(key=lambda p: p[0])
            index[(direction, vin)] = ([p[0] for p in pairs], [p[1] for p in pairs])
    out: dict[int, dict[str, str]] = {}
    for e in sorted((e for e in events if e["at"] is not None), key=lambda e: e["at"]):
        hit = label(e["kind"], e["detail"])
        if not hit or (hit[0], e["vin"]) not in index:
            continue
        direction, line = hit
        before, after, field = anchors[direction]
        times, rows = index[(direction, e["vin"])]
        best, gap = None, None
        for j in range(bisect_left(times, e["at"] - after), bisect_right(times, e["at"] + before)):
            d = rows[j]
            g = abs((e["at"] - times[j]).total_seconds())
            if direction not in out.get(d["id"], {}) and (gap is None or g < gap):
                best, gap = d["id"], g
        if best is not None:
            out.setdefault(best, {})[direction] = line
    return {i: [v[k] for k in ("out", "in") if k in v] for i, v in out.items()}
