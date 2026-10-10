"""What the energy pages say and how they draw it: the Powerwall row, the bar on a charge, the sun share by month."""

import re
from datetime import datetime, timedelta, timezone

import pytest

import energypage
from test_stats_render import context as stats_context, env, text  # noqa: F401 - fixtures

NOW = datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)
READING = {"solar_w": 4200.0, "home_w": 1800.0, "grid_w": -300.0, "battery_w": -2100.0, "soe_pct": 61.4, "source": "cloud", "age_s": 4}


# ---- the Powerwall row


def test_the_row_gives_the_level_and_the_four_flows_with_the_way_each_moves():
    row = energypage.row(READING)
    parts = {p["key"]: p for p in row["parts"]}
    assert row["soc"] == 61.4 and row["fill"] == 61.4 and row["source_label"] == "Tesla"
    assert parts["solar"]["kw"] == "4.2 kW" and parts["home"]["kw"] == "1.8 kW"
    assert (parts["grid"]["arrow"], parts["grid"]["kw"], parts["grid"]["title"]) == ("↑", "0.3 kW", "Exporting to the grid")
    assert (parts["battery"]["arrow"], parts["battery"]["kw"], parts["battery"]["title"]) == ("↑", "2.1 kW", "Charging")
    giving = {p["key"]: p for p in energypage.row({**READING, "battery_w": 3000.0, "grid_w": 800.0})["parts"]}
    assert giving["battery"]["arrow"] == "↓" and giving["battery"]["title"] == "Giving to the house" and giving["grid"]["arrow"] == "↓" and giving["grid"]["title"] == "Importing from the grid"


def test_a_flow_under_fifty_watts_has_no_arrow_and_a_missing_one_is_a_dash():
    row = energypage.row({"solar_w": 12.0, "home_w": 900.0, "grid_w": 20.0, "battery_w": -10.0, "soe_pct": None, "source": "cloud", "age_s": 200})
    parts = {p["key"]: p for p in row["parts"]}
    assert parts["grid"]["arrow"] == "" and parts["battery"]["title"] == "Idle" and parts["solar"]["idle"]
    assert row["soc"] is None and row["fill"] == 0.0 and row["source_label"] == "Tesla"
    assert energypage.row({"solar_w": None, "home_w": None, "grid_w": None, "battery_w": None, "soe_pct": None})["parts"][0]["kw"] == "—"
    assert energypage.row(None) is None
    assert energypage.row({**READING, "soe_pct": 120})["fill"] == 100.0


def macros(env):  # noqa: F811
    return env.from_string('{% import "_parts.html" as parts %}{{ parts.pw_row(pw) }}')


def test_the_row_is_drawn_in_neutral_ink_with_a_fill_bar_and_nothing_without_a_reading(env):  # noqa: F811
    html = macros(env).render(pw=energypage.row(READING))
    assert 'class="pwrow"' in html and "61<small>%</small>" in html and 'class="pwbar"' in html and 'style="width:61%"' in html
    assert "↑2.1 kW" in html and "Powerwall" in html and "gold" not in html and 'title="Tesla, 4 s ago"' in html
    assert macros(env).render(pw=None).strip() == ""
    no_level = macros(env).render(pw=energypage.row({**READING, "soe_pct": None}))
    assert "pw-soc" not in no_level and 'class="pwrow"' in no_level


def test_the_row_css_never_reaches_for_gold():
    css = (__import__("pathlib").Path(__file__).parent / "static" / "style.css").read_text()
    block = css[css.index("/* The house's energy") :]
    assert "var(--gold)" not in block and "var(--mark)" not in block


# ---- the bar on a charge


def charge(**over):
    return {
        "at_home": True,
        "src_status": "ok",
        "src_solar_kwh": 6.0,
        "src_battery_kwh": 2.0,
        "src_grid_kwh": 2.0,
        "src_sun_frac": 0.5,
        "src_grid_cost": 0.4,
        "src_battery_cost": 0.1,
        "src_approx": False,
        **over,
    }


def test_the_bar_has_the_three_parts_in_order_with_their_share_kwh_and_cost():
    mix = energypage.mix_bar(charge())
    assert [p["key"] for p in mix["parts"]] == ["sun", "battery", "grid"]
    assert [round(p["percent"]) for p in mix["parts"]] == [60, 20, 20]
    assert [p["cost"] for p in mix["parts"]] == [0.0, 0.1, 0.4]
    assert mix["sun_share"] == pytest.approx(0.7), "the sun's own 60% and half of the Powerwall's 20%"
    assert [p["approx"] for p in mix["parts"]] == [False, True, False], "the Powerwall's grid-charged part is always an estimate"
    assert energypage.mix_bar(charge(src_approx=True))["parts"][2]["approx"]


def test_a_part_with_nothing_in_it_is_left_out_and_a_charge_without_a_split_has_no_bar():
    mix = energypage.mix_bar(charge(src_battery_kwh=0.0, src_solar_kwh=0.0))
    assert [p["key"] for p in mix["parts"]] == ["grid"]
    assert energypage.mix_bar(charge(src_status="none")) is None and energypage.mix_bar(charge(src_status=None)) is None
    assert energypage.mix_bar(charge(src_solar_kwh=0, src_battery_kwh=0, src_grid_kwh=0)) is None


def test_bar_macro_on_a_row_says_the_sun_share_and_in_detail_the_kwh_and_cost(env):  # noqa: F811
    mix = energypage.mix_bar(charge())
    row = env.from_string('{% import "_parts.html" as parts %}{{ parts.mix_bar(mix) }}').render(mix=mix)
    assert 'class="mixbar"' in row and "<i " not in row and "70% sun" in row and "kWh" not in row
    assert 'aria-label="Sun 60%, Powerwall 20%, Grid 20%"' in row and 'style="--s:60.0%;--b:80.0%"' in row, "one element; the gradient's stops are inline"
    detail = env.from_string('{% import "_parts.html" as parts %}{{ parts.mix_bar(mix, true, cur) }}').render(mix=mix, cur="$")
    assert "Sun</b> 6.0 kWh · $0.00" in detail and "Powerwall</b> 2.0 kWh · ≈$0.10" in detail and "Grid</b> 2.0 kWh · $0.40" in detail
    cheap = env.from_string('{% import "_parts.html" as parts %}{{ parts.mix_bar(mix, true, cur) }}').render(mix=energypage.mix_bar(charge(src_grid_cost=None)), cur="$")
    assert "Grid</b> 2.0 kWh" in cheap and "None" not in cheap, "no plan, no cost"


def base():
    car = {"name": "Alpha", "vin": "V1", "live": True, "garage": False, "updated_age": "", "model": ""}
    return {"root": "", "now": NOW, "car": car, "active": "charges", "vehicles": [car], "q": "", "colors": {"V1": 0}, "names": {"V1": "Alpha"}, "cur": "$", "csrf": "t", "auth_on": False}


def session(**over):
    return {
        "id": 1,
        "vin": "V1",
        "session_start_date": NOW,
        "session_end_date": NOW + timedelta(hours=2),
        "plug_connected_date": NOW,
        "plug_disconnected_date": None,
        "start_level": 40.0,
        "end_level": 80.0,
        "charging_type": "AC",
        "where": "Home",
        "gained": 40.0,
        "duration": timedelta(hours=2),
        "cost": 3.2,
        "kwh": 10.0,
        "rate_plan": "Springfield EV",
        "cost_parts": {},
        "by_hand": False,
        "range_added": 120.0,
        "at_home": True,
        "mix": energypage.mix_bar(charge()),
        **over,
    }


WINDOW = {"start": NOW, "end": NOW, "label": "July 2026", "ym": "2026-07", "prev": "2026-06", "prev_label": "Jun 2026", "next": "2026-08", "next_label": "Aug 2026", "is_current": False}


def charges_html(env, rows):  # noqa: F811
    import daypage

    return env.get_template("charges.html").render(**base(), charges=rows, days=daypage.by_day(rows, timezone.utc, "session_start_date"), window=WINDOW, sort="start", desc=True, sort_base="?", flt={})


def test_a_home_charge_row_carries_the_bar_and_a_charge_without_a_split_does_not(env):  # noqa: F811
    html = charges_html(env, [session(), session(id=2, mix=None, where="Elm Street Supercharger", at_home=False)])
    assert html.count('class="mixbar"') == 1 and "70% sun" in html
    row = html[html.index('class="row-b') :]
    assert row.index('class="mixbar"') < row.index("</a>"), "inside the row it belongs to"


def test_the_charge_page_shows_the_split_under_the_summary(env):  # noqa: F811
    chart = {"t0": 0, "t1": 7200, "primary": {"key": "power", "label": "Power", "unit": " kW", "lo": 0, "hi": 30, "points": []}, "extras": [], "lane": [], "events": [], "ticks": []}
    args = dict(stats={"kwh": 10.0, "peak_kw": 7.2}, chart=chart, hidden=False, samples=[], every=1, readings=0, bill=None)
    html = env.get_template("charge.html").render(**base(), session=session(), **args)
    assert 'class="mix"' in html and "Sun</b> 6.0 kWh" in html and "Powerwall</b> 2.0 kWh · ≈$0.10" in html and html.index('class="mix"') < html.index('class="tiles')
    plain = env.get_template("charge.html").render(**base(), session=session(mix=None), **args)
    assert 'class="mix"' not in plain and "mixbar" not in plain


def test_now_and_the_garage_draw_the_row_only_with_a_reading(env):  # noqa: F811
    ctx = dict(base(), active="now", cars=[], acts=[], act_sig="", today={}, line=[], ticks=[], pw=energypage.row(READING))
    html = env.get_template("garage.html").render(**ctx)
    assert html.count('class="pwrow"') == 1 and html.index("car-cards") < html.index('class="pwrow"') < html.index('class="legend')
    assert 'class="pwrow"' not in env.get_template("garage.html").render(**dict(ctx, pw=None))


# ---- Stats


def test_stats_lists_the_sun_share_by_month_and_nothing_without_a_split(env):  # noqa: F811
    months = energypage.sun_months(
        [
            {"ym": "2026-09", "solar": 60.0, "battery": 20.0, "grid": 20.0, "battery_sun": 10.0},
            {"ym": "2026-08", "solar": 0, "battery": 0, "grid": 0, "battery_sun": 0},
            {"ym": "2026-07", "solar": 10.0, "battery": 0.0, "grid": 30.0, "battery_sun": 0.0},
        ]
    )
    assert [m["ym"] for m in months] == ["2026-09", "2026-07"] and [round(m["percent"]) for m in months] == [70, 25] and months[0]["label"] == "Sep 2026"
    html = env.get_template("stats.html").render(**stats_context(), sun=months)
    section = html[html.index('id="sun"') :]
    assert "Sun share of home charging" in html and "Sep 2026" in section and '<b class="sm-v">70%</b>' in section and "of 100 kWh" in text(section)
    assert 'id="sun"' not in env.get_template("stats.html").render(**stats_context(), sun=[])
    assert 'id="sun"' not in env.get_template("stats.html").render(**stats_context())


def test_new_templates_hold_no_explanatory_paragraph():
    from pathlib import Path

    src = (Path(__file__).parent / "templates" / "settings_energy.html").read_text()
    for p in re.findall(r"<p\b[^>]*>(.*?)</p>", src, re.S):
        bare = re.sub(r"\{%.*?%\}|\{\{.*?\}\}|<[^>]+>", "", p, flags=re.S)
        assert len(" ".join(bare.split())) <= 140, bare


def test_glance_names_what_the_house_reads_from():
    assert energypage.glance({"cloud": {"enabled": False}}) == "Off" and energypage.glance({}) == "Off"
    assert energypage.glance({"cloud": {"enabled": True}}) == "Tesla account"


def test_ago_reads_in_the_unit_that_fits():
    assert [energypage.ago(s) for s in (4, 89, 90, 3000, 7200, None)] == ["4 s ago", "89 s ago", "1 min ago", "50 min ago", "2 h ago", ""]
