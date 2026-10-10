"""One day of one car: every drive, charge
and parked stretch in order, with what the battery did across each. Pure."""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta

from daypage import PLACEHOLDERS
from units import drive_kwh

MIN_PARKED = timedelta(minutes=10)
MIN_SPLIT = timedelta(minutes=10)  # an idle or sleep shorter than this folds into its neighbour


def split_parked(states: list[tuple[datetime, datetime, str]], start: datetime, end: datetime) -> list[tuple[datetime, datetime, str]]:
    """Cut a parked stretch into alternating awake and asleep pieces from the state rows, dropping
    pieces under MIN_SPLIT into their neighbour. Returns (start, end, 'idle'|'sleep') runs."""
    cuts = {start, end}
    for a, b, _ in states:
        for t in (a, b):
            if start < t < end:
                cuts.add(t)
    points = sorted(cuts)
    pieces: list[list] = []
    for a, b in zip(points, points[1:]):
        label = "sleep" if any(x <= a < y and s == "OFFLINE" for x, y, s in states) else "idle"
        if pieces and pieces[-1][2] == label:
            pieces[-1][1] = b
        else:
            pieces.append([a, b, label])
    # fold short pieces into the longer neighbour
    changed = True
    while changed and len(pieces) > 1:
        changed = False
        for i, (a, b, label) in enumerate(pieces):
            if b - a < MIN_SPLIT:
                j = i - 1 if i > 0 else i + 1
                pieces[j][0] = min(pieces[j][0], a)
                pieces[j][1] = max(pieces[j][1], b)
                pieces.pop(i)
                changed = True
                break
    # re-merge neighbours that ended up with the same label
    merged: list[list] = []
    for a, b, label in pieces:
        if merged and merged[-1][2] == label:
            merged[-1][1] = b
        else:
            merged.append([a, b, label])
    return [(a, b, label) for a, b, label in merged]


def level_lookup(levels: list[tuple[datetime, float]]):
    """Battery level at a time, from the (time, level) step series: the last reading at or before it."""
    times = [t for t, _ in levels]

    def at(when: datetime) -> float | None:
        i = bisect_right(times, when) - 1
        return levels[i][1] if i >= 0 else None

    return at


def asleep_share(states: list[tuple[datetime, datetime, str]], start: datetime, end: datetime) -> float | None:
    """Fraction of [start, end) the state rows say the car was OFFLINE; None when nothing is known."""
    known = asleep = 0.0
    for a, b, s in states:
        lo, hi = max(a, start), min(b, end)
        if hi <= lo:
            continue
        secs = (hi - lo).total_seconds()
        known += secs
        if s == "OFFLINE":
            asleep += secs
    return (asleep / known) if known > 0 else None


def overlap_hours(runs: list[tuple[datetime, datetime, object]], start: datetime, end: datetime, pick) -> float:
    """Hours inside [start, end) covered by runs whose value `pick` accepts."""
    total = 0.0
    for a, b, v in runs:
        if not pick(v):
            continue
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            total += (hi - lo).total_seconds() / 3600
    return total


def awake_hours(runs: list[tuple[datetime, datetime, object]], states: list[tuple[datetime, datetime, str]], start: datetime, end: datetime, pick) -> float:
    """Hours inside [start, end) covered by runs `pick` accepts, leaving out the time the car
    was asleep. A mode run spans a sleep when its value was the same at the last reading
    before and the first after, and a sleeping car runs neither Sentry nor climate."""
    total = overlap_hours(runs, start, end, pick)
    for a, b, s in states:
        if s == "OFFLINE" and min(b, end) > max(a, start):
            total -= overlap_hours(runs, max(a, start), min(b, end), pick)
    return max(0.0, total)


FRESH = timedelta(minutes=30)


def since_charge(t0, when, charge_end_level, lv0, lv1, r0, r1, miles, last_move, kwh_per_rated_mi) -> dict:
    """What the battery did between the end of the last charge, `t0`, and `when`.

    lv0 and r0 are the level and rated range readings that held at `t0`, lv1 and r1 at
    `when`, each (value, last seen) or None. A reading stands for a moment only when it
    was still being seen after the last thing that changed the battery: the charge's
    end for lv0 and r0, the last drive (`last_move`) for lv1 and r1. On a day filled
    from drive and charge history the latest reading is from before the drives, and
    subtracting it from itself said 0% used after 70 miles."""

    def held(reading, since):
        return reading[0] if reading and reading[1] is not None and reading[1] >= since - FRESH else None

    after = max(t0, last_move) if last_move else t0
    l0 = charge_end_level if charge_end_level is not None else held(lv0, t0)
    l1, a, b = held(lv1, after), held(r0, t0), held(r1, after)
    rated = (a - b) if a is not None and b is not None else None
    return {
        "ago": when - t0,
        "battery_used": (l0 - l1) if l0 is not None and l1 is not None else None,
        "miles": miles or 0.0,
        "rated_used": rated,
        "efficiency": (miles / rated) if rated and rated > 1 and miles else None,
        "kwh": (rated * kwh_per_rated_mi) if rated and kwh_per_rated_mi else None,
    }


def day_sessions(
    drives: list[dict],
    charges: list[dict],
    states: list[tuple[datetime, datetime, str]],
    levels: list[tuple[datetime, float]],
    start: datetime,
    end: datetime,
    ranges: list[tuple[datetime, float]] | None = None,
    inside: list[tuple[datetime, float]] | None = None,
    modes: list[tuple[datetime, datetime, tuple]] | None = None,
    kwh_per_rated_mi: float | None = None,
) -> list[dict]:
    """Drives, charges and parked stretches (10 min or longer) inside [start, end), in order.
    A parked row carries how much of it was asleep, the battery and rated range lost across
    it, the cabin temperature at both ends, and how long Sentry and the climate ran."""
    at = level_lookup(levels)
    range_at = level_lookup(ranges or [])
    inside_at = level_lookup(inside or [])
    modes = modes or []
    rows: list[dict] = []
    busy: list[tuple[datetime, datetime]] = []
    for d in drives:
        a, b = d["start_date"], d.get("destination_date") or end
        if b <= start or a >= end:
            continue
        rows.append(
            {
                "kind": "drive",
                "start": a,
                "end": d.get("destination_date"),
                "id": d["id"],
                "vin": d.get("vin"),
                "title": f"{d.get('from', '?')} → {d.get('to', '?')}",
                "from": d.get("from"),
                "to": d.get("to"),
                "refs": [d.get("from_ref"), d.get("to_ref")],
                "miles": d.get("miles"),
                "duration": d.get("duration"),
                "level_start": d.get("level_start"),
                "level_end": d.get("level_end"),
                "temp_f": d.get("temp_f"),
                "range_start": d.get("range_start"),
                "range_end": d.get("range_end"),
                "kwh_used": d.get("kwh_used"),
                "wh_per_mi": d.get("wh_per_mi"),
                "max_mph": d.get("max_mph"),
                "cost": d.get("cost"),
                "cost_estimate": d.get("cost_estimate"),
                "fsd_miles": d.get("fsd_miles"),
                # Cabin at either end, from the same series the parked rows already read.
                "inside_start": inside_at(a),
                "inside_end": inside_at(d.get("destination_date") or end),
            }
        )
        busy.append((a, b))
    for c in charges:
        a, b = c.get("session_start_date"), c.get("session_end_date") or end
        if a is None or b <= start or a >= end:
            continue
        rows.append(
            {
                "kind": "charge",
                "start": a,
                "end": c.get("session_end_date"),
                "id": c["id"],
                "vin": c.get("vin"),
                "title": c.get("where") or "Charge",
                "where": c.get("where"),
                "refs": [c.get("where_ref")],
                "kwh": c.get("kwh"),
                "cost": c.get("cost"),
                "cost_estimate": c.get("cost_estimate") or c.get("cost_source") == "average",
                "duration": c.get("duration"),
                "level_start": c.get("start_level"),
                "level_end": c.get("end_level"),
                "charging_type": c.get("charging_type"),
                "range_added": c.get("range_added"),
                "rate_plan": c.get("rate_plan"),
            }
        )
        busy.append((a, b))
    busy.sort()
    cursor = start
    for a, b in busy + [(end, end)]:
        gap_end = min(a, end)
        if gap_end - cursor >= MIN_PARKED:
            for p0, p1, label in split_parked(states, cursor, gap_end):
                share = asleep_share(states, p0, p1)
                l0, l1 = at(p0), at(p1)
                r0, r1 = range_at(p0), range_at(p1)
                range_loss = (r0 - r1) if r0 is not None and r1 is not None else None
                rows.append(
                    {
                        "kind": "parked",
                        "sub": label,
                        "start": p0,
                        "end": p1,
                        "id": None,
                        "vin": None,
                        "title": "Asleep" if label == "sleep" else "Idle",
                        "duration": p1 - p0,
                        "asleep_share": share,
                        "level_start": l0,
                        "level_end": l1,
                        "drop": (l0 - l1) if l0 is not None and l1 is not None else None,
                        "range_start": r0,
                        "range_end": r1,
                        "range_loss": range_loss,
                        "kwh_loss": (range_loss * kwh_per_rated_mi) if range_loss is not None and kwh_per_rated_mi else None,
                        "inside_start": inside_at(p0),
                        "inside_end": inside_at(p1),
                        "sentry_h": awake_hours(modes, states, p0, p1, lambda m: m[0]),
                        "climate_h": awake_hours(modes, states, p0, p1, lambda m: m[1]),
                    }
                )
        cursor = max(cursor, b)
    rows.sort(key=lambda r: r["start"])
    place_parked(rows)
    return rows


def place_parked(rows: list[dict]) -> None:
    """Give each parked row the place the car sat: before the first event, where that event
    began (a drive's start, a charge's place); after a drive, where it ended; a charge with
    no place leaves the car where it was. A place nobody knows ("?") stays unknown.

    Each parked row also carries `refs`: that place as the naming card opens on it (where it
    is, whether it still wants a name), from the drive or charge it came from; empty when
    nothing says where."""
    known = lambda x: x if x and x not in PLACEHOLDERS else None

    def at(r, i):
        refs = r.get("refs") or []
        return [refs[i]] if len(refs) > i and refs[i] else []

    first = next((r for r in rows if r["kind"] in ("drive", "charge")), None)
    if first and first["kind"] == "drive":
        where, refs = known(first.get("from")), at(first, 0)
    elif first:
        where, refs = known(first.get("where")), at(first, 0)
    else:
        where, refs = None, []
    for r in rows:
        if r["kind"] == "parked":
            r["where"], r["refs"] = where, refs if where else []
        elif r["kind"] == "drive":
            where, refs = known(r.get("to")), at(r, 1)
        elif r["kind"] == "charge":
            new_where = known(r.get("where"))
            if new_where:
                where, refs = new_where, at(r, 0)


def day_totals(rows: list[dict], capacity_kwh: float | None, kwh_per_rated_mi: float | None = None) -> dict:
    drives = [r for r in rows if r["kind"] == "drive"]
    charges = [r for r in rows if r["kind"] == "charge"]
    parked = [r for r in rows if r["kind"] == "parked"]
    miles = sum(r.get("miles") or 0 for r in drives)
    used = sum(
        r["kwh_used"] if r.get("kwh_used") is not None else (drive_kwh(r.get("level_start"), r.get("level_end"), capacity_kwh, r.get("range_start"), r.get("range_end"), kwh_per_rated_mi) or 0.0)
        for r in drives
    )
    parked_h = sum(r["duration"].total_seconds() for r in parked) / 3600
    known = [r for r in parked if r.get("asleep_share") is not None]  # no state rows (a day filled from history): unknown, not zero
    asleep_h = sum(r["duration"].total_seconds() / 3600 * r["asleep_share"] for r in known) if known else None
    range_loss = sum(r["range_loss"] for r in parked if r.get("range_loss") is not None and r["range_loss"] > 0)
    kwh_loss = sum(r["kwh_loss"] for r in parked if r.get("kwh_loss") is not None and r["kwh_loss"] > 0)
    # battery used is what left the pack: the drops across drives and parked stretches, not the day's net (a charge would hide them)
    drops = [r["level_start"] - r["level_end"] for r in rows if r["kind"] != "charge" and r.get("level_start") is not None and r.get("level_end") is not None and r["level_start"] > r["level_end"]]
    return {
        "drives": len(drives),
        "miles": miles,
        "used_kwh": used,
        "wh_per_mi": (used * 1000 / miles) if miles > 1 and used > 0 else None,
        "rated_used": sum((r["range_start"] - r["range_end"]) for r in drives if r.get("range_start") is not None and r.get("range_end") is not None and r["range_start"] > r["range_end"]),
        "drive_h": sum(r["duration"].total_seconds() for r in drives if r.get("duration")) / 3600,
        "charges": len(charges),
        "kwh": sum(r.get("kwh") or 0 for r in charges),
        "cost": sum(r.get("cost") or 0 for r in charges),
        # whether any of it is priced on a regional average, so the page marks it
        "cost_estimate": any(r.get("cost") and (r.get("cost_estimate") or r.get("cost_source") == "average") for r in charges),
        "drive_cost_estimate": any(r.get("cost") and r.get("cost_estimate") for r in drives),
        "parked_h": parked_h,
        "asleep_h": asleep_h,
        "idle_h": sum(r["duration"].total_seconds() / 3600 * (1 - r["asleep_share"]) for r in known) if known else None,
        "parked_drop": sum(r["drop"] for r in parked if r.get("drop") is not None and r["drop"] > 0),
        "parked_range_loss": range_loss,
        "parked_kwh_loss": kwh_loss,
        "phantom_mi_per_h": (range_loss / parked_h) if parked_h > 0 and range_loss else None,
        "sentry_h": sum(r.get("sentry_h") or 0 for r in parked),
        "climate_h": sum(r.get("climate_h") or 0 for r in parked),
        "battery_used": sum(drops) if drops else None,
    }


def elevation_profile(track: list[dict]) -> tuple[list[tuple[datetime, float]], float, float]:
    """(series in feet, climb ft, descent ft) from positions that carry elevation in metres. Deltas under 2 m are noise."""
    series, climb, descent, last = [], 0.0, 0.0, None
    for p in track:
        e = p.get("elevation")
        if e is None:
            continue
        series.append((p["at"], e * 3.28084))
        if last is not None:
            d = e - last
            if d >= 2:
                climb += d
            elif d <= -2:
                descent += -d
        last = e
    return series, climb * 3.28084, descent * 3.28084


def time_at_places(rows: list[dict], start: datetime, end: datetime) -> list[dict]:
    """Hours spent at each place through the day: a parked stretch is at the place the drive before it
    ended at (the first one, wherever the first drive left from). Driving is its own line."""
    hours: dict[str, float] = {}
    place = None
    first_drive = next((r for r in rows if r["kind"] == "drive"), None)
    if first_drive:
        place = first_drive["title"].split(" → ")[0]
    for r in rows:
        secs = ((r["end"] or end) - r["start"]).total_seconds()
        if r["kind"] == "drive":
            hours["Driving"] = hours.get("Driving", 0.0) + secs
            place = r["title"].split(" → ")[-1]
        else:
            key = place or "Unnamed spot"
            hours[key] = hours.get(key, 0.0) + secs
    total = sum(hours.values()) or 1.0
    out = [{"place": k, "hours": v / 3600, "share": v / total} for k, v in hours.items()]
    out.sort(key=lambda x: (x["place"] == "Driving", -x["hours"]))
    return out
