from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from units import c_to_f, classify_status, humanize_age, km_to_mi, rated_mile_kwh, ribbon_segments

T0 = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)


def test_unit_conversions():
    assert km_to_mi(1609.344) == pytest.approx(1000)
    assert c_to_f(25.8) == pytest.approx(78.44)
    assert km_to_mi(None) is None and c_to_f(None) is None


@pytest.mark.parametrize(
    "state,conn,charging,expected",
    [
        ("offline", "offline", "off", "Sleeping"),
        ("parked", "reachable", "off", "Idle"),
        ("driving", "reachable", "off", "Driving"),
        ("parked", "reachable", "charging", "Charging"),
        ("offline", "offline", "charging", "Charging"),
        ("ignition_on", "reachable", "off", "Ready"),
        (None, None, None, "Sleeping"),
    ],
)
def test_classify_status(state, conn, charging, expected):
    assert classify_status(state, conn, charging) == expected


def test_ribbon_overlays_charging_and_merges_neighbours():
    h = timedelta(hours=1)
    states = [(T0, T0 + 6 * h, "OFFLINE"), (T0 + 6 * h, T0 + 7 * h, "DRIVING"), (T0 + 7 * h, T0 + 24 * h, "PARKED")]
    charging = [(T0 + 8 * h, T0 + 10 * h, "CHARGING")]
    segs = ribbon_segments(states, charging, T0, T0 + 24 * h)
    labels = [(s["label"], round(s["width"] * 24)) for s in segs]
    assert labels == [("sleeping", 6), ("driving", 1), ("idle", 1), ("charging", 2), ("idle", 14)]
    assert sum(s["width"] for s in segs) == pytest.approx(1.0)


def test_ribbon_gap_is_unknown_and_rows_outside_window_are_clipped():
    h = timedelta(hours=1)
    segs = ribbon_segments([(T0 - 5 * h, T0 + 2 * h, "PARKED")], [], T0, T0 + 4 * h)
    assert [(s["label"], round(s["width"] * 4)) for s in segs] == [("idle", 2), ("unknown", 2)]


def test_humanize_age():
    assert humanize_age(timedelta(seconds=30)) == "just now"
    assert humanize_age(timedelta(minutes=5)) == "5 min ago"
    assert humanize_age(timedelta(hours=3)) == "3 h ago"


from units import current_streak, daily_breakdown

PT = ZoneInfo("America/Los_Angeles")


def test_daily_breakdown_counts_hours_and_wakeups_per_local_day():
    h = timedelta(hours=1)
    # Local midnight Sep 3 PDT = 07:00 UTC.
    d0 = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
    states = [(d0, d0 + 8 * h, "OFFLINE"), (d0 + 8 * h, d0 + 9 * h, "DRIVING"), (d0 + 9 * h, d0 + 20 * h, "OFFLINE"), (d0 + 20 * h, d0 + 21 * h, "PARKED"), (d0 + 21 * h, d0 + 30 * h, "OFFLINE")]
    charging = [(d0 + 20 * h, d0 + 21 * h, "CHARGING")]
    now = d0 + 30 * h  # 06:00 local on Sep 4
    rows = daily_breakdown(states, charging, days=2, tz=PT, now=now)
    assert [r["date"].isoformat() for r in rows] == ["2026-09-03", "2026-09-04"]
    sep3 = rows[0]
    assert sep3["hours"]["sleeping"] == pytest.approx(22)
    assert sep3["hours"]["driving"] == pytest.approx(1)
    assert sep3["hours"]["charging"] == pytest.approx(1)
    assert sep3["wakeups"] == 2
    assert sep3["asleep_pct"] == pytest.approx(22 / 24 * 100)
    sep4 = rows[1]
    assert sep4["span_h"] == pytest.approx(6)
    assert sep4["hours"]["sleeping"] == pytest.approx(6)
    assert sep4["wakeups"] == 0
    assert [seg["label"] for seg in sep3["segments"]] == ["sleeping", "driving", "sleeping", "charging", "sleeping"]


def test_daily_breakdown_reports_unknown_before_logging_started():
    d0 = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
    rows = daily_breakdown([(d0 + timedelta(hours=20), d0 + timedelta(hours=23), "OFFLINE")], [], days=1, tz=PT, now=d0 + timedelta(hours=23))
    assert rows[0]["hours"]["unknown"] == pytest.approx(20)
    assert rows[0]["hours"]["sleeping"] == pytest.approx(3)
    assert rows[0]["asleep_pct"] == pytest.approx(100)


def test_current_streak_uses_the_open_row():
    now = datetime(2026, 9, 4, 6, 0, tzinfo=timezone.utc)
    states = [(now - timedelta(hours=5), now - timedelta(hours=2), "PARKED"), (now - timedelta(hours=2), now, "OFFLINE")]
    label, since = current_streak(states, now)
    assert label == "sleeping" and since == timedelta(hours=2)
    assert current_streak([(now - timedelta(hours=5), now - timedelta(hours=1), "PARKED")], now) is None


from units import charge_stats, trip_stats


def test_trip_stats_speed_energy_efficiency():
    st = trip_stats(miles=30.0, duration=timedelta(minutes=45), level_start=80.0, level_end=70.0, capacity_kwh=117.2)
    assert st["avg_mph"] == pytest.approx(40)
    assert st["used_pct"] == pytest.approx(10)
    assert st["kwh"] == pytest.approx(11.72)
    assert st["wh_per_mi"] == pytest.approx(390.67, rel=1e-3)
    assert trip_stats(None, None, None, None, None) == {"avg_mph": None, "used_pct": None, "kwh": None, "wh_per_mi": None, "rated_used": None, "rated_efficiency": None}
    assert trip_stats(0.2, timedelta(minutes=3), 50, 49, 100)["wh_per_mi"] is None  # too short to mean anything


def test_charge_stats_integrates_power_within_the_session():
    h = timedelta(hours=1)
    powers = [(T0 - h, T0 + h, 10.0), (T0 + h, T0 + 3 * h, 5.0), (T0 + 3 * h, T0 + 5 * h, 0.0)]
    st = charge_stats(powers, start=T0, end=T0 + 3 * h, level_start=40.0, level_end=60.0)
    assert st["kwh"] == pytest.approx(20)  # 1h at 10 (clipped) + 2h at 5
    assert st["peak_kw"] == 10.0
    assert st["avg_kw"] == pytest.approx(20 / 3)
    assert st["gained_pct"] == 20 and st["kwh_per_100"] == pytest.approx(100)
    assert charge_stats([], T0, T0 + h, None, None)["kwh"] == 0
    paused = [(T0, T0 + 2 * h, 10.0), (T0 + 2 * h, T0 + 10 * h, 0.0), (T0 + 10 * h, T0 + 12 * h, 10.0)]
    assert charge_stats(paused, T0, T0 + 12 * h, 40.0, 90.0)["avg_kw"] == pytest.approx(10.0), "the eight-hour pause is not part of the average"


from units import idle_periods


def test_idle_periods_split_on_driving_and_charging_and_measure_drop():
    h = timedelta(hours=1)
    states = [
        (T0, T0 + 10 * h, "OFFLINE"),
        (T0 + 10 * h, T0 + 11 * h, "DRIVING"),
        (T0 + 11 * h, T0 + 12 * h, "PARKED"),
        (T0 + 12 * h, T0 + 20 * h, "OFFLINE"),
        (T0 + 20 * h, T0 + 21 * h, "PARKED"),
        (T0 + 21 * h, T0 + 30 * h, "OFFLINE"),
    ]
    charging = [(T0 + 20 * h, T0 + 21 * h, "CHARGING")]
    levels = [(T0, 80.0), (T0 + 9 * h, 79.0), (T0 + 11 * h, 70.0), (T0 + 19 * h, 68.0), (T0 + 21 * h, 90.0), (T0 + 29 * h, 89.0)]
    now = T0 + 30 * h
    ps = idle_periods(states, levels, charging, now)
    assert [(round(p["hours"]), p["drop_pct"]) for p in ps] == [(9, 1.0), (9, 2.0), (10, 1.0)]
    assert ps[0].get("open") is True  # still parked now
    assert ps[1]["asleep_share"] == pytest.approx(8 / 9)  # 8 h OFFLINE of a 9 h idle
    assert ps[1]["drop_per_day"] == pytest.approx(2.0 / 9 * 24)


def test_idle_periods_ignore_short_stops():
    h = timedelta(hours=1)
    states = [(T0, T0 + 0.5 * h, "PARKED"), (T0 + 0.5 * h, T0 + 1 * h, "DRIVING"), (T0 + 1 * h, T0 + 5 * h, "OFFLINE")]
    ps = idle_periods(states, [(T0, 50.0)], [], T0 + 5 * h)
    assert len(ps) == 1 and ps[0]["hours"] == pytest.approx(4)


def test_days_between_stops_at_now_and_covers_a_whole_past_month():
    from datetime import date
    from units import days_between
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)  # Sep 4, 23:00 PDT
    states = [(datetime(2026, 8, 1, 7, 0, tzinfo=timezone.utc), now, "OFFLINE")]
    days = days_between(states, [], date(2026, 8, 1), date(2026, 8, 31), tz, now)
    assert len(days) == 31 and days[0]["date"] == date(2026, 8, 1) and all(round(d["span_h"]) == 24 for d in days)
    days = days_between(states, [], date(2026, 9, 1), date(2026, 9, 30), tz, now)
    assert len(days) == 4 and round(days[-1]["span_h"]) == 23  # today ends at now, tomorrow does not exist yet


def test_drive_energy_prefers_rated_range_over_nominal_capacity():
    from units import drive_kwh, trip_stats

    assert round(drive_kwh(47.0, 35.0, 75.0, 118.0, 87.7, 0.2475), 2) == 7.50  # 30.3 rated miles x 0.2475
    assert round(drive_kwh(47.0, 35.0, 75.0), 2) == 9.0  # no rated data: 75 kWh x 12 %
    assert drive_kwh(None, None, 75.0) is None and drive_kwh(35.0, 47.0, 75.0) is None  # charging is not a drive
    st = trip_stats(29.2, timedelta(minutes=36), 47.0, 35.0, 75.0, 118.0, 87.7, 0.2475)
    assert round(st["wh_per_mi"]) == 257 and round(st["rated_used"], 1) == 30.3 and round(st["rated_efficiency"], 2) == 0.96


def test_electrics_stats_are_time_weighted():
    from units import electrics_stats

    t = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    rows = [(t, t + timedelta(hours=1), 240.0, 48.0), (t + timedelta(hours=1), t + timedelta(hours=3), 236.0, 30.0)]
    st = electrics_stats(rows, t, t + timedelta(hours=3))
    assert round(st["avg_volts"], 1) == 237.3 and st["max_volts"] == 240.0 and st["avg_amps"] == 36.0 and st["max_amps"] == 48.0
    assert electrics_stats([], t, t + timedelta(hours=1))["avg_volts"] is None


def test_a_drive_is_priced_by_what_a_rated_mile_cost_that_year():
    """The one systematic error left in the energy numbers: an all-time constant put a
    2018 drive on the 2026 figure. The Model 3's own charges measure 0.2524 kWh per
    rated mile in 2018 and 0.2445 in 2026, so that drive read about 2 % cheap."""
    tz = ZoneInfo("America/Los_Angeles")
    by_year = {("V", 2018): 0.2524, ("V", 2023): 0.2427, ("V", 2026): 0.2445}
    at = lambda y, m, d, h: datetime(y, m, d, h, tzinfo=timezone.utc)  # noqa: E731
    assert rated_mile_kwh(by_year, "V", at(2018, 6, 1, 12), 0.2475, tz) == 0.2524
    assert rated_mile_kwh(by_year, "V", at(2026, 6, 1, 12), 0.2475, tz) == 0.2445


def test_a_year_that_did_not_measure_its_own_keeps_the_all_time_figure():
    """And so does a car with a few months of history, and a row with no date at all."""
    tz = ZoneInfo("America/Los_Angeles")
    by_year = {("V", 2018): 0.2524}
    assert rated_mile_kwh(by_year, "V", datetime(2021, 6, 1, tzinfo=timezone.utc), 0.2475, tz) == 0.2475
    assert rated_mile_kwh(by_year, "OTHER", datetime(2018, 6, 1, tzinfo=timezone.utc), 0.2393, tz) == 0.2393
    assert rated_mile_kwh(by_year, "V", None, 0.2475, tz) == 0.2475
    assert rated_mile_kwh({}, "V", datetime(2018, 6, 1, tzinfo=timezone.utc), None, tz) is None


def test_the_year_is_the_local_one():
    """Every page groups a date in the car's zone, and this has to agree with them or a
    New Year's Eve drive would be priced by a year it does not appear in."""
    tz = ZoneInfo("America/Los_Angeles")
    by_year = {("V", 2018): 0.2524, ("V", 2019): 0.2513}
    # 03:00 UTC on Jan 1 is still Dec 31 in California.
    assert rated_mile_kwh(by_year, "V", datetime(2019, 1, 1, 3, tzinfo=timezone.utc), 0.2475, tz) == 0.2524
    assert rated_mile_kwh(by_year, "V", datetime(2019, 1, 1, 9, tzinfo=timezone.utc), 0.2475, tz) == 0.2513


def test_climate_active_reads_the_state_and_never_invents_off():
    from units import climate_active

    for on in ("heating", "cooling", "ventilation"):
        assert climate_active(on) is True, on
    assert climate_active("off") is False
    for unknown in (None, "invalid", "unknown climatization state", ""):
        assert climate_active(unknown) is None, repr(unknown)


def test_tread_is_stored_in_millimetres_however_it_was_read_off_the_gauge():
    from units import tread_display, tread_mm

    assert tread_mm("8", "32nds") == pytest.approx(6.35)
    assert tread_mm("6.4", "mm") == 6.4
    assert tread_mm("", "mm") is None and tread_mm("lots", "mm") is None and tread_mm("40", "mm") is None
    assert tread_display(6.35, metric=False) == "8/32" and tread_display(6.4, metric=True) == "6.4 mm"
    assert tread_display(tread_mm("8.5", "32nds"), metric=False) == "8.5/32", "a half-32nd survives the round trip"
    assert tread_display(None, metric=False) == ""


def test_idle_details_and_filters():
    from datetime import datetime, timedelta, timezone

    from units import filter_idles, idle_details

    t0 = datetime(2026, 9, 20, 8, tzinfo=timezone.utc)
    h = timedelta(hours=1)
    idles = [{"start": t0, "end": t0 + 10 * h, "hours": 10.0}, {"start": t0 + 12 * h, "end": t0 + 14 * h, "hours": 2.0}, {"start": t0 + 20 * h, "end": t0 + 21 * h, "hours": 1.0}]
    drives = [{"destination_date": t0 - h, "to": "Home"}, {"destination_date": t0 + 11 * h, "to": "Work"}, {"destination_date": t0 + 30 * h, "to": "Lake"}]
    sentry = [(t0, t0 + 8 * h, True), (t0 + 8 * h, t0 + 13 * h, False)]
    got = idle_details(idles, drives, sentry)
    assert [g["place"] for g in got] == ["Home", "Work", "Work"], "the last drive to end before the idle began"
    assert got[0]["sentry_share"] == 0.8 and got[1]["sentry_share"] == 0.0 and got[2]["sentry_share"] == 0.0
    assert [g["place"] for g in filter_idles(got, min_h=2)] == ["Home", "Work"]
    assert [g["place"] for g in filter_idles(got, max_h=2)] == ["Work", "Work"]
    assert [g["hours"] for g in filter_idles(got, place="wor")] == [2.0, 1.0]
    assert [g["place"] for g in filter_idles(got, sentry_only=True)] == ["Home"], "Sentry on for most of it"
    assert filter_idles(got) == got


def test_idles_by_day_puts_each_stop_under_the_local_day_it_began():
    from datetime import date, datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from units import idles_by_day

    la = ZoneInfo("America/Los_Angeles")
    t0 = datetime(2026, 9, 21, 6, tzinfo=timezone.utc)  # 11 pm on the 20th in Los Angeles
    h = timedelta(hours=1)
    idles = [{"start": t0 + 9 * h, "hours": 3.0}, {"start": t0, "hours": 8.0}, {"start": t0 - 72 * h, "hours": 2.0}]
    days = [{"date": date(2026, 9, 21)}, {"date": date(2026, 9, 20)}]
    got = idles_by_day(days, idles, la)
    assert [d["date"] for d in got] == [date(2026, 9, 21), date(2026, 9, 20)], "the days keep their order"
    assert [p["hours"] for p in got[0]["idles"]] == [3.0] and [p["hours"] for p in got[1]["idles"]] == [8.0], "by the local day, not UTC's"
    assert sum(len(d["idles"]) for d in got) == 2, "a stop from before the first day shown belongs to none"


def test_humanize_duration_reads_like_a_person_would_say_it():
    from datetime import timedelta
    from units import humanize_duration

    assert humanize_duration(None) == ""
    assert humanize_duration(timedelta(seconds=20)) == "1 min"
    assert humanize_duration(timedelta(minutes=42)) == "42 min"
    assert humanize_duration(timedelta(hours=2)) == "2 h", "no trailing 0 min"
    assert humanize_duration(timedelta(hours=2, minutes=47)) == "2 h 47 min"
    assert humanize_duration(timedelta(hours=47, minutes=59)) == "47 h 59 min", "under two days stays in hours"
    assert humanize_duration(timedelta(days=7, hours=8)) == "7 days 8 h", "a week reads in days, not 176 h"
    assert humanize_duration(timedelta(days=3, minutes=5)) == "3 days"
