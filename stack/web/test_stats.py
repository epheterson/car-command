from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from stats import bucket, summarize

PT = ZoneInfo("America/Los_Angeles")
T = datetime(2026, 9, 3, 16, 0, tzinfo=timezone.utc)  # 9am PDT Thursday


def test_buckets_are_local():
    late = datetime(2026, 9, 4, 6, 30, tzinfo=timezone.utc)  # 11:30pm PDT Sep 3
    assert bucket(late, "day", PT).isoformat() == "2026-09-03"
    assert bucket(late, "week", PT).isoformat() == "2026-08-31"  # Monday
    assert bucket(late, "month", PT).isoformat() == "2026-09-01"


def test_summarize_totals_and_ratios():
    drives = [
        {"start_date": T, "destination_date": T + timedelta(minutes=40), "miles": 20.0, "duration": timedelta(minutes=40), "level_start": 80.0, "level_end": 74.0},
        {"start_date": T + timedelta(days=1), "destination_date": T + timedelta(days=1, minutes=30), "miles": 10.0, "duration": timedelta(minutes=30), "level_start": 74.0, "level_end": 71.0},
        {"start_date": T + timedelta(days=2), "destination_date": None, "miles": None, "duration": None},  # in progress: ignored
    ]
    charges = [{"session_start_date": T + timedelta(hours=10), "kwh": 30.0, "cost": 12.0}]
    rows = summarize(drives, charges, "week", PT, capacity_kwh=100.0)
    assert len(rows) == 1
    r = rows[0]
    assert r["drives"] == 2 and r["miles"] == 30.0 and r["drive_h"] == pytest.approx(70 / 60)
    assert r["used_kwh"] == pytest.approx(9.0)
    assert r["wh_per_mi"] == pytest.approx(300.0)
    assert r["charges"] == 1 and r["added_kwh"] == 30.0 and r["cost"] == 12.0
    assert r["cost_per_mi"] == pytest.approx(0.4)
    assert summarize([], [], "day", PT, None) == []


@pytest.mark.usefixtures("springfield_plan")
def test_the_time_of_use_split_answers_the_question_a_bill_asks():
    """Whether you charge in the cheap hours is the whole of a time-of-use bill, and it
    is the thing a logger that takes one flat typed-in rate cannot tell you."""
    from datetime import datetime, timezone

    from stats import tou_split

    t = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)
    charges = [
        {"at_home": True, "session_start_date": t, "off_peak_kwh": 40.0, "part_peak_kwh": 5.0, "peak_kwh": 5.0},
        {"at_home": True, "session_start_date": t, "off_peak_kwh": 50.0, "part_peak_kwh": 0.0, "peak_kwh": 0.0},
        {"at_home": False, "session_start_date": t, "off_peak_kwh": None, "part_peak_kwh": None, "peak_kwh": None},
    ]
    split = tou_split(charges)
    out = split["periods"]
    assert [p["key"] for p in out] == ["off_peak", "part_peak", "peak"], "cheapest first, the way a bill reads"
    by = {p["key"]: p for p in out}
    assert by["off_peak"]["kwh"] == 90.0 and by["peak"]["kwh"] == 5.0
    assert round(split["off_peak_share"], 3) == 0.9, "90 of 100 kWh landed in the cheap hours"
    assert split["excess"] > 0, "energy drawn outside the cheap hours cost more than it would have inside them"
    # 5 kWh of peak costs more than 5 kWh of off-peak would have, which is the whole
    # point of knowing the split.
    assert by["peak"]["cost"] > by["peak"]["kwh"] * by["off_peak"]["rate"]
    assert by["off_peak"]["rate"] < by["part_peak"]["rate"] < by["peak"]["rate"]
    assert round(sum(p["kwh"] for p in out), 1) == 100.0


def test_no_split_is_shown_when_nothing_was_priced():
    from stats import tou_split

    assert tou_split([]) == {}
    assert tou_split([{"at_home": False, "off_peak_kwh": None}]) == {}, "charges away from home carry no rate of yours"


@pytest.mark.usefixtures("springfield_plan")
def test_a_window_with_no_peak_charging_still_renders():
    """This one took the page down. Mid-backfill only a handful of charges had a split,
    all of them off-peak, so the peak rate was derived from zero kWh and came out None —
    and the template divided by it. The rate now comes from the plan, which knows what
    peak costs whether or not you drew any."""
    from datetime import datetime, timezone

    from stats import tou_split

    t = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)
    split = tou_split([{"at_home": True, "session_start_date": t, "off_peak_kwh": 12.0, "part_peak_kwh": 0.0, "peak_kwh": 0.0}])
    assert split["off_peak_share"] == 1.0
    assert split["peak_multiple"] is not None and split["peak_multiple"] > 1, "peak still costs more, even in a month you avoided it"
    assert split["excess"] is None, "nothing was drawn outside the cheap hours, so nothing was overpaid"
    assert all(p["rate"] is not None for p in split["periods"]), "every period knows its rate from the plan"


def test_a_list_sorts_by_a_whitelisted_column_and_nothing_else():
    from stats import DRIVE_SORTS, sort_rows

    rows = [{"start_date": 3, "miles": 12.0, "cost": None}, {"start_date": 1, "miles": 40.0, "cost": 2.0}, {"start_date": 2, "miles": 5.0, "cost": 1.0}]
    out, key, desc = sort_rows(rows, "miles", True, DRIVE_SORTS)
    assert [r["miles"] for r in out] == [40.0, 12.0, 5.0] and (key, desc) == ("miles", True)
    out, key, _ = sort_rows(rows, "__class__", False, DRIVE_SORTS)
    assert key == "start" and [r["start_date"] for r in out] == [1, 2, 3], "an unknown key falls back to the default instead of being trusted"


def test_rows_without_the_value_go_last_either_way():
    """A drive with no cost figure is not the cheapest drive of the month."""
    from stats import DRIVE_SORTS, sort_rows

    rows = [{"start_date": 1, "cost": None}, {"start_date": 2, "cost": 3.0}, {"start_date": 3, "cost": 1.0}]
    assert [r["cost"] for r in sort_rows(rows, "cost", False, DRIVE_SORTS)[0]] == [1.0, 3.0, None]
    assert [r["cost"] for r in sort_rows(rows, "cost", True, DRIVE_SORTS)[0]] == [3.0, 1.0, None]


def test_charging_efficiency_per_place_is_rated_miles_per_kwh_over_matched_pairs():
    from datetime import datetime, timedelta, timezone

    from stats import charging_summary

    t = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = [
        {
            "session_start_date": t,
            "where": "Home",
            "kwh": 40.0,
            "range_added": 160.0,
            "cost": 16.0,
            "start_level": 20,
            "end_level": 80,
            "duration": timedelta(hours=4),
            "charging_type": "AC",
            "at_home": True,
        },
        {
            "session_start_date": t,
            "where": "Home",
            "kwh": 10.0,
            "range_added": None,
            "cost": 4.0,
            "start_level": 70,
            "end_level": 85,
            "duration": timedelta(hours=1),
            "charging_type": "AC",
            "at_home": True,
        },
        {
            "session_start_date": t,
            "where": "Supercharger",
            "kwh": 50.0,
            "range_added": 175.0,
            "cost": None,
            "start_level": 10,
            "end_level": 80,
            "duration": timedelta(minutes=30),
            "charging_type": "DC",
            "at_home": False,
        },
    ]
    places = {p["where"]: p for p in charging_summary(rows)["places"]}
    assert places["Home"]["mi_per_kwh"] == 4.0, "the session with no range figure is left out of the ratio, not counted as zero"
    assert places["Supercharger"]["mi_per_kwh"] == 3.5


def test_the_feed_gets_a_parked_row_for_each_long_gap_with_the_battery_lost():
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from stats import timeline_days

    t0 = datetime(2026, 9, 16, 16, 0, tzinfo=timezone.utc)
    h = timedelta(hours=1)
    drives = [
        {
            "id": 1,
            "vin": "V1",
            "car_name": "Alpha",
            "start_date": t0,
            "destination_date": t0 + 12 * timedelta(minutes=1),
            "from": "Home",
            "to": "Work",
            "miles": 3.2,
            "duration": 12 * timedelta(minutes=1),
            "level_start": 70,
            "level_end": 69,
        },
        {
            "id": 2,
            "vin": "V1",
            "car_name": "Alpha",
            "start_date": t0 + 9 * h,
            "destination_date": t0 + 9 * h + 11 * timedelta(minutes=1),
            "from": "Work",
            "to": "Home",
            "miles": 3.1,
            "duration": 11 * timedelta(minutes=1),
            "level_start": 68,
            "level_end": 67,
        },
        {
            "id": 3,
            "vin": "V2",
            "car_name": "Bravo",
            "start_date": t0 + 9 * h + 20 * timedelta(minutes=1),
            "destination_date": t0 + 10 * h,
            "from": "A",
            "to": "B",
            "miles": 1.0,
            "duration": 40 * timedelta(minutes=1),
            "level_start": 50,
            "level_end": 49,
        },
    ]
    days = timeline_days(drives, [], [{"vin": "V1", "car_name": "Alpha", "version": "1.0", "seen_at": t0 + 3 * h}], ZoneInfo("UTC"))
    events = [e for d in days for e in d["events"]]
    parked = [e for e in events if e["kind"] == "parked"]
    assert len(parked) == 1, "one gap of an hour or more, on one car; Bravo's single drive is open-ended"
    p = parked[0]
    assert p["title"] == "Work" and p["at"] == t0 + 12 * timedelta(minutes=1) and p["end"] == t0 + 9 * h
    assert p["level_start"] == 69 and p["level_end"] == 68 and p["lost"] == 1
    assert [e["kind"] for e in events] == ["drive", "drive", "software", "parked", "drive"], "newest first, the parked row at the moment it began"
    assert sum(d["miles"] for d in days) == 7.3, "parked rows add nothing to the days' totals"
    days = timeline_days(drives, [], [], ZoneInfo("UTC"), parked=False)
    assert not [e for d in days for e in d["events"] if e["kind"] == "parked"], "a feed filtered to one kind cannot tell parked time: the car may have driven between"


def test_the_calendar_grid_can_begin_its_weeks_on_sunday():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from stats import calendar_grid

    w = {"start": datetime(2026, 9, 1, 7, tzinfo=timezone.utc), "end": datetime(2026, 10, 1, 7, tzinfo=timezone.utc)}
    mon = calendar_grid([], [], w, ZoneInfo("America/Los_Angeles"))
    sun = calendar_grid([], [], w, ZoneInfo("America/Los_Angeles"), week_start=6)
    assert mon[0][0]["date"].weekday() == 0 and sun[0][0]["date"].weekday() == 6
    assert all(len(week) == 7 for week in mon + sun)


def test_the_drive_filter_matches_places_both_ways_length_and_words():
    from stats import filter_charges, filter_drives

    rows = [
        {"id": 1, "from": "Home", "to": "Power Plant", "miles": 48.3, "tag": "Commute", "note": None},
        {"id": 2, "from": "Power Plant", "to": "Home", "miles": 47.9, "tag": None, "note": "rain"},
        {"id": 3, "from": "Home", "to": "Maple Street, Shelbyville", "miles": 0.8, "tag": None, "note": None},
    ]
    assert [d["id"] for d in filter_drives(rows, frm="Home")] == [1, 3]
    assert [d["id"] for d in filter_drives(rows, frm="home", to="Power Plant", both_ways=True)] == [1, 2]
    assert [d["id"] for d in filter_drives(rows, min_mi=10)] == [1, 2]
    assert [d["id"] for d in filter_drives(rows, text="rain")] == [2] and [d["id"] for d in filter_drives(rows, text="commute")] == [1]
    assert filter_drives(rows) == rows
    charges = [
        {"id": 1, "where": "Home", "at_home": True, "charging_type": "AC", "kwh": 60.9},
        {"id": 2, "where": "Oak Lane", "at_home": False, "charging_type": "DC", "kwh": 20.0},
        {"id": 3, "where": "Power Plant", "at_home": False, "charging_type": "AC", "kwh": 0.4},
    ]
    assert [c["id"] for c in filter_charges(charges, kind="home")] == [1]
    assert [c["id"] for c in filter_charges(charges, kind="fast")] == [2]
    assert [c["id"] for c in filter_charges(charges, kind="away")] == [3]
    assert [c["id"] for c in filter_charges(charges, min_kwh=1)] == [1, 2] and [c["id"] for c in filter_charges(charges, where="power plant")] == [3]
