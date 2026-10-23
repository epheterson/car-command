"""The stream beside the car's writer: what Tesla's own stream would make of a window, set
against what the source that writes the car's history stored for it.

Nothing is written. The stream side is the hourly rebuild's working-out (tesla_live), run
over the raw records in memory; the stored side is read from trips, charging_sessions,
drive_ranges and states. Both go through the same arithmetic: miles from the odometer,
kWh used from rated range spent times the car's measured kWh per rated mile (the figure
every page uses), kWh added from the charge's meter, sleep from the OFFLINE stretches.

Also the evidence the planned outage test is read from, per day: records, resent records
(isResend), the times the car said goodbye (DISCONNECTED), and the silences it did not
announce (no record for longer than the drive or parked grace, which is what a receiver
outage looks like from here).

Heavy (a week is up to a few hundred thousand records), so it runs in a background thread
and its result is kept for TTL; a page only ever reads what is kept."""

from __future__ import annotations

import driveedits
import json
import logging
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import tesla_live
import tesla_stream
from history import _t
from teslafi_import import replay

LOG = logging.getLogger("carcommand.tesla")
DAYS = 7
MAX_DAYS = 14
LEAD = timedelta(hours=12)  # records read before the window, so a drive under way at its start is whole
MATCH = timedelta(minutes=5)  # a stream session ending this close to the window's end is still under way
TTL_S = 900
MILES_OFF = (0.5, 0.03)  # a drive's miles disagree past 0.5 mi and 3 %
KWH_OFF = (0.5, 0.03)


def window(now: datetime, days: int, first: datetime | None) -> tuple[datetime, datetime] | None:
    """[lo, hi): the last `days` days (1 to MAX_DAYS), from no earlier than the stream's first record."""
    if first is None:
        return None
    days = max(1, min(MAX_DAYS, int(days)))
    return max(now - timedelta(days=days), first), now


# ---- the stream's side
def stream_out(payloads: list[dict], until: datetime) -> dict:
    """What the hourly rebuild would write from these records, without writing it."""
    recs = tesla_live.with_silence(tesla_stream.records(payloads), until=until)
    samples = tesla_stream.samples(recs, until=until)
    return replay(samples) if samples else {"trips": [], "charging_sessions": [], "states": [], "drive_ranges": []}


def range_at(runs: list[tuple], t: datetime) -> float | None:
    """The value of a stretch table at `t`: the stretch holding it, else the last one before."""
    best = None
    for a, b, v in runs:
        if a <= t:
            if v is not None:
                best = v
            if t <= b:
                break
        else:
            break
    return best


def asleep_hours(states: list[tuple], lo: datetime, hi: datetime) -> float:
    s = sum(max(0.0, (min(b, hi) - max(a, lo)).total_seconds()) for a, b, v in states if v == "OFFLINE")
    return round(s / 3600, 2)


def drives(trips: list[dict], ranges: list[tuple], k: float | None, lo: datetime, hi: datetime) -> list[dict]:
    out = []
    for t in sorted(trips, key=lambda x: x["start_date"]):
        if not lo <= t["start_date"] < hi:
            continue
        a, b = t.get("start_odometer"), t.get("destination_odometer")
        r0, r1 = range_at(ranges, t["start_date"]), range_at(ranges, t["destination_date"])
        kwh = round((r0 - r1) * k, 2) if k and r0 is not None and r1 is not None and r0 >= r1 else None
        out.append({"start": t["start_date"], "end": t["destination_date"], "miles": round(b - a, 2) if a is not None and b is not None else None, "kwh": kwh})
    return out


def charges(sessions: list[dict], lo: datetime, hi: datetime) -> list[dict]:
    return [
        {"start": c["session_start_date"], "end": c["session_end_date"], "kwh": round(c["real_charged"], 2) if c.get("real_charged") is not None else None, "type": c.get("charging_type")}
        for c in sorted(sessions, key=lambda x: x["session_start_date"])
        if lo <= c["session_start_date"] < hi
    ]


def side(trips: list[dict], sessions: list[dict], ranges: list[tuple], states: list[tuple], k: float | None, lo: datetime, hi: datetime) -> dict:
    d, c = drives(trips, ranges, k, lo, hi), charges(sessions, lo, hi)
    return {
        "drives": d,
        "charges": c,
        "totals": {
            "drives": len(d),
            "miles": round(sum(x["miles"] or 0 for x in d), 1),
            "kwh_used": round(sum(x["kwh"] or 0 for x in d), 1),
            "charges": len(c),
            "kwh_added": round(sum(x["kwh"] or 0 for x in c), 1),
            "asleep_h": asleep_hours(states, lo, hi),
        },
    }


def _off(a: float | None, b: float | None, limits: tuple[float, float]) -> bool:
    """Past both limits. A side with no value counts as none: a drive the writer never saw,
    a 0.09 kWh charge it read as nothing, are within the limits of zero."""
    a, b = a or 0.0, b or 0.0
    return abs(a - b) > limits[0] and abs(a - b) > limits[1] * max(abs(a), abs(b))


def _merged(rows: list[dict], value: str) -> dict | None:
    """One side of a group as one session: from the first start to the last end, numbers summed."""
    if not rows:
        return None
    m = {**rows[0], "end": max(r["end"] for r in rows), "n": len(rows)}
    for k, v in rows[0].items():
        if k not in ("start", "end") and (v is None or isinstance(v, (int, float))):
            vals = [r[k] for r in rows if r.get(k) is not None]
            m[k] = round(sum(vals), 2) if vals else None
    return m


def match(stream: list[dict], stored: list[dict], value: str, limits: tuple[float, float], hi: datetime | None = None, joins_splits: bool = False) -> list[dict]:
    """Sessions grouped by overlap, one row per group, in time order.

    A drive one side has as one and the other as two is one group and one difference, not
    two. A group is `off` when one side is missing it or its summed `value` disagrees past
    `limits`; for drives also when the sides split it differently. With `joins_splits`
    (charges) a split is no difference: a cable out for seconds splits a charge on one side
    only. A group still under way at `hi` is `under_way`, never off: the writer has not
    caught up with it yet."""
    tagged = sorted([(r["start"], 0, r) for r in stream] + [(r["start"], 1, r) for r in stored], key=lambda x: (x[0], x[1]))
    groups: list[tuple[list, list, datetime]] = []
    for start, which, r in tagged:
        if groups and start <= groups[-1][2]:
            g = groups[-1]
            groups[-1] = (g[0] + [r] * (which == 0), g[1] + [r] * (which == 1), max(g[2], r["end"]))
        else:
            groups.append(([r] * (which == 0), [r] * (which == 1), r["end"]))
    out = []
    for mine, theirs, end in groups:
        s, w = _merged(mine, value), _merged(theirs, value)
        under_way = hi is not None and s is not None and s["end"] >= hi - MATCH
        split = s is not None and w is not None and s["n"] != w["n"] and not joins_splits
        p = {"at": (s or w)["start"], "stream": s, "stored": w, "under_way": under_way, "off": not under_way and (split or _off(s and s[value], w and w[value], limits))}
        if s and w:
            p["dt_s"] = int((s["start"] - w["start"]).total_seconds())
        out.append(p)
    return out


# ---- the evidence of gaps
def gaps(payloads: list[dict], lo: datetime, hi: datetime, tz) -> list[dict]:
    """Per local day in [lo, hi): records, resent ones, goodbyes (DISCONNECTED), reconnects,
    the silences nobody announced (no record past the drive or parked grace, and no goodbye:
    what a receiver outage looks like) and the longest of those, from the last record before
    it to the next one."""
    days: dict = defaultdict(lambda: {"records": 0, "resent": 0, "disconnects": 0, "reconnects": 0, "silences": 0, "longest_silence_min": 0.0})
    timed = []
    for p in payloads:
        stamp = p.get("createdAt") or p.get("created_at")
        if not stamp or not p.get("vin"):
            continue
        t = tesla_stream.when(stamp)
        timed.append(p)
        if not lo <= t < hi:
            continue
        d = days[t.astimezone(tz).date()]
        d["records"] += 1
        d["resent"] += bool(p.get("isResend"))
        status = str(p.get("status") or "").upper() if "data" not in p else ""
        d["disconnects"] += status == "DISCONNECTED"
        d["reconnects"] += status == "CONNECTED"
    for start, end in silences(timed, hi):
        if lo <= start < hi:
            d = days[start.astimezone(tz).date()]
            d["silences"] += 1
            d["longest_silence_min"] = max(d["longest_silence_min"], round((end - start).total_seconds() / 60, 1))
    return [{"day": k, **v} for k, v in sorted(days.items())]


def silences(payloads: list[dict], until: datetime) -> list[tuple[datetime, datetime]]:
    """The stretches nobody announced, as (the last record before, the next record or
    `until`): no record past the drive or parked grace and no goodbye, which is what an
    outage of the receiver, or of the car's connection, looks like from here."""
    recs = tesla_stream.records(payloads)
    real = {id(r[2]) for r in recs}
    out, last, start = [], None, None
    for t, _vin, fields in tesla_live.with_silence(recs, until=until):
        if id(fields) not in real:  # a disconnect with_silence put in: nothing came for too long
            start = last
            continue
        if start is not None:
            out.append((start, t))
        start, last = None, t
    if start is not None:
        out.append((start, until))  # still silent at the end
    return out


# ---- reading
def raw(conn, vin: str, lo: datetime, hi: datetime) -> list[dict]:
    rows = conn.execute("SELECT record FROM cc_tesla_raw WHERE vin = %s AND at >= %s AND at < %s AND kind IN ('V', 'connectivity') ORDER BY at", (vin, lo, hi)).fetchall()
    out = []
    for r in rows:
        p = r["record"] if isinstance(r, dict) else r[0]
        out.append(p if isinstance(p, dict) else json.loads(p))
    return out


def per_rated_mile(conn, vin: str) -> float | None:
    """The car's measured kWh per rated mile (Settings' figure); None until it has charged enough to measure."""
    row = _t(conn.execute("SELECT kwh_per_rated_mi FROM cc_vehicle_facts WHERE vin = %s", (vin,)).fetchone())
    return float(row[0]) if row and row[0] else None


def stored(conn, vin: str, lo: datetime, hi: datetime) -> dict:
    trips = [
        dict(zip(("start_date", "destination_date", "start_odometer", "destination_odometer"), _t(r)))
        for r in conn.execute(
            "SELECT start_date, destination_date, start_odometer, destination_odometer FROM trips t WHERE vin = %s AND start_date >= %s AND start_date < %s"
            + driveedits.ABSORBED
            + " ORDER BY start_date",
            (vin, lo, hi),
        ).fetchall()
    ]
    sessions = [
        dict(zip(("session_start_date", "session_end_date", "real_charged", "charging_type"), _t(r)))
        for r in conn.execute(
            "SELECT session_start_date, session_end_date, real_charged, charging_type FROM charging_sessions WHERE vin = %s AND session_start_date >= %s AND session_start_date < %s ORDER BY session_start_date",
            (vin, lo, hi),
        ).fetchall()
    ]
    ranges = [
        tuple(_t(r))
        for r in conn.execute(
            "SELECT first_date, last_date, range FROM drive_ranges WHERE drive_id IN (SELECT id FROM drives WHERE vin = %s) AND last_date >= %s AND first_date <= %s ORDER BY first_date",
            (vin, lo - LEAD, hi),
        ).fetchall()
    ]
    states = [
        tuple(_t(r)) for r in conn.execute("SELECT first_date, last_date, state FROM states WHERE vin = %s AND last_date >= %s AND first_date <= %s ORDER BY first_date", (vin, lo, hi)).fetchall()
    ]
    return {"trips": trips, "charging_sessions": sessions, "drive_ranges": ranges, "states": states}


def compare(conn, vin: str, now: datetime, days: int, tz) -> dict | None:
    """The comparison for the last `days` days, or None when the stream has no records for this car."""
    first = _t(conn.execute("SELECT MIN(at) FROM cc_tesla_raw WHERE vin = %s", (vin,)).fetchone())
    w = window(now, days, first[0] if first else None)
    if w is None:
        return None
    lo, hi = w
    payloads = raw(conn, vin, lo - LEAD, hi)
    k = per_rated_mile(conn, vin)
    out = stream_out(payloads, hi)
    mine = side(out["trips"], out["charging_sessions"], out["drive_ranges"], out["states"], k, lo, hi)
    st = stored(conn, vin, lo, hi)
    theirs = side(st["trips"], st["charging_sessions"], st["drive_ranges"], st["states"], k, lo, hi)
    return {
        "vin": vin,
        "lo": lo,
        "hi": hi,
        "days": round((hi - lo).total_seconds() / 86400, 1),
        "stream": mine["totals"],
        "stored": theirs["totals"],
        "drives": match(mine["drives"], theirs["drives"], "miles", MILES_OFF, hi=hi),
        "charges": match(mine["charges"], theirs["charges"], "kwh", KWH_OFF, hi=hi, joins_splits=True),
        "gaps": gaps(payloads, lo, hi, tz),
        "kwh_per_rated_mi": k,
        "computed_at": now,
    }


def incoming_spans(conn, vin: str, since: datetime, now: datetime) -> list[tuple[datetime, datetime]]:
    """The stream's own drives and charges since `since`: what a handoff to it must not cut."""
    import history_writer

    return history_writer.spans(stream_out(raw(conn, vin, since - LEAD, now), now))


class Comparer:
    """One comparison per (car, days) at a time, in a thread; kept TTL_S."""

    def __init__(self, connect, tz, clock=None, ttl_s: int = TTL_S, spawn=None) -> None:
        self.connect, self.tz, self.ttl_s = connect, tz, ttl_s
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True, name="tesla-compare").start())
        self.kept: dict[tuple[str, int], tuple[datetime, dict | None, str | None]] = {}
        self.running: set[tuple[str, int]] = set()
        self.lock = threading.Lock()

    def get(self, vin: str, days: int = DAYS, start: bool = True) -> dict:
        """{"result", "error", "at", "running"}: what is kept, and whether a fresher one is on its way."""
        key = (vin, max(1, min(MAX_DAYS, int(days))))
        now = self.clock()
        with self.lock:
            hit = self.kept.get(key)
            stale = hit is None or (now - hit[0]).total_seconds() >= self.ttl_s
            if stale and start and key not in self.running:
                self.running.add(key)
                self.spawn(lambda: self.run(*key))
            running = key in self.running
        at, result, error = hit or (None, None, None)
        return {"result": result, "error": error, "at": at, "running": running}

    def forget(self, vin: str) -> None:
        with self.lock:
            for key in [k for k in self.kept if k[0] == vin]:
                del self.kept[key]

    def run(self, vin: str, days: int) -> None:
        result = error = None
        try:
            with self.connect() as conn:
                result = compare(conn, vin, self.clock(), days, self.tz)
        except Exception as err:  # noqa: BLE001 - said on the page, never raised into a thread
            LOG.warning("tesla comparison for %s: %s", vin, err)
            error = f"{type(err).__name__}: {err}"[:200]
        with self.lock:
            self.kept[(vin, days)] = (self.clock(), result, error)
            self.running.discard((vin, days))
