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
    if not k or car.get("offline"):  # out of reach: its last values are not an outing under way
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
    # The stream's Idle is the feed's 1: Sentry not on. Read as on, a car with Sentry off flipped on and off
    # each minute as the feed and the stream took turns.
    if s in ("true", "3", "armed", "aware", "panic", "quiet"):
        return True
    if s in ("false", "0", "1", "off", "idle", "unknown"):
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
    """What the Tesla stream's newest values say about now. Two kinds: what needs a fresh word
    (driving, Sentry, charging, the temperatures; said only when the stream spoke in `fresh`), and
    what the car keeps until it says otherwise (lock, doors, windows, the charge limit, plugged in;
    the last thing it said, with `said_at`, since a sleeping car changes none of them). Empty when
    the stream has never spoken."""
    c = carried or {}
    at = c.get("_at")
    if at is None:
        return {}
    out: dict = {"at": at, "said_at": at, "fresh": now - at <= fresh}
    if out["fresh"]:
        gear, speed = c.get("Gear"), c.get("VehicleSpeed")
        out["driving"] = gear in ("D", "R", "N") or (isinstance(speed, (int, float)) and speed > 0.5)
        if "SentryMode" in c:
            out["sentry"] = sentry_on(c["SentryMode"])
            # The alarm itself, not Aware: Aware is someone walking past, several times a day
            out["sentry_threat"] = c["SentryMode"] == "Panic"
        if isinstance(c.get("Odometer"), (int, float)):
            out["odometer"] = float(c["Odometer"])
        if isinstance(c.get("BatteryLevel"), (int, float)):
            out["level"] = float(c["BatteryLevel"])
        for key, name in (("InsideTemp", "inside_f"), ("OutsideTemp", "outside_f"), ("HvacLeftTemperatureRequest", "climate_set_f")):
            if isinstance(c.get(key), (int, float)):
                out[name] = round(c[key] * 9 / 5 + 32, 1)  # the stream sends Celsius
        # climate as CarConnectivity words where they fit; Cabin Overheat Protection is its own, not climate
        hvac = {"On": "on", "Off": "off", "Precondition": "precondition", "OverheatProtect": "overheat"}.get(c.get("HvacPower"))
        if hvac:
            out["climate"] = hvac
        if c.get("ClimateKeeperMode") in ("Off", "On", "Dog", "Party"):
            out["climate_keeper"] = {"Off": None, "On": "keep", "Dog": "dog", "Party": "camp"}[c["ClimateKeeperMode"]]
        if c.get("DefrostMode") in ("Off", "Normal", "Max", "AutoDefog"):
            out["window_heating"] = c["DefrostMode"] != "Off"
        power = sum(c[k] for k in ("ACChargingPower", "DCChargingPower") if isinstance(c.get(k), (int, float)))
        if any(isinstance(c.get(k), (int, float)) for k in ("ACChargingPower", "DCChargingPower")):
            out["charging_power_kw"] = round(power, 2)
        for key, name in (("ChargerVoltage", "charger_volts"), ("ChargeAmps", "charger_amps"), ("ChargeRateMilePerHour", "charge_rate_mph"), ("ChargeCurrentRequest", "charge_amps_set")):
            if isinstance(c.get(key), (int, float)):
                out[name] = round(float(c[key]), 1)
        energy = [c[k] for k in ("ACChargingEnergyIn", "DCChargingEnergyIn") if isinstance(c.get(k), (int, float))]
        if energy:
            out["charge_session_kwh"] = round(max(energy), 2)
        if isinstance(c.get("TimeToFullCharge"), (int, float)):
            out["charge_minutes_left"] = round(c["TimeToFullCharge"] * 60)  # Tesla sends hours
        if isinstance(c.get("DriverSeatOccupied"), bool):
            out["driver_present"] = c["DriverSeatOccupied"]
        if isinstance(c.get("PreconditioningEnabled"), bool):
            out["preconditioning"] = c["PreconditioningEnabled"]
    if "Locked" in c:
        out["locked"] = truth(c["Locked"])
    if isinstance(c.get("DoorState"), dict):
        out["doors_open"] = any(c["DoorState"].values())  # an empty one is all closed: the stream leaves out every false
        out["trunk_open"] = bool(c["DoorState"].get("TrunkRear"))
        out["frunk_open"] = bool(c["DoorState"].get("TrunkFront"))
    windows = [c[k] for k in ("FdWindow", "FpWindow", "RdWindow", "RpWindow") if k in c]
    if windows:
        known = [w for w in windows if w in ("Closed", "PartiallyOpen", "Opened")]
        out["windows_open"] = any(w != "Closed" for w in known) if known else None
    charge = c.get("DetailedChargeState") if c.get("DetailedChargeState") not in (None, "Unknown") else c.get("ChargeState")
    if isinstance(charge, str) and charge not in ("", "Unknown"):
        out["plugged"] = charge != "Disconnected"
        if out["fresh"]:
            out["charging"] = charge in ("Starting", "Charging")
    if isinstance(c.get("ChargeLimitSoc"), (int, float)):
        out["charge_limit"] = float(c["ChargeLimitSoc"])
    if isinstance(c.get("ChargePortDoorOpen"), bool):
        out["port_open"] = c["ChargePortDoorOpen"]
    if c.get("ChargePortLatch") in ("Engaged", "Disengaged", "Blocking"):
        out["cable_latched"] = c["ChargePortLatch"] == "Engaged"
    rated = c.get("RatedRange")
    if isinstance(rated, (int, float)):
        out["range_mi"] = round(float(rated), 1)
        level = c.get("BatteryLevel")
        if isinstance(level, (int, float)) and level >= 5:  # below that the ratio says more about rounding than the pack
            out["range_full_mi"] = round(rated / level * 100, 1)
    if isinstance(c.get("Version"), str) and c["Version"]:
        out["software"] = c["Version"].split(" ")[0]
    if "SoftwareUpdateVersion" in c:
        waiting = str(c.get("SoftwareUpdateVersion") or "").strip()
        out["update_available"] = bool(waiting) and waiting.split(" ")[0] != out.get("software")
    return out


def _pick(*xs):
    return next((x for x in xs if x is not None), None)


def is_home_name(name: str | None, home_names) -> bool:
    """Whether a destination or origin name is the home place: the name it was given (any case), or the
    word Home on its own, which is what a car's navigation says."""
    n = " ".join((name or "").split()).casefold()
    return bool(n) and (n == "home" or n in {" ".join((h or "").split()).casefold() for h in home_names})


def heading(name: str | None, home_names) -> str:
    """The words for going somewhere: "home" for the home place, "to Work" for anywhere else."""
    if not name:
        return ""
    return "home" if is_home_name(name, home_names) else f"to {name}"


def banner(
    car: dict,
    k: str,
    span: tuple[dict, dict] | None,
    now: datetime,
    *,
    route: dict | None = None,
    capacity_kwh: float | None = None,
    kwh_per_rated_mi: float | None = None,
    last_drive: dict | None = None,
    home_names=(),
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
        "origin": None,
        "dest_home": False,
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
        # where it set out from: the end of the drive before, if that ended before this one began
        began = first.get("at") or now
        if last_drive and last_drive.get("destination_date") and last_drive["destination_date"] <= began:
            out["origin"] = last_drive.get("to")
        if route:
            out["dest"], out["minutes_left"] = route.get("name"), route.get("minutes")
            out["dest_home"] = is_home_name(out["dest"], home_names)
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
