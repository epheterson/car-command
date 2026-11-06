"""What a Lucid says about itself that is kept: its own charge records, the Electrify America plan on
the car, the drive mode it is in, and the stretches it was in privacy mode.

The charge records (connector 0.5.0's `charging_records`) are matched to our sessions by car and start
time, the way billed.py matches Tesla's bills. A record carries the energy (no unit given), the times and
the charger's name, never a cost. Its energy stands in for the power estimate where the car has no meter of
its own (facts.charge_facts); a charger named Electrify America marks the charge as one, so the car's free
plan (facts.price_ea) can price it. The read is a background thread working from the tree the site already
holds (nothing asked of the connector or of Lucid), never a page load.

Drive modes and privacy mode are stretches like the Sentry and climate ones (runs.extend_or_start), written
by the recorder tick; the drive page reads the modes it was driven in."""

from __future__ import annotations

import logging
import re
import threading
from datetime import date, datetime, timedelta
from typing import Callable

LOG = logging.getLogger("carcommand.lucid")

DDL = """
CREATE TABLE IF NOT EXISTS cc_lucid_charges (
    id         VARCHAR PRIMARY KEY,
    vin        VARCHAR NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    stopped_at TIMESTAMPTZ,
    kwh        DOUBLE PRECISION,
    charger    VARCHAR,
    network    VARCHAR,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cc_lucid_charges_vin_start ON cc_lucid_charges (vin, started_at);
CREATE TABLE IF NOT EXISTS cc_ea_plans (
    vin     VARCHAR PRIMARY KEY,
    name    VARCHAR NOT NULL,
    since   DATE NOT NULL,
    ends_on DATE
);
CREATE TABLE IF NOT EXISTS cc_drive_modes (
    id         BIGSERIAL PRIMARY KEY,
    vin        VARCHAR NOT NULL,
    first_date TIMESTAMPTZ NOT NULL,
    last_date  TIMESTAMPTZ NOT NULL,
    mode       VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS cc_drive_modes_vin_first ON cc_drive_modes (vin, first_date);
CREATE TABLE IF NOT EXISTS cc_privacy (
    id         BIGSERIAL PRIMARY KEY,
    vin        VARCHAR NOT NULL,
    first_date TIMESTAMPTZ NOT NULL,
    last_date  TIMESTAMPTZ NOT NULL,
    private    BOOLEAN NOT NULL
);
CREATE INDEX IF NOT EXISTS cc_privacy_vin_first ON cc_privacy (vin, first_date);
"""

WINDOW = timedelta(minutes=10)  # a record's start and our session's start agree to within this
EVERY_S = 900  # the worker looks at the tree this often
_EA = re.compile(r"electrify\s*america|\bEA\b", re.I)
MODE_WORDS = {"sport-plus": "Sport+", "test-drive": "Test drive", "young-adult": "Young adult", "track-a": "Track A", "track-b": "Track B", "track-c": "Track C"}


def network_of(charger) -> str | None:
    """'electrify_america' for a charger named for it; None for any other or unnamed one."""
    return "electrify_america" if isinstance(charger, str) and _EA.search(charger) else None


def mode_word(mode: str | None) -> str | None:
    """A drive mode as the car's app words it: 'sport-plus' is Sport+, 'comfort' is Comfort."""
    return None if not mode else MODE_WORDS.get(mode, mode.replace("-", " ").capitalize())


def _row(r, *keys):
    return tuple(r[k] for k in keys) if isinstance(r, dict) else tuple(r)


def plan_for(conn, vin: str) -> dict | None:
    """The car's Electrify America plan as the pricer wants it: name, the day it was first seen (charges before
    it are not priced by it) and the day it ends; None for a car with none."""
    rows = conn.execute("SELECT name, since, ends_on FROM cc_ea_plans WHERE vin = %s", (vin,)).fetchall()
    if not rows:
        return None
    name, since, ends = _row(rows[0], "name", "since", "ends_on")
    return {"name": name, "since": since, "ends": ends}


def keep_plan(conn, vin: str, name: str | None, ends: date | None, today: date) -> bool:
    """Remember the car's plan. True when it is new or its end moved, so the charges at Electrify America
    since it began are priced again. A car that stops saying (no name) keeps what it had."""
    if not name:
        return False
    got = conn.execute(
        """INSERT INTO cc_ea_plans (vin, name, since, ends_on) VALUES (%s, %s, %s, %s)
           ON CONFLICT (vin) DO UPDATE SET name = EXCLUDED.name, ends_on = EXCLUDED.ends_on
           WHERE cc_ea_plans.name IS DISTINCT FROM EXCLUDED.name OR cc_ea_plans.ends_on IS DISTINCT FROM EXCLUDED.ends_on
           RETURNING vin""",
        (vin, name, today, ends),
    ).fetchone()
    return got is not None


def store(conn, vin: str, records: list[dict]) -> list[str]:
    """Insert or refresh the car's records; the ids of those new or whose energy or charger changed."""
    changed = []
    for r in records:
        got = conn.execute(
            """INSERT INTO cc_lucid_charges (id, vin, started_at, stopped_at, kwh, charger, network, fetched_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, now())
               ON CONFLICT (id) DO UPDATE SET stopped_at = EXCLUDED.stopped_at, kwh = EXCLUDED.kwh, charger = EXCLUDED.charger, network = EXCLUDED.network, fetched_at = now()
               WHERE cc_lucid_charges.kwh IS DISTINCT FROM EXCLUDED.kwh OR cc_lucid_charges.charger IS DISTINCT FROM EXCLUDED.charger OR cc_lucid_charges.stopped_at IS DISTINCT FROM EXCLUDED.stopped_at
               RETURNING id""",
            (r["id"], vin, r["start"], r["stop"], r["kwh"], r["charger"], network_of(r["charger"])),
        ).fetchone()
        if got:
            changed.append(r["id"])
    return changed


def redo_for(conn, record_ids: list[str]) -> list[int]:
    """Our sessions these records match whose facts were taken before the record was known: the energy is set
    where the car had no meter (and the record's size is believable) and the ids come back to be priced again."""
    if not record_ids:
        return []
    rows = conn.execute(
        """SELECT c.id, r.kwh, f.kwh AS had, (c.real_charged IS NULL AND m.kwh IS NULL) AS unmetered
           FROM cc_lucid_charges r
           JOIN charging_sessions c ON c.vin = r.vin AND c.session_start_date BETWEEN r.started_at - interval '10 minutes' AND r.started_at + interval '10 minutes'
           JOIN cc_charge_facts f ON f.session_id = c.id
           LEFT JOIN cc_charge_meter m ON m.session_id = c.id
           WHERE r.id = ANY(%s)""",
        (record_ids,),
    ).fetchall()
    import lucidfields

    ids = []
    for row in rows:
        sid, kwh, had, unmetered = _row(row, "id", "kwh", "had", "unmetered")
        if unmetered and lucidfields.plausible_kwh(kwh, had or 0.0):
            conn.execute("UPDATE cc_charge_facts SET kwh = %s WHERE session_id = %s", (kwh, sid))
        ids.append(sid)
    return ids


def plan_sessions(conn, vin: str) -> list[int]:
    """Our sessions at Electrify America (by Lucid's record) for this car: the ones a plan changing is a reason to price again."""
    rows = conn.execute(
        """SELECT DISTINCT c.id FROM cc_lucid_charges r
           JOIN charging_sessions c ON c.vin = r.vin AND c.session_start_date BETWEEN r.started_at - interval '10 minutes' AND r.started_at + interval '10 minutes'
           WHERE r.vin = %s AND r.network = 'electrify_america'""",
        (vin,),
    ).fetchall()
    return [_row(r, "id")[0] for r in rows]


class RecordsWorker(threading.Thread):
    """Reads the charge records and the plan off the tree the site already holds and keeps them.

    `tree()` -> {vin: car tree} (the held one, no call to the connector), `read(tree)` -> (records, plan name,
    plan end) for one car, `clear(conn, ids)` puts sessions back in the queue to be priced (facts.clear_prices)."""

    def __init__(self, connect: Callable, tree: Callable, read: Callable, clear: Callable, today: Callable[[], date]) -> None:
        super().__init__(daemon=True, name="lucid-records")
        self.connect, self.tree, self.read, self.clear, self.today = connect, tree, read, clear, today
        self.stop = threading.Event()

    def round(self) -> int:
        """One pass over every car on the tree; how many sessions were put back to be priced."""
        redo = 0
        for vin, car in (self.tree() or {}).items():
            records, plan, ends = self.read(car)
            if not records and not plan:
                continue
            with self.connect() as conn:
                changed = store(conn, vin, records)
                ids = redo_for(conn, changed)
                if keep_plan(conn, vin, plan, ends, self.today()):
                    ids += plan_sessions(conn, vin)
                if ids:
                    self.clear(conn, sorted(set(ids)))
                    redo += len(set(ids))
                conn.commit()
        return redo

    def run(self) -> None:
        self.stop.wait(240)  # let startup, and the first read of the connector, settle
        while not self.stop.is_set():
            try:
                self.round()
            except Exception as err:  # noqa: BLE001 - a bad pass waits for the next one
                LOG.warning("lucid records: %s", err)
            self.stop.wait(EVERY_S)


def modes_between(conn, vin: str, start: datetime, end: datetime) -> list[str]:
    """The drive modes the car was in between these times, in order, each once (a mode left and come back to
    reads twice): for a drive's page. One indexed look at a small table."""
    rows = conn.execute(
        "SELECT mode FROM cc_drive_modes WHERE vin = %s AND last_date >= %s AND first_date <= %s ORDER BY first_date",
        (vin, start, end),
    ).fetchall()
    out: list[str] = []
    for r in rows:
        mode = _row(r, "mode")[0]
        if not out or out[-1] != mode:
            out.append(mode)
    return out


def private_between(conn, vin: str, start: datetime, end: datetime) -> bool:
    """Whether the car was in privacy mode at any time in this span: what to call a gap in its data."""
    row = conn.execute(
        "SELECT 1 FROM cc_privacy WHERE vin = %s AND private AND last_date >= %s AND first_date <= %s LIMIT 1",
        (vin, start, end),
    ).fetchone()
    return row is not None


def private_hours(conn, vin: str, start: datetime, end: datetime) -> float:
    """Hours of this span the car spent in privacy mode, for the day's tiles: what a quiet stretch was, rather than no signal."""
    row = conn.execute(
        """SELECT COALESCE(SUM(EXTRACT(EPOCH FROM (LEAST(last_date, %s) - GREATEST(first_date, %s)))), 0) / 3600 AS h
           FROM cc_privacy WHERE vin = %s AND private AND last_date > %s AND first_date < %s""",
        (end, start, vin, start, end),
    ).fetchone()
    return float(_row(row, "h")[0] or 0) if row else 0.0
