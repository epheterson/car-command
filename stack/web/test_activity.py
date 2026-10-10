"""Now's banners: which cars are out, since when, and only the figures the readings carry."""

from datetime import datetime, timedelta, timezone

import activity
import tesla_nav

NOW = datetime(2026, 10, 6, 17, 30, tzinfo=timezone.utc)


def mins(m):
    return NOW - timedelta(minutes=m)


def car(**kw):
    return {"vin": "V1", "name": "Alpha", "status": "Driving", "updated": mins(1), "live": False, "soc": 70.0, "odometer_mi": 1010.0} | kw


def test_only_a_car_doing_something_now_has_a_banner():
    assert activity.kind(car(), NOW) == "driving"
    assert activity.kind(car(status="Charging"), NOW) == "charging"
    assert activity.kind(car(status="Idle"), NOW) is None and activity.kind(car(status="Sleeping"), NOW) is None
    assert activity.kind(car(updated=mins(40)), NOW) is None, "the newest state row of a car gone quiet is not now"
    assert activity.kind(car(updated=None), NOW) is None


def pos(m, state, odo, level):
    return {"at": mins(m), "state": state, "odometer": odo, "level": level}


def test_the_run_is_the_stretch_the_newest_reading_is_in():
    rows = [pos(0, "Driving", 1010, 70), pos(5, "Driving", 1006, 71), pos(10, "Driving", 1002, 72), pos(14, "Idle", 1000, 72), pos(60, "Driving", 990, 75)]
    first, last = activity.run(rows, "Driving", NOW)
    assert first["at"] == mins(10) and last["at"] == NOW
    assert activity.run(rows, "Charging", NOW) is None
    assert activity.run([pos(30, "Driving", 1, 1)], "Driving", NOW) is None, "a stale newest reading is no run"
    gap = [pos(0, "Driving", 1010, 70), pos(30, "Driving", 1000, 72)]
    assert activity.run(gap, "Driving", NOW)[0]["at"] == NOW, "a hole longer than GAP is a stop between two drives"


def test_position_and_feed_rows_read_the_same():
    assert activity.position_row({"at": NOW, "state": "DRIVING", "odometer": 5.0, "level": 50.0})["state"] == "Driving"
    assert activity.position_row({"at": NOW, "state": "Driving"})["state"] == "Driving"
    r = activity.feed_row({"at": NOW, "car_state": "Charging", "odometer": "1,0", "level": "61", "range": "", "kw": "7.2", "kwh_added": "9.5"})
    assert r["state"] == "Charging" and r["level"] == 61.0 and r["odometer"] is None and r["range"] is None and r["kw"] == 7.2


def test_a_drive_so_far_by_rated_range():
    first = {"at": mins(20), "odometer": 1000.0, "level": 72.0, "range": 220.0}
    last = {"at": NOW, "odometer": 1010.0, "level": 69.0, "range": 210.0}
    b = activity.banner(car(), "driving", (first, last), NOW, route={"name": "Work", "minutes": 8}, capacity_kwh=75.0, kwh_per_rated_mi=0.25)
    assert b["since"] == timedelta(minutes=20) and b["miles"] == 10.0 and b["dest"] == "Work" and b["minutes_left"] == 8
    assert round(b["wh_per_mi"]) == 250 and b["estimate"] is False


def test_a_drive_so_far_by_battery_is_an_estimate_and_needs_a_real_drop():
    first = {"at": mins(20), "odometer": 1000.0, "level": 75.0}
    b = activity.banner(car(soc=70.0), "driving", (first, {"at": NOW, "odometer": 1020.0, "level": 70.0}), NOW, capacity_kwh=80.0)
    assert b["estimate"] is True and round(b["wh_per_mi"]) == 200  # 5 % of 80 kWh over 20 mi
    small = activity.banner(car(soc=74.0), "driving", (first, {"at": NOW, "odometer": 1004.0, "level": 74.0}), NOW, capacity_kwh=80.0)
    assert small["wh_per_mi"] is None and small["estimate"] is False, "one whole-percent step could be anything from 0 to 2"


def test_a_drive_with_nothing_known_says_only_that_it_is_driving():
    b = activity.banner(car(odometer_mi=None, soc=None), "driving", None, NOW)
    assert b["since"] is None and b["miles"] is None and b["wh_per_mi"] is None and b["dest"] is None


def test_a_live_car_reads_now_from_its_own_tree():
    first = {"at": mins(10), "odometer": 1000.0, "level": 75.0}
    b = activity.banner(car(live=True, odometer_mi=1012.5), "driving", (first, {"at": mins(1), "odometer": 1011.0, "level": 75.0}), NOW)
    assert b["miles"] == 12.5


def test_a_charge_so_far():
    first = {"at": mins(95), "level": 41.0}
    last = {"at": NOW, "level": 58.0, "kw": 7.4, "kwh_added": 13.2}
    b = activity.banner(car(status="Charging", soc=None, place="Home"), "charging", (first, last), NOW)
    assert b["place"] == "Home" and b["kw"] == 7.4 and b["level_from"] == 41.0 and b["level_now"] == 58.0 and b["kwh_added"] == 13.2
    assert b["since"] == timedelta(minutes=95)
    idle = activity.banner(car(status="Charging", charging_power_kw=0.0), "charging", None, NOW)
    assert idle["kw"] is None and idle["level_from"] is None


def test_the_signature_names_the_cars_and_what_they_do():
    assert activity.signature([{"vin": "V1", "kind": "driving"}, {"vin": "V2", "kind": "charging"}]) == "V1:driving V2:charging"
    assert activity.signature([]) == ""


def test_sentry_from_the_feed_and_the_stream():
    assert activity.sentry_on("3") is True and activity.sentry_on("true") is True
    assert activity.sentry_on("1") is False, "since 2025 the feed's 1 is idle"
    assert activity.sentry_on("Armed") is True and activity.sentry_on("Off") is False
    assert activity.sentry_on(None) is None and activity.sentry_on("") is None and activity.sentry_on("weird") is None


def test_the_stream_says_driving_only_while_fresh():
    c = {"_at": mins(1), "Gear": "D", "Odometer": 1200.5, "BatteryLevel": 66.0, "SentryMode": "Off"}
    st = activity.stream_now(c, NOW)
    assert st["driving"] is True and st["odometer"] == 1200.5 and st["level"] == 66.0 and st["sentry"] is False
    assert activity.stream_now(dict(c, _at=mins(30)), NOW) == {}
    assert activity.stream_now(None, NOW) == {}
    assert activity.stream_now({"_at": mins(1), "Gear": "P"}, NOW)["driving"] is False


def v(key, value):
    return {"key": key, "value": value}


def test_a_route_from_the_raw_records_after_a_restart():
    recs = [
        (mins(1), {"data": [v("MinutesToArrival", {"doubleValue": 7.6}), v("MilesToArrival", {"doubleValue": 3.1})]}),
        (mins(25), {"data": [v("DestinationName", {"stringValue": "Bravo Market, 12 Elm Street, Springfield"})]}),
    ]
    assert tesla_nav.route_from_records(recs, NOW) == {"name": "Bravo Market", "minutes": 8, "miles": 3.1}
    stale = [(mins(25), recs[1][1]), (mins(30), recs[0][1])]
    assert tesla_nav.route_from_records(stale, NOW) == {"name": "Bravo Market", "minutes": None, "miles": None}
    cleared = [(mins(2), {"data": [v("DestinationName", {"invalid": True})]})] + recs[1:]
    assert tesla_nav.route_from_records(cleared, NOW) is None, "a destination cancelled is no route"
    assert tesla_nav.route_from_records([], NOW) is None
