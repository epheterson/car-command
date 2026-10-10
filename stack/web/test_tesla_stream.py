"""Tesla's stream into the samples the replay already turns into history."""

from datetime import datetime, timedelta, timezone

import pytest

import tesla_stream as ts

ALPHA = "5YJ3E1EA0JF000001"
T0 = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def payload(minutes, vin=ALPHA, resend=False, **fields):
    """One Payload as the receiver's JSON gives it, fields by their proto names."""
    data = []
    for key, value in fields.items():
        if isinstance(value, tuple):
            data.append({"key": key, "value": {"locationValue": {"latitude": value[0], "longitude": value[1]}}})
        elif isinstance(value, bool):
            data.append({"key": key, "value": {"booleanValue": value}})
        elif isinstance(value, str) and value.startswith(("ShiftState", "DetailedChargeState", "ChargeState")):
            kind = "shiftStateValue" if value.startswith("Shift") else "detailedChargeStateValue" if value.startswith("Detailed") else "chargingValue"
            data.append({"key": key, "value": {kind: value}})
        elif isinstance(value, str):
            data.append({"key": key, "value": {"stringValue": value}})
        else:
            data.append({"key": key, "value": {"doubleValue": value}})
    return {"data": data, "createdAt": at(minutes), "vin": vin, "isResend": resend}


def connectivity(minutes, status, vin=ALPHA):
    return {"vin": vin, "status": status, "createdAt": at(minutes)}


# ---- Task 1: decoding
@pytest.mark.parametrize(
    "value,expected",
    [
        ({"stringValue": "34.5"}, 34.5),
        ({"stringValue": "2026.32.5"}, "2026.32.5"),
        ({"floatValue": 12.5}, 12.5),
        ({"doubleValue": 3.25}, 3.25),
        ({"intValue": 7}, 7),
        ({"longValue": "123456"}, 123456),
        ({"booleanValue": True}, True),
        ({"locationValue": {"latitude": 39.78, "longitude": -89.65}}, (39.78, -89.65)),
        ({"shiftStateValue": "ShiftStateD"}, "D"),
        ({"detailedChargeStateValue": "DetailedChargeStateCharging"}, "Charging"),
        ({"chargingValue": "ChargeStateComplete"}, "Complete"),
        ({"invalid": True}, ts.INVALID),  # not "no value": it clears what was carried
        ({}, None),
        ({"string_value": "5"}, 5.0),
    ],
)
def test_every_kind_of_value_decodes(value, expected):
    assert ts.value_of(value) == expected


def test_records_are_per_car_in_time_order_with_connectivity_as_a_field():
    recs = ts.records([payload(2, VehicleSpeed=20.0), connectivity(0, "CONNECTED"), payload(1, Gear="ShiftStateD", Location=(39.78, -89.65))])
    assert [(r[0], r[1]) for r in recs] == [(T0, ALPHA), (T0 + timedelta(minutes=1), ALPHA), (T0 + timedelta(minutes=2), ALPHA)]
    assert recs[0][2] == {"_connected": True} and recs[1][2] == {"Gear": "D", "Location": (39.78, -89.65)} and recs[2][2] == {"VehicleSpeed": 20.0}


def test_a_resent_copy_of_a_record_is_dropped():
    recs = ts.records([payload(1, VehicleSpeed=20.0), payload(1, resend=True, VehicleSpeed=20.0)])
    assert len(recs) == 1


@pytest.mark.parametrize("stamp", ["2026-09-07T15:00:00Z", "2026-09-07T15:00:00.123456789Z", "2026-09-07T08:00:00-07:00", "2026-09-07T15:00:00.5+00:00"])
def test_created_times_in_every_shape(stamp):
    t = ts.when(stamp)
    assert t.tzinfo is not None and abs((t - T0).total_seconds()) < 1


# ---- Task 2: sessions
HOME, WORK = (39.7800, -89.6500), (39.8000, -89.6000)


def run(payloads, until_min, **kw):
    return ts.samples(ts.records(payloads), until=T0 + timedelta(minutes=until_min), **kw)


def commute(start=0, stop_at_light=False, park_for=None):
    """Parked at home, a drive to work (optionally a stop with the gear in park), parked."""
    p = [connectivity(start, "CONNECTED"), payload(start, Gear="ShiftStateP", Location=HOME, Odometer=12000.0, BatteryLevel=70.0, RatedRange=210.0, VehicleSpeed=0.0)]
    p += [payload(start + 1, Gear="ShiftStateD"), payload(start + 2, VehicleSpeed=30.0, Location=(39.79, -89.63)), payload(start + 4, VehicleSpeed=0.0)]
    if stop_at_light:
        p += [payload(start + 5, Gear="ShiftStateP"), payload(start + 6.5, Gear="ShiftStateD")]  # 90 s in park
    if park_for:
        p += [payload(start + 5, Gear="ShiftStateP"), payload(start + 5 + park_for, Gear="ShiftStateD")]
    end = start + 8 + (park_for or 0)
    p += [payload(end - 1, VehicleSpeed=25.0, Location=WORK), payload(end, VehicleSpeed=0.0, Gear="ShiftStateP", Odometer=12004.0, BatteryLevel=69.0)]
    return p


def drives(samples):
    return sorted({s["drive_no"] for s in samples if s["drive_no"]})


def test_a_commute_is_one_drive_and_the_samples_have_the_replays_shape():
    out = run(commute(), 20)
    assert drives(out) == [1]
    import teslafi_import

    expected = set(teslafi_import.sample_from_row({"Date": "2026-09-07 08:00:00", "vin": ALPHA, "state": "online"}))
    assert set(out[0]) == expected, "the same keys sample_from_row makes"
    driving = [s for s in out if s["driving"]]
    assert driving[0]["time"] == T0 + timedelta(minutes=1) and all(s["drive_no"] == 1 for s in driving)
    closing = [s for s in out if s["drive_no"] and not s["driving"]]
    assert len(closing) == 1 and closing[0]["odometer"] == 12004.0, "the sample where it parks closes the drive, with the final odometer"
    assert not any(s["drive_no"] for s in out if not s["driving"] and s is not closing[0]), "parked minutes after it carry no drive number"
    assert out[-1]["odometer"] == 12004.0 and out[-1]["level"] == 69.0 and out[-1]["range"] == 210.0, "carried forward"


def test_a_short_stop_in_park_continues_the_drive():
    assert drives(run(commute(stop_at_light=True), 20)) == [1]


def test_a_stop_of_more_than_three_minutes_makes_two_drives():
    assert drives(run(commute(park_for=10), 40)) == [1, 2]


def test_a_disconnect_longer_than_the_grace_ends_a_drive():
    p = commute()[:4] + [connectivity(4.5, "DISCONNECTED"), connectivity(9, "CONNECTED"), payload(9.2, VehicleSpeed=20.0)]
    assert drives(run(p, 10)) == [1, 2]


def charging(start, *states):
    """(minutes after start, state, kW, kWh in) steps of a home charge."""
    p = [connectivity(start, "CONNECTED"), payload(start, Gear="ShiftStateP", Location=HOME, BatteryLevel=40.0)]
    for m, state, kw, kwh in states:
        p.append(payload(start + m, DetailedChargeState=f"DetailedChargeState{state}", ACChargingPower=kw, ACChargingEnergyIn=kwh, ChargerVoltage=240.0, ChargeAmps=32.0))
    return p


def charges(samples):
    return sorted({s["charge_no"] for s in samples if s["charge_no"]})


def test_a_charge_with_a_short_pause_is_one_charge_and_its_energy_is_the_cars_meter():
    out = run(charging(0, (1, "Starting", 0.0, 0.0), (2, "Charging", 7.6, 0.1), (30, "Stopped", 0.0, 3.8), (33, "Charging", 7.6, 3.8), (60, "Complete", 0.0, 7.4)), 70)
    assert charges(out) == [1]
    on = [s for s in out if s["charging"]]
    assert on and all(s["power"] == 7.6 for s in on if s["charging_state"] == "Charging")
    assert max(s["energy_added"] or 0 for s in out) == 7.4 and on[0]["volts"] == 240.0 and on[0]["amps"] == 32.0
    assert not out[-1]["charging"] and out[-1]["charging_state"] == "Complete" and out[-1]["power"] == 0.0


def test_two_charges_more_than_five_minutes_apart_are_two():
    out = run(charging(0, (1, "Charging", 7.6, 0.1), (20, "Stopped", 0.0, 2.5), (40, "Charging", 7.6, 2.5), (60, "Complete", 0.0, 5.0)), 70)
    assert charges(out) == [1, 2]


def test_a_dc_charge_is_fast():
    p = [connectivity(0, "CONNECTED"), payload(0, DetailedChargeState="DetailedChargeStateCharging", DCChargingPower=150.0, DCChargingEnergyIn=5.0, FastChargerPresent=True)]
    s = run(p, 2)[-1]
    assert s["charging"] and s["fast_charger"] and s["power"] == 150.0 and s["energy_added"] == 5.0


def test_a_car_that_disconnects_is_asleep_after_fifteen_minutes_and_awake_when_it_speaks():
    p = [
        connectivity(0, "CONNECTED"),
        payload(0, Gear="ShiftStateP", Location=HOME, BatteryLevel=70.0),
        connectivity(10, "DISCONNECTED"),
        connectivity(100, "CONNECTED"),
        payload(100.5, BatteryLevel=69.0),
    ]
    out = run(p, 105)
    asleep = [s for s in out if s["asleep"]]
    assert asleep and asleep[0]["time"] == T0 + timedelta(minutes=25) and asleep[-1]["time"] < T0 + timedelta(minutes=100)
    assert not out[-1]["asleep"] and out[-1]["level"] == 69.0


def test_filler_samples_close_the_gaps_five_minutes_awake_fifteen_asleep():
    p = [connectivity(0, "CONNECTED"), payload(0, Gear="ShiftStateP", Location=HOME), connectivity(20, "DISCONNECTED")]
    out = run(p, 120)
    times = [round((s["time"] - T0).total_seconds() / 60) for s in out]
    awake_gaps = [b - a for a, b, s in zip(times, times[1:], out[1:]) if not s["asleep"] and b <= 20]
    asleep_gaps = [b - a for a, b, s in zip(times, times[1:], out[1:]) if s["asleep"]]
    assert awake_gaps and max(awake_gaps) <= 5 and asleep_gaps and max(asleep_gaps[1:]) <= 15
    assert times[-1] <= 120 and 120 - times[-1] <= 15, "the tail is filled up to until"


def test_a_drive_over_midnight_is_one_drive():
    late = 8 * 60 + 55  # 23:55 local is 06:55Z next day; any wall-clock midnight works the same: no reset at a date
    assert drives(run(commute(start=late, park_for=None), late + 20)) == [1]


def test_records_out_of_order_are_put_back_in_order():
    p = commute()
    p[2], p[4] = p[4], p[2]
    assert drives(run(p, 20)) == [1]


def test_units_are_converted_in_one_place():
    p = [connectivity(0, "CONNECTED"), payload(0, Odometer=100.0, VehicleSpeed=100.0, RatedRange=300.0, OutsideTemp=20.0, InsideTemp=25.0, Gear="ShiftStateD")]
    km = run(p, 1, miles=False)[-1]
    assert round(km["odometer"], 2) == 62.14 and round(km["speed"], 2) == 62.14 and round(km["range"], 1) == 186.4
    mi = run(p, 1)[-1]
    assert mi["odometer"] == 100.0 and mi["speed"] == 100.0 and mi["outside_f"] == 68.0 and mi["inside_f"] == 77.0


def test_power_at_the_wheels_is_positive_drawing_and_negative_regenerating():
    # the car's own sign: current out of the pack is negative (test_tesla_units.py)
    draw = run([connectivity(0, "CONNECTED"), payload(0, PackVoltage=400.0, PackCurrent=-50.0, Gear="ShiftStateD")], 1)[-1]
    regen = run([connectivity(0, "CONNECTED"), payload(0, PackVoltage=400.0, PackCurrent=25.0, Gear="ShiftStateD")], 1)[-1]
    assert draw["power_kw"] == 20.0 and regen["power_kw"] == -10.0


def test_sentry_reads_as_on_or_off():
    on = run([connectivity(0, "CONNECTED"), {"vin": ALPHA, "createdAt": at(0), "data": [{"key": "SentryMode", "value": {"sentryModeStateValue": "SentryModeStateArmed"}}]}], 1)[-1]
    off = run([connectivity(0, "CONNECTED"), {"vin": ALPHA, "createdAt": at(0), "data": [{"key": "SentryMode", "value": {"sentryModeStateValue": "SentryModeStateOff"}}]}], 1)[-1]
    assert on["sentry_raw"] == "true" and off["sentry_raw"] == "false"


def test_two_cars_are_kept_apart():
    bravo = "5YJ3E1EA0JF000002"
    p = commute() + [connectivity(0, "CONNECTED", vin=bravo), payload(1, vin=bravo, Gear="ShiftStateD", VehicleSpeed=10.0)]
    out = run(p, 20)
    assert {s["vin"] for s in out} == {ALPHA, bravo} and drives([s for s in out if s["vin"] == bravo]) == [1]


# ---- Task 3: a day of the stream through the replay the TeslaFi history goes through
def test_a_day_of_the_stream_becomes_two_drives_a_charge_and_a_night_asleep():
    import teslafi_import

    day = commute(start=0)  # 08:00 local-ish: to work, 4 mi
    day += [payload(9 * 60, Gear="ShiftStateD"), payload(9 * 60 + 2, VehicleSpeed=30.0, Location=(39.79, -89.63)), payload(9 * 60 + 7, VehicleSpeed=25.0, Location=HOME)]
    day += [payload(9 * 60 + 8, VehicleSpeed=0.0, Gear="ShiftStateP", Odometer=12008.0, BatteryLevel=67.0)]  # home, 4 mi
    day += [payload(9 * 60 + 15, DetailedChargeState="DetailedChargeStateCharging", ACChargingPower=7.6, ACChargingEnergyIn=0.1, ChargerVoltage=240.0, ChargeAmps=32.0)]
    day += [
        payload(9 * 60 + 75, ACChargingEnergyIn=7.6, BatteryLevel=74.0),
        payload(9 * 60 + 135, DetailedChargeState="DetailedChargeStateComplete", ACChargingPower=0.0, ACChargingEnergyIn=15.2, BatteryLevel=80.0),
    ]
    day += [connectivity(9 * 60 + 150, "DISCONNECTED")]
    samples = run(day, 20 * 60)
    out = teslafi_import.replay(samples)
    trips = sorted(out["trips"], key=lambda t: t["start_date"])
    assert len(trips) == 2, [(t["start_date"], t["destination_date"]) for t in trips]
    assert [round(t["destination_odometer"] - t["start_odometer"], 1) for t in trips] == [4.0, 4.0]
    assert trips[0]["destination_date"] - trips[0]["start_date"] <= timedelta(minutes=8)
    sessions = out["charging_sessions"]
    assert len(sessions) == 1 and abs((sessions[0].get("energy_added") or sessions[0].get("real_charged") or 0) - 15.2) < 0.2, sessions
    assert "OFFLINE" in {r[2] for r in out["connection_states"]}, "the night asleep is a state of its own"
    assert len(out["positions"]) >= 4, "positions where the car moved"


# ---- the review's fixes
def invalid(minutes, *keys):
    return {"vin": ALPHA, "createdAt": at(minutes), "isResend": False, "data": [{"key": k, "value": {"invalid": True}} for k in keys]}


def test_a_signal_that_goes_invalid_is_no_longer_carried():
    """Parked, Tesla marks VehicleSpeed invalid; off, Gear too. Carried forward, the drive never ended."""
    p = [connectivity(0, "CONNECTED"), payload(0, VehicleSpeed=30.0, Location=HOME), payload(5, VehicleSpeed=0.0), invalid(5.5, "VehicleSpeed", "Location")]
    out = run(p, 60)
    assert not out[-1]["driving"] and out[-1]["latitude"] is None
    assert drives(out) == [1] and not any(s["driving"] for s in out if s["time"] > T0 + timedelta(minutes=6))


def test_a_short_loss_of_signal_mid_drive_is_still_one_drive():
    p = commute()
    p.insert(3, connectivity(2.5, "DISCONNECTED"))
    p.insert(4, connectivity(3.2, "CONNECTED"))
    out = run(p, 20)
    assert drives(out) == [1], "a tunnel is not the end of a drive"


def test_a_dc_charge_keeps_its_own_meter_when_the_power_dips():
    p = [connectivity(0, "CONNECTED"), payload(0, ACChargingEnergyIn=23.9)]  # yesterday's home charge, still on the AC counter
    p += [payload(1, DetailedChargeState="DetailedChargeStateCharging", DCChargingPower=150.0, DCChargingEnergyIn=1.0)]
    p += [payload(10, DCChargingPower=0.0, DCChargingEnergyIn=20.0), payload(11, DCChargingPower=120.0, DCChargingEnergyIn=21.0), payload(20, DCChargingEnergyIn=29.5)]
    out = run(p, 21)
    session = [s for s in out if s["charge_no"]]
    assert all(s["energy_added"] != 23.9 for s in session) and session[-1]["energy_added"] == 29.5


def test_a_number_that_is_not_a_number_is_no_value():
    assert ts.value_of({"floatValue": "NaN"}) is None and ts.value_of({"doubleValue": "Infinity"}) is None
    p = [connectivity(0, "CONNECTED"), {"vin": ALPHA, "createdAt": at(0), "data": [{"key": "VehicleSpeed", "value": {"floatValue": "NaN"}}]}]
    assert run(p, 1)[-1]["speed"] is None


def test_a_record_without_a_time_or_a_car_is_skipped():
    recs = ts.records([{"vin": ALPHA, "createdAt": None, "data": []}, {"vin": "", "createdAt": at(0), "data": [{"key": "Soc", "value": {"doubleValue": 50.0}}]}, payload(1, Soc=51.0)])
    assert [(r[1], r[2]) for r in recs] == [(ALPHA, {"Soc": 51.0})]


# ---- minors from the review
def test_a_charge_that_loses_the_car_closes_when_it_leaves_not_at_sleep():
    p = charging(0, (1, "Charging", 7.6, 0.1), (30, "Charging", 7.6, 3.8)) + [connectivity(31, "DISCONNECTED")]
    out = run(p, 90)
    last = max(s["time"] for s in out if s["charge_no"])
    assert last == T0 + timedelta(minutes=31), "the session ends where the car went quiet, not 15 minutes later"


def test_an_unknown_detailed_state_does_not_hide_the_plain_one():
    p = [connectivity(0, "CONNECTED"), payload(0, DetailedChargeState="DetailedChargeStateUnknown", ChargeState="ChargeStateCharging", ACChargingPower=7.0)]
    assert run(p, 1)[-1]["charging"]
