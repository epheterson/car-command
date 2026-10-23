"""Top routes and destinations: the place pairs the cars drive between most, both ways
folded into one, and the places they arrive at most.

A route is two places (named or found, as every list names them), whichever way it was
driven; a drive from a place back to it (a loop) is a route of its own. Each says how
often, when last, the usual time, energy and Wh per mile, the auto-tag rule that covers it,
and, per direction with enough drives, the hour to leave that has been quickest. Pure:
drives with their two ends already placed in, rows out."""

from __future__ import annotations

from datetime import timedelta
from statistics import median

import autotag

# A route needs this many drives to be listed; a leaving hour needs this many drives in it
# to count, at least two hours have to count, and the quickest has to beat the route's usual
# time by QUICKER before it is worth saying: a minute in twenty is the traffic, not the hour.
MIN_DRIVES = 2
HOUR_MIN = 5
QUICKER = 0.15


def _key(a: dict, b: dict) -> tuple:
    return tuple(sorted((a["id"], b["id"])))


def route_key(a: int, b: int) -> str:
    """A route's address, its two places' ids low to high ("12-40"; "12-12" for a loop):
    what the route page and the list both go by."""
    lo, hi = sorted((a, b))
    return f"{lo}-{hi}"


def quickest_hour(trips: list[tuple]) -> dict | None:
    """(local departure hour, minutes) pairs of one direction: the hour whose median trip
    was shortest, its median, and the median of them all; None when fewer than two hours
    have HOUR_MIN drives, or the quickest is not QUICKER than the usual time."""
    by: dict[int, list[float]] = {}
    for h, m in trips:
        by.setdefault(h, []).append(m)
    ok = {h: median(v) for h, v in by.items() if len(v) >= HOUR_MIN}
    if len(ok) < 2:
        return None
    h = min(ok, key=lambda k: (ok[k], k))
    typical = median(m for _, m in trips)
    return {"hour": h, "minutes": ok[h], "typical": typical, "n": len(by[h])} if typical > 0 and (typical - ok[h]) / typical >= QUICKER else None


def routes(drives: list[dict], rules: list[dict], tz, keep: int = 25) -> list[dict]:
    """The busiest routes, most driven first. `drives` carry `start_place` and `end_place`
    (a place dict or None), start/destination dates, miles and kwh_used."""
    acc: dict[tuple, dict] = {}
    for d in drives:
        a, b = d.get("start_place"), d.get("end_place")
        if not a or not b or d.get("destination_date") is None:
            continue
        r = acc.setdefault(
            _key(a, b),
            {"a": a if a["id"] <= b["id"] else b, "b": b if a["id"] <= b["id"] else a, "n": 0, "last": None, "first": None, "mins": [], "ids": [], "kwh": 0.0, "kmi": 0.0, "kn": 0, "dirs": {}},
        )
        r["n"] += 1
        if d.get("id") is not None:
            r["ids"].append(d["id"])
        r["last"] = max(r["last"] or d["start_date"], d["start_date"])
        r["first"] = min(r["first"] or d["start_date"], d["start_date"])
        mins = (d["destination_date"] - d["start_date"]).total_seconds() / 60
        r["mins"].append(mins)
        if d.get("kwh_used") and d.get("miles"):
            r["kwh"] += d["kwh_used"]
            r["kmi"] += d["miles"]
            r["kn"] += 1
        r["dirs"].setdefault(a["id"], {"from": a, "to": b, "n": 0, "trips": []})
        r["dirs"][a["id"]]["n"] += 1
        r["dirs"][a["id"]]["trips"].append((d["start_date"].astimezone(tz).hour, mins))
    out = []
    for r in acc.values():
        if r["n"] < MIN_DRIVES:
            continue
        ways = sorted(r["dirs"].values(), key=lambda w: -w["n"])
        quick = []
        for w in ways:
            q = quickest_hour(w["trips"])
            if q:
                quick.append({**q, "from": w["from"]["name"]})
        # named the way it is mostly driven: Home ⇄ Work for a commute that starts at home
        pa, pb = ways[0]["from"], ways[0]["to"]
        a, b = pa["name"], pb["name"]
        out.append(
            {
                "a": pa,
                "b": pb,
                "loop": r["a"]["id"] == r["b"]["id"],
                "key": route_key(r["a"]["id"], r["b"]["id"]),
                "ids": r["ids"],
                "n": r["n"],
                "ways": [(w["from"]["name"], w["to"]["name"], w["n"]) for w in ways],
                "first": r["first"],
                "last": r["last"],
                "minutes": median(r["mins"]),
                "time": timedelta(minutes=round(median(r["mins"]))),
                "kwh": r["kwh"] / r["kn"] if r["kn"] else None,
                "wh_per_mi": r["kwh"] * 1000 / r["kmi"] if r["kmi"] else None,
                "tag": autotag.match(rules, a, b) or autotag.match(rules, b, a),
                "quick": quick,
            }
        )
    out.sort(key=lambda r: (-r["n"], -r["last"].timestamp()))
    top = max((r["n"] for r in out), default=1)
    for r in out:
        r["share"] = r["n"] / top
    return out[:keep]


def destinations(drives: list[dict], keep: int = 15) -> list[dict]:
    """The places the drives ended at, most first: how often, when last."""
    acc: dict[int, dict] = {}
    for d in drives:
        p = d.get("end_place")
        if not p or d.get("destination_date") is None:
            continue
        r = acc.setdefault(p["id"], {"place": p, "n": 0, "last": None})
        r["n"] += 1
        r["last"] = max(r["last"] or d["destination_date"], d["destination_date"])
    out = sorted(acc.values(), key=lambda r: (-r["n"], -r["last"].timestamp()))
    top = max((r["n"] for r in out), default=1)
    for r in out:
        r["share"] = r["n"] / top
    return out[:keep]
