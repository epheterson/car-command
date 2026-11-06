"""The miles a car drove out of coverage. A car with no signal sends nothing, and the odometer on the
far side of the silence is higher than where the last drive ended: that stretch is in no drive. This
fills it as a drive marked "no signal" (cc_gap_drives): the time bounds are the last fix before and the
first fix after, the distance is the odometer's, the energy is the battery's drop where both ends have a
level, and there is no route (the map draws a dashed straight line).

The rule, for any source (the Lucid connector, a Tesla's feed, stream or history), over a car's drives
in time order:
- Take each pair of neighbouring finished drives with nothing between them. The odometer step from the
  first's end to the second's start is the unlogged stretch; below MIN_MILES (a rounded reading, a
  sleeping car's creep) or above what MAX_MPH allows in the time between (a bad odometer reading), it is
  left alone, and so is a pair that overlaps.
- Readings that carry an odometer inside the silence (a parked fix, a charge's) are anchors: the stretch
  is cut wherever two neighbouring anchors differ by MIN_MILES or more, so a car that drove, parked and
  drove on is two drives, and the park between them is not driving. With no anchors, one drive, the
  whole silence as its bounds.
- A drive never overlaps a charge. A charge inside a stretch cuts it into legs (before it, after it), the odometer step
  shared out by straight-line distance last fix -> charge place -> first fresh fix. A charge with no place cannot be
  measured to: the drive then ends at that charge's start, whole step kept, and `note` says the rest is unaccounted for.
- A stretch much longer than its distance needs (over an hour, or under 10 mph on average) is a window: the times
  only bound it. It keeps its distance and energy but has no duration, speed or driving time, and reads "sometime
  between 9:47 and 4:05 PM".
- A missed start is not a drive of its own: when a stretch is a window and the car is already moving at the next
  drive's first reading, it sat parked where the last drive left it and the feed woke a minute into the next drive.
  That drive starts from the parked spot and odometer instead, earlier by the missed miles at the first reading's
  speed (LATE_START_MPH at least, never more than LATE_START_MAX), and keeps its id.
- Never over a real drive: nothing is written where any trip overlaps the stretch. Once filled the pair
  has no step left, so a rerun finds nothing.
Pure: trips and readings in, drives to add out. history.apply_gap_drives writes them.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DDL = """
CREATE TABLE IF NOT EXISTS cc_gap_drives (
    trip_id    INTEGER PRIMARY KEY,
    vin        VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    note       TEXT,
    window_only BOOLEAN NOT NULL DEFAULT FALSE
);
ALTER TABLE cc_gap_drives ADD COLUMN IF NOT EXISTS note TEXT;
ALTER TABLE cc_gap_drives ADD COLUMN IF NOT EXISTS window_only BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS cc_late_starts (
    trip_id    INTEGER PRIMARY KEY,
    vin        VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    old_start  TIMESTAMPTZ NOT NULL,
    old_odometer DOUBLE PRECISION,
    old_lat    DOUBLE PRECISION,
    old_lon    DOUBLE PRECISION
);
"""
MIN_MILES = 0.5  # a step under this is a rounded odometer, not a drive nobody saw
MAX_MPH = 150.0  # a step the time between cannot hold is a bad reading
LEVEL_NEAR = timedelta(minutes=20)  # a battery level further from a drive's end than this says nothing about it
SLOW_MPH = 10.0  # a stretch this slow on average, or longer than LONG, did not take that long driving: its times are a window
LONG = timedelta(hours=1)
LIVE_KEY = "nosignal_live_since"  # cc_state: when the automatic fill first ran; it fills only silences that began after
LATE_START_MPH = 15.0  # the slowest speed a missed start is reckoned at
LATE_START_MAX = timedelta(minutes=15)  # longer than this at the first reading's speed is not a missed start
RECENT = timedelta(days=14)  # what the automatic fill looks back over; older history is filled by hand (make fill-gaps)


def _level(fixes: list[dict], at: datetime, before: bool) -> float | None:
    """The battery level of the fix nearest `at` on one side of it, within LEVEL_NEAR."""
    best = None
    for f in fixes:
        if f.get("level") is None:
            continue
        d = (at - f["at"]) if before else (f["at"] - at)
        if timedelta(0) <= d <= LEVEL_NEAR and (best is None or d < best[0]):
            best = (d, f["level"])
    return best[1] if best else None


def _metres(a: tuple, b: tuple) -> float:
    dy = (b[0] - a[0]) * 111_320.0
    dx = (b[1] - a[1]) * 111_320.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


def _around_charges(t0, o0, p0, t1, o1, p1, charges: list[dict]) -> tuple[list[tuple], str | None]:
    """The legs [(t0, o0, p0, t1, o1, p1)] of one stretch with the charges inside it taken out, and a note when a
    charge has no place. Positions are (lat, lon) or None."""
    inside = sorted((c for c in charges if c["start"] < t1 and c["end"] > t0), key=lambda c: c["start"])
    if not inside:
        return [(t0, o0, p0, t1, o1, p1)], None
    if any(c.get("lat") is None or c.get("lon") is None for c in inside):
        first = inside[0]
        return [
            (t0, o0, p0, max(t0, first["start"]), o1, (first.get("lat"), first.get("lon")) if first.get("lat") is not None else None)
        ], "A charge in the silence has no place, so the time after it is not counted."
    # legs: start -> first charge, between charges, last charge -> end; the park at a charge is no distance
    marks = [(t0, p0)]
    for c in inside:
        marks += [(max(t0, c["start"]), (c["lat"], c["lon"])), (min(t1, c["end"]), (c["lat"], c["lon"]))]
    marks.append((t1, p1))
    legs = [(marks[i], marks[i + 1]) for i in range(0, len(marks), 2)]
    known = all(p is not None and p[0] is not None for _, p in marks)
    dist = [_metres(a[1], b[1]) if known else 1.0 for a, b in legs]
    total = sum(dist) or 1.0
    out, odo = [], o0
    for (a, b), d in zip(legs, dist):
        step = (o1 - o0) * d / total
        if b[0] > a[0]:
            out.append((a[0], odo, a[1], b[0], odo + step, b[1]))
        odo += step
    return out, None


def _late_start(fixes: list[dict], b: dict, t0: datetime, miles: float) -> datetime | None:
    """When the next drive `b` was caught already moving: its start pulled back to cover `miles` at the speed of its
    first reading (within two minutes of its start). None when that reading is not moving or the miles need longer."""
    first = next((f for f in fixes if b["start"] <= f["at"] <= b["start"] + timedelta(minutes=2)), None)
    if first is None or not (first.get("speed") or 0) > 0:
        return None
    took = timedelta(hours=miles / max(float(first["speed"]), LATE_START_MPH))
    return None if took > LATE_START_MAX else max(t0, b["start"] - took)


def is_window(start: datetime, end: datetime, miles: float) -> bool:
    """Whether a filled drive's times only bound it: the silence is much longer than the distance needs."""
    hours = (end - start).total_seconds() / 3600
    return end - start > LONG or hours <= 0 or miles / hours < SLOW_MPH


def plan(trips: list[dict], fixes: list[dict], capacity: float | None = None, since: datetime | None = None, charges: list[dict] = (), begun_after: datetime | None = None) -> list[dict]:
    """The drives to add, oldest first: {start, end, start_odometer, destination_odometer, start_lat, start_lon,
    end_lat, end_lon, miles, kwh}. `trips` are driverebuild.load's (id, start, end, odo0, odo1, lat0, lon0, lat1,
    lon1); `fixes` cc_positions rows (at, lat, lon, odo, level). Only stretches ending at or after `since`."""
    ts = sorted(trips, key=lambda t: t["start"])
    fixes = sorted(fixes, key=lambda f: f["at"])
    out = []
    for a, b in zip(ts, ts[1:]):
        if a["end"] is None or b["end"] is None or a["odo1"] is None or b["odo0"] is None or b["start"] <= a["end"]:
            continue
        if b["odo0"] - a["odo1"] < MIN_MILES or (since is not None and b["start"] < since) or (begun_after is not None and a["end"] < begun_after):
            continue
        pts = [(a["end"], a["odo1"], a["lat1"], a["lon1"])]
        for f in fixes:
            if a["end"] < f["at"] < b["start"] and f.get("odo") is not None and pts[-1][1] <= f["odo"] <= b["odo0"]:
                pts.append((f["at"], f["odo"], f.get("lat"), f.get("lon")))
        pts.append((b["start"], b["odo0"], b["lat0"], b["lon0"]))
        for (t0, o0, la0, lo0), (t1, o1, la1, lo1) in zip(pts, pts[1:]):
            hours = (t1 - t0).total_seconds() / 3600
            if o1 - o0 < MIN_MILES or hours <= 0 or (o1 - o0) / hours > MAX_MPH:
                continue
            legs, note = _around_charges(t0, o0, (la0, lo0), t1, o1, (la1, lo1), list(charges))
            late = _late_start(fixes, b, t0, o1 - o0) if t1 == b["start"] and len(legs) == 1 and note is None and is_window(t0, t1, o1 - o0) else None
            if late is not None:
                out.append(
                    {
                        "late_start": b["id"],
                        "start": late,
                        "end": t1,
                        "start_odometer": o0,
                        "destination_odometer": o1,
                        "start_lat": la0,
                        "start_lon": lo0,
                        "end_lat": la1,
                        "end_lon": lo1,
                        "miles": o1 - o0,
                        "kwh": None,
                        "note": None,
                        "window": False,
                    }
                )
                continue
            for u0, q0, pa, u1, q1, pb in legs:
                if q1 - q0 < MIN_MILES:
                    continue
                l0, l1 = _level(fixes, u0, True), _level(fixes, u1, False)
                kwh = (l0 - l1) / 100 * capacity if capacity and l0 is not None and l1 is not None and l0 > l1 else None
                end = pb or (None, None)
                out.append(
                    {
                        "start": u0,
                        "end": u1,
                        "start_odometer": q0,
                        "destination_odometer": q1,
                        "start_lat": pa[0],
                        "start_lon": pa[1],
                        "end_lat": end[0],
                        "end_lon": end[1],
                        "miles": q1 - q0,
                        "kwh": kwh,
                        "level_start": l0,
                        "level_end": l1,
                        "note": note,
                        "window": is_window(u0, u1, q1 - q0),
                    }
                )
    return out


def line(g: dict, tz) -> str:
    """One planned drive as a line for the dry run."""
    if g.get("late_start"):
        return f"missed start   {g['start'].astimezone(tz):%Y-%m-%d %H:%M} -> {g['end'].astimezone(tz):%H:%M}, {g['miles']:.1f} mi joins the drive that follows (odometer {g['start_odometer']:.1f} -> {g['destination_odometer']:.1f})"
    kwh = (f", {g['kwh']:.1f} kWh" if g.get("kwh") else "") + (", times are a window" if g.get("window") else "")
    return f"no-signal drive {g['start'].astimezone(tz):%Y-%m-%d %H:%M} -> {g['end'].astimezone(tz):%H:%M}, {g['miles']:.1f} mi (odometer {g['start_odometer']:.1f} -> {g['destination_odometer']:.1f}){kwh}"


def main(argv: list[str] | None = None) -> int:
    """make fill-gaps: the dead zones since a date, for one car or all. A dry run unless --write."""
    ap = argparse.ArgumentParser(description="fill the miles a car drove out of coverage as drives marked no signal")
    ap.add_argument("since", help="ISO date or time: stretches ending on or after it")
    ap.add_argument("--vin", help="one car (default: every car)")
    ap.add_argument("--write", action="store_true", help="add the drives (the default only lists them)")
    args = ap.parse_args(argv)
    tz = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))
    since = datetime.fromisoformat(args.since)
    if since.tzinfo is None:
        since = since.replace(tzinfo=tz)
    import psycopg

    import history

    total = 0
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        if not args.write:
            conn.execute("SET TRANSACTION READ ONLY")
        else:
            conn.execute(DDL)
        for vin, name in [tuple(r) for r in conn.execute("SELECT vin, COALESCE(name, vin) FROM vehicles WHERE (%s::text IS NULL OR vin = %s) ORDER BY vin", (args.vin, args.vin)).fetchall()]:
            found = history.fill_gaps(conn, vin, since, dry_run=not args.write)
            total += len(found)
            print(f"{name}: {len(found)} no-signal drive(s) {'added' if args.write else 'would be added'}")
            for g in found:
                print("  " + line(g, tz))
        if args.write:
            conn.commit()
        else:
            conn.rollback()
    print(f"{total} {'added' if args.write else 'would be added; dry run, nothing changed (--write to add them)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
