"""Chart payloads: thin, carried in, and in the order the script expects."""

from datetime import datetime, timedelta, timezone

from chartdata import lane, payload, thin

T0 = datetime(2026, 9, 26, 7, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


def test_thin_carries_the_last_reading_in_and_keeps_the_last_point():
    series = [(T0 - 3 * H, 59.0), (T0 + H, 57.0), (T0 + 2 * H, 39.0)] + [(T0 + 3 * H + timedelta(seconds=i), 30.0) for i in range(2000)]
    pts = thin(series, T0, T0 + 24 * H, n=100)
    assert pts[0] == [T0.timestamp(), 59.0], "the level at the day's start is the last one before it"
    assert pts[-1][1] == 30.0 and len(pts) <= 102
    assert all(a[0] <= b[0] for a, b in zip(pts, pts[1:]))
    assert thin([], T0, T0 + H) == []
    assert thin([(T0 - H, 50.0)], T0, T0 + H) == [[T0.timestamp(), 50.0]], "asleep all day: one point, a flat line"


def test_lane_names_what_the_car_was_doing():
    segs = [
        {"start": T0, "end": T0 + H, "label": "sleeping"},
        {"start": T0 + H, "end": T0 + 2 * H, "label": "idle"},
        {"start": T0 + 2 * H, "end": T0 + 3 * H, "label": "driving"},
        {"start": T0 + 3 * H, "end": T0 + 4 * H, "label": "charging"},
        {"start": T0 + 4 * H, "end": T0 + 5 * H, "label": "unknown"},
    ]
    assert [k for _a, _b, k in lane(segs)] == ["asleep", "parked", "driving", "charging", "unknown"]
    assert lane(segs)[0][:2] == [T0.timestamp(), (T0 + H).timestamp()]


def test_payload_shape():
    p = payload(
        T0,
        T0 + 24 * H,
        {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": [[T0.timestamp(), 59.0]]},
        [],
        [],
        [{"a": 1, "b": 2, "kind": "drive", "title": "Home → Work", "figs": "3.1 mi"}],
    )
    assert p["t0"] == T0.timestamp() and p["t1"] == (T0 + 24 * H).timestamp()
    assert p["primary"]["key"] == "level" and p["extras"] == [] and p["events"][0]["title"] == "Home → Work"


def test_extras_scale_to_their_own_readings_and_carry_outside_temperature():
    from datetime import datetime, timedelta, timezone

    from chartdata import extras
    from units import Units

    t0 = datetime(2026, 9, 4, 7, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=24)
    ranges = [(t0 + timedelta(hours=1), 452.0), (t0 + timedelta(hours=5), 440.0)]
    temps = [(t0 + timedelta(hours=1), 68.0), (t0 + timedelta(hours=2), -40.0), (t0 + timedelta(hours=6), 86.0)]
    rng, temp = extras(ranges, temps, t0, t1, Units())
    assert rng["key"] == "range" and rng["hi"] >= 452 and rng["hi"] == 500, "a 450-mile car is not pinned to a 400 ceiling"
    assert temp["key"] == "temp" and temp["label"] == "Outside" and temp["unit"] == "°F"
    assert [v for _, v in temp["points"]] == [68.0, 86.0], "-40 is the sensor's no-reading, not weather"
    assert temp["lo"] <= 68 and temp["hi"] >= 86 and temp["lo"] < temp["hi"]
    rng_km, temp_c = extras(ranges, temps, t0, t1, Units(metric=True))
    assert rng_km["unit"] == " km" and rng_km["hi"] >= 727 and temp_c["unit"] == "°C" and round(temp_c["points"][-1][1]) == 30
    assert extras([], [], t0, t1, Units()) == [], "nothing to add, nothing offered"
    assert extras([(t0, 180.0)], [], t0, t1, Units())[0]["hi"] == 200


def test_ticks_suit_the_window_and_follow_local_wall_clock():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from chartdata import ticks

    tz = ZoneInfo("America/Los_Angeles")
    drive = ticks(datetime(2026, 9, 26, 9, 20, tzinfo=tz), datetime(2026, 9, 26, 10, 0, tzinfo=tz), tz)
    assert [k[1] for k in drive] == ["9:20", "9:30", "9:40", "9:50"], "a 40-minute drive gets ten-minute labels"
    charge = ticks(datetime(2026, 9, 27, 0, 16, tzinfo=tz), datetime(2026, 9, 27, 8, 21, tzinfo=tz), tz)
    assert [k[1] for k in charge] == ["2 AM", "4 AM", "6 AM", "8 AM"]
    day = ticks(datetime(2026, 9, 27, tzinfo=tz), datetime(2026, 9, 28, tzinfo=tz), tz)
    assert [k[1] for k in day] == ["12 AM", "6 AM", "12 PM", "6 PM"] and not any(k[2] for k in day)
    fall = ticks(datetime(2026, 11, 1, tzinfo=tz), datetime(2026, 11, 2, tzinfo=tz), tz)
    assert [k[1] for k in fall][:2] == ["12 AM", "6 AM"] and fall[1][0] - fall[0][0] == 7 * 3600, "the 25-hour day keeps wall-clock labels"
    week = ticks(datetime(2026, 9, 21, tzinfo=tz), datetime(2026, 9, 28, tzinfo=tz), tz)
    assert len(week) == 7 and week[0][1] == "Mon 21" and all(k[2] for k in week[1:])


def test_nice_scales_cross_zero_for_regeneration():
    from chartdata import nice

    lo, hi = nice([0.0, 11.4])
    assert lo == 0 and 11.4 <= hi <= 20
    lo, hi = nice([-62.0, 148.0])
    assert lo <= -62 and lo < 0 < hi and hi >= 148, "braking below zero stays on the chart"
    lo, hi = nice([230.0, 245.0], zero=False)
    assert 200 <= lo <= 230 and 245 <= hi <= 260, "a narrow band is not stretched down to zero"
    lo, hi = nice([5.0])
    assert lo < hi


def test_drive_chart_leads_with_battery_and_offers_the_rest():
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from chartdata import drive_chart
    from units import Units

    tz = ZoneInfo("America/Los_Angeles")
    a = datetime(2026, 9, 26, 16, 20, tzinfo=timezone.utc)
    b = a + timedelta(minutes=40)
    at = lambda m: a + timedelta(minutes=m)
    ev = {"a": a.timestamp(), "b": b.timestamp(), "kind": "drive", "title": "Home → Work", "figs": "31.7 mi"}
    p = drive_chart(
        a,
        b,
        [(at(0), 90.0), (at(40), 81.0)],
        [(at(5), 35.0), (at(20), 71.0)],
        [(at(0), 400.0), (at(30), 900.0)],
        [(at(5), 60.0), (at(10), -40.0)],
        [],
        [(at(0), 70.0), (at(40), 72.0)],
        ev,
        Units(),
        tz,
    )
    assert p["primary"]["key"] == "level" and [x["key"] for x in p["extras"]] == ["speed", "elevation", "power", "temp"]
    power = next(x for x in p["extras"] if x["key"] == "power")
    assert power["lo"] < 0 < power["hi"], "regeneration below zero stays on the scale"
    assert p["lane"] == [[a.timestamp(), b.timestamp(), "driving"]] and p["events"] == [ev] and p["ticks"]
    km = drive_chart(a, b, [], [(at(5), 62.0)], [(at(0), 1000.0)], [], [], [], ev, Units(metric=True), tz)
    assert km["primary"]["key"] == "speed" and km["primary"]["unit"] == " km/h" and round(km["primary"]["points"][-1][1]) == 100, "no battery readings: speed leads, in km/h"
    assert km["extras"][0]["unit"] == " m" and round(km["extras"][0]["points"][0][1]) == 305
    empty = drive_chart(a, b, [], [], [], [], [], [], ev, Units(), tz)
    assert empty["primary"]["points"] == [] and empty["extras"] == [], "a drive filled from history: nothing to draw, nothing thrown"


def test_charge_chart_leads_with_power():
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from chartdata import charge_chart
    from units import Units

    a = datetime(2026, 9, 27, 7, 16, tzinfo=timezone.utc)
    b = a + timedelta(hours=8)
    at = lambda h: a + timedelta(hours=h)
    ev = {"a": a.timestamp(), "b": b.timestamp(), "kind": "charge", "title": "Home", "figs": "76.3 kWh"}
    p = charge_chart(
        a, b, [(at(0), 11.4), (at(7), 0.0)], [(at(0), 23.0), (at(8), 90.0)], [(at(0), 80.0), (at(8), 320.0)], [(at(0), 240.0)], [(at(0), 48.0)], ev, Units(), ZoneInfo("America/Los_Angeles")
    )
    assert p["primary"]["key"] == "power" and p["primary"]["lo"] == 0 and p["primary"]["hi"] >= 11.4
    assert [x["key"] for x in p["extras"]] == ["level", "range", "volts", "amps"] and p["lane"][0][2] == "charging"
    km = charge_chart(a, b, [], [(at(0), 23.0)], [(at(0), 80.0)], [], [], ev, Units(metric=True), ZoneInfo("America/Los_Angeles"))
    assert km["primary"]["points"] == [] and km["extras"][1]["unit"] == " km", "no power readings: nothing primary, the rest still offered"


def test_ticks_skip_the_hour_the_clocks_jump_over():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from chartdata import ticks

    tz = ZoneInfo("America/Los_Angeles")
    night = ticks(datetime(2027, 3, 14, 0, 30, tzinfo=tz), datetime(2027, 3, 14, 5, 30, tzinfo=tz), tz)
    assert [k[1] for k in night] == ["1 AM", "3 AM", "4 AM", "5 AM"], "2 AM never happened that night"
    at = [k[0] for k in night]
    assert at == sorted(set(at)), "no two labels on one instant, none out of order"
    quarter = ticks(datetime(2027, 3, 14, 1, 30, tzinfo=tz), datetime(2027, 3, 14, 3, 30, tzinfo=tz), tz)
    assert "2:15" not in [k[1] for k in quarter] and [k[0] for k in quarter] == sorted(k[0] for k in quarter)


def test_ticks_for_weeks_and_months_stay_few_and_read_as_dates():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from chartdata import ticks

    tz = ZoneInfo("America/Los_Angeles")
    month = ticks(datetime(2026, 9, 1, tzinfo=tz), datetime(2026, 10, 1, tzinfo=tz), tz)
    assert 3 <= len(month) <= 7 and month[0][1] == "Sep 1", "a month gets a label a week, from the first midnight at or after the start"
    quarter = ticks(datetime(2026, 7, 1, tzinfo=tz), datetime(2026, 9, 29, tzinfo=tz), tz)
    assert 3 <= len(quarter) <= 7 and all(k[2] for k in quarter[1:])
    year = ticks(datetime(2025, 10, 1, tzinfo=tz), datetime(2026, 10, 1, tzinfo=tz), tz)
    assert 3 <= len(year) <= 13
    decade = ticks(datetime(2018, 8, 29, tzinfo=tz), datetime(2026, 9, 30, tzinfo=tz), tz)
    assert 3 <= len(decade) <= 8, "past seven 30-day steps the step grows instead of falling back to days"
    later = ticks(datetime(2026, 9, 1, 15, 20, tzinfo=tz), datetime(2026, 10, 1, 15, 20, tzinfo=tz), tz)
    assert later[0][1] == "Sep 2" and later[0][2], "a window starting mid-day labels from the next midnight"


def test_a_series_chart_has_no_lane_and_scales_each_line():
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from chartdata import series_chart

    a = datetime(2026, 9, 1, tzinfo=timezone.utc)
    b = a + timedelta(days=30)
    p = series_chart(
        a,
        b,
        {"key": "level", "label": "Battery", "unit": "%", "series": [(a, 80.0), (b, 60.0)], "scale": "pct"},
        [{"key": "range", "label": "Range", "unit": " mi", "series": [(a, 250.0)], "scale": "zero"}, {"key": "temp", "label": "Outside", "unit": "°F", "series": [], "scale": "tight"}],
        ZoneInfo("America/Los_Angeles"),
    )
    assert p["lane"] == [] and p["events"] == [] and p["primary"]["hi"] == 100 and [x["key"] for x in p["extras"]] == ["range"] and 3 <= len(p["ticks"]) <= 7
    empty = series_chart(a, b, {"key": "level", "label": "Battery", "unit": "%", "series": [], "scale": "pct"}, [], ZoneInfo("America/Los_Angeles"))
    assert empty["primary"]["points"] == [] and empty["primary"]["label"] == "Battery"
