from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from manual import charge_from_form, drive_from_form, parse_local

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 17, 22, 0, tzinfo=timezone.utc)
HOME = {"name": "Home", "latitude": 39.78, "longitude": -89.65}


def test_a_local_field_becomes_an_aware_time_in_the_site_zone():
    assert parse_local("2026-09-17T13:30", TZ) == datetime(2026, 9, 17, 13, 30, tzinfo=TZ)
    assert parse_local("", TZ) is None and parse_local("yesterday", TZ) is None


def test_a_charge_by_hand_needs_a_car_times_and_energy_and_lands_where_the_place_is():
    form = {"vin": "V1", "start": "2026-09-16T22:00", "end": "2026-09-17T05:00", "kwh": "42.5", "level_start": "30", "level_end": "80", "type": "ac", "cost": "12.75"}
    row = charge_from_form(form, TZ, NOW, HOME)
    assert row["real_charged"] == 42.5 and row["real_cost"] == 12.75 and row["charging_type"] == "AC" and row["latitude"] == 39.78
    assert row["session_start_date"] < row["session_end_date"] and row["end_level"] == 80
    assert charge_from_form(dict(form, kwh=""), TZ, NOW, HOME).startswith("kWh added is needed")
    assert charge_from_form(dict(form, end="2026-09-16T21:00"), TZ, NOW, HOME) == "the end is before the start"
    assert charge_from_form(dict(form, end="2026-09-20T05:00"), TZ, NOW, HOME) == "the end is in the future"
    assert charge_from_form(dict(form, level_end=""), TZ, NOW, HOME) == "give both battery levels or neither"
    assert charge_from_form(dict(form, cost=""), TZ, NOW, None)["real_cost"] is None
    assert charge_from_form(dict(form, vin=""), TZ, NOW, HOME) == "pick a car"


def test_a_drive_by_hand_lays_its_distance_on_the_last_odometer():
    form = {"vin": "V1", "start": "2026-09-17T08:00", "end": "2026-09-17T08:40", "miles": "26.3", "level_start": "80", "level_end": "71"}
    row = drive_from_form(form, TZ, NOW, HOME, None, 95_400.0)
    assert row["start_odometer"] == 95_400.0 and row["destination_odometer"] == 95_426.3
    assert row["start_latitude"] == 39.78 and row["end_latitude"] is None and row["level_end"] == 71
    assert drive_from_form(form, TZ, NOW, None, None, None)["start_odometer"] == 0.0
    assert drive_from_form(dict(form, miles="0"), TZ, NOW, None, None, None).startswith("distance is needed")
