"""Which source writes a car's history, and the handoff when that changes.

One writer per car: a Tesla can be followed by its own stream, the TeslaFi feed and
TeslaMate at once, and each of them replays a rolling window into the same tables, so two
writing at once would double every drive. The choice is kept per car in cc_state
(`history_writer:<vin>`); with none kept, the sources behave as before (the TeslaFi feed and
TeslaMate write, the stream writes only while no other live source has written in the
last day and a half), so an install that never opens the choice sees no change. A source
that is not the writer keeps its raw records (cc_tesla_raw, cc_teslafi_rows) and writes
nothing else, so the comparison can be read and the choice undone at any time.

The handoff, when the writer changes. The stored history runs to the newest import's end
(`at`). The boundary B is the latest moment at or before it that no drive or charge of
either source touches (PAD either side), so neither the outgoing nor the incoming source
has a session cut in two. Then:
- every import of the car is marked as ending at B, and the incoming one as having got to
  B, so its next rolling replay opens at B exactly (its floor and its resume, as each
  live source's window_start reads them) and nowhere else;
- stretches (states, temperatures, levels…) that cross B are cut at B less a second, so
  the incoming replay's rolling clear (history.clear_import, which takes any stretch
  touching its window) leaves the part before B alone;
- the outgoing sources' positions and software rows from B on go, because a rolling clear
  takes only its own import's.
Drives, charges and stretches from B on stay until the incoming replay, forced at the next
tick, rewrites [B, now] over them, so the pages never show an empty hour. Nothing before B
is touched, and nothing from B on survives twice.

A rebuild and a handoff never interleave: both take a per-car advisory lock for their
transaction (write_history's commit lets it go)."""

from __future__ import annotations

from datetime import datetime, timedelta

from history import INTERVAL_TABLES, _t

# kind -> the import id prefix its rolling replay writes under
PREFIX = {"tesla": "tesla-stream:", "teslafi": "teslafi-live:", "teslamate": "teslamate-live:"}
NAMES = {"tesla": "Tesla (direct)", "teslafi": "TeslaFi (API)", "teslamate": "TeslaMate"}
# app.source_of's word for each, and back
SOURCE = {"tesla": "tesla", "teslafi": "feed", "teslamate": "teslamate"}
KIND_OF_SOURCE = {v: k for k, v in SOURCE.items()}
KEY = "history_writer:"
PAD = timedelta(minutes=5)  # a boundary keeps this far from any session's ends: both sources' clocks for a session differ by a poll
LOOK_BACK = timedelta(days=2)  # sessions this far before the stored end are considered for the boundary
CUT = timedelta(seconds=1)
VIN_STRETCHES = [t for t, (_c, key) in INTERVAL_TABLES.items() if key == "vin"] + ["cc_inside_temperatures", "cc_modes", "cc_charging_electrics"]
DRIVE_STRETCHES = [t for t, (_c, key) in INTERVAL_TABLES.items() if key == "drive"]


def import_id(kind: str, vin: str) -> str:
    return PREFIX[kind] + vin


def lock(conn, vin: str) -> None:
    """This car's history to this transaction alone, until it commits."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("history:" + vin,))


def chosen(conn, vin: str) -> str | None:
    row = _t(conn.execute("SELECT value FROM cc_state WHERE key = %s", (KEY + vin,)).fetchone())
    return row[0] if row and row[0] in PREFIX else None


def all_chosen(conn) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM cc_state WHERE key LIKE %s", (KEY + "%",)).fetchall()
    return {k[len(KEY) :]: v for k, v in map(_t, rows) if v in PREFIX}


def writes(conn, vin: str, kind: str) -> bool | None:
    """Whether `kind` writes this car's history: True or False once chosen, None when no
    choice is kept and the source's own default rule decides."""
    c = chosen(conn, vin)
    return None if c is None else c == kind


def options(current: str | None, have: dict[str, bool]) -> list[dict]:
    """The writers a car can choose between: the sources it has, and always the current one."""
    return [{"kind": k, "name": NAMES[k], "current": k == current} for k in PREFIX if have.get(k) or k == current]


def boundary(at: datetime, busy: list[tuple[datetime, datetime]], pad: timedelta = PAD) -> datetime:
    """The latest moment at or before `at` that no session in `busy` comes within `pad` of."""
    b, spans = at, sorted(busy, reverse=True)
    moved = True
    while moved:
        moved = False
        for s, e in spans:
            if s - pad <= b <= e + pad:
                b, moved = s - pad - CUT, True
    return b


def spans(out: dict) -> list[tuple[datetime, datetime]]:
    """A replay's drives and charges as (start, end)."""
    return [(t["start_date"], t["destination_date"]) for t in out.get("trips") or []] + [(c["session_start_date"], c["session_end_date"]) for c in out.get("charging_sessions") or []]


def stored_spans(conn, vin: str, since: datetime) -> list[tuple[datetime, datetime]]:
    trips = conn.execute("SELECT start_date, destination_date FROM trips WHERE vin = %s AND destination_date >= %s", (vin, since)).fetchall()
    charges = conn.execute(
        "SELECT session_start_date, COALESCE(session_end_date, session_start_date) FROM charging_sessions WHERE vin = %s AND COALESCE(session_end_date, session_start_date) >= %s", (vin, since)
    ).fetchall()
    return [tuple(_t(r)) for r in trips] + [tuple(_t(r)) for r in charges]


def handoff(conn, vin: str, new: str, incoming: list[tuple[datetime, datetime]], first_raw: datetime | None, end: datetime | None = None, covers=None) -> tuple[datetime | None, str | None]:
    """Hand this car's history to `new` (see the module's note). (B, None) when done, (None,
    None) when nothing was stored yet, (None, why not) when the incoming source cannot write
    everything from B on: no records from before B, or `covers(B)` names a gap it has.
    `end` reaches the handover back from the stored end (to where the outgoing source went
    silent, so the incoming one fills what it missed)."""
    imports = [tuple(_t(r)) for r in conn.execute("SELECT id, last_at FROM cc_imports WHERE vin = %s", (vin,)).fetchall()]
    ends = [last for _id, last in imports if last is not None]
    if not ends:
        return None, None
    at = max(ends) if end is None else min(max(ends), end)
    b = boundary(at, stored_spans(conn, vin, at - LOOK_BACK) + list(incoming))
    if first_raw is not None and first_raw >= b:
        return None, f"{NAMES[new]} has records from {first_raw:%Y-%m-%d %H:%M} UTC; the stored history runs to {b:%Y-%m-%d %H:%M} UTC. Switch once it has been recording longer than that gap."
    problem = covers(b) if covers else None
    if problem:
        return None, problem
    cut = b - CUT
    for table in VIN_STRETCHES:
        conn.execute(f"UPDATE {table} SET last_date = GREATEST(first_date, %s) WHERE vin = %s AND first_date < %s AND last_date >= %s", (cut, vin, b, b))
    for table in DRIVE_STRETCHES:
        conn.execute(f"UPDATE {table} SET last_date = GREATEST(first_date, %s) WHERE drive_id IN (SELECT id FROM drives WHERE vin = %s) AND first_date < %s AND last_date >= %s", (cut, vin, b, b))
    others = [import_id(k, vin) for k in PREFIX if k != new]
    conn.execute("DELETE FROM cc_positions WHERE vin = %s AND at >= %s AND import_id = ANY(%s)", (vin, b, others))
    conn.execute("DELETE FROM cc_software WHERE vin = %s AND seen_at >= %s AND import_id = ANY(%s)", (vin, b, others))
    conn.execute("UPDATE cc_imports SET last_at = %s, first_at = LEAST(first_at, %s) WHERE vin = %s AND last_at > %s", (b, b, vin, b))
    conn.execute(
        "INSERT INTO cc_imports (id, vin, filename, rows, first_at, last_at) VALUES (%s, %s, 'handoff', 0, %s, %s) ON CONFLICT (id) DO UPDATE SET last_at = LEAST(cc_imports.last_at, EXCLUDED.last_at)",
        (import_id(new, vin), vin, b, b),
    )
    return b, None


def choose(
    conn, vin: str, new: str, current: str | None, incoming: list[tuple[datetime, datetime]] = (), first_raw: datetime | None = None, end: datetime | None = None, covers=None
) -> tuple[datetime | None, str | None]:
    """Keep `new` as this car's writer, handing the history over when it changes. Commits.
    (B, problem) as handoff() gives them; (None, None) for no handoff needed."""
    if new not in PREFIX:
        return None, "No such source."
    lock(conn, vin)
    b, problem = (None, None) if new == current else handoff(conn, vin, new, incoming, first_raw, end, covers)
    if problem:
        conn.rollback()
        return None, problem
    conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (KEY + vin, new))
    note = f"{new} from {b.isoformat()}" if b else f"{new} (kept)"
    conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f"history_handoff:{vin}", note))
    conn.commit()
    return b, None


FRESH = timedelta(minutes=5)  # a poller that answered this recently is following the car now
FRESH_ROW = timedelta(minutes=10)


def switch(conn, vin: str, new: str, current: str | None, now: datetime, tz, last_ok: datetime | None = None, teslamate_spans=None) -> tuple[datetime | None, str | None]:
    """choose(), after the work that makes the handover safe. Slow (two days of records): a
    background job's, never a page's.
    - The incoming source's own drives and charges near the stored end (its raw records; for
      TeslaMate, `teslamate_spans(vin, since)` reads its drives), so the boundary cuts none.
    - Leaving the stream: the handover reaches back to the stream's first silence since the
      incoming source last wrote, so the incoming source's raw rows fill what the stream missed.
    - The incoming source must cover everything from the boundary to now (coverage()); a
      switch that would hand it a stretch it cannot write is refused, with why and the fix."""
    if new == current:
        return choose(conn, vin, new, current)
    import tesla_compare  # it reads the stream, which reads this module

    stored_end = _t(conn.execute("SELECT MAX(last_at) FROM cc_imports WHERE vin = %s", (vin,)).fetchone())
    since = ((stored_end[0] if stored_end else None) or now) - LOOK_BACK
    if new == "tesla":
        incoming = tesla_compare.incoming_spans(conn, vin, since, now)
    elif new == "teslafi":
        incoming = teslafi_spans(conn, vin, since, tz)
    else:
        incoming = list(teslamate_spans(vin, since)) if teslamate_spans else []
    end = None
    if current == "tesla":
        own = _t(conn.execute("SELECT last_at FROM cc_imports WHERE id = %s", (import_id(new, vin),)).fetchone())
        wrote_to = (own[0] if own else None) or since
        gone = [a for a, _b in tesla_compare.silences(tesla_compare.raw(conn, vin, wrote_to - tesla_compare.LEAD, now), now) if a >= wrote_to]
        end = min(gone) if gone else None
    return choose(conn, vin, new, current, incoming, first_raw(conn, new, vin), end, lambda b: coverage(conn, vin, new, b, now, last_ok))


def coverage(conn, vin: str, new: str, b: datetime, now: datetime, last_ok: datetime | None) -> str | None:
    """Why `new` cannot write everything from `b` to now, or None when it can."""
    if new == "tesla":
        import tesla_compare

        lo = b - PAD
        for a, z in tesla_compare.silences(tesla_compare.raw(conn, vin, lo - tesla_compare.LEAD, now), now):
            if z > lo:
                return (
                    f"The stream went silent from {a:%b %-d %H:%M} to {z:%H:%M} UTC without the car saying goodbye, after the point the switch would hand over from ({b:%H:%M} UTC), "
                    "and it cannot fill that now. Wait until the current source's next hourly replay moves the switch point past it, or check the receiver (Connect Tesla, step 5) if it is still silent."
                )
        return None
    if last_ok is not None and now - last_ok <= FRESH:
        return None
    if new == "teslafi":
        newest = _t(conn.execute("SELECT MAX(at) FROM cc_teslafi_rows WHERE vin = %s", (vin,)).fetchone())
        if newest and newest[0] is not None and now - newest[0] <= FRESH_ROW:
            return None
        since = f" since {newest[0]:%b %-d %H:%M} UTC" if newest and newest[0] else ""
        return f"TeslaFi has not answered for this car{since}, so it would leave a stretch unwritten. Check its token (Cars and sources), then switch once it answers again."
    return "TeslaMate has not answered for this car in the last few minutes, so it would leave a stretch unwritten. Check that its database is reachable (Cars and sources), then switch."


def teslafi_spans(conn, vin: str, since: datetime, tz) -> list[tuple[datetime, datetime]]:
    """The drives and charges the TeslaFi feed's raw rows hold since `since`, as its replay would make them."""
    from teslafi_import import replay, sample_from_row

    rows = [tuple(_t(r)) for r in conn.execute("SELECT at, data FROM cc_teslafi_rows WHERE vin = %s AND at >= %s ORDER BY at", (vin, since)).fetchall()]
    samples = [s for s in (sample_from_row(row, tz, when=at) for at, row in rows) if s]
    return spans(replay(samples)) if samples else []


def first_raw(conn, kind: str, vin: str) -> datetime | None:
    """The incoming source's first raw record: before it, it cannot write anything."""
    table = {"tesla": "cc_tesla_raw", "teslafi": "cc_teslafi_rows"}.get(kind)
    if table is None:
        return None  # TeslaMate keeps its own history; Car Command has no raw copy of it to look at
    row = _t(conn.execute(f"SELECT MIN(at) FROM {table} WHERE vin = %s", (vin,)).fetchone())
    return row[0] if row else None
