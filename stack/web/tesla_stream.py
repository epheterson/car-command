"""Tesla's Fleet Telemetry stream into the samples the TeslaFi replay already understands.

The receiver hands over each car's changes as records: a Payload of (field, value) pairs
with the time the car made it, sent only when a value changes, and connectivity records
when the car comes and goes. TeslaFi's export is the opposite: a full reading every
minute, with TeslaFi's own drive, charge and sleep numbers. This makes the second from the
first, so `teslafi_import.replay` and `history.write_history` turn a stream into history
with nothing new downstream: every field is carried forward from its last value, sessions
are numbered by the rules below, and filler samples close the gaps the stream leaves.

What the caller owes it (T2b, the live half):
- A window that starts while the car is asleep, as the TeslaFi feed's replay does: drive
  and charge numbers are counted from 1 in each call and only mean something within it.
- A DISCONNECTED record for silence. The receiver writes one only when the car's socket
  closes; if records stop for any other reason (the receiver restarted, a message was
  missed), the caller adds one after a few minutes of nothing, or a car will drive on.
- `miles` says what the car sends (miles and mph, Tesla's documented units); the samples
  are always in miles, mph and °F.

Pure: dicts in, dicts out. Message shapes from teslamotors/fleet-telemetry
`protos/vehicle_data.proto` as protojson writes them (camelCase), with snake_case and the
older string-typed values accepted too."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from itertools import groupby

# Enum values arrive by name; the prefix is the enum's, the rest is the meaning.
ENUM_PREFIXES = ("DetailedChargeState", "ChargeState", "ShiftState", "SentryModeState", "CarType", "WindowState")
VALUE_KEYS = {
    "stringValue": "string_value",
    "floatValue": "float_value",
    "doubleValue": "double_value",
    "intValue": "int_value",
    "longValue": "long_value",
    "booleanValue": "boolean_value",
    "locationValue": "location_value",
    "shiftStateValue": "shift_state_value",
    "chargingValue": "charging_value",
    "detailedChargeStateValue": "detailed_charge_state_value",
    "sentryModeStateValue": "sentry_mode_state_value",
    "carTypeValue": "car_type_value",
    "doorValue": "door_value",
    "windowStateValue": "window_state_value",
}
# The Doors message's six bools (protojson leaves out every false one), by the name that survives case and underscores.
DOORS = {n.lower(): n for n in ("DriverFront", "DriverRear", "PassengerFront", "PassengerRear", "TrunkFront", "TrunkRear")}
NUMBER = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


class _Invalid:
    """Tesla's word for "this signal cannot be measured now" (speed when parked, gear when off).
    Unlike an absent field it clears the carried value."""

    def __repr__(self):
        return "INVALID"


INVALID = _Invalid()


def _get(d: dict, camel: str):
    return d.get(camel, d.get(VALUE_KEYS.get(camel, camel)))


def value_of(value: dict):
    """One Value, as Python: numbers as numbers, a location as (lat, lon), an enum by its meaning."""
    if not value:
        return None
    if value.get("invalid"):
        return INVALID
    for camel in VALUE_KEYS:
        v = _get(value, camel)
        if v is None:
            continue
        if camel == "locationValue":
            return (float(v.get("latitude")), float(v.get("longitude")))
        if camel == "doorValue":
            return {DOORS[k.replace("_", "").lower()]: bool(b) for k, b in (v if isinstance(v, dict) else {}).items() if k.replace("_", "").lower() in DOORS}
        if camel in ("shiftStateValue", "chargingValue", "detailedChargeStateValue", "sentryModeStateValue", "carTypeValue", "windowStateValue"):
            name = str(v)
            for p in ENUM_PREFIXES:
                if name.startswith(p):
                    return name[len(p) :]
            return name
        if camel == "longValue":
            return int(v)
        if camel == "stringValue":
            s = str(v).strip()
            return float(s) if NUMBER.match(s) else s
        if camel in ("floatValue", "doubleValue"):
            try:
                f = float(v)  # protojson writes NaN and Infinity as strings
            except (TypeError, ValueError):
                return None
            return f if math.isfinite(f) else None
        return v
    return None


def when(stamp: str) -> datetime:
    """An RFC 3339 time, nanoseconds and all."""
    s = stamp.strip().replace("Z", "+00:00")
    m = re.match(r"^(.*?\.\d{1,6})\d*(.*)$", s)
    if m:
        s = m.group(1) + m.group(2)
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def records(payloads: list[dict]) -> list[tuple[datetime, str, dict]]:
    """(time, vin, {field: value}) in time order per car. A connectivity record becomes the
    field `_connected`; a resent copy of a record already seen is dropped."""
    out, seen = [], set()
    for p in payloads:
        vin, stamp = p.get("vin"), p.get("createdAt") or p.get("created_at")
        if not vin or not stamp:
            continue  # a record that names no car or no time cannot be placed
        t = when(stamp)
        if "status" in p and "data" not in p:
            fields = {"_connected": str(p["status"]).upper() == "CONNECTED"}
        else:
            fields = {}
            for d in p.get("data") or []:
                fields[d.get("key")] = value_of(d.get("value") or {})
        key = (vin, t, tuple(sorted((k, repr(v)) for k, v in fields.items())))
        if key in seen:
            continue
        seen.add(key)
        out.append((t, vin, fields))
    out.sort(key=lambda r: (r[1] or "", r[0]))
    return out


# ---- sessions. The rules, each pinned by a test in test_tesla_stream.py.
DRIVING_GEARS = {"D", "R", "N"}
MOVING_MPH = 0.5
DRIVE_END = timedelta(minutes=3)  # a stop longer than this in park ends the drive; shorter is a light
CHARGING_STATES = {"Starting", "Charging"}
CHARGE_END = timedelta(minutes=5)  # a pause longer than this ends the charge
SLEEP_AFTER = timedelta(minutes=15)  # gone this long after a disconnect: asleep
FILL_AWAKE = timedelta(minutes=5)  # a sample at least this often, so the replay's runs have no holes
FILL_ASLEEP = timedelta(minutes=15)
KM = 0.621371
# The car reports pack current negative while the pack discharges: two cars, parked and
# unplugged (DetailedChargeState Disconnected), read -0.2 to -0.9 A for half an hour, their
# electronics' draw of about 150 W, and never above 0 (see test_tesla_units.py). So
# power drawn is -volts x amps: positive at the wheels, negative regenerating. A real
# drive is the last check.
PACK_SIGN = -1.0


def _f(c: float | None) -> float | None:
    return None if c is None else round(c * 9 / 5 + 32, 2)


class _Car:
    """One car's carried state and its session counters."""

    def __init__(self, vin: str, miles: bool):
        self.vin, self.miles = vin, miles
        self.cur: dict = {}
        self.connected = True
        self.gone_at: datetime | None = None  # the disconnect, while no record has come since
        self.drive_no = self.charge_no = 0
        self.in_drive = self.in_charge = False
        self.not_driving_since: datetime | None = None
        self.not_charging_since: datetime | None = None
        self.session_dc = False

    def apply(self, t: datetime, fields: dict) -> None:
        if "_connected" in fields:
            # A car out of reach is not driving; whether the drive has ended is the grace's to
            # decide (DRIVE_END), so a tunnel does not cut a drive in two.
            self.connected = bool(fields["_connected"])
            self.gone_at = None if self.connected else t
            return
        self.connected, self.gone_at = True, None
        for k, v in fields.items():
            if v is INVALID:
                self.cur.pop(k, None)
            elif v is not None:
                self.cur[k] = v

    def asleep(self, t: datetime) -> bool:
        return self.gone_at is not None and t - self.gone_at >= SLEEP_AFTER

    def dist(self, v):
        return None if v is None else (v if self.miles else round(v * KM, 4))

    def sample(self, t: datetime) -> dict:
        c = self.cur
        asleep = self.asleep(t)
        speed = self.dist(c.get("VehicleSpeed"))
        driving = self.connected and not asleep and (c.get("Gear") in DRIVING_GEARS or (speed or 0) > MOVING_MPH)
        closes_drive = closes_charge = False
        if driving:
            if not self.in_drive or (self.not_driving_since and t - self.not_driving_since > DRIVE_END):
                self.drive_no += 1
            self.in_drive, self.not_driving_since = True, None
        elif self.in_drive and self.not_driving_since is None:
            # the moment it parks (or leaves) closes the drive: the final odometer and place arrive with it
            self.not_driving_since, closes_drive = t, True
        detailed = c.get("DetailedChargeState")
        state = detailed if detailed not in (None, "Unknown") else c.get("ChargeState")
        # out of reach is not charging, as out of reach is not driving; the 5-minute grace keeps a short drop in one session
        charging = self.connected and not asleep and state in CHARGING_STATES
        dc_now = (c.get("DCChargingPower") or 0) > 0 or bool(c.get("FastChargerPresent"))
        if charging:
            if not self.in_charge or (self.not_charging_since and t - self.not_charging_since > CHARGE_END):
                self.charge_no += 1
                self.session_dc = False
            self.in_charge, self.not_charging_since = True, None
            # once a session is seen fast, it stays fast: a dip to 0 kW must not hand its energy to the AC counter
            self.session_dc = self.session_dc or dc_now
        elif self.in_charge and self.not_charging_since is None:
            # the moment it stops closes the charge: the final energy arrives with it
            self.not_charging_since, closes_charge = t, True
        dc = dc_now or (self.in_charge and self.session_dc)
        kw = (c.get("DCChargingPower") if dc else c.get("ACChargingPower")) or 0.0
        energy = c.get("DCChargingEnergyIn") if dc and c.get("DCChargingEnergyIn") is not None else c.get("ACChargingEnergyIn")
        volts, amps = c.get("PackVoltage"), c.get("PackCurrent")
        loc = c.get("Location") or (None, None)
        sentry = c.get("SentryMode")
        return {
            "time": t,
            "blank": False,
            "vin": self.vin,
            "name": c.get("VehicleName"),
            "model": None,
            "software": c.get("Version"),
            "odometer": self.dist(c.get("Odometer")),
            "level": c.get("BatteryLevel", c.get("Soc")),
            "range": self.dist(c.get("RatedRange", c.get("EstBatteryRange"))),
            "outside_f": _f(c.get("OutsideTemp")),
            "driving": driving,
            "asleep": asleep,
            "charging": charging,
            "charging_state": state,
            "power": float(kw) if charging else 0.0,
            "energy_added": energy,
            "fsd_since_reset": None,
            "volts": c.get("ChargerVoltage"),
            "amps": c.get("ChargeAmps"),
            "fast_charger": dc,
            # Fleet Telemetry names the charger, e.g. FastChargerSupercharger; absent, the network is unknown
            "supercharger": dc and "supercharger" in str(c.get("FastChargerType") or "").lower(),
            "latitude": loc[0],
            "longitude": loc[1],
            "heading": c.get("GpsHeading"),
            "speed": speed,
            "elevation": None,
            "power_kw": round(PACK_SIGN * volts * amps / 1000, 3) if volts is not None and amps is not None else None,
            "inside_f": _f(c.get("InsideTemp")),
            "sentry_raw": "" if sentry is None else ("true" if sentry is True or sentry not in (False, "Off", "Unknown") else "false"),
            "climate_raw": "",
            "fan_on": False,
            "drive_no": self.drive_no if driving or closes_drive else 0,
            "charge_no": self.charge_no if charging or closes_charge else 0,
        }


def samples(recs: list[tuple[datetime, str, dict]], *, until: datetime, miles: bool = True) -> list[dict]:
    """Samples in sample_from_row's shape, per car: one at every record, and fillers from the
    carried state so no gap is longer than FILL_AWAKE (FILL_ASLEEP asleep), up to `until`."""
    out: list[dict] = []
    for vin, group in groupby(sorted(recs, key=lambda r: (r[1] or "", r[0])), key=lambda r: r[1]):
        car, last = _Car(vin, miles), None
        for t, _vin, fields in list(group) + [(until, vin, None)]:
            while last is not None:
                step = FILL_ASLEEP if car.asleep(last) else FILL_AWAKE
                nxt = last + step
                if car.gone_at is not None and last < car.gone_at + SLEEP_AFTER < nxt:
                    nxt = car.gone_at + SLEEP_AFTER  # the moment it falls asleep is a sample of its own
                if nxt >= t:
                    break
                out.append(car.sample(nxt))
                last = nxt
            if fields is None:
                break
            car.apply(t, fields)
            out.append(car.sample(t))
            last = t
    return out
