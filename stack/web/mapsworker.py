"""Make sure the local maps add-on has the regions the cars actually go to. Runs in the
background; a page never waits on it. One question per 0.1-degree cell per day.

Cells are floored, not rounded, to the 0.1-degree bucket: round() would put -89.649 and
-89.651 in different cells (-89.6 vs -89.7), splitting one neighbourhood's two points into
two regions. floor() keeps both in the cell that contains them."""

from __future__ import annotations

import math
from datetime import datetime


def _cell(lat: float, lon: float) -> tuple[float, float]:
    return (round(math.floor(lat * 10) / 10, 1), round(math.floor(lon * 10) / 10, 1))


def cells(points) -> list[tuple[float, float]]:
    """Distinct 0.1-degree cells, in the order their points first appear."""
    out, seen = [], set()
    for lat, lon, *_ in points:
        if lat is None or lon is None:
            continue
        c = _cell(lat, lon)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


class Worker:
    """Asks the maps add-on to cover every cell the cars have visited since the last tick.

    `endpoints_since(since)` returns (lat, lon, date) triples; `since=None` means "all
    history" — the first tick after an install or a restart with nothing recorded yet.
    `client` is a MapsClient: `.ensure` and `.status` both return None on any failure, which
    this treats as "the add-on is down right now", not an error to raise."""

    def __init__(self, client, endpoints_since, now) -> None:
        self.client, self.endpoints_since, self.now = client, endpoints_since, now
        self.asked: set[tuple[tuple[float, float], str]] = set()
        self.since: datetime | None = None

    def tick(self) -> dict:
        if self.client.status() is None:
            return {"asked": 0, "queued": 0, "covered": 0}
        pts = self.endpoints_since(self.since)
        today = self.now().date().isoformat()
        out = {"asked": 0, "queued": 0, "covered": 0}
        # pts is in time order (endpoints_since orders by date). since must only advance
        # past points whose cell was actually handled — asked successfully this tick, or
        # already asked earlier today — before a failed ensure() stops the pass. Otherwise
        # the next tick's endpoints_since(since) filters out everything after the point
        # that failed, and those cells are never asked again (fix round 1, Important).
        advanced_to = self.since
        asked_this_tick: set[tuple[float, float]] = set()
        for lat, lon, ts in pts:
            if lat is None or lon is None:
                continue
            c = _cell(lat, lon)
            if (c, today) not in self.asked and c not in asked_this_tick:
                r = self.client.ensure(c[0], c[1], "where your cars drive")  # shown on Settings › Maps; never a coordinate
                if r is None:
                    break  # don't advance past this point; retry its cell next tick
                self.asked.add((c, today))
                asked_this_tick.add(c)
                out["asked"] += 1
                if r.get("result") in ("queued", "already queued"):
                    out["queued"] += 1
                elif r.get("result") == "covered":
                    out["covered"] += 1
            advanced_to = ts if advanced_to is None else max(advanced_to, ts)
        if advanced_to is not None:
            self.since = advanced_to
        return out
