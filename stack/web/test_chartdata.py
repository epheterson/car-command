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
    assert p["primary"]["key"] == "level" and [x["key"] for x in p["extras"]] == ["speed", "power", "temp"]
    assert p["strip"]["key"] == "elevation" and p["strip"]["draw"] == "line", "height is a profile under the lane, on the same clock"
    assert next(x for x in p["extras"] if x["key"] == "speed")["draw"] == "line" and "draw" not in p["primary"], "sampled series are lines; the battery stays steps"
    power = next(x for x in p["extras"] if x["key"] == "power")
    assert power["lo"] < 0 < power["hi"], "regeneration below zero stays on the scale"
    assert p["lane"] == [] and p["events"] == [ev] and p["ticks"], "one drive is one state: no lane bar, the card names the drive"
    assert p["strip"]["tone"] == "data", "the height strip is neutral, its dot and card row too"
    km = drive_chart(a, b, [], [(at(5), 62.0)], [(at(0), 1000.0)], [], [], [], ev, Units(metric=True), tz)
    assert km["primary"]["key"] == "speed" and km["primary"]["unit"] == " km/h" and round(km["primary"]["points"][-1][1]) == 100, "no battery readings: speed leads, in km/h"
    assert km["strip"]["unit"] == " m" and round(km["strip"]["points"][0][1]) == 305
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
    assert [x["key"] for x in p["extras"]] == ["level", "range", "volts", "amps"] and p["lane"] == [] and p["events"][0]["kind"] == "charge"
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


def test_annotations_are_optional_and_checked():
    from chartdata import BAND_KINDS, annotations

    base = payload(T0, T0 + H, {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": []}, [], [], [])
    assert not {"marks", "bands", "avg", "on", "xkind"} & set(base), "nothing given, nothing carried"
    got = annotations(
        marks=[[T0 + H, "2026.32.1"], [T0.timestamp(), "Jan"], [T0, ""], ["noon", "x"], {"t": 1}],
        bands=[[T0, T0 + H, "driving", "Home → Work"], [T0 + H, T0, "charging"], [T0, T0 + H, "lava"], [T0, T0 + H, "note"]],
        avg={"value": 62.5},
    )
    assert got["marks"] == [[T0.timestamp(), "Jan"], [(T0 + H).timestamp(), "2026.32.1"]], "in x order, the blank and malformed left out"
    assert got["bands"] == [[T0.timestamp(), (T0 + H).timestamp(), "driving", "Home → Work"], [T0.timestamp(), (T0 + H).timestamp(), "note", ""]]
    assert {b[2] for b in got["bands"]} <= set(BAND_KINDS), "a backwards span or an unknown kind is dropped"
    assert got["avg"] == {"value": 62.5, "label": "Average"}
    assert annotations(avg={"value": float("nan")}) == {} and annotations(avg={"value": True}) == {}


def test_every_extra_has_its_own_tone_and_the_first_shown_must_exist():
    lines = [{"key": k, "label": k, "unit": "", "lo": 0, "hi": 1, "points": []} for k in ("range", "tire_fl", "temp", "tire_fr")]
    p = payload(T0, T0 + H, lines[0], lines, [], [], on=["temp", "nope"])
    assert [x["tone"] for x in p["extras"]] == ["range", "x0", "temp", "x1"], "a known series keeps its colour on every page; the rest take spares in turn"
    assert p["on"] == ["temp"]
    assert "tone" not in lines[0], "the caller's dicts are not changed"


def test_a_chart_along_the_odometer():
    from chartdata import dist_ticks, distance_payload, thin_x

    ticks = dist_ticks(12_345, 98_765)
    assert [k[1] for k in ticks] == ["20k", "40k", "60k", "80k"] and all(12_345 <= k[0] <= 98_765 for k in ticks)
    assert [k[1] for k in dist_ticks(0, 900)][:3] == ["0", "200", "400"]
    pts = thin_x([(x * 10.0, 380 - x / 100) for x in range(10_000)], 0, 100_000, n=50)
    assert len(pts) <= 52 and pts[-1][0] == 99_990.0
    p = distance_payload(12_345, 98_765, {"key": "full", "label": "Range at 100%", "unit": " mi", "lo": 300, "hi": 400, "points": pts}, [], ticks, " mi", marks=[[50_000, "2025"]])
    assert p["xkind"] == "dist" and p["xunit"] == " mi" and p["t0"] == 12_345 and p["t1"] == 98_765
    assert p["lane"] == [] and p["events"] == [] and p["marks"] == [[50_000.0, "2025"]]


def test_the_payload_names_the_zone_its_ticks_are_in():
    """The hover card formats its time in this zone (chart.js), so it says the same clock as
    the axis whatever zone the browser is in."""
    from zoneinfo import ZoneInfo

    from chartdata import charge_chart, drive_chart, series_chart
    from units import Units

    tz = ZoneInfo("America/Chicago")
    line = {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": []}
    assert payload(T0, T0 + H, line, [], [], [], tz=tz)["tz"] == "America/Chicago"
    assert "tz" not in payload(T0, T0 + H, line, [], [], []), "no zone given, none claimed"
    ev = {"a": T0.timestamp(), "b": (T0 + H).timestamp(), "kind": "drive", "title": "", "figs": ""}
    lv = [(T0, 80.0), (T0 + H, 70.0)]
    d = drive_chart(T0, T0 + H, lv, [(T0, 30.0), (T0 + H, 40.0)], [], [], [], [], ev, Units(), tz, on=["speed"])
    assert d["tz"] == "America/Chicago" and d["on"] == ["speed"]
    assert charge_chart(T0, T0 + H, [(T0, 7.0)], lv, [], [], [], dict(ev, kind="charge"), Units(), tz)["tz"] == "America/Chicago"
    assert series_chart(T0, T0 + H, {"key": "level", "label": "Battery", "unit": "%", "series": lv, "scale": "pct"}, [], tz)["tz"] == "America/Chicago"


def test_a_line_keeps_each_slices_highest_and_lowest_reading():
    from chartdata import peaks

    t = [T0 + timedelta(seconds=i) for i in range(4000)]
    speeds = [(x, 52.0 if i == 1234 else 30.0 + (i % 7)) for i, x in enumerate(t)]
    pts = peaks(speeds, T0, t[-1], n=100)
    assert max(v for _s, v in pts) == 52.0, "the top speed survives thinning"
    assert len(pts) <= 4 * 101 and [s for s, _v in pts] == sorted(s for s, _v in pts)


def test_a_charges_sampled_readings_are_lines_and_its_battery_steps():
    from zoneinfo import ZoneInfo

    from chartdata import charge_chart
    from units import Units

    ev = {"a": T0.timestamp(), "b": (T0 + H).timestamp(), "kind": "charge", "title": "", "figs": ""}
    pts = [(T0, 7.0), (T0 + H, 7.2)]
    c = charge_chart(T0, T0 + H, pts, [(T0, 60.0), (T0 + H, 70.0)], [], [(T0, 240.0)], [(T0, 30.0)], ev, Units(), ZoneInfo("America/Chicago"), rate=[(T0, 25.0)])
    kinds = {x["key"]: x.get("draw") for x in [c["primary"], *c["extras"]]}
    assert kinds == {"power": "line", "level": None, "rate": "line", "volts": "line", "amps": "line"} and c["lane"] == []
    assert c["primary"]["gap"] > 3600, "an hour held at one power is two readings an hour apart, not a hole in the line"


def test_a_scale_has_a_round_middle_as_well_as_round_ends():
    from chartdata import nice

    assert nice([296.0, 309.0], zero=False) == (295, 315), "295, 305, 315 rather than 295, 302.5, 310"
    for vals in ([61.0, 79.0], [0.0, 37.0], [512.0, 601.0], [296.0, 309.0]):
        lo, hi = nice(vals, zero=False)
        assert lo <= min(vals) and hi >= max(vals) and ((lo + hi) / 2) % 5 == 0, f"{vals}: {lo}, {(lo + hi) / 2}, {hi}"


def test_a_parked_stretch_draws_its_point_or_two_on_a_snug_axis():
    from chartdata import snug_level_scale

    assert snug_level_scale([77.0, 76.2]) == (76, 78), "a 0.8-point drop fills most of the chart, not a tenth of 75–85"
    assert snug_level_scale([80.0, 80.0]) == (79, 81) and snug_level_scale([99.9, 100.0]) == (98, 100), "never past 100"
    lo, hi = snug_level_scale([61.3, 63.9])
    assert lo <= 61.3 and hi >= 63.9 and (hi - lo) % 2 == 0, "a whole middle label"
    assert snug_level_scale([80.0, 70.0]) == (65.0, 85.0), "a stretch that moved five points or more is scaled like a drive"


def test_volts_held_since_an_earlier_charge_are_still_a_line_on_this_one():
    """cc_charging_electrics keeps runs: a home charger's 240 V can be one row from a charge
    weeks ago to the one after this. Its first date falls before this charge, so fed as a
    reading it was dropped and the Volts and Amps chips never appeared; clipped to the
    session it is a line across the whole charge."""
    from zoneinfo import ZoneInfo

    from chartdata import charge_chart
    from samples import run_points
    from units import Units

    ev = {"a": T0.timestamp(), "b": (T0 + H).timestamp(), "kind": "charge", "title": "", "figs": ""}
    held = [(T0 - 40 * 24 * H, T0 + 9 * 24 * H, 240.0)]
    c = charge_chart(T0, T0 + H, [(T0, 11.0)], [(T0, 60.0)], [], run_points(held, T0, T0 + H), run_points([(f, lst, 46.0) for f, lst, _ in held], T0, T0 + H), ev, Units(), ZoneInfo("America/Chicago"))
    lines = {x["key"]: x for x in c["extras"]}
    assert "volts" in lines and "amps" in lines
    assert lines["volts"]["points"][0][0] == T0.timestamp() and lines["volts"]["points"][-1] == [(T0 + H).timestamp(), 240.0]


def test_ticks_over_two_years_name_the_year():
    from zoneinfo import ZoneInfo

    from chartdata import ticks

    t0 = datetime(2023, 9, 9, tzinfo=timezone.utc)
    out = ticks(t0, datetime(2025, 10, 1, tzinfo=timezone.utc), ZoneInfo("America/Chicago"))
    assert out and all(label[-4:].isdigit() for _, label, _ in out), [label for _, label, _ in out]
