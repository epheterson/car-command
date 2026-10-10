"""What the banner at the top of Now says while a car is out doing something: which cars are
driving or charging, since when, and the figures that can be worked out honestly from the
readings so far. Pure; the route gathers the rows (app.activity_banners).

A run is the stretch of one state the newest reading is in: readings newest first, walked back
while the state holds and no hole is longer than GAP. Its first reading is where the drive or
the charge began: the odometer, the battery and the time there are the "so far" figures' start.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from units import drive_kwh

FRESH = timedelta(minutes=15)  # a car last heard from longer ago than this is not "now"
GAP = timedelta(minutes=20)  # a hole this long in a run ends it: a stop, then a new drive
LEVEL_DROP_MIN = 3.0  # whole-percent battery steps: below this the energy is mostly rounding
MIN_MILES = 1.0  # a figure per mile over less than this is noise
KINDS = {"Driving": "driving", "Charging": "charging"}

# The TeslaFi feed's carState in the status words state_word() takes (nowpage._FEED_STATES)
_FEED = {"Driving": "Driving", "Charging": "Charging"}


def kind(car: dict, now: datetime) -> str | None:
    """'driving' or 'charging' when the car is doing it now, else None. A status read from
    history long ago (the newest state row of a car that has gone quiet) is not now."""
    k = KINDS.get(car.get("status") or "")
    if not k:
        return None
    at = car.get("updated")
    if at is None or now - at > FRESH:
        return None
    return k


def position_row(r: dict) -> dict:
    """A cc_positions row as a reading. The recorder writes the status word ("Driving"),
    the importers the history's (DRIVING); one test for both."""
    state = "Driving" if str(r.get("state") or "").upper() == "DRIVING" else (r.get("state") or "")
    return {"at": r["at"], "state": state, "odometer": r.get("odometer"), "level": r.get("level")}


def _f(x) -> float | None:
    try:
        return float(x) if x not in (None, "", "None", "NULL") else None
    except (TypeError, ValueError):
        return None


def feed_row(r: dict) -> dict:
    """A TeslaFi feed row (cc_teslafi_rows, the columns app.activity_rows selects) as a reading."""
    return {
        "at": r["at"],
        "state": _FEED.get(r.get("car_state") or "", r.get("car_state") or ""),
        "odometer": _f(r.get("odometer")),
        "level": _f(r.get("level")),
        "range": _f(r.get("range")),
        "kw": _f(r.get("kw")),
        "kwh_added": _f(r.get("kwh_added")),
    }


def run(rows: list[dict], state: str, now: datetime) -> tuple[dict, dict] | None:
    """(first, newest) reading of the run of `state` the newest reading is in, or None when
    the newest reading is of another state or is stale. `rows` are newest first."""
    if not rows or rows[0]["state"] != state or now - rows[0]["at"] > FRESH:
        return None
    first = rows[0]
    for r in rows[1:]:
        if r["state"] != state or first["at"] - r["at"] > GAP:
            break
        first = r
    return first, rows[0]


def sentry_on(raw) -> bool | None:
    """The guard mode from a TeslaFi feed row's sentry_mode or the stream's SentryMode.
    The feed's numbers changed in 2025 (teslafi_import, MODE ERAS): 3 is on, 1 idle; a
    live row is from now, so only the new reading counts. None when the car has not said."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("true", "3", "idle", "armed", "aware", "panic", "quiet"):  # the stream's words: on unless Off (tesla_stream)
        return True
    if s in ("false", "0", "1", "off", "unknown"):
        return False
    return None


def truth(value) -> bool | None:
    """True, False or None for a value the stream gave for a yes-or-no: a bool, 1 or 0, or the words. None means
    the car did not say, which is not the same as no."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in ("true", "1", "yes", "locked"):
        return True
    if text in ("false", "0", "no", "unlocked"):
        return False
    return None


def stream_now(carried: dict | None, now: datetime, fresh: timedelta = timedelta(minutes=10)) -> dict:
    """What the Tesla stream's newest values say about now: driving, Sentry, the odometer and
    the battery. Empty when the stream has said nothing for a while."""
    c = carried or {}
    at = c.get("_at")
    if at is None or now - at > fresh:
        return {}
    gear, speed = c.get("Gear"), c.get("VehicleSpeed")
    out = {"at": at, "driving": gear in ("D", "R", "N") or (isinstance(speed, (int, float)) and speed > 0.5)}
    if "SentryMode" in c:
        out["sentry"] = sentry_on(c["SentryMode"])
    if "Locked" in c:
        out["locked"] = truth(c["Locked"])
    if isinstance(c.get("DoorState"), dict):
        out["doors_open"] = any(c["DoorState"].values())  # an empty one is all closed: the stream leaves out every false
    windows = [c[k] for k in ("FdWindow", "FpWindow", "RdWindow", "RpWindow") if k in c]
    if windows:
        known = [w for w in windows if w in ("Closed", "PartiallyOpen", "Opened")]
        out["windows_open"] = any(w != "Closed" for w in known) if known else None
    if isinstance(c.get("Odometer"), (int, float)):
        out["odometer"] = float(c["Odometer"])
    if isinstance(c.get("BatteryLevel"), (int, float)):
        out["level"] = float(c["BatteryLevel"])
    return out


def _pick(*xs):
    return next((x for x in xs if x is not None), None)


def banner(
    car: dict,
    k: str,
    span: tuple[dict, dict] | None,
    now: datetime,
    *,
    route: dict | None = None,
    capacity_kwh: float | None = None,
    kwh_per_rated_mi: float | None = None,
) -> dict:
    """One car's banner: who, what, where to, and the figures so far. A figure that cannot be
    worked out from the readings is None and the banner leaves it out.

    Energy on a drive is rated range used x this car's measured kWh per rated mile when the
    readings carry range (units.drive_kwh); else battery drop x capacity, marked as an
    estimate and only once the drop is LEVEL_DROP_MIN points, because the battery is read in
    whole percent and two points could be one or three."""
    first, last = span if span else ({}, {})
    live = bool(car.get("live"))
    # the connector's tree is newer than the last position written from it
    odo_now = _pick(car.get("odometer_mi") if live else None, last.get("odometer"), car.get("odometer_mi"))
    level_now = _pick(car.get("soc") if live else None, last.get("level"), car.get("soc"))
    since = now - first["at"] if first.get("at") else None
    out = {
        "vin": car["vin"],
        "name": car.get("name") or car["vin"],
        "kind": k,
        "since": since if since is None or since >= timedelta(0) else None,
        "dest": None,
        "minutes_left": None,
        "miles": None,
        "wh_per_mi": None,
        "estimate": False,
        "place": None,
        "kw": None,
        "level_from": None,
        "level_now": level_now,
        "kwh_added": None,
    }
    if k == "driving":
        if route:
            out["dest"], out["minutes_left"] = route.get("name"), route.get("minutes")
        o0 = first.get("odometer")
        if o0 is not None and odo_now is not None and odo_now >= o0:
            out["miles"] = odo_now - o0
        r0, r1, l0 = first.get("range"), last.get("range"), first.get("level")
        by_range = r0 is not None and r1 is not None and kwh_per_rated_mi
        if by_range:
            kwh = drive_kwh(None, None, None, r0, r1, kwh_per_rated_mi)
        elif l0 is not None and level_now is not None and l0 - level_now >= LEVEL_DROP_MIN:
            kwh = drive_kwh(l0, level_now, capacity_kwh)
            out["estimate"] = kwh is not None
        else:
            kwh = None
        if kwh is not None and out["miles"] and out["miles"] >= MIN_MILES:
            out["wh_per_mi"] = kwh * 1000 / out["miles"]
        else:
            out["estimate"] = False
    else:
        out["place"] = car.get("place") or car.get("address")
        kw = _pick(car.get("charging_power_kw"), last.get("kw"))
        out["kw"] = kw if kw and kw > 0 else None
        out["level_from"] = first.get("level")
        added = last.get("kwh_added")
        out["kwh_added"] = added if added and added > 0 else None
    return out


def signature(banners: list[dict]) -> str:
    """Which cars show and doing what: the page swaps a banner's words in place while this
    holds (so the scene keeps moving), and redraws the set when it changes."""
    return " ".join(f"{b['vin']}:{b['kind']}" for b in banners)
