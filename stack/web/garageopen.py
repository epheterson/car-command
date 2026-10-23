"""Open the garage as a car pulls into the driveway: timed from how cars really come home.

The model is learned from the Teslas' streams (a fix a second): every trip home becomes its road,
points along it with the seconds left to the driveway. A Lucid sends a fix about every 45 s while
driving; each fix on a learned road predicts when it reaches the driveway, and the door is started
then, less the door's own time and the owner's aim, on a timer between fixes. A later fix that shows
the car stopped short or turned away calls it off, or, when the door has already started, says so
at once. A road nobody came home by matches nothing, so a car crossing nearby starts nothing.

With the driveway camera, arming costs nothing: the camera starts the door only for a car turning into
the driveway. So it arms on every approach inside NEAR_M (a learned road or not, decided on the fix it
is seen on), a stop (the light at the neighbourhood's entrance, a fix that says 0 mph mid-drive) never
calls it off, only moving AWAY_M farther away does, and a watch that saw nothing arms again on the next
fix (the door has to open on every approach, not most). Coming AWAY_M closer inside the bubble arms it
however the car got there, a neighbour's up the street included.

Validated on 12 of the Teslas' trips home, each predicted from the other 11, thinned to a fix every
45 s at every phase (180 approaches), aim 13 s: the door fully open a median 8 s before the
driveway; 13 missed (no fix in time: the door stays shut); 25 waited over 5 s, the worst 16 s;
7 open more than 25 s early. The spread is the 45 s between fixes, not the model.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

ARM_M = 1500  # away at least this far before a trip home counts
NEAR_M = 1200  # profiles reach this far out
DRIVEWAY_M = 40  # the driveway is where the car comes within this of where it parks and stays
DOOR_S = 13.0  # the door's own time to open; measured on the real door before Open
AIM_S = 13.0  # fully open this long before the car reaches the driveway
NEXT_S = 50.0  # a Lucid's next fix comes about 45 s on: decide on this fix if that would be too late
STEP_M = 20  # a learned road keeps a point about this often
MATCH_M = 35  # a fix this close to a learned road is on it
STOP_MIN = 5  # a stop near home this long is a place cars stay (playground, neighbour)
AWAY_M = 30  # with the camera: this much farther from home than the last fix is turning away, not GPS noise
MODES = ("off", "watch", "open")


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    return math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360


def angle(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


# ---- learning, from dense tracks: [{t, lat, lon, mph, gear}] in time order, one car


def arrivals(track: list[dict], home: dict, gap_s: float = 600) -> list[dict]:
    """Each trip home in a track: from ARM_M away to parked inside home's radius. Returns
    {fixes (with d), entry (the driveway fix), bearing (of the car from home at 300 m)}."""
    out, start = [], None
    for x in track:
        x["d"] = distance_m(x["lat"], x["lon"], home["lat"], home["lon"])
    for i, x in enumerate(track):
        if i and (x["t"] - track[i - 1]["t"]).total_seconds() > gap_s:
            start = None  # asleep or out of reach between: not one drive
        if x["d"] > ARM_M:
            start = i
        elif start is not None and x["d"] < home["radius_m"] and (x.get("gear") == "P" or not x.get("mph")):
            fixes = track[start : i + 1]
            park = fixes[-1]
            stay = next((k for k in range(len(fixes)) if all(distance_m(y["lat"], y["lon"], park["lat"], park["lon"]) < DRIVEWAY_M * 1.5 for y in fixes[k:])), len(fixes) - 1)
            at300 = next((y for y in fixes if y["d"] < 300), park)
            out.append({"fixes": fixes, "entry": fixes[stay], "bearing": bearing(home["lat"], home["lon"], at300["lat"], at300["lon"])})
            start = None
    return out


def learn(trips: list[dict]) -> list[list]:
    """The roads home: every trip's fixes inside NEAR_M before the driveway, one about every
    STEP_M along it, as [lat, lon, seconds to the driveway, trip]. A fix is later matched to the
    points near it, so direction and curves come from the roads cars really took."""
    points = []
    for k, trip in enumerate(trips):
        last = None
        for x in trip["fixes"]:
            if x["t"] > trip["entry"]["t"] or x["d"] > NEAR_M:
                continue
            if last is None or distance_m(last["lat"], last["lon"], x["lat"], x["lon"]) >= STEP_M:
                points.append([round(x["lat"], 6), round(x["lon"], 6), round((trip["entry"]["t"] - x["t"]).total_seconds(), 1), k])
                last = x
    return points


def directions(trips: list[dict]) -> list[dict]:
    """For Settings only: the directions trips come home from, [{bearing, trips}], grouped within 40 degrees."""
    groups: list[list[float]] = []
    for b in sorted(t["bearing"] for t in trips):
        g = next((g for g in groups if angle(statistics.median(g), b) <= 40), None)
        (g.append(b) if g is not None else groups.append([b]))
    return [{"bearing": round(statistics.median(g)), "trips": len(g)} for g in groups]


def stops(ends: list[dict], home: dict, near_m: float = NEAR_M) -> list[dict]:
    """Where cars have stayed near home (not at it): drive ends between home's radius and near_m
    with STOP_MIN or more before the next drive, by 50 m cell. Returns [{lat, lon, d, bearing, n}]."""
    cells: dict[tuple, list[dict]] = {}
    for e in ends:
        d = distance_m(e["lat"], e["lon"], home["lat"], home["lon"])
        if home["radius_m"] < d < near_m and (e.get("stay_min") or 0) >= STOP_MIN:
            cells.setdefault((round(e["lat"] / 0.00045), round(e["lon"] / 0.00057)), []).append(e)
    out = []
    for es in cells.values():
        lat, lon = statistics.mean(e["lat"] for e in es), statistics.mean(e["lon"] for e in es)
        out.append({"lat": lat, "lon": lon, "d": round(distance_m(lat, lon, home["lat"], home["lon"])), "bearing": round(bearing(home["lat"], home["lon"], lat, lon)), "n": len(es)})
    return sorted(out, key=lambda s: -s["n"])


# ---- the live judgement, per fix


def time_to_driveway(points: list[list], lat: float, lon: float) -> float | None:
    """The median, over the trips whose road passes within MATCH_M of here, of their seconds
    from here to the driveway; None off every road home."""
    best: dict[int, tuple[float, float]] = {}
    for plat, plon, secs, k in points:
        if abs(plat - lat) > 0.0005 or abs(plon - lon) > 0.0007:
            continue
        m = distance_m(lat, lon, plat, plon)
        if m <= MATCH_M and (k not in best or m < best[k][0]):
            best[k] = (m, secs)
    return statistics.median(secs for _, secs in best.values()) if best else None


@dataclass
class Trip:
    armed: bool = False
    last_d: float | None = None
    last_fix: tuple | None = None
    go: datetime | None = None  # when the door is to be started
    started: datetime | None = None  # when it was (or, watching, would have been)
    said_no_show: bool = False


@dataclass
class Approach:
    model: dict = field(default_factory=dict)  # {"home": {lat, lon, radius_m}, "roads": learn(), "stops": stops()}
    trips: dict[str, Trip] = field(default_factory=dict)
    aim: float = AIM_S  # the owner's: how long before the driveway the door is fully open
    door: float = DOOR_S
    camera: bool = False  # the driveway camera picks the moment: arm on every approach, never call off on a stop

    def step(self, vin: str, fix: dict, now: datetime) -> list[tuple[str, dict]]:
        """One fix ({lat, lon, mph}). Events: ("schedule", {go, d, eta, stop_m?}), ("cancel", {why}),
        ("short", {why, d}) when the door was started and the car then stopped or turned,
        ("arrived", {}), ("missed", {})."""
        home, lat, lon = self.model.get("home"), fix.get("lat"), fix.get("lon")
        if not home or lat is None or lon is None:
            return []
        trip = self.trips.setdefault(vin, Trip())
        fresh = (lat, lon) != trip.last_fix  # the car repeats its last fix between its own updates
        d = distance_m(lat, lon, home["lat"], home["lon"])
        out: list[tuple[str, dict]] = []
        if d > ARM_M:
            if trip.go and not trip.started:
                out.append(("cancel", {"why": "turned away"}))
            trip.armed, trip.go, trip.started, trip.said_no_show = True, None, None, False
        elif d < home["radius_m"]:
            if trip.armed:
                out.append(("arrived", {}) if trip.go else ("missed", {}))
            trip.armed = False
            if not (self.camera and trip.go and not trip.started):
                trip.go, trip.started = None, None  # with the camera still looking, its sighting starts the door: home's radius ends 150 m out, before the turn-in
        elif (trip.armed or (self.camera and trip.last_d is not None and d < trip.last_d - AWAY_M)) and fresh and trip.last_d is not None:
            trip.armed = True  # with the camera, any real approach inside the bubble arms: back from a neighbour's too
            mph = fix.get("mph") or 0
            if trip.go and ((d > trip.last_d + AWAY_M) if self.camera else (d > trip.last_d or mph < 1)):
                why = "stopped short" if mph < 1 and not self.camera else "turned away"
                if trip.started:
                    out.append(("short", {"why": why, "d": round(d)}))  # the door is already going: say so at once
                    trip.armed = False
                else:
                    out.append(("cancel", {"why": why}))
                trip.go = None
            elif not trip.go and d < trip.last_d and (mph >= 2 or self.camera):
                out += self._judge(trip, lat, lon, d, mph, now)
        if fresh:
            trip.last_fix, trip.last_d = (lat, lon), d
        return out

    def _judge(self, trip: Trip, lat: float, lon: float, d: float, mph: float, now: datetime) -> list[tuple[str, dict]]:
        eta = time_to_driveway(self.model.get("roads") or [], lat, lon)
        near = self.camera and d <= NEAR_M
        if eta is None and near:
            eta = d / max(mph * 0.44704, 5.0)  # off the learned roads: the camera still looks
        if eta is None:
            return []  # not on a road anyone has come home by
        wait = eta - self.door - self.aim
        if wait > NEXT_S and not near and mph * 0.44704 * NEXT_S < d - self.model["home"]["radius_m"]:
            return []  # the next fix will still be in time, and still outside home: faster than the learned trips, it may not be
        trip.go = now + timedelta(seconds=max(0.0, wait))
        stop = next((s for s in self.model.get("stops") or [] if s["d"] < d - 30 and distance_m(lat, lon, s["lat"], s["lon"]) < d), None)
        return [("schedule", {"go": trip.go, "d": round(d), "eta": round(eta), **({"stop_m": stop["d"]} if stop else {})})]

    def due(self, vin: str, now: datetime) -> bool:
        """Whether the door is to be started now (once): the timer between fixes."""
        trip = self.trips.get(vin)
        if trip and trip.go and not trip.started and now >= trip.go:
            trip.started = now
            return True
        return False
