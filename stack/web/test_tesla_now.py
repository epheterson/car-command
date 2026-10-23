"""The rest of a Tesla's now from its stream: climate, charging detail, the port, range, a waiting update."""

from datetime import datetime, timedelta, timezone

import activity
import tesla_live
import tesla_net
import tesla_stream
import units

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)


def test_the_new_enums_read_by_their_meaning():
    assert tesla_stream.value_of({"hvacPowerValue": "HvacPowerStatePrecondition"}) == "Precondition"
    assert tesla_stream.value_of({"climateKeeperModeValue": "ClimateKeeperModeStateDog"}) == "Dog"
    assert tesla_stream.value_of({"defrostModeValue": "DefrostModeStateMax"}) == "Max"
    assert tesla_stream.value_of({"chargePortLatchValue": "ChargePortLatchEngaged"}) == "Engaged"
    assert tesla_stream.value_of({"cabin_overheat_protection_mode_value": "CabinOverheatProtectionModeStateFanOnly"}) == "FanOnly"


def test_the_car_is_asked_for_them_and_keeps_them():
    asked = {"HvacPower", "ClimateKeeperMode", "DefrostMode", "ChargePortDoorOpen", "ChargePortLatch", "TimeToFullCharge", "ChargeRateMilePerHour", "DriverSeatOccupied", "SoftwareUpdateVersion"}
    assert asked <= set(tesla_net.FIELDS) and asked <= set(tesla_live.LIVE_FIELDS)
    assert {"RatedRange", "ACChargingPower", "ChargerVoltage", "Version"} <= set(tesla_live.LIVE_FIELDS)


def fresh(**kw):
    return activity.stream_now({"_at": NOW - timedelta(minutes=1), **kw}, NOW)


def test_climate_charging_and_the_driver_while_awake():
    st = fresh(HvacPower="On", InsideTemp=20.0, HvacLeftTemperatureRequest=21.0, ClimateKeeperMode="Dog", DefrostMode="Off", DriverSeatOccupied=True)
    assert st["climate"] == "on" and st["climate_set_f"] == 69.8 and st["climate_keeper"] == "dog" and st["window_heating"] is False and st["driver_present"] is True
    st = fresh(ACChargingPower=7.2, ChargerVoltage=240.0, ChargeAmps=30.0, ACChargingEnergyIn=12.34, TimeToFullCharge=1.5, ChargeRateMilePerHour=29.0, ChargeCurrentRequest=32.0)
    assert st["charging_power_kw"] == 7.2 and st["charger_volts"] == 240.0 and st["charger_amps"] == 30.0 and st["charge_session_kwh"] == 12.34
    assert st["charge_minutes_left"] == 90 and st["charge_rate_mph"] == 29.0 and st["charge_amps_set"] == 32.0


def test_cabin_overheat_protection_is_not_climate():
    st = fresh(HvacPower="OverheatProtect")
    assert st["climate"] == "overheat" and units.climate_active(st["climate"]) is False
    assert units.climate_active("precondition") is True and units.climate_active("on") is True


def test_port_range_and_a_waiting_update_stand_while_the_car_sleeps():
    old = {
        "_at": NOW - timedelta(hours=5),
        "ChargePortDoorOpen": False,
        "ChargePortLatch": "Engaged",
        "RatedRange": 210.0,
        "BatteryLevel": 70.0,
        "Version": "2026.32.5 abc123",
        "SoftwareUpdateVersion": "2026.38.1",
    }
    st = activity.stream_now(old, NOW)
    assert st["port_open"] is False and st["cable_latched"] is True and st["range_mi"] == 210.0 and st["range_full_mi"] == 300.0
    assert st["software"] == "2026.32.5" and st["update_available"] is True
    assert activity.stream_now({**old, "SoftwareUpdateVersion": ""}, NOW)["update_available"] is False
    assert activity.stream_now({**old, "SoftwareUpdateVersion": "2026.32.5"}, NOW)["update_available"] is False  # the one it runs
    assert "climate" not in st and "charging_power_kw" not in st  # these need a fresh word
