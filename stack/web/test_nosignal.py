"""Miles driven out of coverage: the odometer step between two drives is a drive nobody saw.

The shape is 2026-11-03: the car was driving at 46 mph, the cloud then repeated that state for 1 h 45 min, and the next
drive began 6.9 miles up the odometer. Springfield coordinates."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import driverebuild as dr
import nosignal
import notify
import reach

T0 = datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc)
M = timedelta(minutes=1)
LAT, LON = 39.7817, -89.6501


def trip(i, a, b, odo0, odo1, manual=False):
    return {"id": i, "start": T0 + a * M, "end": None if b is None else T0 + b * M, "manual": manual, "odo0": odo0, "odo1": odo1, "lat0": LAT, "lon0": LON, "lat1": LAT + 0.01 * i, "lon1": LON}


def fix(minute, odo=None, level=None):
    return {"at": T0 + minute * M, "lat": LAT, "lon": LON, "odo": odo, "level": level}


# the first drive ends at +60, the car is silent, the next begins at +165
LOST = [trip(1, 0, 60, 2380.0, 2395.57), trip(2, 165, 180, 2402.47, 2410.0)]


def test_the_odometer_step_between_two_drives_is_filled_as_one_estimated_drive():
    (g,) = nosignal.plan(LOST, [fix(59, 2395.5, 80.0), fix(166, 2402.5, 74.0)], capacity=100.0)
    assert (g["start"], g["end"]) == (T0 + 60 * M, T0 + 165 * M), "the bounds are the last fix and the first fresh one"
    assert round(g["miles"], 2) == 6.9 and (g["start_odometer"], g["destination_odometer"]) == (2395.57, 2402.47)
    assert (g["start_lat"], g["end_lat"]) == (LAT + 0.01, LAT), "from where the first drive ended to where the next began"
    assert round(g["kwh"], 1) == 6.0, "the battery's drop (80 to 74 percent) of a 100 kWh pack"


def test_no_battery_reading_means_no_energy_and_a_rise_is_not_a_drop():
    assert nosignal.plan(LOST, [], capacity=100.0)[0]["kwh"] is None
    assert nosignal.plan(LOST, [fix(59, level=70.0), fix(166, level=75.0)], capacity=100.0)[0]["kwh"] is None
    assert nosignal.plan(LOST, [fix(59, level=80.0), fix(166, level=74.0)], capacity=None)[0]["kwh"] is None
    assert nosignal.plan(LOST, [fix(10, level=90.0), fix(166, level=74.0)], capacity=100.0)[0]["kwh"] is None, "a level from long before the silence says nothing about it"


def test_a_small_step_and_a_car_that_slept_fill_nothing():
    rounded = [trip(1, 0, 60, 2380.0, 2395.57), trip(2, 165, 180, 2395.9, 2410.0)]
    assert nosignal.plan(rounded, []) == [], "0.33 mi is a rounded reading"
    slept = [trip(1, 0, 60, 2380.0, 2395.57), trip(2, 600, 620, 2395.57, 2400.0)]
    assert nosignal.plan(slept, []) == [], "asleep overnight: the odometer did not move"
    back = [trip(1, 0, 60, 2380.0, 2395.57), trip(2, 165, 180, 2380.0, 2390.0)]
    assert nosignal.plan(back, []) == [], "an odometer that went down is a bad reading"


def test_a_step_the_time_cannot_hold_is_a_bad_reading_not_a_drive():
    assert nosignal.plan([trip(1, 0, 60, 2380.0, 2395.57), trip(2, 70, 80, 2495.0, 2500.0)], []) == [], "100 miles in 10 minutes"


def test_a_second_run_finds_nothing_and_a_drive_is_never_made_over_another():
    (g,) = nosignal.plan(LOST, [])
    filled = LOST + [trip(3, 60, 165, g["start_odometer"], g["destination_odometer"])]
    assert nosignal.plan(filled, []) == [], "the pair has no step left"
    assert nosignal.plan([trip(1, 0, 60, 2380.0, 2395.57), trip(2, 50, 80, 2402.0, 2410.0)], []) == [], "overlapping drives leave no silence to fill"
    assert nosignal.plan([trip(1, 0, 60, 2380.0, 2395.57), trip(2, 165, None, 2402.47, None)], []) == [], "a drive still going is not a boundary"


def test_a_stretch_is_cut_where_the_car_parked_between_two_readings():
    park = [fix(80, 2397.0), fix(120, 2397.0), fix(130, 2399.0)]  # drove 1.4 mi, sat, drove 2 mi, then 3.5 mi more
    legs = nosignal.plan(LOST, park)
    assert [(g["start"], g["end"], round(g["miles"], 2)) for g in legs] == [(T0 + 60 * M, T0 + 80 * M, 1.43), (T0 + 120 * M, T0 + 130 * M, 2.0), (T0 + 130 * M, T0 + 165 * M, 3.47)]
    assert sum(g["miles"] for g in legs) == pytest.approx(6.9), "the legs add up to the odometer's step"


def test_only_stretches_that_end_since_the_cutoff_are_filled():
    assert nosignal.plan(LOST, [], since=T0 + 200 * M) == []
    assert len(nosignal.plan(LOST, [], since=T0 + 100 * M)) == 1


def test_the_dry_run_line_names_the_stretch():
    (g,) = nosignal.plan(LOST, [])
    assert "6.9 mi" in nosignal.line(g, ZoneInfo("UTC")) and "2395.6 -> 2402.5" in nosignal.line(g, ZoneInfo("UTC"))


# --- the drive still open when the signal goes -----------------------------------------------------


def silent_day():
    states = [{"first": T0, "last": T0, "state": "DRIVING"}]
    fx = [{"at": T0 + i * M, "lat": LAT + i * 1e-4, "lon": LON, "odo": 2380.0 + i * 0.5, "speed": 46.0} for i in range(31)]
    return states, fx


def test_a_drive_open_when_the_signal_goes_ends_at_its_last_fix_not_at_now():
    states, fx = silent_day()
    (d,) = dr.plan_drives(states, fx, now=T0 + 31 * M + dr.SILENCE)
    assert d["end"] == T0 + 30 * M and d["silent"] is True and d["destination_odometer"] == 2395.0
    assert dr.plan_drives(states, fx, now=T0 + 40 * M) == [], "ten minutes of quiet is a red light, not a dead zone: still open"


def test_the_trip_the_plugin_holds_open_is_closed_at_that_fix_not_waited_for():
    states, fx = silent_day()
    wanted = dr.plan_drives(states, fx, now=T0 + 120 * M)
    open_trip = {"id": 9, "start": T0, "end": None, "manual": False}
    (act,) = dr.plan_changes(wanted, [open_trip])
    assert act["do"] == "change" and act["id"] == 9 and act["drive"]["end"] == T0 + 30 * M
    other = dict(wanted[0], silent=False)
    assert dr.plan_changes([other], [open_trip])[0]["do"] == "wait", "a drive that is not silent still waits for its open trip"


def test_a_gap_drive_is_not_the_rebuilds_to_move_and_blocks_a_real_drive_over_it():
    states, fx = silent_day()
    wanted = dr.plan_drives(states, fx, now=T0 + 120 * M)
    gap = {"id": 5, "start": T0 + 10 * M, "end": T0 + 20 * M, "manual": True}
    assert dr.plan_changes(wanted, [gap])[0]["do"] == "hand"


# --- what reacts to a car "driving" -----------------------------------------------------------------


def test_a_stale_driving_car_is_left_alone_by_the_alerts():
    assert reach.stale_drive({"status": "Driving", "offline": True})
    assert not reach.stale_drive({"status": "Driving", "offline": False}) and not reach.stale_drive({"status": "Sleeping", "offline": True})
    saved = []
    w = notify.Watcher(lambda vin: None, lambda vin, text: saved.append(text))
    car = {"vin": "V1", "name": "Alpha", "live": True, "status": "Idle", "soc": 70.0, "place": "Home"}
    w.step(car)
    gone = dict(car, status="Driving", offline=True)
    assert w.step(gone) == [] and w.last_driving("V1") is None, "a stale Driving is neither news nor the car driving now"
    assert [k for k, _ in w.step(dict(car, status="Driving"))] == ["driving"], "the alert fires when the car really is driving"


# --- a charge inside the silence ---------------------------------------------------------------------


def charge(a, b, lat=LAT + 0.05, lon=LON):
    return {"start": T0 + a * M, "end": T0 + b * M, "lat": lat, "lon": lon}


def test_a_charge_inside_the_silence_makes_a_leg_before_and_after_never_over_it():
    legs = nosignal.plan(LOST, [], charges=[charge(100, 130)])
    assert [(g["start"], g["end"]) for g in legs] == [(T0 + 60 * M, T0 + 100 * M), (T0 + 130 * M, T0 + 165 * M)]
    assert sum(g["miles"] for g in legs) == pytest.approx(6.9), "the odometer step is shared out"
    assert legs[0]["destination_odometer"] == pytest.approx(legs[1]["start_odometer"]), "the park at the charger moves nothing"
    assert (legs[0]["end_lat"], legs[1]["start_lat"]) == (LAT + 0.05, LAT + 0.05) and legs[0]["note"] is None
    near = nosignal.plan(LOST, [], charges=[charge(100, 130, lat=LAT + 0.0105)])  # the charger right beside where the first drive ended
    assert len(near) == 1 and near[0]["start"] == T0 + 130 * M and near[0]["miles"] > 6.0, "a leg the distances make under half a mile is dropped, the rest keeps its share"


def test_a_charge_with_no_place_ends_the_drive_at_its_start_and_says_so():
    (g,) = nosignal.plan(LOST, [], charges=[charge(100, 130, lat=None, lon=None)])
    assert (g["start"], g["end"]) == (T0 + 60 * M, T0 + 100 * M) and g["miles"] == pytest.approx(6.9)
    assert "no place" in g["note"] and g["end_lat"] is None


def test_a_charge_outside_the_silence_changes_nothing():
    assert len(nosignal.plan(LOST, [], charges=[charge(10, 30), charge(170, 175)])) == 1


def test_a_stretch_much_longer_than_its_distance_is_a_window_and_a_short_one_is_not():
    assert nosignal.plan(LOST, [])[0]["window"] is True, "105 minutes is over the hour"
    quick = [trip(1, 0, 60, 2380.0, 2395.57), trip(2, 82, 90, 2402.47, 2410.0)]
    assert nosignal.plan(quick, [])[0]["window"] is False, "6.9 miles in 22 minutes is a drive"
    slow = [trip(1, 0, 60, 2380.0, 2395.57), trip(2, 100, 110, 2397.0, 2410.0)]
    assert nosignal.plan(slow, [])[0]["window"] is True, "1.4 miles in 40 minutes is 2 mph"


def test_only_silences_that_began_after_the_floor_are_filled():
    assert nosignal.plan(LOST, [], begun_after=T0 + 61 * M) == []
    assert len(nosignal.plan(LOST, [], begun_after=T0 + 60 * M)) == 1
