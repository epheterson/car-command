"""The units and shapes the stream really sends, pinned from two cars' first records
(2026-10-05; values synthesized, every shape as it arrived).

What the first records showed, against the TeslaFi feed's rows for the same cars at the
same minute:
- Odometer and RatedRange are miles: equal to TeslaFi's odometer and battery_range to the
  hundredth on both cars.
- OutsideTemp and InsideTemp are °C: equal to TeslaFi's Celsius outside_temp and inside_temp.
- BatteryLevel and Soc are percent (doubles); ChargeLimitSoc is an intValue; EnergyRemaining is kWh.
- PackCurrent is negative while the pack discharges: parked and unplugged, -0.2 to -0.9 A,
  never above 0 (tesla_stream.PACK_SIGN).
- Parked, Gear, VehicleSpeed and FastChargerType arrive as {"invalid": true}.
- ChargeState is a stringValue ("Idle"), DetailedChargeState a detailedChargeStateValue,
  SentryMode a sentryModeStateValue.
- Every V record carries isResend (a boolean); connectivity records carry a connectionId
  and a networkInterface ("wifi", "cellular" or ""); an errors record can hold an empty list.
- VehicleSpeed's unit could not be seen yet (both cars were parked): the documented mph is
  assumed, as the odometer's miles suggest."""

import json
from datetime import datetime, timedelta, timezone

import tesla_live
import tesla_stream as ts

ALPHA = "5YJ3E1EA0JF000001"
T0 = datetime(2026, 10, 5, 4, 16, 10, 995116, tzinfo=timezone.utc)


def stamp(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f") + "090Z"  # the receiver writes nanoseconds


def snapshot(t=T0) -> dict:
    """The full record a car sends when it connects, in the order and shapes it came."""
    return {
        "vin": ALPHA,
        "createdAt": stamp(t),
        "isResend": False,
        "data": [
            {"key": "ACChargingEnergyIn", "value": {"doubleValue": 41.697389510224646}},
            {"key": "ACChargingPower", "value": {"doubleValue": 0}},
            {"key": "BatteryLevel", "value": {"doubleValue": 57.808398950131235}},
            {"key": "ChargeAmps", "value": {"doubleValue": 0}},
            {"key": "ChargeLimitSoc", "value": {"intValue": 80}},
            {"key": "ChargeState", "value": {"stringValue": "Idle"}},
            {"key": "ChargerVoltage", "value": {"doubleValue": 1.550999991595745}},
            {"key": "DCChargingEnergyIn", "value": {"doubleValue": 38.07999914884567}},
            {"key": "DCChargingPower", "value": {"doubleValue": 0}},
            {"key": "DetailedChargeState", "value": {"detailedChargeStateValue": "DetailedChargeStateDisconnected"}},
            {"key": "EnergyRemaining", "value": {"doubleValue": 36.4199991863966}},
            {"key": "FastChargerPresent", "value": {"booleanValue": False}},
            {"key": "FastChargerType", "value": {"invalid": True}},
            {"key": "Gear", "value": {"invalid": True}},
            {"key": "GpsHeading", "value": {"doubleValue": 192.52173374159145}},
            {"key": "InsideTemp", "value": {"doubleValue": 34.80000111460686}},
            {"key": "Location", "value": {"locationValue": {"latitude": 39.7817, "longitude": -89.6501}}},
            {"key": "Odometer", "value": {"doubleValue": 12345.67389997679}},
            {"key": "OutsideTemp", "value": {"doubleValue": 27}},
            {"key": "PackCurrent", "value": {"doubleValue": -0.800000011920929}},
            {"key": "PackVoltage", "value": {"doubleValue": 390.02999123744667}},
            {"key": "RatedRange", "value": {"doubleValue": 143.9234661764028}},
            {"key": "SentryMode", "value": {"sentryModeStateValue": "SentryModeStateOff"}},
            {"key": "Soc", "value": {"doubleValue": 57.74278215223097}},
            {"key": "VehicleName", "value": {"stringValue": "Alpha"}},
            {"key": "VehicleSpeed", "value": {"invalid": True}},
            {"key": "Version", "value": {"stringValue": "2026.26.6.5"}},
        ],
    }


def connectivity(t, status, interface="wifi") -> dict:
    return {"vin": ALPHA, "status": status, "createdAt": stamp(t), "connectionId": "1dc711a6-0000-4cf5-8da1-c1c543778e8d", "networkInterface": interface}


def parked_half_hour() -> list[dict]:
    """Connected, the snapshot, then the pack's voltage and current about once a second."""
    out = [connectivity(T0 - timedelta(seconds=5), "CONNECTED"), snapshot()]
    for i in range(1, 600):
        t = T0 + timedelta(seconds=i)
        out.append({"vin": ALPHA, "createdAt": stamp(t), "isResend": False, "data": [{"key": "PackCurrent", "value": {"doubleValue": -0.2 - (i % 8) * 0.1}}]})
    return out + [connectivity(T0 + timedelta(minutes=10, seconds=24), "DISCONNECTED")]


def last_sample():
    recs = ts.records([snapshot()])
    return ts.samples(recs, until=T0 + timedelta(seconds=1))[-1]


def test_odometer_and_rated_range_are_miles_and_pass_through_unchanged():
    s = last_sample()
    assert s["odometer"] == 12345.67389997679 and s["range"] == 143.9234661764028


def test_temperatures_are_celsius_and_become_fahrenheit():
    s = last_sample()
    assert s["outside_f"] == 80.6 and s["inside_f"] == 94.64


def test_a_parked_unplugged_car_is_neither_driving_nor_charging():
    s = last_sample()
    assert not s["driving"] and not s["charging"] and s["charging_state"] == "Disconnected"
    assert s["speed"] is None, "an invalid speed is no speed"
    assert s["sentry_raw"] == "false" and s["software"] == "2026.26.6.5" and s["name"] == "Alpha"
    assert s["level"] == 57.808398950131235 and s["latitude"] == 39.7817


def test_every_value_shape_decodes():
    fields = ts.records([snapshot()])[0][2]
    assert fields["ChargeLimitSoc"] == 80 and fields["ChargeState"] == "Idle"
    assert fields["Gear"] is ts.INVALID and fields["VehicleSpeed"] is ts.INVALID and fields["FastChargerType"] is ts.INVALID
    assert fields["FastChargerPresent"] is False and fields["SentryMode"] == "Off"


def test_the_pack_discharging_while_parked_is_power_drawn_not_regeneration():
    recs = ts.records(parked_half_hour())
    samples = ts.samples(recs, until=T0 + timedelta(minutes=11))
    drawn = [s["power_kw"] for s in samples if s["power_kw"] is not None and not s["asleep"]]
    assert drawn and all(p >= 0 for p in drawn), "current out of the pack reads negative; the sign makes it power drawn"
    assert 0.05 < max(drawn) < 0.4, "a parked car's electronics, a few hundred watts"


def test_the_records_arrive_with_their_times_flags_and_kinds_as_the_receiver_sends_them():
    for kind, rec in (("V", snapshot()), ("connectivity", connectivity(T0, "CONNECTED", "cellular")), ("errors", {"vin": ALPHA, "createdAt": stamp(T0), "errors": []})):
        got = tesla_live.parse([f"tesla_telemetry_{kind}".encode(), json.dumps(rec).encode()], T0 + timedelta(hours=1))
        assert got[0] == kind and got[1] == ALPHA and abs((got[2] - T0).total_seconds()) < 1e-3, "the car's own time, nanoseconds cut"
    recs = ts.records([connectivity(T0, "CONNECTED"), connectivity(T0 + timedelta(minutes=10), "DISCONNECTED", "")])
    assert [r[2] for r in recs] == [{"_connected": True}, {"_connected": False}]
