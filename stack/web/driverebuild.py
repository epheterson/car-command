"""Drives rebuilt from a connector car's own readings: its `states` rows and `cc_positions`.

The database plugin opens a trip when the car enters IGNITION_ON/DRIVING and closes it on the
way out, but also on DRIVING -> IGNITION_ON, a move inside that set. A car that stops for
half a minute with the ignition on (a light, a toll) loses the rest of the drive: the next
IGNITION_ON -> DRIVING opens nothing. A car out of cell service keeps its last state and
sends no readings, so a dead zone is one long "drive" with a hole in it. This module works the
drives out again from what was recorded and puts `trips` right, keeping every drive id that is
already right.

A drive breaks at:
- PARKED, OFFLINE or any state that is not IGNITION_ON/DRIVING;
- an IGNITION_ON of 5 minutes or more, or one a charge overlaps;
- a gap of 10 minutes or more between readings: the odometer step across it is unlogged
  miles, belonging to neither drive, and no line is drawn over it;
- a stationary run of 5 minutes or more (same odometer, no speed).
Shorter stops stay inside the drive.

A trip the plugin wrote that never moved is not a drive: the car was on and parked
(odometer step under MIN_MI, no speed above zero, both ends within STILL_M of each other, and
no drive worked out over it). The rebuild removes it, hand-entered drives excepted, and the
time becomes a "car on" stretch (`cc_car_on`): the car on, parked. The same goes for any
ignition-on time no drive holds, once the readings in it say the car stood.

Imported (TeslaFi, TeslaMate) cars are not touched: their sources number their own drives.

Run in the background by app.py after readings are written, and by hand as
`make rebuild-drives FROM=2026-09-04 [VIN=...] [ARGS=--dry-run]`.
"""

from __future__ import annotations

import argparse
import bisect
import logging
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import driveedits
import history
from manual import DDL as MANUAL_DDL

LOG = logging.getLogger("driverebuild")
TZ = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))

ACTIVE = ("DRIVING", "IGNITION_ON")
BREAK_IGNITION = timedelta(minutes=5)
GAP = timedelta(minutes=10)
STILL = timedelta(minutes=5)
STILL_MI = 0.05  # an odometer step smaller than this is the car standing
MIN_MI = 0.05  # a piece that moved less than this is not a drive
HOLE = timedelta(minutes=10)  # state rows further apart than this are not one run
LEAVE = timedelta(minutes=2)  # a drive ends where the state changed, if that was this soon after the last DRIVING row
EDGE = timedelta(seconds=60)  # readings this close outside a drive's states still belong to it
SETTLE = timedelta(minutes=10)  # a drive that ended this recently may still be getting its last readings
END_TOL = timedelta(minutes=3)
END_LONG = timedelta(minutes=20)  # a trip that runs this much past the drive's last reading only sat there with the state still on; its end stands
START_LATE = timedelta(minutes=3)
START_EARLY = timedelta(minutes=45)  # the plugin starts a trip at IGNITION_ON, however long before the car moves
TO_DO = ("create", "change", "merge", "remove")  # what writes to `trips`
MERGE_SHARE = 0.5  # a trip this much inside a wanted drive is part of it
STILL_M = 10.0  # readings this close together (GPS wander of a parked car) are one place
EDGE = timedelta(minutes=3)  # readings this close outside a car-on run still say whether it stood
CAR_ON_MIN = timedelta(minutes=5)  # the car on and standing for this long, with no drive, is car-on time


def _span(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds()


def runs(states: list[dict]) -> list[tuple[list[dict], datetime | None]]:
    """Consecutive IGNITION_ON/DRIVING rows, each run with the time of the state that followed it."""
    out, cur = [], []
    for i, s in enumerate(states):
        if s["state"] in ACTIVE:
            if cur and s["first"] - (cur[-1]["last"] or cur[-1]["first"]) > HOLE:
                out.append((cur, None))
                cur = []
            cur.append(s)
        elif cur:
            out.append((cur, s["first"]))
            cur = []
    if cur:
        out.append((cur, None))
    return out


def segments(states: list[dict], charges: list[tuple[datetime, datetime]]) -> list[dict]:
    """The stretches of one drive's states: start, end, and whether the car is still in it."""
    out = []
    for rows, after in runs(states):
        ends = [rows[k + 1]["first"] if k + 1 < len(rows) else (after or rows[k]["last"] or rows[k]["first"]) for k in range(len(rows))]
        open_run = after is None
        cur: list[int] = []

        def flush(cur=cur, rows=rows, ends=ends, open_run=open_run):
            drive = [k for k in cur if rows[k]["state"] == "DRIVING"]
            if drive:
                kd = drive[-1]
                last = rows[kd]["last"] or rows[kd]["first"]
                end = min(ends[kd], last + LEAVE)
                out.append({"start": rows[cur[0]]["first"], "end": end, "open": open_run and kd == len(rows) - 1})
            cur.clear()

        for k, r in enumerate(rows):
            long_stop = r["state"] == "IGNITION_ON" and (ends[k] - r["first"] >= BREAK_IGNITION or any(a < ends[k] and b > r["first"] for a, b in charges))
            if long_stop:
                flush()
            else:
                cur.append(k)
        flush()
    return out


def pieces(fx: list[dict]) -> tuple[list[tuple[int, int]], dict[int, dict]]:
    """Index ranges of readings that are one drive each, and for each range after a dead
    stretch what that stretch left unlogged: {first index: {"miles", "minutes"}}."""
    cuts: list[tuple[int, int]] = []
    gaps: dict[int, dict] = {}
    i0 = i1 = None

    def close():
        nonlocal i0, i1
        if i0 is not None and fx[i1]["at"] - fx[i0]["at"] >= STILL:
            cuts.append((i0, i1))
        i0 = i1 = None

    for k in range(1, len(fx)):
        a, b = fx[k - 1], fx[k]
        step = None if a["odo"] is None or b["odo"] is None else b["odo"] - a["odo"]
        if b["at"] - a["at"] >= GAP:
            close()
            cuts.append((k - 1, k))
            gaps[k] = {"miles": max(step or 0.0, 0.0), "minutes": _span(a["at"], b["at"]) / 60}
        elif step is not None and abs(step) < STILL_MI and (b["speed"] or 0) <= 1:
            if i0 is None:
                i0 = k - 1
            i1 = k
        else:
            close()
    close()
    out, start = [], 0
    for i, j in sorted(cuts):
        if i >= start:
            out.append((start, i))
        start = max(start, j)
    out.append((start, len(fx) - 1))
    return out, gaps


def plan_drives(states: list[dict], fixes: list[dict], charges: list[tuple[datetime, datetime]] = (), now: datetime | None = None) -> list[dict]:
    """The drives these states and readings add up to, oldest first. Each has start, end,
    odometers and positions at both ends, miles, and `unlogged` ({"miles", "minutes"}) for the
    dead stretch just before it, if any. A drive still going, or that ended within SETTLE of
    `now`, is left out."""
    states = sorted(states, key=lambda s: s["first"])
    fixes = sorted((f for f in fixes if f["lat"] is not None and f["lon"] is not None), key=lambda f: f["at"])
    times = [f["at"] for f in fixes]
    out = []
    for seg in segments(states, list(charges)):
        if seg["open"] or (now is not None and seg["end"] > now - SETTLE):
            continue
        fx = fixes[bisect.bisect_left(times, seg["start"] - EDGE) : bisect.bisect_right(times, seg["end"] + EDGE)]
        if len(fx) < 2:
            continue
        ranges, gaps = pieces(fx)
        for n, (s, e) in enumerate(ranges):
            if s == e:
                continue
            a, b = fx[s], fx[e]
            miles = None if a["odo"] is None or b["odo"] is None else b["odo"] - a["odo"]
            start = seg["start"] if n == 0 else a["at"]
            end = seg["end"] if n == len(ranges) - 1 else b["at"]
            if (miles is not None and miles < MIN_MI) or (miles is None and end - start < timedelta(minutes=2)):
                continue
            out.append(
                {
                    "start": start,
                    "end": end,
                    "start_odometer": a["odo"],
                    "destination_odometer": b["odo"],
                    "start_lat": a["lat"],
                    "start_lon": a["lon"],
                    "end_lat": b["lat"],
                    "end_lon": b["lon"],
                    "miles": miles,
                    "unlogged": gaps.get(s),
                }
            )
    return out


def _overlap(e: dict, w: dict) -> float:
    return max(0.0, _span(max(e["start"], w["start"]), min(e["end"], w["end"])))


def plan_changes(wanted: list[dict], existing: list[dict]) -> list[dict]:
    """What to do to `trips` (existing: id, start, end or None, manual) to match `wanted`.
    Actions, oldest first: keep, create, change (the best-overlapping trip takes the wanted
    drive's end, and its start when it begins late or far early), merge (another trip inside
    the wanted drive goes), hand (a hand-entered drive already covers it), wait (a trip that
    overlaps is still open). A trip no wanted drive overlaps is left alone."""
    closed = [e for e in existing if e["end"] is not None]
    taken: set[int] = set()
    out = []
    for w in wanted:
        if any(e["manual"] and _overlap(e, w) > 0 for e in closed):
            out.append({"do": "hand", "drive": w})
            continue
        if any(e["end"] is None and e["start"] < w["end"] for e in existing):
            out.append({"do": "wait", "drive": w})
            continue
        near = [e for e in closed if not e["manual"] and e["id"] not in taken and _overlap(e, w) > 0]
        if not near:
            out.append({"do": "create", "drive": w})
            continue
        main = max(near, key=lambda e: (_overlap(e, w), -e["start"].timestamp()))
        taken.add(main["id"])
        start_ok = w["start"] - START_EARLY <= main["start"] <= w["start"] + START_LATE
        swallows = any(w2 is not w and w["end"] <= w2["start"] < main["end"] for w2 in wanted)
        end_ok = -END_TOL <= (main["end"] - w["end"]) <= (END_TOL if swallows else END_LONG)
        if start_ok and end_ok:
            out.append({"do": "keep", "id": main["id"], "drive": w})
        else:
            out.append({"do": "change", "id": main["id"], "drive": w, "start": main["start"] if start_ok else w["start"], "was": (main["start"], main["end"])})
        for e in near:
            if e is not main and _overlap(e, w) >= MERGE_SHARE * max(_span(e["start"], e["end"]), 1.0):
                taken.add(e["id"])
                out.append({"do": "merge", "id": e["id"], "into": main["id"], "start": e["start"], "end": e["end"]})
    return out


def _metres(lat0: float, lon0: float, lat1: float, lon1: float) -> float:
    dy = (lat1 - lat0) * 111_320.0
    dx = (lon1 - lon0) * 111_320.0 * math.cos(math.radians((lat0 + lat1) / 2))
    return math.hypot(dx, dy)


def stood_still(fx: list[dict], odos: tuple = (), spots: tuple = ()) -> bool:
    """Whether these readings are a car that did not move: at least two, no odometer step of
    MIN_MI or more across them (and `odos`, the trip's own), no speed above zero, and every
    position (and `spots`, the trip's own ends) within STILL_M of the first. Anything less
    certain is not "still": a missing odometer or position counts for nothing either way. A trip
    whose own two ends carry an odometer and a position is itself two readings, so one fix inside
    it is enough (the plugin's 48-minute trip of 10-21 had one: the car on, polled rarely)."""
    ends = len(odos) == 2 and all(x is not None for x in odos) and len(spots) == 2 and all(p[0] is not None and p[1] is not None for p in spots)
    if len(fx) < (1 if ends else 2):
        return False
    odo = [x for x in [f["odo"] for f in fx] + list(odos) if x is not None]
    if odo and max(odo) - min(odo) >= MIN_MI:
        return False
    if any((f["speed"] or 0) > 0 for f in fx):
        return False
    pts = [(f["lat"], f["lon"]) for f in fx] + [p for p in spots if p[0] is not None and p[1] is not None]
    return all(_metres(*pts[0], *p) <= STILL_M for p in pts[1:])


def _sorted_fixes(fixes: list[dict]) -> tuple[list[dict], list[datetime]]:
    fixes = sorted((f for f in fixes if f["lat"] is not None and f["lon"] is not None), key=lambda f: f["at"])
    return fixes, [f["at"] for f in fixes]


def phantom_trips(trips: list[dict], fixes: list[dict], wanted: list[dict], now: datetime | None = None) -> list[dict]:
    """Trips the plugin wrote that are not drives, as `remove` actions: closed, not entered by
    hand, not just ended, no wanted drive over any of it (a trip that is the start or a stop
    of a real drive is plan_changes' to keep or change), and the car stood still the whole
    time. A trip with fewer than two readings in it is left: nothing says it did not move."""
    fixes, times = _sorted_fixes(fixes)
    out = []
    for t in trips:
        if t["manual"] or t["end"] is None or (now is not None and t["end"] > now - SETTLE):
            continue
        if any(w["start"] < t["end"] and w["end"] > t["start"] for w in wanted):
            continue
        fx = fixes[bisect.bisect_left(times, t["start"]) : bisect.bisect_right(times, t["end"])]
        if stood_still(fx, (t.get("odo0"), t.get("odo1")), ((t.get("lat0"), t.get("lon0")), (t.get("lat1"), t.get("lon1")))):
            out.append({"do": "remove", "id": t["id"], "start": t["start"], "end": t["end"]})
    return out


def _less(span: tuple[datetime, datetime], drives: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """What is left of `span` outside every one of `drives`."""
    left = [span]
    for a, b in drives:
        left = [q for p in left for q in ((p[0], min(p[1], a)), (max(p[0], b), p[1])) if q[1] > q[0]]
    return left


def car_on(states: list[dict], fixes: list[dict], drives: list[tuple[datetime, datetime]], now: datetime | None = None) -> list[dict]:
    """The time the car was on and parked (the states say the car was on, not that anyone was in it): each run of IGNITION_ON/DRIVING
    states, less the spans of `drives` (every trip that stays and every drive wanted), that is
    CAR_ON_MIN or longer and whose readings say the car stood. A run still going, or ended
    within SETTLE of `now`, is left out. Oldest first, {"do": "caron", "start", "end"}."""
    fixes, times = _sorted_fixes(fixes)
    out = []
    for rows, after in runs(sorted(states, key=lambda s: s["first"])):
        if after is None or (now is not None and after > now - SETTLE):
            continue
        for a, b in _less((rows[0]["first"], after), drives):
            # the readings either side count too: a car on and still is polled rarely
            if b - a >= CAR_ON_MIN and stood_still(fixes[bisect.bisect_left(times, a - EDGE) : bisect.bisect_right(times, b + EDGE)]):
                out.append({"do": "caron", "start": a, "end": b})
    return out


def line(a: dict, tz=TZ) -> str:
    """One action as a line for the dry run."""
    w = a.get("drive")

    def at(t):
        return t.astimezone(tz).strftime("%m-%d %H:%M")

    if a["do"] == "merge":
        return f"merge   #{a['id']} {at(a['start'])}-{at(a['end'])} into #{a['into']}"
    if a["do"] == "remove":
        return f"remove  #{a['id']} {at(a['start'])}-{at(a['end'])} never moved"
    if a["do"] == "caron":
        return f"car on  {at(a['start'])}-{at(a['end'])} {_span(a['start'], a['end']) / 60:.0f} min parked"
    miles = "" if w["miles"] is None else f" {w['miles']:.1f} mi"
    lost = ""
    if w.get("unlogged"):
        lost = f"  [{w['unlogged']['miles']:.1f} mi unlogged over {w['unlogged']['minutes']:.0f} min before]"
    if a["do"] == "change":
        return f"change  #{a['id']} {at(a['was'][0])}-{at(a['was'][1])} -> {at(a['start'])}-{at(w['end'])}{miles}{lost}"
    tag = {"create": "create ", "keep": "keep   ", "hand": "by hand", "wait": "waiting"}[a["do"]]
    return f"{tag} {'#' + str(a['id']) + ' ' if a.get('id') else ''}{at(w['start'])}-{at(w['end'])}{miles}{lost}"


# --- the database side --------------------------------------------------------------------------


def connector_cars(conn, since: datetime, vin: str | None = None) -> list[tuple[str, str]]:
    """Cars whose drives the database plugin writes: more live readings than imported ones
    since `since`. A car on a TeslaFi or TeslaMate feed keeps its source's drive numbers."""
    rows = conn.execute(
        """SELECT v.vin, COALESCE(v.name, v.vin) AS name FROM vehicles v
            WHERE (%s::text IS NULL OR v.vin = %s)
              AND (SELECT count(*) FROM cc_positions p WHERE p.vin = v.vin AND p.at >= %s AND p.import_id IS NULL)
                > (SELECT count(*) FROM cc_positions p WHERE p.vin = v.vin AND p.at >= %s AND p.import_id IS NOT NULL)
              AND EXISTS (SELECT 1 FROM states s WHERE s.vin = v.vin AND s.last_date >= %s)
            ORDER BY v.vin""",
        (vin, vin, since, since, since),
    ).fetchall()
    return [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in rows]


def _rows(conn, sql: str, args) -> list[dict]:
    cur = conn.execute(sql, args)
    names = [d.name for d in cur.description]
    return [r if isinstance(r, dict) else dict(zip(names, r)) for r in cur.fetchall()]


def load(conn, vin: str, lo: datetime, hi: datetime) -> tuple[list[dict], list[dict], list[tuple], list[dict]]:
    """States, readings, charges and trips for one car between lo and hi."""
    states = _rows(conn, "SELECT first_date AS first, last_date AS last, state::text AS state FROM states WHERE vin = %s AND last_date >= %s AND first_date <= %s ORDER BY first_date", (vin, lo, hi))
    fixes = _rows(
        conn,
        "SELECT at, latitude AS lat, longitude AS lon, odometer AS odo, speed FROM cc_positions WHERE vin = %s AND at >= %s AND at <= %s AND latitude IS NOT NULL ORDER BY at",
        (vin, lo - EDGE, hi + EDGE),
    )
    charges = [
        (r["a"], r["b"])
        for r in _rows(
            conn,
            "SELECT session_start_date AS a, COALESCE(session_end_date, now()) AS b FROM charging_sessions WHERE vin = %s AND session_start_date IS NOT NULL AND session_start_date <= %s AND COALESCE(session_end_date, now()) >= %s",
            (vin, hi, lo),
        )
    ]
    manual = {r["ref_id"] for r in _rows(conn, "SELECT ref_id FROM cc_manual WHERE kind = 'drive'", ())} if driveedits._has(conn, "cc_manual") else set()
    trips = [
        {
            "id": r["id"],
            "start": r["start_date"],
            "end": r["destination_date"],
            "manual": r["id"] in manual,
            "odo0": r["start_odometer"],
            "odo1": r["destination_odometer"],
            "lat0": r["start_position_latitude"],
            "lon0": r["start_position_longitude"],
            "lat1": r["destination_position_latitude"],
            "lon1": r["destination_position_longitude"],
        }
        for r in _rows(
            conn,
            "SELECT id, start_date, destination_date, start_odometer, destination_odometer, start_position_latitude, start_position_longitude, destination_position_latitude, destination_position_longitude FROM trips WHERE vin = %s AND COALESCE(destination_date, now()) >= %s AND start_date <= %s ORDER BY start_date",
            (vin, lo, hi),
        )
    ]
    return states, fixes, charges, trips


def run(conn, vin: str, since: datetime, now: datetime | None = None, dry_run: bool = False, pad: timedelta = timedelta(days=1)) -> list[dict]:
    """Put one car's trips since `since` right. Returns the actions. A dry run reads only (the
    caller sets the transaction read-only) and leaves the car's drive edits where they are."""
    now = now or datetime.now(timezone.utc)
    lo = since - pad
    if not dry_run:
        conn.execute("SET LOCAL lock_timeout = '5s'")  # an import of this car holding the lock: try again next time, do not stall the recorder
        conn.execute(MANUAL_DDL)
        conn.execute(driveedits.DDL)
        driveedits.lock(conn, vin)  # an edit and an import of this car wait for us, and we for them
        driveedits.unapply(conn, vin, since, now)  # the drives as the source had them; the edits go back on below
    states, fixes, charges, trips = load(conn, vin, lo, now)
    wanted = [w for w in plan_drives(states, fixes, charges, now) if w["start"] >= since]
    recent = [t for t in trips if t["end"] is None or t["end"] >= since]
    actions = plan_changes(wanted, recent)
    gone = phantom_trips(recent, fixes, wanted, now)
    actions += gone
    removed = {a["id"] for a in gone}
    standing = [(t["start"], t["end"] or now) for t in trips if t["id"] not in removed] + [(w["start"], w["end"]) for w in wanted]
    car = [a for a in car_on(states, fixes, standing, now) if a["start"] >= since]
    actions += car
    if not dry_run:
        history.apply_trip_plan(conn, vin, actions)
        history.apply_car_on(conn, vin, since, now, car)
        driveedits.reapply(conn, vin)
        conn.commit()
    return actions


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="rebuild a connector car's drives from its states and readings")
    ap.add_argument("since", help="ISO date or time")
    ap.add_argument("--vin", help="one car (default: every connector car)")
    ap.add_argument("--dry-run", action="store_true", help="list what would be created, changed or merged and write nothing")
    args = ap.parse_args(argv)
    since = datetime.fromisoformat(args.since)
    if since.tzinfo is None:
        since = since.replace(tzinfo=TZ)
    import psycopg

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        if args.dry_run:
            conn.execute("SET TRANSACTION READ ONLY")
        for vin, name in connector_cars(conn, since, args.vin):
            acts = run(conn, vin, since, dry_run=args.dry_run)
            todo = [a for a in acts if a["do"] in TO_DO]
            print(
                f"{name}: {len([a for a in acts if a['do'] not in ('remove', 'incar')])} drive(s) since {since.astimezone(TZ):%Y-%m-%d}, {len([a for a in acts if a['do'] == 'keep'])} right already, {len(todo)} {'to do' if args.dry_run else 'done'}"
            )
            for a in acts:
                if a["do"] != "keep":
                    print("  " + line(a))
        if args.dry_run:
            conn.rollback()
    print("dry run: nothing changed" if args.dry_run else "done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
