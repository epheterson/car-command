"""Each Settings section saves its own settings and nothing else."""

import settings as settings_mod
from settingsform import LABELS, OWNS, RETIRED, SECTIONS, capacity_changes, converted, fast_rate_changed, parse, reprices

WAS = dict(settings_mod.DEFAULTS)


def test_every_stored_setting_belongs_to_exactly_one_section():
    owned = [k for keys in OWNS.values() for k in keys]
    assert len(owned) == len(set(owned)), "a key in two sections would be saved from both"
    assert set(owned) | set(RETIRED) == set(settings_mod.DEFAULTS)
    assert [k for k, _ in SECTIONS] == ["cars", "money", "energy", "alerts", "commands", "look", "maps", "data", "access"] and LABELS["look"] == "Units and look"


def test_parse_returns_only_the_sections_own_keys_even_from_a_form_with_everything():
    everything = {
        "currency": "€",
        "plan": "Flat",
        "units": "metric",
        "theme": "dark",
        "week_start": "sunday",
        "clock": "24",
        "date_order": "dmy",
        "timezone": "Europe/Paris",
        "min_drive_mi": "1",
        "min_charge_kwh": "1",
        "weather_shown": "1",
        "tire_low_psi": "30",
        "tire_high_psi": "50",
        "plug_reminder_at": "21:00",
        "plug_reminder_below": "40",
        "summary_at": "08:00",
        "dc_price": "0.5",
        "gas_mpg": "30",
        "gas_price": "4",
    }
    for section, _label in SECTIONS:
        got = parse(section, everything, WAS, channels=("discord",), kinds={"parked": "Parked"})
        assert set(got) <= set(OWNS[section]), (section, set(got) - set(OWNS[section]))


def test_an_empty_form_changes_nothing():
    for section, _label in SECTIONS:
        assert parse(section, {}, WAS, channels=("discord",), kinds={"parked": "Parked"}) == {}, section


def test_a_field_not_on_the_page_keeps_its_value():
    was = dict(WAS, tire_low_psi=30.0, alert_routes={"parked": ["discord"]})
    got = parse("alerts", {"plug_reminder_at": "21:00", "plug_reminder_below": "50", "summary_at": "07:00"}, was, channels=(), kinds={"parked": "Parked"})
    assert "tire_low_psi" not in got and "alert_routes" not in got, "no channel: no routing table and no tire fields on the page"
    got = parse("alerts", {"tire_low_psi": "", "routes_shown": "1"}, was, channels=("discord",), kinds={"parked": "Parked"})
    assert got["tire_low_psi"] is None and got["alert_routes"] == {"parked": []}, "shown and blanked is off; shown and unticked is off"


def test_look_and_data_values():
    got = parse("look", {"units": "metric", "theme": "nonsense", "week_start": "sunday", "clock": "24", "date_order": "dmy", "timezone": "Not/AZone"}, WAS)
    assert got == {"units": "metric", "theme": "system", "week_start": "sunday", "clock": "24", "date_order": "dmy", "timezone": None}
    got = parse("data", {"min_drive_mi": "99", "min_charge_kwh": "0.5", "weather_shown": "1"}, WAS)
    assert got == {"min_drive_mi": 0.1, "min_charge_kwh": 0.5, "weather": False}, "out of range falls back; an unticked box is off only when the page showed it"


def test_money_values_and_what_reprices():
    import cost

    form = {"plan": "", "from0": "2026-01-01", "off0": "0.3", "part0": "0.4", "peak0": "0.5", "dc_price": "9", "dc_per_min": "0.2"}
    got = parse("money", form, WAS)
    assert "currency" not in got and got["dc_price"] is None and got["dc_per_min"] == 0.2
    assert parse("look", {"currency": " € "}, WAS) == {"currency": "€"}, "the symbol lives on Units and look now"
    assert WAS["rate_plan"] is None, "a new install has no plan"
    assert got["rate_plan"] == cost.compile_rows("Home", [{"from": "2026-01-01", "off_peak": 0.3, "part_peak": 0.4, "peak": 0.5}], []), "one row and no schedule: flat at the off-peak price"
    assert reprices(got, WAS) and fast_rate_changed(got, WAS)
    assert "rate_plan" not in parse("money", {"from0": ""}, WAS), "a blank table keeps the saved plan"
    same = parse("money", {"dc_price": "", "dc_per_min": ""}, WAS)
    assert not reprices(same, WAS) and not fast_rate_changed(same, WAS)


def test_the_rows_editor_round_trips_a_plan_without_repricing():
    """Saving the Money page unchanged must not look like a rate change: that throws away
    every priced home charge and prices them all again."""
    import cost
    from conftest import SPRINGFIELD_RATES, SPRINGFIELD_SCHEDULE

    plan = cost.compile_rows("Springfield EV", SPRINGFIELD_RATES, SPRINGFIELD_SCHEDULE)
    was = dict(WAS, rate_plan=plan)
    rates, sched = cost.plan_rows(plan)
    form = {"plan": "Springfield EV"}
    for i, r in enumerate(rates + [{"from": "", "off_peak": "", "part_peak": "", "peak": ""}]):
        form.update({f"from{i}": r["from"], f"off{i}": str(r["off_peak"]), f"part{i}": str(r["part_peak"]), f"peak{i}": str(r["peak"])})
    for i, r in enumerate(sched + [None]):
        form.update({f"sp{i}": (r or {}).get("period", "off_peak"), f"sd{i}": (r or {}).get("days", "all"), f"ss{i}": str(r["start"]) if r else "", f"se{i}": str(r["end"]) if r else ""})
        form.update({f"sm{i}_{m}": "on" for m in range(1, 13) if r and (not r["months"] or m in r["months"])})
    got = parse("money", form, was)
    assert "rate_plan" not in got and not reprices(got, was), "unchanged rows save nothing"
    renamed = parse("money", dict(form, plan="Springfield Night"), was)
    assert renamed["rate_plan"]["name"] == "Springfield Night" and not reprices(renamed, was), "a new name is only a label"
    form["ss0"] = "17"  # peak now starts at 5pm
    moved = parse("money", form, was)
    assert reprices(moved, was) and cost.plan_rows(moved["rate_plan"])[1][0]["start"] == 17


def test_a_grid_plans_prices_are_edited_in_place():
    import cost

    weekday = [[0] * 12 + [1] * 4 + [2] * 4 + [3] * 4 for _ in range(12)]
    periods = [{"key": f"p{i}", "label": f"P{i}", "price": 0.1 * (i + 1)} for i in range(4)]
    plan = {"name": "Springfield TOU", "source": "urdb:x", "versions": [{"from": "2026-01-01", "periods": periods, "weekday": weekday, "weekend": weekday}]}
    was = dict(WAS, rate_plan=plan)
    assert parse("money", {"plan": "Springfield TOU", "pv0_0": "0.1", "pv0_1": "0.2", "pv0_2": "0.30000000000000004", "pv0_3": "0.4"}, was) == {}, "nothing changed, nothing saved"
    got = parse("money", {"plan": "Springfield TOU", "pv0_0": "0.12", "pv0_3": "", "pv0_9": "5"}, was)
    new = got["rate_plan"]
    assert [p["price"] for p in new["versions"][0]["periods"]] == [0.12, 0.2, 0.30000000000000004, 0.4] and new["versions"][0]["weekday"] == weekday
    assert plan["versions"][0]["periods"][0]["price"] == 0.1, "the saved plan is not changed under the caller"
    assert reprices(got, was) and cost.valid_plan(new)


def test_money_reads_fuel_in_the_units_in_force():
    metric = dict(WAS, units="metric")
    got = parse("money", {"gas_l100": "8.4", "gas_ppl": "1.90"}, metric)
    assert round(got["gas_mpg"], 1) == 28.0 and round(got["gas_price"], 2) == 7.19
    assert parse("money", {"gas_mpg": "30", "gas_price": "4.6"}, WAS) == {"gas_mpg": 30.0, "gas_price": 4.6}


def test_metric_fuel_round_trip_stores_nothing_new():
    st = dict(WAS, units="metric", gas_mpg=28.0, gas_price=4.6, min_drive_mi=0.1)
    shown = converted(st)
    assert {k: shown[k] for k in ("gas_l100", "gas_ppl", "min_drive_km")} == {"gas_l100": "8.4", "gas_ppl": "1.22", "min_drive_km": "0.2"}
    assert parse("money", {"gas_l100": shown["gas_l100"], "gas_ppl": shown["gas_ppl"]}, st) == {}
    assert parse("data", {"min_drive_km": shown["min_drive_km"], "min_charge_kwh": "0"}, st) == {"min_charge_kwh": 0.0}
    assert parse("data", {"min_drive_km": "1.6"}, st) == {"min_drive_mi": 0.9942}, "a typed km figure is stored in miles"
    assert converted(WAS) == {}, "an imperial install shows the stored figures"


def test_capacity_unchanged_is_not_an_override():
    current = {"V1": 81.234, "V2": None}
    assert capacity_changes({"capacity_V1": "81.2", "capacity_V2": ""}, current) == {}
    assert capacity_changes({"capacity_V1": "75", "capacity_V2": "900", "capacity_X": "60"}, current) == {"V1": 75.0, "V2": None}
    assert capacity_changes({"capacity_V1": ""}, current) == {"V1": None}, "blank leaves it to the source"
    assert capacity_changes({"capacity_V1": ""}, current, {"V1": 75.0}) == {"V1": 75.0}, "blank goes back to the source's figure when there is one"
    assert capacity_changes({"capacity_V1": "90"}, current, {"V1": 75.0}) == {"V1": 90.0}, "a typed figure still wins"


def test_a_stale_tab_in_the_other_units_still_saves_its_fuel_figures():
    """Money open in one system while Units and look switched the install to the other: the field
    names say which unit was typed, so the figures are read by them rather than dropped."""
    got = parse("money", {"gas_l100": "8.4", "gas_ppl": "1.90"}, WAS)
    assert round(got["gas_mpg"], 1) == 28.0 and round(got["gas_price"], 2) == 7.19
    got = parse("money", {"gas_mpg": "30", "gas_price": "4.6"}, dict(WAS, units="metric"))
    assert got == {"gas_mpg": 30.0, "gas_price": 4.6}


def _post_of(html: str) -> dict:
    """The form a browser would post for this page, untouched: inputs with their values,
    the selected option of each select (else its first), ticked boxes as "on", the checked
    radio of each group. Controls that name another form (form="…") are not this form's."""
    from html.parser import HTMLParser

    out: dict = {}

    class P(HTMLParser):
        select = None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if a.get("form"):
                return
            if tag == "input" and a.get("name"):
                if a.get("type") in ("checkbox", "radio"):
                    if "checked" in a:
                        out[a["name"]] = a.get("value") or "on"
                else:
                    out[a["name"]] = a.get("value") or ""
            elif tag == "select":
                self.select = a.get("name")
                out[self.select] = None
            elif tag == "option" and self.select and ("selected" in a or out[self.select] is None):
                out[self.select] = a.get("value")

        def handle_endtag(self, tag):
            if tag == "select":
                self.select = None

    P().feed(html)
    return out


def _money_page(plan) -> str:
    from pathlib import Path

    import jinja2

    import filters
    from planview import money_context

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    car = {"name": "Alpha", "vin": "V1", "live": True}
    base = {
        "root": "",
        "now": __import__("datetime").datetime(2026, 9, 4, 16, tzinfo=__import__("datetime").timezone.utc),
        "car": car,
        "active": "",
        "vehicles": [car],
        "q": "",
        "colors": {"V1": 0},
        "names": {"V1": "Alpha"},
        "cur": "$",
    }
    return env.get_template("settings_money.html").render(**base, st={"currency": "$", "units": "imperial"}, conv={}, section="money", label="Money", **money_context(plan))


def test_a_long_schedule_and_rate_history_survive_an_unchanged_save():
    """Overlapping first-match rows can come back as more spans than were typed. Every row
    the page shows has to be read back, or saving the page for any other reason quietly
    drops hours from the plan and re-prices every home charge on the wrong schedule."""
    import cost

    schedule = [{"period": "peak", "days": "all", "start": m, "end": m + 1, "months": [m]} for m in range(1, 13)]
    schedule += [{"period": "part_peak", "days": "weekdays", "start": 16, "end": 21, "months": None}, {"period": "peak", "days": "weekends", "start": 17, "end": 19, "months": [6, 7]}]
    rates = [{"from": f"{2010 + i}-01-01", "off_peak": 0.2 + i / 100, "part_peak": 0.3 + i / 100, "peak": 0.4 + i / 100} for i in range(14)]
    plan = cost.compile_rows("Springfield EV", rates, schedule)
    shown_rates, shown_rows = cost.plan_rows(plan)
    assert len(shown_rows) > 12 and len(shown_rates) > 12
    was = dict(WAS, rate_plan=plan)
    form = _post_of(_money_page(plan))
    assert parse("money", form, was).get("rate_plan") is None, "nothing changed, nothing saved"
    got = parse("money", dict(form, sc_price="0.5"), was)
    assert got["sc_price"] == 0.5 and not reprices(got, was)
    # The blank row at the end of each table still adds one, however long the table is.
    n = len(rates)
    added = parse("money", dict(form, **{f"from{n}": "2030-01-01", f"off{n}": "0.5", f"part{n}": "0.6", f"peak{n}": "0.7"}), was)
    assert [v["from"] for v in added["rate_plan"]["versions"]][-1] == "2030-01-01" and len(added["rate_plan"]["versions"]) == n + 1
    s = len(shown_rows)
    added = parse("money", dict(form, **{f"sp{s}": "peak", f"sd{s}": "all", f"ss{s}": "23", f"se{s}": "24"}), was)
    assert added["rate_plan"]["versions"][0]["weekday"][0][23] == 2, "a schedule row added below the twelfth"


def test_commands_section_reads_the_switch_tiers_and_holds_only_when_shown():
    import settingsform as sf

    assert sf.parse("commands", {}, sf.DEFAULTS) == {}, "nothing on the page: nothing changes"
    form = {
        "commands_shown": "1",
        "commands_on": "on",
        "tiers_shown": "1",
        "tier_5YJ3E1EA0JF000001": "3",
        "tier_BRAVO0000000000002": "1",
        "tier_bad vin!": "3",
        "tier_CHARLIE00000000003": "7",
        "hold_shown": "1",
        "hold_5YJ3E1EA0JF000001": "on",
        "hold_from_5YJ3E1EA0JF000001": "16:30",
        "hold_until_5YJ3E1EA0JF000001": "nonsense",
        "hold_from_BRAVO0000000000002": "15:00",
    }
    got = sf.parse("commands", form, sf.DEFAULTS)
    assert got == {
        "commands_on": True,
        "command_tiers": {"5YJ3E1EA0JF000001": 3},
        "charge_hold": {"5YJ3E1EA0JF000001": {"from": "16:30", "until": "00:00"}},
    }
    assert sf.parse("commands", {"commands_shown": "1"}, sf.DEFAULTS) == {"commands_on": False}, "an unticked switch posts nothing: off"


def test_commands_start_off_and_show_on_the_landing():
    import settingsform as sf

    assert sf.DEFAULTS["commands_on"] is False and sf.DEFAULTS["charge_hold"] == {}
    rows = {r["key"]: r["value"] for g in sf.index(sf.DEFAULTS, 1, [], True) for r in g["rows"]}
    assert rows["commands"] == "Off"
    assert {r["key"]: r["value"] for g in sf.index({**sf.DEFAULTS, "commands_on": True}, 1, [], True) for r in g["rows"]}["commands"] == "On"


def test_a_kind_the_alerts_page_left_out_keeps_its_routing():
    was = {**WAS, "alert_routes": {"sentry": ["discord"], "parked": ["discord"]}}
    kinds = {"parked": "Parked", "sentry": "Sentry", "keyfob": "Key fob"}
    got = parse("alerts", {"routes_shown": "1", "routes_kinds": "parked"}, was, channels=("discord",), kinds=kinds)
    assert got["alert_routes"] == {"parked": [], "sentry": ["discord"]}, "unticked parked is off; sentry was not on the page, so it is as saved; keyfob stays unset (everywhere)"
    got = parse("alerts", {"routes_shown": "1"}, was, channels=("discord",), kinds=kinds)
    assert got["alert_routes"] == {"parked": [], "sentry": [], "keyfob": []}, "a form without the list is every kind, as before"


def test_tire_limits_are_typed_in_bar_on_a_metric_install_and_kept_in_psi():
    st = dict(WAS, units="metric", tire_low_psi=36.0, tire_high_psi=None)
    shown = converted(st)
    assert shown["tire_low_bar"] == "2.48" and shown["tire_high_bar"] == ""
    assert parse("alerts", {"tire_low_bar": shown["tire_low_bar"], "tire_high_bar": ""}, st) == {}, "untouched: the stored psi is not rounded through bar"
    assert parse("alerts", {"tire_low_bar": "2.5", "tire_high_bar": "3.2"}, st) == {"tire_low_psi": 36.26, "tire_high_psi": 46.41}
    assert parse("alerts", {"tire_low_bar": "", "tire_high_bar": ""}, st) == {"tire_low_psi": None}
    assert parse("alerts", {"tire_low_bar": "0.2"}, st) == {"tire_low_psi": None}, "out of range is refused"
    assert parse("alerts", {"tire_low_psi": "34", "tire_high_psi": "45"}, WAS) == {"tire_low_psi": 34.0, "tire_high_psi": 45.0}


def test_saving_the_routing_table_marks_every_channel_on_it_as_chosen():
    got = parse("alerts", {"routes_shown": "1"}, {**WAS, "alert_routed": ["telegram"]}, channels=("discord", "webhook"), kinds={"parked": "Parked"})
    assert got["alert_routed"] == ["discord", "telegram", "webhook"], "a channel whose boxes were all left unticked still counts as chosen"
