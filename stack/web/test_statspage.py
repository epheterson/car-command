"""The Stats page's month arithmetic."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from statspage import bars, by_month, compare, shift, window_sum
from units import Units


def row(y, m, miles=0.0, drives=0, kwh=0.0, cost=0.0):
    return {"period": date(y, m, 1), "miles": miles, "drives": drives, "added_kwh": kwh, "cost": cost, "used_kwh": miles * 0.3, "drive_h": drives * 0.5, "charges": 1 if kwh else 0}


def test_shift_crosses_years():
    assert shift((2026, 1), -1) == (2025, 12) and shift((2025, 12), 13) == (2027, 1)


def _year_of(focus_miles, last_miles, other_miles, focus=(2026, 10)):
    """A focus month and the twelve before it: last month, then eleven alike."""
    rows = [row(*focus, focus_miles, int(focus_miles / 10), focus_miles * 0.3, focus_miles * 0.03), row(*shift(focus, -1), last_miles, int(last_miles / 10), last_miles * 0.3, last_miles * 0.03)]
    rows += [row(*shift(focus, -i), other_miles, int(other_miles / 10), other_miles * 0.3, other_miles * 0.03) for i in range(2, 14)]
    return by_month(rows)


def test_a_month_in_progress_compares_like_spans_not_whole_months():
    """Today is Oct 5: Oct 1–5 is set beside Sep 1–5 and the average of each month's 1–5,
    never beside all of September."""
    from statspage import heads

    whole = _year_of(60, 1500, 1200)
    like = _year_of(60, 50, 40)  # every month cut to its first five days
    fraction = 4.5 / 31  # mid-day on the 5th
    c = {x["key"]: x for x in compare(whole, (2026, 10), fraction, (2025, 10), like)}
    assert c["miles"]["this"] == 60 and c["miles"]["last"] == 50, "Sep 1–5, not September"
    assert round(c["miles"]["vs_last"], 3) == 0.2 and c["miles"]["n_avg"] == 12
    assert round(c["miles"]["avg"], 2) == round((50 + 11 * 40) / 12, 2), "the 12-month average of days 1–5"
    assert round(c["miles"]["vs_avg"], 3) == round(60 / ((50 + 11 * 40) / 12) - 1, 3)
    assert round(c["cost_per_mi"]["this"], 3) == 0.03 and round(c["cost_per_mi"]["vs_last"], 3) == 0.0, "a rate compares as a rate"
    h = heads((2026, 10), fraction, 5)
    assert h == {"this": "Oct 1–5", "last": "Sep 1–5", "avg": "12-mo avg, 1–5", "partial": True}


def test_a_month_in_progress_projects_its_whole_month_at_the_pace_so_far():
    whole, like = _year_of(60, 1500, 1200), _year_of(60, 50, 40)
    fraction = 5 / 31
    c = {x["key"]: x for x in compare(whole, (2026, 10), fraction, (2025, 10), like)}
    assert round(c["miles"]["proj"]) == 372 and round(c["drives"]["proj"]) == round(6 / fraction), "60 mi in 5 of 31 days is on pace for 372"
    assert c["cost_per_mi"]["proj"] is None, "a rate has no full-month figure"
    early = {x["key"]: x for x in compare(whole, (2026, 10), 0.5 / 31, (2025, 10), like)}
    assert early["miles"]["proj"] is None, "half a day in, a pace says nothing"


def test_a_whole_month_compares_whole_to_whole_with_no_projection():
    from statspage import heads

    whole, like = _year_of(1300, 1500, 1200), _year_of(60, 50, 40)
    c = {x["key"]: x for x in compare(whole, (2026, 9), 1.0, (2025, 9), like)}
    assert c["miles"]["this"] == 1500 and c["miles"]["proj"] is None, "a finished month ignores the like-span rows"
    c = {x["key"]: x for x in compare(whole, (2026, 10), 1.0, (2025, 10))}
    assert c["miles"]["this"] == 1300 and c["miles"]["last"] == 1500 and round(c["miles"]["vs_last"], 3) == round(1300 / 1500 - 1, 3)
    assert heads((2026, 10), 1.0, None) == {"this": "Oct", "last": "Sep", "avg": "12-mo avg", "partial": False}


def test_a_span_label_cuts_to_the_shorter_month():
    from statspage import span_label

    assert span_label((2026, 2), 31) == "Feb 1–28" and span_label((2026, 3), 1) == "Mar 1" and span_label((2024, 2), 30) == "Feb 1–29"


def test_vs_average_bars_scale_to_their_track_and_clamp_with_a_cap():
    from statspage import vbar

    assert vbar(None) is None
    assert vbar(0.12) == {"side": "up", "w": 12.0, "cap": False}
    assert vbar(-0.5) == {"side": "down", "w": 50.0, "cap": False}
    assert vbar(1.45) == {"side": "up", "w": 100.0, "cap": True}, "+145% fills its half and is capped, never longer"
    assert vbar(7.0)["w"] == 100.0 and vbar(0.0)["side"] == "flat"


def test_the_month_in_progress_bar_carries_a_projected_extension():
    months = by_month([row(2026, 10, 60), row(2026, 9, 400), row(2026, 8, 380)])
    g = bars({"V1": months}, ["V1"], (2026, 10), (2026, 8), Units(), n=3, proj=372.0)
    oct_ = g["bars"][-1]
    assert oct_["proj"] and oct_["proj"]["value"] == 372.0 and oct_["proj"]["y"] < oct_["top"], "the extension sits above what it has so far"
    assert round(oct_["proj"]["y"] + oct_["proj"]["h"], 1) == oct_["top"], "and meets it exactly"
    assert g["grid"][-1]["value"] >= 372, "the axis makes room for the projection"
    assert all(b["proj"] is None for b in g["bars"][:-1])
    assert bars({"V1": months}, ["V1"], (2026, 10), (2026, 8), Units(), n=3, proj=50.0)["bars"][-1]["proj"] is None, "a pace below what is done draws nothing"


def test_a_young_car_averages_what_it_has_and_a_first_month_has_no_last():
    months = by_month([row(2026, 9, 300, 30), row(2026, 8, 600, 60), row(2026, 7, 900, 90)])
    c = {x["key"]: x for x in compare(months, (2026, 9), fraction=1.0, first=(2026, 7))}
    assert c["miles"]["n_avg"] == 2 and c["miles"]["avg"] == 750
    first = {x["key"]: x for x in compare(months, (2026, 7), fraction=1.0, first=(2026, 7))}
    assert first["miles"]["last"] is None and first["miles"]["vs_last"] is None and first["miles"]["avg"] is None


def test_a_month_with_nothing_logged_is_a_zero_not_a_gap():
    months = by_month([row(2026, 9, 300, 30), row(2026, 7, 900, 90)])
    c = {x["key"]: x for x in compare(months, (2026, 9), fraction=1.0, first=(2026, 7))}
    assert c["miles"]["last"] == 0 and c["miles"]["vs_last"] is None, "August parked: zero, and no percentage against zero"
    b = bars({"V1": months}, ["V1"], (2026, 9), (2026, 7), Units(), n=3)
    assert [x["label"] for x in b["bars"]] == ["Jul", "Aug", "Sep"] and b["bars"][1]["value"] == 0 and b["bars"][2]["focus"]


def test_bars_stack_by_car_in_display_units_with_an_average_line():
    a = by_month([row(2026, 9, 100), row(2026, 8, 200)])
    b = by_month([row(2026, 9, 50), row(2026, 8, 100)])
    g = bars({"V1": a, "V2": b}, ["V1", "V2"], (2026, 9), (2026, 8), Units(metric=True), n=2)
    sep = g["bars"][-1]
    assert [s["vin"] for s in sep["segs"]] == ["V1", "V2"] and round(sep["value"]) == 241, "150 mi is 241 km"
    assert g["avg"]["value"] is not None and round(g["avg"]["value"]) == round(300 * 1.609344), "the average is of the months before the focus"
    assert [x["vin"] for x in g["legend"]] == ["V1", "V2"] and round(g["legend"][0]["total"]) == round(300 * 1.609344), "Alpha's two bars added up, the focus month included"
    assert all(s["h"] >= 0 for x in g["bars"] for s in x["segs"]) and g["bars"][0]["x"] < g["bars"][1]["x"]


def test_stacked_segments_meet_exactly_and_the_stack_has_one_top():
    """Each segment's bottom is the one below's top, to the tenth the SVG gets, so no seam
    shows between cars; the stack carries its own top for the one rounded cap."""
    cars = {v: by_month([row(2026, 9, m)]) for v, m in (("V1", 100.37), ("V2", 50.61), ("V3", 211.3))}
    g = bars(cars, ["V1", "V2", "V3"], (2026, 9), (2026, 9), Units(), n=1)
    b = g["bars"][-1]
    edge = g["base"]
    for s in b["segs"]:
        assert round(s["y"] + s["h"], 1) == edge, (s, edge)
        edge = s["y"]
    assert b["top"] == edge and b["top"] < g["base"]
    empty = bars({"V1": by_month([row(2026, 8, 100)])}, ["V1"], (2026, 9), (2026, 8), Units(), n=1)["bars"][-1]
    assert empty["top"] == g["base"], "a month with nothing has its top on the axis"


def test_window_sum_takes_the_months_inside_the_window():
    rows = [row(2026, 9, 100, 10), row(2026, 8, 200, 20), row(2025, 9, 400, 40)]
    t = window_sum(rows, date(2025, 10, 1), None)
    assert t["miles"] == 300 and t["drives"] == 30
    assert window_sum(rows, None, date(2026, 9, 1))["miles"] == 600


def test_the_first_logged_month_is_left_out_of_comparisons():
    from statspage import first_full

    months = by_month([row(2026, 8, 300, 10), row(2026, 9, 900, 30)])
    assert first_full(months) == (2026, 9), "logging began mid-August: August is a stub, not a month to compare against"
    c = {x["key"]: x for x in compare(months, (2026, 9), fraction=1.0, first=first_full(months))}
    assert c["miles"]["last"] is None and c["miles"]["avg"] is None and c["miles"]["vs_last"] is None
    assert first_full({}) is None


def test_a_narrow_drawing_for_a_phone_keeps_every_month_with_short_labels():
    months = by_month([row(2026, 9, 100), row(2026, 8, 200)])
    g = bars({"V1": months}, ["V1"], (2026, 9), (2026, 8), Units(), w=360, h=190, short=True)
    assert g["w"] == 360 and len(g["bars"]) == 12 and [b["label"] for b in g["bars"]][-3:] == ["J", "A", "S"]
    assert all(b["x"] + b["w"] <= 360 - g["R"] for b in g["bars"])


def test_a_car_with_years_of_history_is_averaged_over_twelve_whole_months():
    from statspage import fetch_from, first_full

    focus = (2026, 9)
    start = fetch_from(focus)
    months = {}
    ym = start
    while ym <= focus:
        months.update(by_month([row(ym[0], ym[1], 1000, 100)]))
        ym = shift(ym, 1)
    c = {x["key"]: x for x in compare(months, focus, 1.0, max(first_full(months), shift(focus, -12)))}
    assert c["miles"]["n_avg"] == 12, "the month the fetch opens on is the stub skipped, not a real one"


TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 5, 14, 0, tzinfo=TZ)


def test_range_presets_resolve_in_local_time():
    from statspage import resolve

    r = resolve("month", None, None, NOW)
    assert r["since"] == datetime(2026, 10, 1, tzinfo=TZ) and r["until"] is None and r["in_progress"] and r["label"] == "Oct 1–5, 2026"
    r = resolve("last-month", None, None, NOW)
    assert (r["since"], r["until"], r["label"]) == (datetime(2026, 9, 1, tzinfo=TZ), datetime(2026, 10, 1, tzinfo=TZ), "Sep 1–30, 2026")
    assert resolve("30d", None, None, NOW)["since"] == datetime(2026, 9, 6, tzinfo=TZ) and resolve("30d", None, None, NOW)["label"] == "Sep 6 – Oct 5, 2026"
    assert resolve("last-year", None, None, NOW)["label"] == "Jan 1 – Dec 31, 2025"
    assert resolve(None, None, None, NOW)["key"] == "year" and resolve("all", None, None, NOW)["label"] == "All time"
    r = resolve(None, date(2025, 12, 20), date(2026, 1, 4), NOW)
    assert r["key"] == "custom" and r["until"] == datetime(2026, 1, 5, tzinfo=TZ) and r["label"] == "Dec 20, 2025 – Jan 4, 2026", "dates alone are a custom range, `to` inclusive"
    assert resolve("custom", date(2026, 9, 1), date(2026, 10, 5), NOW)["until"] is None, "a range reaching today is still running"
    assert resolve("bogus", None, None, NOW)["key"] == "year"


def test_a_running_range_compares_span_for_span():
    from statspage import resolve, versus

    month = resolve("month", None, None, NOW)
    prev = versus(month, "prev")
    assert prev["since"] == datetime(2026, 9, 1, tzinfo=TZ) and prev["until"] == datetime(2026, 9, 5, 14, 0, tzinfo=TZ) and prev["label"] == "Sep 1–5, 2026", "Oct 1–5 against Sep 1–5"
    yoy = versus(month, "yoy")
    assert yoy["since"] == datetime(2025, 10, 1, tzinfo=TZ) and yoy["until"] == datetime(2025, 10, 5, 14, 0, tzinfo=TZ)
    year = versus(resolve("year", None, None, NOW), "prev")
    assert year["label"] == "Jan 1 – Oct 5, 2025", "this year so far against the same days of last year"
    last = versus(resolve("last-month", None, None, NOW), "prev")
    assert (last["since"], last["until"]) == (datetime(2026, 8, 1, tzinfo=TZ), datetime(2026, 9, 1, tzinfo=TZ)), "a finished month against the whole month before"
    days = versus(resolve("30d", None, None, NOW), "prev")
    assert days["until"] == datetime(2026, 9, 6, tzinfo=TZ) and days["since"] == datetime(2026, 9, 6, tzinfo=TZ) - (NOW - datetime(2026, 9, 6, tzinfo=TZ))
    assert versus(resolve("all", None, None, NOW), "prev") is None and versus(month, None) is None


def test_controls_link_each_choice_and_keep_the_rest_of_the_state():
    from urllib.parse import parse_qs

    from statspage import controls, period

    r = period(None, None, None, None, None, NOW)
    assert r["key"] == "month" and r["since"] == datetime(2026, 10, 1, tzinfo=TZ) and r["until"] is None and r["next"] is None
    state = {"period": "month", "month": None, "year": None, "from": None, "to": None, "vs": "prev", "by": "day", "over": "1", "vin": "V1"}
    c = controls(state, r)
    on = lambda xs: [x["key"] for x in xs if x["on"]]
    assert [p["label"] for p in c["periods"]] == ["Month", "Year", "All", "Custom"] and on(c["periods"]) == ["month"]
    assert c["versus"] == [] and on(c["grains"]) == ["day"]
    q = lambda href: {k: v[0] for k, v in parse_qs(href).items()}
    assert q(c["periods"][1]["href"]) == {"period": "year", "vs": "prev", "by": "day", "vin": "V1"}
    assert q(c["periods"][-1]["href"]) == {"period": "custom", "from": "2026-10-01", "to": "2026-10-05", "vs": "prev", "by": "day", "vin": "V1"}, "Custom opens on the period's dates"
    assert q(c["step"]["prev"]) == {"month": "2026-09", "vs": "prev", "by": "day", "vin": "V1"} and c["step"]["next"] is None
    assert q(c["grains"][1]["href"]) == {"vs": "prev", "vin": "V1"}, "week is the default"
    y = period("year", None, "2025", None, None, NOW)
    assert (y["since"], y["until"]) == (datetime(2025, 1, 1, tzinfo=TZ), datetime(2026, 1, 1, tzinfo=TZ))
    cy = controls({**state, "period": "year", "year": "2025"}, y)
    assert [v["key"] for v in cy["versus"]] == ["none", "prev"], "a year's previous period is last year already"
    assert "month" not in q(cy["step"]["prev"]) and q(cy["step"]["prev"])["year"] == "2024"


def test_the_old_range_links_open_where_they_meant():
    from statspage import legacy_state, period

    assert legacy_state("last-month", NOW) == {"period": "month", "month": "2026-09"}
    assert legacy_state("last-year", NOW) == {"period": "year", "year": "2025"}
    days = period("30d", None, None, None, None, NOW)
    assert days["key"] == "custom" and days["since"] == datetime(2026, 9, 6, tzinfo=TZ), "30 days is two dates now"
    assert period("year", None, None, None, None, NOW)["in_progress"] and period("bogus", None, None, None, None, NOW)["key"] == "month"
    assert period("month", "2027-01", None, None, None, NOW)["ym"] == "2026-10", "never past this month"


def test_months_before_the_car_arrived_are_not_averaged_or_drawn():
    """A stray row eleven months before the car arrived made every empty month between
    average in as a zero (it showed +714% vs avg over a run of empty bars)."""
    from statspage import first_full, history_start

    months = by_month([row(2025, 10, 3, 1), row(2026, 8, 300, 10), row(2026, 9, 900, 30), row(2026, 10, 120, 4)])
    focus = (2026, 10)
    assert history_start(months, focus) == (2026, 8) and first_full(months, focus) == (2026, 9), "August starts it; August itself is a stub"
    c = {x["key"]: x for x in compare(months, focus, fraction=1.0, first=first_full(months, focus))}
    assert c["miles"]["n_avg"] == 1 and c["miles"]["avg"] == 900, "only September is averaged"
    b = bars({"V1": months}, ["V1"], focus, first_full(months, focus), Units(), since=history_start(months, focus))
    assert [x["label"] for x in b["bars"]] == ["Aug", "Sep", "Oct"], "no empty bars for the months before it"
    parked = by_month([row(2026, 6, 300, 10), row(2026, 9, 900, 30), row(2026, 10, 120, 4)])
    assert history_start(parked, focus) == (2026, 6), "two empty months in a row are a car parked, still zeros"


def test_a_thin_baseline_gives_its_figure_not_a_percent():
    """A car's first fortnight: 376 mi against Sep 1–14's 21 read "▲ 1,659 %", and its cents a
    mile on 21 miles read "▼ 87 %". Past five times the baseline, the percent says nothing."""
    from statspage import change

    young = by_month([row(2026, 10, 376.0, 52, 120.0, 30.0), row(2026, 9, 21.0, 19, 70.0, 22.0)])
    c = {x["key"]: x for x in compare(young, (2026, 10), 1.0, (2026, 9))}
    assert c["miles"]["vs_last"] is None and c["miles"]["thin_last"] and c["miles"]["last"] == 21.0
    assert c["cost_per_mi"]["vs_last"] is None and c["cost_per_mi"]["thin_last"], "a rate over 21 miles is as thin as the miles"
    assert round(c["drives"]["vs_last"], 2) == round(52 / 19 - 1, 2) and not c["drives"]["thin_last"], "2.7 times the drives is a real change"
    assert c["cost"]["vs_last"] is not None, "the money is not thin"
    assert change("miles", {"miles": 100.0}, {"miles": 200.0}) == -0.5, "a fall is never thin"
    assert change("miles", {"miles": 100.0}, {"miles": 0.0}) is None


def test_miles_the_odometer_moved_between_drives_are_counted_once():
    from datetime import timedelta

    from statspage import unlogged_miles

    t = datetime(2026, 3, 1, 9, 0, tzinfo=ZoneInfo("America/Chicago"))
    legs = [(100.0, 112.0), (112.0, 130.0), (134.5, 140.0), (139.9, 150.0), (9000.0, 9010.0)]
    drives = [{"start_date": t + timedelta(hours=i), "start_odometer": a, "destination_odometer": b} for i, (a, b) in enumerate(legs)]
    assert unlogged_miles(list(reversed(drives))) == 4.5, "a hop the logger missed counts; a step back and a jump of thousands do not"
    assert unlogged_miles([]) == 0 and unlogged_miles(drives[:1]) == 0


def test_the_legend_carries_the_value_when_the_only_month_is_the_focus():
    """A new install has one month: the legend said 0 beside a 151 mi bar."""
    a = by_month([row(2026, 9, 151)])
    g = bars({"V1": a}, ["V1"], (2026, 9), (2026, 9), Units(), n=6)
    assert g["bars"][-1]["value"] == 151 and g["legend"][0]["total"] == 151
