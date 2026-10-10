"""Drives drawn on the roads they took, for cars that report too seldom to draw it themselves.

A Lucid's cloud position moves every 15 to 60 s while it drives, and a TeslaFi history
every 30 to 60 s, so the line through those positions cuts every corner. The local maps
add-on matches them to the road network (its /match, on the local Valhalla only), and the
result is kept here, one row a drive, beside the trips and never in place of the positions:
the car's own readings stay exactly as recorded, and every figure on a page (distance,
speed, efficiency) still comes from them and the odometer, never from the matched line.

Which drives: those whose positions are more than SPARSE_GAP_S apart at the median. A
Tesla streaming at 1 Hz already draws its roads; it is looked at once, marked "dense" and
never sent. Everything else is matched newest first, a few at a time, in the background.
A match that does not agree with the car is not drawn: a line much longer or shorter than
the odometer says, or one Valhalla placed few positions on, stays raw with the reason kept.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from math import cos, hypot, radians
from statistics import median
from typing import Callable, Sequence

DDL = """
CREATE TABLE IF NOT EXISTS cc_trip_routes (
    vin          VARCHAR NOT NULL,
    start_date   TIMESTAMPTZ NOT NULL,
    trip_end     TIMESTAMPTZ,
    status       VARCHAR NOT NULL,
    reason       VARCHAR,
    geometry     TEXT,
    geometry_simple TEXT,
    geometry_thumb  TEXT,
    source       VARCHAR NOT NULL DEFAULT 'valhalla',
    confidence   DOUBLE PRECISION,
    length_m     DOUBLE PRECISION,
    odometer_m   DOUBLE PRECISION,
    median_gap_s DOUBLE PRECISION,
    maps_version VARCHAR,
    attempts     INTEGER NOT NULL DEFAULT 0,
    attempt_version VARCHAR,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (vin, start_date)
);
"""
# Keyed by car and start time like cc_trip_extras, so a re-import that renumbers the trips
# keeps them; trip_end is the drive's end when it was matched, and a row whose drive has
# since changed end is neither drawn nor kept (the worker matches it again). geometry is the
# whole matched line, for the drive's own page; geometry_simple the same within SIMPLE_M,
# for the maps that draw many drives; geometry_thumb at most THUMB_POINTS, for the
# timeline's small pictures. attempts counts failed tries on attempt_version's build.

SPARSE_GAP_S = 8.0  # median seconds between positions above which a drive is matched
PIN_MAX_M = 300.0  # a parked spot farther than this from the first or last reading is not this drive's
MIN_CONFIDENCE = 0.8  # share of positions Valhalla placed on the road it chose
LENGTH_BAND = (0.7, 1.3)  # matched length over the odometer's distance
LENGTH_SLACK_M = 150.0  # the odometer counts in tenths of a km or mile; a short drive needs room
MILE_M = 1609.344
SIMPLE_M = 10.0  # the many-drive maps' line keeps every turn wider than this
THUMB_POINTS = 60
MAX_ATTEMPTS = 3  # tries on one build before a drive is left raw until the next build


def meters(a: Sequence[float], b: Sequence[float]) -> float:
    k = 111_320.0
    return hypot((a[0] - b[0]) * k, (a[1] - b[1]) * k * cos(radians((a[0] + b[0]) / 2)))


def median_gap(times: Sequence[datetime]) -> float | None:
    """Median seconds between a drive's distinct position times; None under two."""
    ts = sorted(set(times))
    gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
    return median(gaps) if gaps else None


def is_sparse(gap: float | None) -> bool:
    return gap is not None and gap > SPARSE_GAP_S


def trace(rows: Sequence[tuple], before: Sequence[float] | None, after: Sequence[float] | None, start: datetime, end: datetime, max_m: float = PIN_MAX_M) -> list[list[float]]:
    """What /match is sent: the drive's (at, lat, lon) positions as [lat, lon, epoch s], one
    per moment, opened by where the car was parked before it and closed by where it parked
    after, each only when within `max_m` of the nearest reading, so the matched line starts
    and ends at the kerb rather than wherever the first report happened to land."""
    pts: list[list[float]] = []
    for at, lat, lon in rows:
        if lat is None or lon is None:
            continue
        t = at.timestamp()
        if pts and t <= pts[-1][2]:
            continue
        pts.append([lat, lon, t])
    if not pts:
        return pts
    if _near(before, pts[0], max_m):
        pts.insert(0, [before[0], before[1], min(start.timestamp(), pts[0][2] - 1)])
    if _near(after, pts[-1], max_m):
        pts.append([after[0], after[1], max(end.timestamp(), pts[-1][2] + 1)])
    return pts


def _near(spot, p, max_m: float) -> bool:
    return spot is not None and None not in spot[:2] and 0 < meters(spot, p) <= max_m


def pin(line: list, before: Sequence[float] | None, after: Sequence[float] | None, max_m: float = PIN_MAX_M) -> list:
    """The matched line from the parked spot to the parked spot: the matcher puts both on
    the nearest road, and a car parks in a driveway or a lot, a few metres off it."""
    if not line:
        return line
    out = list(line)
    if _near(before, out[0], max_m) and meters(before, out[0]) > 3:
        out.insert(0, [round(before[0], 6), round(before[1], 6)])
    if _near(after, out[-1], max_m) and meters(after, out[-1]) > 3:
        out.append([round(after[0], 6), round(after[1], 6)])
    return out


def line_m(line: Sequence[Sequence[float]]) -> float:
    return sum(meters(a, b) for a, b in zip(line, line[1:]))


def judge(confidence: float | None, length_m: float, odometer_m: float | None) -> str | None:
    """Why a matched line should not be drawn, or None when it agrees with the car. The
    odometer is the referee: never much longer than it says, and, once it moved a tenth of a
    mile, never much shorter (below that its own rounding is most of the figure)."""
    if confidence is None or confidence < MIN_CONFIDENCE:
        return f"low confidence ({0 if confidence is None else confidence:.2f})"
    if odometer_m is None:
        return None
    lo, hi = LENGTH_BAND
    if length_m > hi * odometer_m + LENGTH_SLACK_M:
        return f"{length_m / odometer_m:.2f}× the odometer's distance" if odometer_m >= 0.1 * MILE_M else "longer than the odometer moved"
    if odometer_m >= 0.1 * MILE_M and length_m < lo * odometer_m - LENGTH_SLACK_M:
        return f"{length_m / odometer_m:.2f}× the odometer's distance"
    return None


# --- encoded polyline (precision 5: about a metre), the compact form the table keeps --------


def encode(points: Sequence[Sequence[float]], precision: int = 5) -> str:
    k = 10**precision
    out, plat, plon = [], 0, 0
    for lat, lon in points:
        ilat, ilon = round(lat * k), round(lon * k)
        for v in (ilat - plat, ilon - plon):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ilat, ilon
    return "".join(out)


def decode(encoded: str, precision: int = 5) -> list[list[float]]:
    out, i, lat, lon, k = [], 0, 0, 0, 10.0**precision
    while i < len(encoded):
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(encoded[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat += vals[0]
        lon += vals[1]
        out.append([round(lat / k, precision), round(lon / k, precision)])
    return out


def simplify(points: Sequence[Sequence[float]], tol_m: float = SIMPLE_M) -> list:
    """Douglas-Peucker in metres: the fewest vertices that stay within `tol_m` of the line."""
    pts = [list(p) for p in points]
    if len(pts) < 3:
        return pts
    k = cos(radians(sum(p[0] for p in pts) / len(pts))) * 111_320.0
    xy = [(p[1] * k, p[0] * 111_320.0) for p in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = xy[a], xy[b]
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        far, at = -1.0, None
        for i in range(a + 1, b):
            px, py = xy[i]
            t = 0.0 if seg == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg))
            d = hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > far:
                far, at = d, i
        if at is not None and far > tol_m:
            keep[at] = True
            stack += [(a, at), (at, b)]
    return [p for p, kept in zip(pts, keep) if kept]


def thin(points: Sequence, limit: int = THUMB_POINTS) -> list:
    """At most `limit` points, evenly spaced, both ends kept."""
    if len(points) <= limit:
        return list(points)
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def geometries(line: list) -> dict:
    """The three stored forms of a matched line: whole, simplified, thumbnail."""
    simple = simplify(line)
    return {"geometry": encode(line), "geometry_simple": encode(simple), "geometry_thumb": encode(thin(simple))}


# --- reading: the line a page draws ------------------------------------------------------

COLUMNS = {"full": "geometry", "simple": "COALESCE(geometry_simple, geometry)", "thumb": "COALESCE(geometry_thumb, geometry_simple, geometry)"}


def matched_lines(conn, vin: str, start: datetime, end: datetime, kind: str = "simple") -> dict:
    """{start_date: (trip_end, encoded line)} for one car's matched drives starting in
    [start, end): one range read on the table's key. `kind` is "full" (the drive page
    alone), "simple" (maps of many drives) or "thumb" (the timeline's pictures).
    `line_for` picks a drive's out."""
    sql = f"SELECT start_date, trip_end, {COLUMNS[kind]} AS line FROM cc_trip_routes WHERE status = 'matched' AND vin = %s AND start_date >= %s AND start_date < %s"
    rows = conn.execute(sql, (vin, start, end)).fetchall()
    return {r["start_date"]: (r["trip_end"], r["line"]) for r in rows if r["line"]}


def line_for(lines: dict, start: datetime, end: datetime | None) -> list | None:
    """A drive's matched line from `matched_lines`, only while it still describes that drive:
    a drive whose end has changed since (a re-import, a merge) is matched again, raw till then."""
    hit = lines.get(start)
    if not hit or hit[0] != end:
        return None
    return decode(hit[1])


def matched_line(conn, vin: str, start: datetime, trip_end: datetime | None, kind: str = "simple") -> list | None:
    """One drive's matched line (see line_for)."""
    return line_for(matched_lines(conn, vin, start, start + timedelta(microseconds=1), kind), start, trip_end)


def choose(raw: list, matched: list | None) -> list:
    """What a map draws for a drive: the matched line when there is one, the readings otherwise."""
    return matched if matched and len(matched) >= 2 else raw


# --- the background worker ---------------------------------------------------------------

CANDIDATES_SQL = """
SELECT t.vin, t.start_date, t.destination_date, t.start_odometer, t.destination_odometer
FROM trips t LEFT JOIN cc_trip_routes r ON r.vin = t.vin AND r.start_date = t.start_date
WHERE t.destination_date IS NOT NULL AND t.destination_date < now() - interval '5 minutes'
  AND (r.vin IS NULL OR r.trip_end IS DISTINCT FROM t.destination_date
       OR (r.status <> 'dense' AND r.maps_version IS DISTINCT FROM %s
           AND NOT (r.attempt_version IS NOT DISTINCT FROM %s AND r.attempts >= %s)))
ORDER BY t.start_date DESC LIMIT %s"""


def candidates_args(version: str, limit: int) -> tuple:
    return (version, version, MAX_ATTEMPTS, limit)


SAVE_SQL = """
INSERT INTO cc_trip_routes (vin, start_date, trip_end, status, reason, geometry, geometry_simple, geometry_thumb, source,
                            confidence, length_m, odometer_m, median_gap_s, maps_version, attempts, attempt_version, created_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'valhalla', %s, %s, %s, %s, %s, 0, NULL, now())
ON CONFLICT (vin, start_date) DO UPDATE SET trip_end = EXCLUDED.trip_end, status = EXCLUDED.status, reason = EXCLUDED.reason,
    geometry = EXCLUDED.geometry, geometry_simple = EXCLUDED.geometry_simple, geometry_thumb = EXCLUDED.geometry_thumb,
    confidence = EXCLUDED.confidence, length_m = EXCLUDED.length_m, odometer_m = EXCLUDED.odometer_m,
    median_gap_s = EXCLUDED.median_gap_s, maps_version = EXCLUDED.maps_version, attempts = 0, attempt_version = NULL, created_at = now()"""

# A try that failed (the add-on timed out, or Valhalla was down for this one drive). Counted
# per build; at MAX_ATTEMPTS the drive is no longer a candidate until the next build. A line
# matched on an earlier build, for the same drive, is kept and still drawn meanwhile.
FAIL_SQL = """
INSERT INTO cc_trip_routes (vin, start_date, trip_end, status, reason, attempts, attempt_version, created_at)
VALUES (%s, %s, %s, 'failed', %s, 1, %s, now())
ON CONFLICT (vin, start_date) DO UPDATE SET
    attempts = CASE WHEN cc_trip_routes.attempt_version IS NOT DISTINCT FROM EXCLUDED.attempt_version THEN cc_trip_routes.attempts + 1 ELSE 1 END,
    attempt_version = EXCLUDED.attempt_version, reason = EXCLUDED.reason,
    status = CASE WHEN cc_trip_routes.status = 'matched' AND cc_trip_routes.trip_end IS NOT DISTINCT FROM EXCLUDED.trip_end THEN 'matched' ELSE 'failed' END,
    geometry = CASE WHEN cc_trip_routes.status = 'matched' AND cc_trip_routes.trip_end IS NOT DISTINCT FROM EXCLUDED.trip_end THEN cc_trip_routes.geometry END,
    geometry_simple = CASE WHEN cc_trip_routes.status = 'matched' AND cc_trip_routes.trip_end IS NOT DISTINCT FROM EXCLUDED.trip_end THEN cc_trip_routes.geometry_simple END,
    geometry_thumb = CASE WHEN cc_trip_routes.status = 'matched' AND cc_trip_routes.trip_end IS NOT DISTINCT FROM EXCLUDED.trip_end THEN cc_trip_routes.geometry_thumb END,
    trip_end = EXCLUDED.trip_end"""

TRANSIENT = (None, "busy", "unavailable", "timeout")


class Worker:
    """Matches a few drives a tick, newest first, until every drive has a row for the
    add-on's current build; a new build (a region added, a monthly refresh) sends every
    sparse drive through again. Never alters cc_positions.

    A drive the add-on could not answer for (timed out, busy, Valhalla down) is charged a
    try only once the add-on is shown healthy (the next drive gets a real answer, or the
    add-on's status still answers), and the tick moves on to the next drive; two such
    drives in a row is the add-on being down, and the tick stops without charging either.
    So one drive can never stall the backfill: after MAX_ATTEMPTS it waits for a new build.

    `candidates(version, limit)` -> trip dicts; `positions(trip)` -> ((at, lat, lon) rows,
    parked before, parked after); `save(trip, row)`; `fail(trip, reason, version)`;
    `client` is a MapsClient; `sleep` spaces the calls so the add-on's place lookups (one
    Valhalla worker) are never starved.
    """

    def __init__(self, client, candidates: Callable, positions: Callable, save: Callable, fail: Callable, sleep: Callable = lambda s: None, batch: int = 6, pause_s: float = 2.0) -> None:
        self.client, self.candidates, self.positions, self.save, self.fail, self.sleep = client, candidates, positions, save, fail, sleep
        self.batch, self.pause_s = batch, pause_s

    def tick(self) -> dict:
        out = {"matched": 0, "raw": 0, "dense": 0, "failed": 0, "waiting": 0}
        status = self.client.status()
        version = (status or {}).get("version")
        if version is None:
            return out  # no add-on, or nothing built yet: nothing to match against
        version = str(version)
        asked = False
        pending = None  # (trip, reason): failed, not yet charged
        for trip in self.candidates(version, self.batch):
            rows, before, after = self.positions(trip)
            gap = median_gap([r[0] for r in rows])
            odo = trip_odometer_m(trip)
            base = {"version": version, "gap": gap, "odometer_m": odo}
            if not is_sparse(gap):
                # dense, or under two readings: the readings are the route (or there is none)
                self.save(trip, {**base, "status": "dense", "reason": "readings close enough" if gap is not None else "under two readings"})
                out["dense"] += 1
                continue
            if asked:
                self.sleep(self.pause_s)
            asked = True
            pts = trace(rows, before, after, trip["start_date"], trip["destination_date"])
            m = self.client.match(pts)
            result = (m or {}).get("result")
            if result in TRANSIENT:
                if pending is not None:
                    out["waiting"] += 2
                    return out  # two in a row: the add-on is down; neither drive is charged
                pending = (trip, result or "no answer")
                continue
            if pending is not None:  # the add-on answered this one: the last failure was that drive's own
                self._charge(pending, version, out)
                pending = None
            if result != "matched":
                self.save(trip, {**base, "status": "raw", "reason": str(m.get("reason") or "no match")[:120]})
                out["raw"] += 1
                continue
            line = pin(m.get("points") or [], before, after)
            length = line_m(line)
            why = judge(m.get("confidence"), length, odo)
            row = {**base, "confidence": m.get("confidence"), "length_m": round(length, 1)}
            if why or len(line) < 2:
                self.save(trip, {**row, "status": "raw", "reason": why or "no line"})
                out["raw"] += 1
            else:
                self.save(trip, {**row, "status": "matched", **geometries(line)})
                out["matched"] += 1
        if pending is not None:
            if (self.client.status() or {}).get("version") is not None:
                self._charge(pending, version, out)
            else:
                out["waiting"] += 1  # the add-on stopped answering: not the drive's fault
        return out

    def _charge(self, pending: tuple, version: str, out: dict) -> None:
        trip, reason = pending
        self.fail(trip, reason, version)
        out["failed"] += 1


def trip_odometer_m(trip: dict) -> float | None:
    a, b = trip.get("start_odometer"), trip.get("destination_odometer")
    if a is None or b is None or b < a:
        return None
    return (b - a) * MILE_M


def save_args(trip: dict, row: dict) -> tuple:
    return (
        trip["vin"],
        trip["start_date"],
        trip["destination_date"],
        row["status"],
        row.get("reason"),
        row.get("geometry"),
        row.get("geometry_simple"),
        row.get("geometry_thumb"),
        row.get("confidence"),
        row.get("length_m"),
        row.get("odometer_m"),
        row.get("gap"),
        row.get("version"),
    )


def fail_args(trip: dict, reason: str, version: str) -> tuple:
    return (trip["vin"], trip["start_date"], trip["destination_date"], str(reason)[:120], version)


def summary(got: dict) -> str | None:
    """The tick's log line, when it did anything at all (failures included)."""
    if not any(got.values()):
        return None
    return "routes: {matched} matched, {raw} kept raw, {dense} dense, {failed} failed, {waiting} waiting on the maps add-on".format(**got)
