"""Fixing the car's own mistakes on a drive: merge two drives a short stop split, split one
that ran on through a stop, leave one bad reading out.

Nothing is deleted and every edit is one row to remove again (Undo). A merge hands the first
drive the second's end (the second stays in `trips`, out of every list and total by the
absorbed clause); a split cuts the first drive at a reading and writes the rest as a drive
of its own; a left-out reading stays in `cc_positions` and is skipped by what draws or
measures a drive. What each edit overwrote is kept on the edit, so Undo puts it back.

Edits are keyed by start time, the way the loaders key everything: a re-import deletes and
rewrites the drives it covers, so write_history lifts the edits off the span it is about to
clear (unapply) and puts them back on what it wrote (reapply). Facts and cached routes of an
edited drive are dropped for the next fill to compute again, a few hundred at a time."""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone

LOG = logging.getLogger("driveedits")

# What a refusal says, by a short code: the drive page is handed the code in the address, never the words.
MSG = {
    "gone": "This drive was merged into another one; change that one.",
    "nodrive": "No such drive.",
    "still": "This drive is still going.",
    "nonext": "There is no drive after this one to merge with.",
    "nextstill": "The next drive is still going.",
    "before": "The next drive starts before this one ends.",
    "long": "The next drive left more than half an hour later.",
    "charged": "The car charged between the two.",
    "between": "A hidden or very short drive sits between the two.",
    "hidden": "Unhide this drive to merge it.",
    "fresh": "The next drive ended a few minutes ago; give it a moment to settle.",
    "freshcut": "This drive ended a few minutes ago; give it a moment to settle.",
    "stale": "That page was out of date. Look at the drive again and try once more.",
    "outside": "That moment is not inside this drive any more.",
    "noreading": "That reading is not in this drive.",
    "nocut": "There is no reading inside this drive to cut at.",
    "noodo": "That reading has no odometer to cut the distance at.",
    "exists": "A drive already starts at that reading.",
    "nomerge": "That could not be merged.",
    "nosplit": "That could not be split.",
    "later": "A later change to these drives has to be undone first.",
    "undone": "That change was already undone.",
    "pick": "Pick a reading.",
    "pickcut": "Pick a reading to split at.",
    "stands": "This drive is part of a change you made; undo that first.",
    "dup": "That change is already made.",
}
CODES = {v: k for k, v in MSG.items()}
LOCK = 0x64726576  # the first half of the advisory lock an edit and an import both take for a car
FRESH = timedelta(minutes=5)  # the connector may still be filling in where a drive ended

MERGE_GAP = timedelta(minutes=30)  # a stop longer than this is a parked stretch, not a pause
SPIKE_M = 100  # a fix this far above or below both neighbours is the receiver, not a hill (as facts.ELEVATION_SQL)
JUMP_M = 500  # a fix this far from both neighbours, at a speed no car reaches, is a bad position
JUMP_MS = 70.0  # about 155 mph

DDL = """
CREATE TABLE IF NOT EXISTS cc_drive_edits (
    id         BIGSERIAL PRIMARY KEY,
    vin        VARCHAR NOT NULL,
    kind       VARCHAR NOT NULL,         -- 'merge' or 'split'
    a_start    TIMESTAMPTZ NOT NULL,     -- the drive the edit changed, by its start
    b_start    TIMESTAMPTZ NOT NULL,     -- merge: the drive taken in; split: the moment it was cut
    a_id       INTEGER,
    b_id       INTEGER,                  -- merge: the drive taken in; split: the drive written for the rest
    before     JSONB,                    -- the end the first drive had before this edit
    data       JSONB,                    -- split: the cut's odometer and position
    span_end   TIMESTAMPTZ,              -- the last moment the edit reaches, for lifting it off a re-import
    after      JSONB,                    -- the end the first drive had once the edit was on: a re-import that puts the same back works nothing out again
    waiting_since TIMESTAMPTZ,           -- set while a re-import has no drives to put the edit on
    applied    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (vin, kind, a_start, b_start)
);
CREATE TABLE IF NOT EXISTS cc_drive_excluded (
    vin        VARCHAR NOT NULL,
    at         TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (vin, at)
);
"""

# The drives a merge took in are out of every list and total, on the clause the thresholds ride.
ABSORBED = " AND t.id NOT IN (SELECT b_id FROM cc_drive_edits WHERE kind = 'merge' AND applied AND b_id IS NOT NULL)"
# The end of a drive: what a merge copies from the second drive and a split rewrites.
END_COLS = ("destination_date", "destination_odometer", "destination_position_latitude", "destination_position_longitude", "destination_location_uid")
TRIP_COLS = ("vin", "start_date", "start_position_latitude", "start_position_longitude", "start_location_uid", "start_odometer", *END_COLS)


def left_out(alias: str = "p") -> str:
    """The clause that skips readings left out, for a query over cc_positions as `alias`."""
    return f" AND NOT EXISTS (SELECT 1 FROM cc_drive_excluded x WHERE x.vin = {alias}.vin AND x.at = {alias}.at)"


# ---- pure ----


def parse_at(value) -> datetime | None:
    """A moment from a form: an ISO time (a reading's own) or epoch seconds (picked on the chart)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromtimestamp(float(text), timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    try:
        got = datetime.fromisoformat(text)
    except ValueError:
        return None
    return got if got.tzinfo else got.replace(tzinfo=timezone.utc)


def merge_refusal(a: dict, b: dict | None, charged: bool, now: datetime | None = None) -> str | None:
    """Why `a` cannot take in `b`, the drive after it, or None. The pause is the gap between
    `a` arriving and `b` leaving, and only a short one makes the two a single drive. With `now`, a drive
    that ended minutes ago waits: the connector may still be filling in where it ended."""
    if a.get("destination_date") is None:
        return MSG["still"]
    if b is None:
        return MSG["nonext"]
    if b.get("destination_date") is None:
        return MSG["nextstill"]
    gap = b["start_date"] - a["destination_date"]
    if gap < timedelta(0):
        return MSG["before"]
    if gap > MERGE_GAP:
        return MSG["long"]
    if charged:
        return MSG["charged"]
    if now is not None and b["destination_date"] > now - FRESH:
        return MSG["fresh"]
    return None


def cut_point(fixes: list[dict], start: datetime, end: datetime, at: datetime) -> dict | str:
    """The reading a drive is cut at: the one nearest `at` among those strictly inside it that
    were not left out, with its odometer (the nearest reading's own, else the line between its
    neighbours'). A string says why there is no cut."""
    inside = [f for f in fixes if start < f["at"] < end and not f.get("excluded")]
    if not inside:
        return MSG["nocut"]
    fix = min(inside, key=lambda f: abs((f["at"] - at).total_seconds()))
    odo = fix.get("odometer")
    if odo is None:
        known = [f for f in fixes if f.get("odometer") is not None and not f.get("excluded")]
        before = [f for f in known if f["at"] <= fix["at"]]
        after = [f for f in known if f["at"] >= fix["at"]]
        if not before or not after:
            return MSG["noodo"]
        p, q = before[-1], after[0]
        span = (q["at"] - p["at"]).total_seconds()
        odo = p["odometer"] if span <= 0 else p["odometer"] + (q["odometer"] - p["odometer"]) * (fix["at"] - p["at"]).total_seconds() / span
    return {"at": fix["at"], "odometer": odo, "latitude": fix.get("latitude"), "longitude": fix.get("longitude")}


def _metres(a: dict, b: dict) -> float:
    la, lb = math.radians(a["latitude"]), math.radians(b["latitude"])
    h = math.sin((lb - la) / 2) ** 2 + math.cos(la) * math.cos(lb) * math.sin(math.radians(b["longitude"] - a["longitude"]) / 2) ** 2
    return 12742000 * math.asin(math.sqrt(h))


def suspects(track: list[dict]) -> set[datetime]:
    """The readings that look like the receiver or the sensor, not the car: a position far from
    both neighbours at a speed no car reaches, or a height more than SPIKE_M off both. Only a
    hint, so it errs toward few; readings already left out are not looked at."""
    live = [f for f in track if not f.get("excluded")]
    out = set()
    for i in range(1, len(live) - 1):
        p, f, n = live[i - 1], live[i], live[i + 1]
        if all(x.get("latitude") is not None and x.get("longitude") is not None for x in (p, f, n)):
            far = []
            for o in (p, n):
                dt = abs((f["at"] - o["at"]).total_seconds())
                d = _metres(f, o)
                far.append(d > JUMP_M and d / max(dt, 1.0) > JUMP_MS)
            if all(far):
                out.add(f["at"])
        es = [x.get("elevation") for x in (p, f, n)]
        if None not in es and abs(es[1] - es[0]) > SPIKE_M and abs(es[1] - es[2]) > SPIKE_M:
            out.add(f["at"])
    return out


def with_flagged(shown: list[dict], every: list[dict], flagged: set[datetime]) -> list[dict]:
    """The thinned table plus every row that was left out or looks wrong, in time order: a row
    that could be fixed must not be one of the rows skipped."""
    have = {r["at"] for r in shown}
    extra = [r for r in every if r["at"] in flagged and r["at"] not in have]
    return sorted([*shown, *extra], key=lambda r: r["at"]) if extra else shown


# ---- SQL ----


def _all(conn, sql: str, args=()) -> list[dict]:
    """Rows as dicts, whether the connection hands back tuples (the loaders) or dicts (the app)."""
    cur = conn.execute(sql, args)
    if cur.description is None:
        return []
    names = [d.name for d in cur.description]
    return [dict(zip(names, r.values() if isinstance(r, dict) else r)) for r in cur.fetchall()]


def _one(conn, sql: str, args=()) -> dict | None:
    rows = _all(conn, sql, args)
    return rows[0] if rows else None


def _has(conn, table: str) -> bool:
    return bool(_all(conn, "SELECT to_regclass(%s) AS t", (table,))[0]["t"])


def lock(conn, vin: str) -> None:
    """Held to the end of the transaction by an edit and by an import of the same car, so one
    never changes a drive under the other."""
    conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (LOCK, vin))


def _jsonable(d: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}


def _revived(d: dict) -> dict:
    return {k: (datetime.fromisoformat(v) if k == "destination_date" and v else v) for k, v in d.items()}


SLACK = timedelta(seconds=60)  # how far a re-import may move a drive's start before the edit no longer finds it


def _trip_at(conn, vin: str, start: datetime, exact: bool = False) -> dict | None:
    """The drive that starts at `start`, or the nearest within a minute either side: the same
    drive when another source dates its start a second off."""
    if exact:
        return _one(conn, f"SELECT id, {', '.join(TRIP_COLS)} FROM trips WHERE vin = %s AND start_date = %s", (vin, start))
    return _one(
        conn,
        f"SELECT id, {', '.join(TRIP_COLS)} FROM trips WHERE vin = %s AND start_date BETWEEN %s AND %s ORDER BY abs(extract(epoch FROM start_date - %s)) LIMIT 1",
        (vin, start - SLACK, start + SLACK, start),
    )


def forget(conn, vin: str, ids: list[int], starts: list[datetime]) -> None:
    """What was worked out for drives that have just changed, dropped for the next fill and the
    route matcher to work out again: their facts, and the road line drawn for them."""
    if ids and _has(conn, "cc_trip_facts"):
        conn.execute("DELETE FROM cc_trip_facts WHERE trip_id = ANY(%s)", (ids,))
    if starts and _has(conn, "cc_trip_routes"):
        conn.execute("DELETE FROM cc_trip_routes WHERE vin = %s AND start_date = ANY(%s)", (vin, starts))


def _set_end(conn, trip_id: int, end: dict) -> bool:
    cur = conn.execute(f"UPDATE trips SET {', '.join(f'{c} = %({c})s' for c in END_COLS)} WHERE id = %(id)s", {**end, "id": trip_id})
    return cur.rowcount == 1


def _hidden(conn, trip_id: int) -> bool:
    return bool(_one(conn, "SELECT 1 AS x FROM cc_hidden WHERE kind = 'drive' AND ref_id = %s", (trip_id,)))


def _tag(conn, trip_id: int) -> dict | None:
    return _one(conn, "SELECT tag, note FROM cc_drive_tags WHERE trip_id = %s", (trip_id,))


def _copy_tag(conn, src: int, dst: int) -> bool:
    got = conn.execute("INSERT INTO cc_drive_tags (trip_id, tag, note) SELECT %s, tag, note FROM cc_drive_tags WHERE trip_id = %s ON CONFLICT DO NOTHING", (dst, src))
    return got.rowcount == 1


def absorbed_by(conn, trip_id: int) -> int | None:
    """The drive that took this one in by a merge, if one did."""
    row = _one(conn, "SELECT a_id FROM cc_drive_edits WHERE kind = 'merge' AND applied AND b_id = %s ORDER BY id DESC LIMIT 1", (trip_id,))
    return row["a_id"] if row else None


def involved(conn, trip_id: int) -> bool:
    """Whether an edit stands on this drive (removing it would leave the edit pointing nowhere)."""
    return bool(_one(conn, "SELECT 1 AS x FROM cc_drive_edits WHERE a_id = %s OR b_id = %s LIMIT 1", (trip_id, trip_id)))


def _apply(conn, e: dict) -> bool:
    """Put an edit onto the drives as they are now. False when they are not what the edit was
    made on (the source no longer has that drive, or it ends elsewhere). Facts and cached road
    lines are dropped only when the end it writes is not the one it wrote last time."""
    first = e["a_id"] is None
    a = _trip_at(conn, e["vin"], e["a_start"])
    if a is None or a["destination_date"] is None:
        return False
    before = {c: a[c] for c in END_COLS}
    a_start, b_start = a["start_date"], e["b_start"]
    if e["kind"] == "merge":
        b = _trip_at(conn, e["vin"], e["b_start"])
        if b is None or b["id"] == a["id"] or b["destination_date"] is None or b["destination_date"] <= a["destination_date"]:
            return False
        end = {c: b[c] for c in END_COLS}
        if not _set_end(conn, a["id"], end):
            return False
        made, span_end, b_start, made_ids = b["id"], b["destination_date"], b["start_date"], [a["id"]]
        carried = False
        if first and _tag(conn, a["id"]) is None and _tag(conn, b["id"]):  # the second drive's tag and note go on the merged one
            carried = _copy_tag(conn, b["id"], a["id"])
            copied = _tag(conn, a["id"]) if carried else None
            conn.execute("UPDATE cc_drive_edits SET data = %s WHERE id = %s", (json.dumps({"carried": carried, "tag": copied and copied["tag"], "note": copied and copied["note"]}), e["id"]))
    else:
        cut, d = e["b_start"], e["data"] or {}
        if not a["start_date"] < cut < a["destination_date"] or _trip_at(conn, e["vin"], cut, exact=True):
            return False
        end = {
            "destination_date": cut,
            "destination_odometer": d.get("odometer"),
            "destination_position_latitude": d.get("latitude"),
            "destination_position_longitude": d.get("longitude"),
            "destination_location_uid": None,
        }
        rest = {
            **{c: a[c] for c in END_COLS},
            "vin": a["vin"],
            "start_date": cut,
            "start_odometer": d.get("odometer"),
            "start_position_latitude": d.get("latitude"),
            "start_position_longitude": d.get("longitude"),
            "start_location_uid": None,
        }
        if not _set_end(conn, a["id"], end):
            return False
        # the drive written for the rest keeps the id it had, so its tag and its page survive a re-import
        want = e["b_id"] if e["b_id"] is not None and not _one(conn, "SELECT 1 AS x FROM trips WHERE id = %s", (e["b_id"],)) else None
        names = ("id", *TRIP_COLS) if want is not None else TRIP_COLS
        made = _one(conn, f"INSERT INTO trips ({', '.join(names)}) VALUES ({', '.join(f'%({c})s' for c in names)}) RETURNING id", {**rest, "id": want})["id"]
        if first:  # the first half's hidden flag, tag and note are the second half's too
            _copy_tag(conn, a["id"], made)
            conn.execute("INSERT INTO cc_hidden (kind, ref_id) SELECT 'drive', %s WHERE EXISTS (SELECT 1 FROM cc_hidden WHERE kind = 'drive' AND ref_id = %s) ON CONFLICT DO NOTHING", (made, a["id"]))
        span_end, made_ids = before["destination_date"], [a["id"], made]
    changed = first or e.get("after") != _jsonable(end)
    conn.execute(
        "UPDATE cc_drive_edits SET a_id = %s, b_id = %s, a_start = %s, b_start = %s, before = %s, after = %s, span_end = %s, applied = TRUE, waiting_since = NULL WHERE id = %s",
        (a["id"], made, a_start, b_start, json.dumps(_jsonable(before)), json.dumps(_jsonable(end)), span_end, e["id"]),
    )
    if changed:
        forget(conn, e["vin"], made_ids, [a_start, *([b_start] if e["kind"] == "split" else [])])
    return True


def _unapply(conn, e: dict, forget_now: bool = True) -> None:
    """Put the drives back as the source had them, keeping the edit to apply again. An import
    leaves the facts alone (forget_now False): the edit goes straight back on."""
    a = _trip_at(conn, e["vin"], e["a_start"])
    if a is not None and e["before"]:
        _set_end(conn, a["id"], _revived(e["before"]))
    ids, starts = [a["id"]] if a else [], [e["a_start"]]
    if e["kind"] == "split" and e["b_id"] is not None:
        conn.execute("DELETE FROM trips WHERE id = %s AND vin = %s AND start_date = %s", (e["b_id"], e["vin"], e["b_start"]))
        ids.append(e["b_id"])
        starts.append(e["b_start"])
    if forget_now:
        forget(conn, e["vin"], ids, starts)
    conn.execute("UPDATE cc_drive_edits SET applied = FALSE WHERE id = %s", (e["id"],))


def unapply(conn, vin: str, lo: datetime, hi: datetime) -> int:
    """Lift every applied edit that reaches [lo, hi] off its drives, newest first, before a
    re-import clears that span. Returns how many."""
    live = [e for e in _all(conn, "SELECT * FROM cc_drive_edits WHERE vin = %s AND applied ORDER BY id DESC", (vin,)) if e["a_start"] - SLACK <= hi and (e["span_end"] or e["a_start"]) + SLACK >= lo]
    for e in live:
        _unapply(conn, e, forget_now=False)
    return len(live)


def reapply(conn, vin: str) -> int:
    """Put back every edit that is lifted, oldest first, onto what the import wrote. An edit
    whose drives are not there waits (and says so once) and is tried again at the next import."""
    done = 0
    for e in _all(conn, "SELECT * FROM cc_drive_edits WHERE vin = %s AND NOT applied ORDER BY id", (vin,)):
        if _apply(conn, e):
            done += 1
            continue
        forget(conn, vin, [i for i in (e["a_id"], e["b_id"]) if i is not None], [e["a_start"]])  # what was worked out on the edited ends is no longer true
        if e["waiting_since"] is None:
            LOG.warning("drive edit %s (%s at %s) waits: its drives are not in the history now", e["id"], e["kind"], e["a_start"])
            conn.execute("UPDATE cc_drive_edits SET waiting_since = now() WHERE id = %s", (e["id"],))
    return done


def waiting_for(conn, trip: dict) -> list[dict]:
    """Edits waiting for drives that are not in the history, whose span covers this drive."""
    return _all(
        conn,
        "SELECT id, kind, a_start FROM cc_drive_edits WHERE vin = %s AND NOT applied AND waiting_since IS NOT NULL AND a_start <= %s AND COALESCE(span_end, a_start) >= %s ORDER BY id",
        (trip["vin"], trip["destination_date"] or trip["start_date"], trip["start_date"]),
    )


def next_drive(conn, trip: dict, where: str) -> dict | None:
    """The drive after this one for the same car that `where` lets through (the lists' own clause)."""
    return _one(
        conn,
        f"SELECT t.id, {', '.join('t.' + c for c in TRIP_COLS)} FROM trips t WHERE t.vin = %s AND t.start_date > %s{where} ORDER BY t.start_date LIMIT 1",
        (trip["vin"], trip["start_date"]),
    )


def merge_partner(conn, a: dict, where: str) -> tuple[dict | None, str | None]:
    """The drive `a` could take in and what stops it, if anything: the row right after it
    (taken in drives apart) must also be the next one the lists show, so no hidden or very short
    drive is swallowed between, and the first drive must be one the lists show too."""
    if _hidden(conn, a["id"]):
        return None, MSG["hidden"]
    b = next_drive(conn, a, ABSORBED) if a["destination_date"] is not None else None
    if b is not None:
        listed = next_drive(conn, a, where)
        if listed is None or listed["id"] != b["id"]:
            return b, MSG["between"]
    charged = bool(
        a["destination_date"]
        and b
        and _one(
            conn,
            "SELECT 1 AS x FROM charging_sessions WHERE vin = %s AND session_start_date < %s" " AND COALESCE(session_end_date, LEAST(now(), session_start_date + interval '24 hours')) > %s LIMIT 1",
            (a["vin"], b["start_date"], a["destination_date"]),
        )
    )
    return b, merge_refusal(a, b, charged, datetime.now(timezone.utc))


def _trip(conn, trip_id: int) -> dict | None:
    return _one(conn, f"SELECT id, {', '.join(TRIP_COLS)} FROM trips WHERE id = %s", (trip_id,))


def _add(conn, vin: str, kind: str, a: dict, b_start: datetime, data: dict | None) -> dict | None:
    got = _one(
        conn,
        "INSERT INTO cc_drive_edits (vin, kind, a_start, b_start, data, applied) VALUES (%s, %s, %s, %s, %s, FALSE) ON CONFLICT DO NOTHING RETURNING id",
        (vin, kind, a["start_date"], b_start, json.dumps(data) if data is not None else None),
    )
    return _all(conn, "SELECT * FROM cc_drive_edits WHERE id = %s", (got["id"],))[0] if got else None


def _editable(conn, trip_id: int) -> tuple[dict | None, str | None]:
    """The drive, or why it cannot be edited: not there, or taken in by a merge. The car's lock
    is taken first, so what is read here is what another edit or an import left."""
    vin = vin_of(conn, trip_id)
    if vin is None:
        return None, MSG["nodrive"]
    lock(conn, vin)
    a = _trip(conn, trip_id)
    if a is None:
        return None, MSG["nodrive"]
    if absorbed_by(conn, trip_id) is not None:
        return None, MSG["gone"]
    return a, None


def merge(conn, trip_id: int, where: str, partner: datetime | None = None) -> str | None:
    """Make this drive and the one after it a single drive. A string says why not. `partner` is
    the start of the drive the page offered: a second tap, or a page left open while something
    else changed, would otherwise merge with whatever comes next now."""
    a, why = _editable(conn, trip_id)
    if a is None:
        return why
    b, why = merge_partner(conn, a, where)
    if why:
        return why
    if partner is None or b["start_date"] != partner:
        return MSG["stale"]
    edit = _add(conn, a["vin"], "merge", a, b["start_date"], None)
    if edit is None:
        return MSG["dup"]
    return None if _apply(conn, edit) else MSG["nomerge"]


def split(conn, trip_id: int, at: datetime) -> str | None:
    """Cut this drive in two at the reading nearest `at`. A string says why not."""
    a, why = _editable(conn, trip_id)
    if a is None:
        return why
    if a["destination_date"] is None:
        return MSG["still"]
    if a["destination_date"] > datetime.now(timezone.utc) - FRESH:
        return MSG["freshcut"]
    if not a["start_date"] < at < a["destination_date"]:  # a second tap on the same cut lands on the drive's new end
        return MSG["outside"]
    fixes = _all(
        conn,
        "SELECT p.at, p.latitude, p.longitude, p.odometer, EXISTS (SELECT 1 FROM cc_drive_excluded x WHERE x.vin = p.vin AND x.at = p.at) AS excluded "
        "FROM cc_positions p WHERE p.vin = %s AND p.at BETWEEN %s AND %s ORDER BY p.at",
        (a["vin"], a["start_date"], a["destination_date"]),
    )
    cut = cut_point(fixes, a["start_date"], a["destination_date"], at)
    if isinstance(cut, str):
        return cut
    if _trip_at(conn, a["vin"], cut["at"], exact=True):
        return MSG["exists"]
    edit = _add(conn, a["vin"], "split", a, cut["at"], {"odometer": cut["odometer"], "latitude": cut["latitude"], "longitude": cut["longitude"]})
    if edit is None:
        return MSG["dup"]
    return None if _apply(conn, edit) else MSG["nosplit"]


def edits_for(conn, trip_id: int) -> list[dict]:
    """The edits that made or changed this drive, newest first: what its page can undo."""
    return _all(conn, "SELECT id, kind, a_id, b_id, b_start, created_at FROM cc_drive_edits WHERE applied AND (a_id = %s OR (kind = 'split' AND b_id = %s)) ORDER BY id DESC", (trip_id, trip_id))


def edits_for_full(conn, trip_id: int) -> dict | None:
    """The newest edit on this drive with every column, for what undoing it would do."""
    edits = edits_for(conn, trip_id)
    return _one(conn, "SELECT * FROM cc_drive_edits WHERE id = %s", (edits[0]["id"],)) if edits else None


def undo_loses(conn, e: dict) -> str | None:
    """What undoing a cut would throw away: the tag and note of the second half, when the
    first has its own. None when nothing is lost (they move to the first half otherwise)."""
    if e["kind"] != "split" or e["b_id"] is None:
        return None
    mine, rest = _tag(conn, e["a_id"]), _tag(conn, e["b_id"])
    if not (mine and rest) or (mine["tag"], mine["note"]) == (rest["tag"], rest["note"]):
        return None
    return rest["tag"]


def undo(conn, edit_id: int, trip_id: int) -> tuple[str | None, int | None]:
    """Take an edit off the drive it was made on: (why not, None), or (None, the drive it
    changed). Not while a later edit stands on the same drives (that one is undone first)."""
    t = _trip(conn, trip_id)
    if t is None:
        return MSG["nodrive"], None
    lock(conn, t["vin"])
    e = _one(conn, "SELECT * FROM cc_drive_edits WHERE id = %s AND vin = %s AND (NOT applied OR a_id = %s OR b_id = %s)", (edit_id, t["vin"], trip_id, trip_id))
    if e is None:
        return MSG["undone"], None
    ids = [i for i in (e["a_id"], e["b_id"]) if i is not None]
    if e["applied"] and _one(conn, "SELECT 1 AS x FROM cc_drive_edits WHERE id > %s AND applied AND (a_id = ANY(%s) OR b_id = ANY(%s) OR a_start = %s) LIMIT 1", (edit_id, ids, ids, e["a_start"])):
        return MSG["later"], None
    if e["applied"]:
        _unapply(conn, e)
    else:
        forget(conn, e["vin"], ids, [e["a_start"]])
    conn.execute("DELETE FROM cc_drive_edits WHERE id = %s", (edit_id,))
    if e["kind"] == "merge" and (e["data"] or {}).get("carried"):  # the tag copied from the second drive goes with the merge
        conn.execute(
            "DELETE FROM cc_drive_tags WHERE trip_id = %s AND tag = %s AND note IS NOT DISTINCT FROM %s", (e["a_id"], e["data"].get("tag"), e["data"].get("note"))
        )  # not a tag the owner has since changed
    if e["kind"] == "split" and e["b_id"] is not None:  # the second half is gone: its tag and note go to the first when it has none
        if _tag(conn, e["a_id"]) is None:
            conn.execute("UPDATE cc_drive_tags SET trip_id = %s WHERE trip_id = %s", (e["a_id"], e["b_id"]))
        conn.execute("DELETE FROM cc_drive_tags WHERE trip_id = %s", (e["b_id"],))
        conn.execute("DELETE FROM cc_hidden WHERE kind = 'drive' AND ref_id = %s", (e["b_id"],))
    return None, e["a_id"]


def vin_of(conn, trip_id: int) -> str | None:
    row = _one(conn, "SELECT vin FROM trips WHERE id = %s", (trip_id,))
    return row["vin"] if row else None


def _touch(conn, trip: dict) -> None:
    """A drive whose readings changed: its top speed, climb and cached road line are worked out again."""
    if _has(conn, "cc_trip_facts"):
        conn.execute("UPDATE cc_trip_facts SET max_mph = NULL, elev_max_m = NULL, elev_min_m = NULL WHERE trip_id = %s", (trip["id"],))
    if _has(conn, "cc_trip_routes"):
        conn.execute("DELETE FROM cc_trip_routes WHERE vin = %s AND start_date = %s", (trip["vin"], trip["start_date"]))


def _inside(conn, trip_id: int, at: datetime) -> tuple[dict | None, str | None]:
    t, why = _editable(conn, trip_id)
    if t is None:
        return None, why
    ok = _one(
        conn,
        "SELECT 1 AS x FROM trips t WHERE t.id = %s AND t.start_date <= %s AND COALESCE(t.destination_date, now()) >= %s AND EXISTS (SELECT 1 FROM cc_positions p WHERE p.vin = t.vin AND p.at = %s)",
        (trip_id, at, at, at),
    )
    return (t, None) if ok else (None, MSG["noreading"])


def leave_out(conn, trip_id: int, at: datetime) -> str | None:
    """Skip one reading of this drive in everything drawn or measured from it."""
    trip, why = _inside(conn, trip_id, at)
    if trip is None:
        return why
    conn.execute("INSERT INTO cc_drive_excluded (vin, at) VALUES (%s, %s) ON CONFLICT DO NOTHING", (trip["vin"], at))
    _touch(conn, trip)
    return None


def take_back(conn, trip_id: int, at: datetime) -> str | None:
    trip, why = _inside(conn, trip_id, at)
    if trip is None:
        return why
    conn.execute("DELETE FROM cc_drive_excluded WHERE vin = %s AND at = %s", (trip["vin"], at))
    _touch(conn, trip)
    return None
