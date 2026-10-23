"""The Calendar's strips and battery line, and the tire days, from rows the pages already have."""

from datetime import date, datetime, timedelta, timezone

import pytest

import calview
from units import Units

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def cell(d, miles=0.0, kwh=0.0, other=False):
    return {"date": d, "other": other, "drives": 1 if miles else 0, "miles": miles, "charges": 1 if kwh else 0, "kwh": kwh}


def test_day_values_take_distance_and_kwh_from_the_cells_and_cost_from_the_day_rows():
    cells = [cell(date(2026, 9, 1), 10.0), cell(date(2026, 9, 2), kwh=30.0), cell(date(2026, 8, 31), 50.0, other=True)]
    v = calview.day_values(cells, {date(2026, 9, 2): {"cost": 4.5}}, Units(metric=True))
    assert v["dist"] == {date(2026, 9, 1): pytest.approx(16.09, abs=0.01)}, "in the active units; a day of the next month is not this one's"
    assert v["kwh"] == {date(2026, 9, 2): 30.0} and v["cost"] == {date(2026, 9, 2): 4.5}


def test_a_month_has_a_strip_per_measure_with_its_total_in_words():
    vals = {"dist": {date(2026, 9, 1): 10.0, date(2026, 9, 3): 2.5}, "kwh": {}, "cost": {date(2026, 9, 3): 4.5}}
    ms = calview.month_strips(2026, 9, vals, date(2026, 9, 10), Units(), "€", href=lambda d: f"/day?date={d}")
    assert [m["key"] for m in ms] == ["dist", "kwh", "cost"] and [m["label"] for m in ms] == ["Driven", "Charged", "Cost"]
    assert ms[0]["total"] == "12 mi" and ms[1]["total"] == "—" and ms[2]["total"] == "€4.50"
    assert len(ms[0]["strip"]["bars"]) == 30 and ms[0]["strip"]["bars"][2]["label"] == "Thu Sep 3: 2.5 mi" and ms[0]["strip"]["bars"][0]["href"] == "/day?date=2026-09-01"


def test_a_year_sums_its_days_into_months_and_weeks_that_agree_with_the_total():
    vals = {"dist": {date(2026, 1, 5): 10.0, date(2026, 1, 6): 5.0, date(2026, 3, 2): 20.0, date(2025, 12, 31): 99.0}, "kwh": {}, "cost": {}}
    dist, _, _ = calview.year_strips(2026, vals, date(2026, 6, 1), Units(), "$", month_href=lambda y, m: f"/m/{y}-{m}")
    assert dist["total"] == "35 mi" and dist["months"]["total"] == 35.0, "a day of last year is not this year's"
    assert dist["months"]["bars"][0]["value"] == 15.0 and dist["months"]["bars"][2]["value"] == 20.0 and dist["months"]["bars"][0]["href"] == "/m/2026-1"
    assert dist["weeks"]["total"] == 35.0 and dist["months"]["bars"][6]["future"]


def test_the_average_level_is_weighted_by_how_long_each_level_lasted():
    series = [(T0, 80.0), (T0 + timedelta(hours=6), 80.0), (T0 + timedelta(hours=6), 50.0)]
    assert calview.time_average(series, T0, T0 + timedelta(hours=24)) == pytest.approx((80 * 6 + 50 * 18) / 24), "the last reading holds to the end"
    assert calview.time_average([(T0 - timedelta(days=2), 70.0)], T0, T0 + timedelta(hours=1)) == 70.0, "a reading from before the window counts from its start"
    assert calview.time_average([], T0, T0 + timedelta(hours=1)) is None


def test_the_month_battery_line_shades_drives_and_charges_and_draws_the_average():
    levels = [(T0, 80.0), (T0 + timedelta(days=1), 80.0), (T0 + timedelta(days=1), 60.0), (T0 + timedelta(days=2), 60.0)]
    drives = [{"start_date": T0 + timedelta(days=1), "destination_date": T0 + timedelta(days=1, minutes=30), "miles": 12.0, "from": "Home", "to": "Work"}]
    charges = [{"session_start_date": T0 + timedelta(days=1, hours=2), "session_end_date": T0 + timedelta(days=1, hours=6), "kwh": 20.0, "where": "Home"}]
    c = calview.month_chart(levels, drives, charges, T0, T0 + timedelta(days=30), T0 + timedelta(days=2), timezone.utc, Units())
    assert c["primary"]["key"] == "level" and c["primary"]["points"][0] == [T0.timestamp(), 80.0]
    assert [b[2] for b in c["bands"]] == ["driving", "charging"] and c["bands"][0][3] == "Drive 12.0 mi · Home → Work" and c["bands"][1][3] == "Charge 20.0 kWh · Home"
    assert c["avg"]["value"] == pytest.approx(70.0), "averaged up to now, not over the empty rest of the month"
    assert c["lane"] == [] and c["events"] == [] and c["ticks"]
    assert calview.month_chart([], drives, charges, T0, T0 + timedelta(days=30), T0, timezone.utc, Units()) is None


def test_tire_days_are_the_four_on_one_scale_with_their_average_and_the_temperature():
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(3)]
    rows = [{"day": d, "fl": 40.0 - i, "fr": 41.0, "rl": 42.0, "rr": 43.0} for i, d in enumerate(days)]
    temps = [{"day": days[0], "f": 50.0}, {"day": days[1], "f": -40.0}, {"day": date(2025, 12, 1), "f": 20.0}]
    c = calview.tire_days(rows, temps, Units(), timezone.utc)
    assert c["primary"]["label"] == "Average of four" and c["primary"]["points"][0][1] == pytest.approx(41.5) and c["days"] == 3
    keys = [x["key"] for x in c["extras"]]
    assert keys == ["fl", "fr", "rl", "rr", "temp"] and c["on"] == ["fl", "fr", "rl", "rr", "temp"], "every tire drawn, the temperature last so the right scale is its own"
    assert all((x["lo"], x["hi"]) == (c["primary"]["lo"], c["primary"]["hi"]) for x in c["extras"][:4]), "one scale, so a soft tire reads lower"
    assert len({x["tone"] for x in c["extras"]}) == 5
    assert [p[1] for p in c["extras"][4]["points"]] == [50.0], "no -40 non-reading, nothing from before the first tire day"
    assert calview.tire_days(rows[:1], temps, Units(), timezone.utc) is None


def _macro(path, name):
    from pathlib import Path

    import jinja2

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")), autoescape=True)
    env.globals["est"] = lambda flag, why: ""
    return getattr(env.get_template(path).module, name)


def test_a_strip_with_links_is_a_labelled_group_each_bar_link_named():
    from strips import month_days

    html = str(_macro("_strips.html", "strip")(month_days(2026, 9, {date(2026, 9, 3): 12.0}, href=lambda d: f"/day?date={d}"), "Distance each day of September"))
    assert 'role="group" aria-label="Distance each day of September"' in html and 'role="img"' not in html, "a screen reader reaches the links inside"
    assert '<a href="/day?date=2026-09-03" aria-label="Thu Sep 3: 12">' in html


def test_the_totals_switch_is_pressed_buttons():
    ms = calview.month_strips(2026, 9, {"dist": {}, "kwh": {}, "cost": {}}, date(2026, 9, 10), Units(), "$")
    html = str(_macro("_calsum.html", "calsum")(ms, "September", ""))
    assert html.count('aria-pressed="true"') == 1 and html.count('aria-pressed="false"') == 2 and 'role="tab' not in html


def test_the_tire_chart_averages_wherever_all_four_are_known_and_draws_the_owners_limit():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    s = t0.timestamp()
    per = {"fl": [[s, 40.0], [s + 7200, 42.0]], "fr": [[s, 40.0]], "rl": [[s + 3600, 36.0]], "rr": [[s, 40.0]]}
    c = calview.tire_chart(t0, t0 + timedelta(hours=4), per, [], Units(), timezone.utc, low=35.0)
    assert c["primary"]["points"] == [[s + 3600, 39.0], [s + 7200, 39.5]], "from the moment the fourth tire is known"
    assert c["avg"] == {"value": 35.0, "label": "Alert under"} and c["primary"]["lo"] <= 35
    assert calview.tire_chart(t0, t0, {"fl": []}, [], Units(), timezone.utc) is None


def test_a_tire_well_under_the_other_three_is_flagged_and_a_cold_morning_is_not():
    now = calview.tire_now({"fl": [[0, 41.0]], "fr": [[0, 41.2]], "rl": [[0, 38.6]], "rr": [[0, 41.0]]})
    assert [t["low"] for t in now] == [False, False, True, False] and now[2]["label"] == "Rear left"
    cold = calview.tire_now({w: [[0, 36.0]] for w in ("fl", "fr", "rl", "rr")})
    assert not any(t["low"] for t in cold) and calview.tire_now({"fl": [[0, 40.0]]}) is None


def test_a_months_charges_split_home_public_and_fast():
    rows = [
        {"at_home": True, "kwh": 30.0, "cost": 4.0},
        {"charging_type": "DC", "kwh": 40.0, "cost": 18.0},
        {"charging_type": "AC", "kwh": 10.0},
        {"at_home": True, "charging_type": "AC", "kwh": 5.0, "cost": 1.0},
    ]
    split = {r["key"]: r for r in calview.charge_split(rows)}
    assert split["home"]["n"] == 2 and split["home"]["kwh"] == 35.0 and split["home"]["cost"] == 5.0
    assert split["fast"]["cost"] == 18.0 and split["public"]["n"] == 1 and [r["key"] for r in calview.charge_split(rows[:1])] == ["home"]


def test_a_days_temperature_is_its_drives_mean_and_ignores_no_reading():
    t = datetime(2026, 9, 1, 15, tzinfo=timezone.utc)
    temps = calview.day_temps([{"start_date": t, "temp_f": 60.0}, {"start_date": t, "temp_f": 70.0}, {"start_date": t, "temp_f": -40.0}, {"start_date": t}], timezone.utc)
    assert temps == {date(2026, 9, 1): 65.0}


def test_the_month_battery_axis_reads_in_round_tens():
    assert calview.round_levels([55.0, 89.0]) == (50, 90) and calview.round_levels([61.0, 88.0]) == (60, 100)
    assert calview.round_levels([95.0, 99.5]) == (80, 100) and calview.round_levels([2.0, 5.0]) == (0, 20)
    for vals in ([55.0, 89.0], [61.0, 88.0], [33.0, 34.0], [10.0, 99.0]):
        lo, hi = calview.round_levels(vals)
        assert lo % 10 == 0 and hi % 10 == 0 and (lo + hi) / 2 % 10 == 0 and lo <= min(vals) and hi >= max(vals)


def test_the_places_a_car_charges_most_fold_spots_under_one_name():
    spots = [
        {"lat": 1.0, "lon": 1.0, "n": 300, "kwh": 3000.0, "cost": 600.0, "priced_kwh": 3000.0},
        {"lat": 1.001, "lon": 1.0, "n": 20, "kwh": 200.0, "cost": 40.0, "priced_kwh": 200.0},
        {"lat": 2.0, "lon": 2.0, "n": 5, "kwh": 250.0, "cost": 0.0, "priced_kwh": 0.0},
        {"lat": 3.0, "lon": 3.0, "n": 5, "kwh": 100.0, "cost": 50.0, "priced_kwh": 100.0},
    ]
    names = {1.0: "Home", 1.001: "Home", 2.0: "Springfield Supercharger"}
    top = calview.charge_places(spots, lambda lat, lon: names.get(lat), top=3)
    assert [p["where"] for p in top] == ["Home", "Springfield Supercharger", "Unnamed spot"] and top[0]["n"] == 320 and top[0]["per_kwh"] == pytest.approx(0.2)
    assert top[1]["per_kwh"] is None and top[2]["per_kwh"] == 0.5
