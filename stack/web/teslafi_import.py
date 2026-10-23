"""TeslaFi CSV import: one poll row a minute in, Car Command history out.

Validated against full-history exports. The export is **151 columns**,
not the ~90 the public API docs suggest, and it carries TeslaFi's own session
counters, which is the single most useful thing in it:

    driveNumber / chargeNumber / idleNumber / sleepNumber

Those are session ids TeslaFi already assigned. Segmenting on them is exact,
where inferring trips from `shift_state` gaps is guesswork. Nothing here has to
re-derive what the source already knows.

Two stages, both pure and testable without a database:
    parse(text_or_lines) -> normalized samples
    replay(samples)      -> the interval tables the database plugin would have
                            written live, plus trips and charging_sessions

## Units, settled on real rows rather than assumed

TeslaFi ships both metric and imperial for the same quantity. The CarConnectivity
database plugin stores **locale** units, and this household is US, so the
imperial columns are the ones that line up with what the live Lucid connector
already writes (verified: DB odometer 1015.38 mi against a live tree reporting
1634.1 km; DB outside temp 78.44 against a live 25.8 C).

    odometer, odometerF   miles (identical in a US account)
    battery_range         miles
    outside_temp          Celsius      <- do NOT use
    outside_tempF         Fahrenheit   <- use this, it matches the DB
    speed                 mph
    charger_power         kW

## charge_energy_added is the car's own meter, with a catch

It is the truth for kWh per session (charger_power goes missing on one car from
2025 on, so integrating power under-reads there). But the car keeps the last
session's total on the field until the next charge actually starts, so the
first rows of a session carry a stale value (a 1-minute "session" showing
62 kWh). Energy per session is therefore the sum of positive increments
within the session, which ignores the stale value and the reset to zero,
and survives two charges sharing one chargeNumber.

## Session numbers stick sometimes

A handful of driveNumber/chargeNumber values span months in both exports
(a charge from October 2019 to September 2021). A session is broken when
its rows are more than SESSION_GAP apart.

## Mode eras

`sentry_mode` and `is_climate_on` changed encoding in 2025, when TeslaFi moved
to Tesla's Fleet API. Before: 1 or True means on (the fan ran 100 % of the time
`is_climate_on` was 1). After: Sentry on is 3 (1 is idle, 4 to 6 are rarer
states), climate on is 2 (the fan ran 7 % of the time it read 1, 98 % when 2).
TeslaFi's own day page counts only the 3 and the 2. replay() flips to the new
reading the first time it sees a 3 or a 2 for the car. Under the new reading a 1
is never climate, fan or not: on both cars' feeds (60 days, 2026-10) the 1s with
the fan running were Cabin Overheat Protection (95 % with the cabin over 35 °C,
a median 43 °C on one car and 37 °C on the other) or the fan running down just
after a 2, never someone's climate.

Sentry's new codes, read beside the same car's own stream on the same minutes: 0 off,
1 idle, 3 armed, 4 aware (something came near). 5 and 6 are taken as the stream's next
two, panic and quiet; neither has been seen. Aware is Sentry at work: read as off, a car
on Sentry all day showed a one-minute gap at every person who walked past.

## Fields that look useful and are not

`energy_remaining` and `pack_voltage` are populated but wrong in these exports:
energy_remaining came back as 61559425262992549 (the same sentinel that appears
in `tokens` and `id`), and pack_voltage as 90 on a Model 3 whose pack is ~350 V.
Neither is mapped. Do not be tempted by the column name.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# TeslaFi writes naive local timestamps; the export is stamped in the account's
# timezone. Verified: an export's last row carries the local
# wall-clock time of the pull.
DEFAULT_TZ = ZoneInfo("America/Los_Angeles")

NULLISH = {"", "none", "null", "<invalid>", "nan"}
TRUTHY = {"true", "1", "yes"}
DRIVING_SHIFTS = {"D", "R", "N"}
ASLEEP_STATES = {"asleep", "offline"}


def _clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in NULLISH else s


# What TeslaFi's charging_state says about the cable. "Charging", "Complete", "Stopped"
# and "Starting" are all states with a cable in; "Disconnected" and "NoPower" are not.
# Anything else, including a blank sleeping row, is the car not having said.
PLUGGED_STATES = {"Charging", "Complete", "Stopped", "Starting"}
UNPLUGGED_STATES = {"Disconnected", "NoPower"}


def plugged_state(charging_state) -> bool | None:
    """True with a cable in, False without, None when the row does not say."""
    v = _clean(charging_state) if charging_state is not None else None
    if not v:
        return None
    if v in PLUGGED_STATES:
        return True
    if v in UNPLUGGED_STATES:
        return False
    return None


def charge_limit(v) -> float | None:
    """The charge limit the car is set to (charge_limit_soc), or None when the row has none."""
    try:
        n = float(_clean(v)) if v is not None and _clean(v) else None
    except ValueError:
        return None
    return n if n is not None and 0 < n <= 100 else None


def _f(v):
    s = _clean(v)
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _i(v):
    f = _f(v)
    return None if f is None else int(f)


def _time(v, tz) -> datetime | None:
    s = _clean(v)
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def iter_samples(lines, tz=DEFAULT_TZ):
    """Yield normalized TeslaFi rows in file order. Holds nothing; safe for a
    2.9M-row export. Rows without a parseable Date are skipped."""
    for row in csv.DictReader(lines):
        s = sample_from_row(row, tz)
        if s is not None:
            yield s


def sample_from_row(row: dict, tz=DEFAULT_TZ, when: datetime | None = None) -> dict | None:
    """One TeslaFi row (a CSV line or the feed API's JSON, same field names) as a sample.
    `when` stands in for a missing Date (the live feed is read as it happens)."""
    if True:
        t = _time(row.get("Date"), tz) or when
        if t is None:
            return None
        shift = (_clean(row.get("shift_state")) or "").upper()
        state = (_clean(row.get("state")) or "").lower()
        charging_state = _clean(row.get("charging_state"))
        power = _f(row.get("charger_power")) or 0.0
        return {
            "time": t,
            # a poll that returned nothing: no state, no vin, no level. It says the logger ran, not what the car was doing.
            "blank": not state and _clean(row.get("vin")) is None and _f(row.get("battery_level")) is None,
            "vin": _clean(row.get("vin")),
            "name": _clean(row.get("display_name")) or _clean(row.get("vehicle_name")),
            "model": _clean(row.get("car_type")),
            "software": _clean(row.get("car_version")),
            "odometer": _f(row.get("odometer")),
            "level": _f(row.get("battery_level")),
            "range": _f(row.get("battery_range")),
            "outside_f": _f(row.get("outside_tempF")),
            "driving": shift in DRIVING_SHIFTS,
            # since TeslaFi's move to the Fleet API (2025) `state` reads "online" through most sleeps; TeslaFi's own
            # sleepNumber is the reliable signal, and it exists in every era
            "asleep": state in ASLEEP_STATES or bool(_i(row.get("sleepNumber"))),
            "charging": charging_state == "Charging",
            "charging_state": charging_state,
            "power": power if charging_state == "Charging" else 0.0,
            "energy_added": _f(row.get("charge_energy_added")),
            "fsd_since_reset": _f(row.get("SelfDrivingMilesSinceReset")),  # the car's own FSD trip meter
            "volts": _f(row.get("charger_voltage")),
            "amps": _f(row.get("charger_actual_current")),
            # one export says True/False, another 1/0; the type column agrees on "Tesla" for a Supercharger
            "fast_charger": (_clean(row.get("fast_charger_present")) or "").lower() in TRUTHY or _clean(row.get("fast_charger_type")) == "Tesla",
            # the charger type says "Tesla" at a Supercharger and names the connector elsewhere
            "supercharger": _clean(row.get("fast_charger_type")) == "Tesla" or _clean(row.get("fast_charger_brand")) == "Tesla",
            "latitude": _f(row.get("latitude")),
            "longitude": _f(row.get("longitude")),
            "heading": _f(row.get("heading")),
            "speed": _f(row.get("speed")),
            "elevation": _f(row.get("elevation")),  # metres
            # kW at the wheels, negative under regeneration. One of the 151 columns that
            # was never mapped, and the only per-moment reading in the export that the
            # drive page could not draw.
            "power_kw": _f(row.get("power")),
            "inside_f": _f(row.get("inside_tempF")),
            # raw, decided in replay(): the encoding changed in 2025 (see MODE ERAS below)
            "sentry_raw": (_clean(row.get("sentry_mode")) or "").lower(),
            "climate_raw": (_clean(row.get("is_climate_on")) or "").lower(),
            # TeslaFi's own session ids: 0 or blank means "not in one".
            "drive_no": _i(row.get("driveNumber")) or 0,
            "charge_no": _i(row.get("chargeNumber")) or 0,
        }


def parse(source, tz=DEFAULT_TZ) -> list[dict]:
    """Normalize TeslaFi rows into a time-sorted list. `source` is CSV text or
    any iterable of lines. For a full export use iter_samples + replay instead;
    this one materializes everything."""
    lines = io.StringIO(source) if isinstance(source, str) else source
    return sorted(iter_samples(lines, tz), key=lambda s: s["time"])


# TeslaFi's car_type is the literal string "model" in the exports, so the
# model line comes from the VIN: position 4 is Tesla's model code.
VIN_MODEL = {"S": "Model S", "3": "Model 3", "X": "Model X", "Y": "Model Y", "C": "Cybertruck", "R": "Roadster", "T": "Semi"}
# Usable pack capacity in kWh, for "energy used = capacity x battery drop". Long Range
# packs for both household cars; the export carries nothing better. Editable in
# drives.capacity if a car turns out to be a different trim.
USABLE_KWH = {"Model 3": 75.0, "Model Y": 75.0, "Model S": 95.0, "Model X": 95.0, "Cybertruck": 120.0}


def model_from_vin(vin: str | None) -> str | None:
    if not vin or len(vin) < 4:
        return None
    return VIN_MODEL.get(vin[3].upper())


def vehicle_of(samples) -> dict:
    """VIN, name and model for the file. Many rows carry none of these while the
    car sleeps, so take the first row that does rather than the first row."""
    got = {}
    for s in samples:
        for k in ("vin", "name", "model", "software"):
            if k not in got and s.get(k):
                got[k] = s[k]
        if len(got) == 4:
            break
    return got


class _Runs:
    """Collapse consecutive equal values into (first, last, value) intervals,
    one sample at a time. None is skipped, so a run survives a gap."""

    __slots__ = ("rows", "cur")

    def __init__(self):
        self.rows: list = []
        self.cur = None

    def add(self, t, v):
        if v is None:
            return
        if self.cur is not None and self.cur[2] == v:
            self.cur[1] = t
        elif self.cur is not None and self.cur[0] == t:
            # two polls in the same second with different values: the table
            # keys on first_date, so the later value replaces, never a twin run
            self.cur[2] = v
        else:
            if self.cur is not None:
                self.rows.append(tuple(self.cur))
            self.cur = [t, t, v]

    def done(self) -> list[tuple]:
        if self.cur is not None:
            self.rows.append(tuple(self.cur))
            self.cur = None
        return self.rows


def _runs(samples, key) -> list[tuple[datetime, datetime, object]]:
    r = _Runs()
    for s in samples:
        r.add(s["time"], key(s))
    return r.done()


def vehicle_state(s: dict) -> str:
    if s["driving"]:
        return "DRIVING"
    if s["asleep"]:
        return "OFFLINE"
    return "PARKED"


# A parked car reports the same coordinates once a minute for hours. The live
# recorder only writes a position when the car moves; this keeps the import to
# the same rule. 15 m is below GPS jitter while parked and above it while moving.
MOVED_M = 15.0

DRIVE_GAP = timedelta(hours=1)
# A drive's first and last minutes fall between two polls: the reading just before its first
# driving row, and just after its last, are where it really began and ended, when this close.
DRIVE_EDGE = timedelta(minutes=5)
EDGE_MI = 3.0  # more than this between the two readings is not the same drive's edge
CHARGE_GAP = timedelta(hours=6)
# A "charge" that added under this, over less than BLIP_TIME, with the level not rising, is
# the car reading Charging for a moment (woken for a scheduled charge, a cable nudged).
BLIP_KWH = 0.05
BLIP_TIME = timedelta(minutes=5)

FOLD = timedelta(hours=1)
# A fold shows as a step back of one hour less a poll interval. One export
# also has a two-second step back in August 2021 (clock jitter); that is a
# clamp, not a fold, or an hour of rows would be pushed forward.
FOLD_MIN, FOLD_MAX = timedelta(minutes=45), timedelta(minutes=75)


def _moved(a, b) -> bool:
    if a is None:
        return True
    dlat = (a[0] - b[0]) * 111_320.0
    dlon = (a[1] - b[1]) * 111_320.0 * 0.79  # cos(38 deg); close enough for a threshold
    return (dlat * dlat + dlon * dlon) ** 0.5 > MOVED_M


def _continues(trip: dict, odo) -> bool:
    """Whether a reading at `odo` carries on from where `trip` had got to (EDGE_MI at most further)."""
    end = trip["destination_odometer"]
    return odo is None or end is None or 0 <= odo - end <= EDGE_MI


def _blip(ch: dict) -> bool:
    rose = ch["start_level"] is not None and ch["end_level"] is not None and ch["end_level"] > ch["start_level"]
    return (ch["real_charged"] or 0.0) < BLIP_KWH and ch["session_end_date"] - ch["session_start_date"] < BLIP_TIME and not rose


SENTRY_ON_CODES = ("3", "4", "5", "6")  # MODE ERAS: armed, aware, panic, quiet


def replay(samples, new_modes: bool = False, edges: bool = False) -> dict:
    """Rebuild the interval tables, trips, charging sessions, positions and
    software history in one pass over any iterable of samples.

    The export is naive local time, so every November one wall-clock hour
    appears twice. iter_samples converts as the first occurrence (PDT); when
    a row lands 45 to 75 minutes earlier than the row before it, it is the
    second occurrence (PST) and belongs one hour later in UTC, as does every
    following row until the clock passes where the first pass ended. Anything else
    out of order (clock jitter of a few seconds) is clamped so no interval
    runs backwards.

    `new_modes` reads sentry and climate the 2025 way from the first row (MODE ERAS): the
    live feed is all from then, and a day and a half with no 3 or 2 in it is not the old
    encoding come back. `edges` gives each drive the readings either side of it (DRIVE_EDGE):
    for TeslaFi's once-a-minute rows, not for a logger that records its own drives densely."""
    runs = {
        k: _Runs()
        for k in (
            "states",
            "connection_states",
            "charging_states",
            "drive_levels",
            "drive_ranges",
            "outside_temperatures",
            "charging_powers",
            "software",
            "inside_temperatures",
            "modes",
            "charging_electrics",
        )
    }
    positions: list[tuple] = []
    trips: dict[int, dict] = {}
    charges: dict[int, dict] = {}
    vehicle: dict = {}
    first = last = None
    new_sentry = new_climate = new_modes  # MODE ERAS: flip once a 3 or more (sentry) or 2 (climate) has been seen
    fold_until = None  # while set, rows converting earlier than this are the second pass through a fold
    n = 0
    last_pos = None
    parked = None  # (time, odometer, lat, lon) of the newest reading outside a drive: where the next one starts
    ended = None  # the drive the last driving row belonged to, until a reading outside it closes its tail

    prev_tr = None  # the drive the sample just before this one was in, when it was in one
    for s in samples:
        n += 1
        t = s["time"]
        before, prev_tr = prev_tr, None
        if fold_until is not None and t < fold_until:
            t = t + FOLD
        elif last is not None and t < last:
            fold_until = None
            if FOLD_MIN < last - t < FOLD_MAX:
                fold_until = last
                t = t + FOLD
            else:
                t = last
        else:
            fold_until = None
        if first is None:
            first = t
        last = t
        for k in ("vin", "name", "model", "software"):
            if k not in vehicle and s.get(k):
                vehicle[k] = s[k]
        if s.get("blank"):
            continue  # BLANK ROWS: TeslaFi writes one every few minutes while a car sleeps; treating them as awake would cut every sleep into pieces

        runs["states"].add(t, vehicle_state(s))
        runs["connection_states"].add(t, "OFFLINE" if s["asleep"] else "REACHABLE")
        runs["charging_states"].add(t, "CHARGING" if s["charging"] else "OFF")
        runs["drive_levels"].add(t, s["level"])
        runs["drive_ranges"].add(t, s["range"])
        runs["outside_temperatures"].add(t, s["outside_f"])
        # 0 kW when not charging, so an 11 kW run cannot bridge two sessions
        runs["charging_powers"].add(t, s["power"] if s["charging"] else 0.0)
        runs["software"].add(t, s["software"])
        runs["inside_temperatures"].add(t, s.get("inside_f"))
        if s["charging"] and (s.get("volts") or s.get("amps")):
            runs["charging_electrics"].add(t, (s.get("volts") or 0.0, s.get("amps") or 0.0))
        # Sentry and cabin conditioning as one run of (sentry, climate). A sleeping car has neither (each
        # keeps it awake), so sleep ends a run: carried over a night, one Sentry run had a parked night's
        # drain counted as Sentry's, and one climate run lasted 25 hours.
        if s["asleep"]:
            runs["modes"].add(t, (False, False))
        else:
            sr, cr = s.get("sentry_raw", ""), s.get("climate_raw", "")
            if sr in SENTRY_ON_CODES:
                new_sentry = True
            if cr == "2":
                new_climate = True
            sentry_on = sr == "true" or sr in SENTRY_ON_CODES or (sr == "1" and not new_sentry)
            climate_on = cr in ("true", "2") or (cr == "1" and not new_climate)
            runs["modes"].add(t, (sentry_on, climate_on))

        lat, lon = s["latitude"], s["longitude"]
        if lat is not None and lon is not None and (s["drive_no"] or _moved(last_pos, (lat, lon))):
            positions.append((t, lat, lon, s["heading"], s["level"], s["odometer"], s["speed"], vehicle_state(s), s.get("elevation"), s.get("power_kw")))
            last_pos = (lat, lon)

        odo = s["odometer"]
        if edges and not s["drive_no"] and odo is not None:
            # the first reading after a drive is where it stopped: the tail the last driving row missed
            if ended is not None and t - ended["destination_date"] <= DRIVE_EDGE and ended["destination_odometer"] is not None and 0 <= odo - ended["destination_odometer"] <= EDGE_MI:
                ended["destination_odometer"] = odo
                if lat is not None and lon is not None:
                    ended["destination_position_latitude"], ended["destination_position_longitude"] = lat, lon
            ended = None
            parked = (t, odo, lat, lon)

        if s["drive_no"]:
            tr = trips.get(s["drive_no"])
            if tr is None and edges and before is not None and t - before["destination_date"] <= DRIVE_EDGE and _continues(before, s["odometer"]):
                # the feed's number stepped between two driving polls with nothing between them: one drive, as the
                # stream would count it (a pause in park is a poll outside a drive, and ends it)
                tr = trips[s["drive_no"]] = before
            if tr is not None and t - tr["destination_date"] > DRIVE_GAP:
                trips[(s["drive_no"], tr["start_date"])] = tr  # the stuck number's earlier drive, kept under its own key
                tr = None
            if tr is None:
                tr = trips[s["drive_no"]] = {
                    "start_date": t,
                    "destination_date": t,
                    "fsd_start": s.get("fsd_since_reset"),
                    "fsd_end": s.get("fsd_since_reset"),
                    "start_odometer": s["odometer"],
                    "destination_odometer": s["odometer"],
                    "start_position_latitude": lat,
                    "start_position_longitude": lon,
                    "destination_position_latitude": lat,
                    "destination_position_longitude": lon,
                }
                # the first minute's miles, and the place it left, from the reading just before
                if edges and parked is not None and odo is not None and t - parked[0] <= DRIVE_EDGE and 0 <= odo - parked[1] <= EDGE_MI:
                    tr["start_odometer"] = parked[1]
                    if parked[2] is not None and parked[3] is not None:
                        tr["start_position_latitude"], tr["start_position_longitude"] = parked[2], parked[3]
            ended, parked, prev_tr = tr, None, tr
            tr["destination_date"] = t
            if s["odometer"] is not None:
                if tr["start_odometer"] is None:
                    tr["start_odometer"] = s["odometer"]
                tr["destination_odometer"] = s["odometer"]
            if s.get("fsd_since_reset") is not None:
                if tr.get("fsd_start") is None:
                    tr["fsd_start"] = s["fsd_since_reset"]
                tr["fsd_end"] = s["fsd_since_reset"]
            if lat is not None and lon is not None:
                if tr["start_position_latitude"] is None:
                    tr["start_position_latitude"], tr["start_position_longitude"] = lat, lon
                tr["destination_position_latitude"], tr["destination_position_longitude"] = lat, lon

        if s["charge_no"]:
            ch = charges.get(s["charge_no"])
            if ch is not None and t - ch["session_end_date"] > CHARGE_GAP:
                charges[(s["charge_no"], ch["session_start_date"])] = ch
                ch = None
            if ch is None:
                ch = charges[s["charge_no"]] = {
                    "session_start_date": t,
                    "session_end_date": t,
                    "start_level": s["level"],
                    "end_level": s["level"],
                    "session_position_latitude": lat,
                    "session_position_longitude": lon,
                    "session_odometer": s["odometer"],
                    "real_charged": 0.0,
                    "_last_added": s["energy_added"],
                    "charging_type": "AC",
                }
            ch["session_end_date"] = t
            if s["level"] is not None:
                if ch["start_level"] is None:
                    ch["start_level"] = s["level"]
                ch["end_level"] = s["level"]
            if s["energy_added"] is not None:
                prev = ch["_last_added"]
                if prev is not None and s["energy_added"] > prev:
                    ch["real_charged"] += s["energy_added"] - prev
                ch["_last_added"] = s["energy_added"]
            if s["fast_charger"]:
                ch["charging_type"] = "DC"
            if s.get("supercharger"):
                ch["network"] = "supercharger"

    out = {k: r.done() for k, r in runs.items()}
    out["positions"] = positions
    for tr in {id(t): t for t in trips.values()}.values():
        # FSD miles in the drive: the FSD trip meter's rise, when it rose and by no more than the drive itself
        a, b = tr.pop("fsd_start", None), tr.pop("fsd_end", None)
        miles = (tr["destination_odometer"] - tr["start_odometer"]) if tr["destination_odometer"] is not None and tr["start_odometer"] is not None else None
        d = (b - a) if a is not None and b is not None else None
        tr["fsd_miles"] = d if d is not None and 0 <= d <= (miles + 1 if miles is not None else 10**6) else None
    out["trips"] = sorted((tr for tr in {id(t): t for t in trips.values()}.values() if tr["destination_date"] > tr["start_date"]), key=lambda x: x["start_date"])
    for ch in charges.values():
        ch.pop("_last_added", None)
        if ch["real_charged"] == 0.0:
            ch["real_charged"] = None
    out["charging_sessions"] = sorted((ch for ch in charges.values() if not _blip(ch)), key=lambda x: x["session_start_date"])
    if vehicle.get("vin") and (not vehicle.get("model") or vehicle["model"].lower() in ("model", "tesla")):
        vehicle["model"] = model_from_vin(vehicle["vin"]) or vehicle.get("model")
    out["vehicle"] = vehicle
    out["rows"] = n
    out["first"] = first
    out["last"] = last
    return out
