"""Units are a display concern, and the way they go wrong is silent.

Storage is one system — miles, mph, °F, Wh/mi, feet — and every page converts on the way
out. The failure that matters is not a crash: it is a number that was never converted
sitting next to a label that says km, which reads as fact and is wrong by 60 %.

Two guards here. One renders every template with metric on and checks the arithmetic. The
other scans the templates for a value whose *name* says it carries a unit but which is
formatted with the plain number filter — the mistake a mechanical pass makes, because a
value that shares its label with the one beside it has no label of its own to find.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jinja2
import pytest

import filters
import units
from units import KM_PER_MI, Units

TEMPLATES = Path(__file__).parent / "templates"


@pytest.fixture
def env():
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)))
    filters.register(e)
    yield e
    units.use_units(False)


# ---- the arithmetic --------------------------------------------------------


def test_imperial_is_the_stored_system_and_passes_through():
    u = Units(metric=False)
    assert u.distance(100) == 100 and u.speed(65) == 65 and u.temperature(72) == 72
    assert u.per_distance(300) == 300 and u.elevation(1000) == 1000


def test_metric_converts_each_quantity_the_right_way():
    u = Units(metric=True)
    assert u.distance(100) == pytest.approx(160.9344)
    assert u.speed(65) == pytest.approx(104.6074, abs=0.001)
    assert u.temperature(72) == pytest.approx(22.222, abs=0.001)
    assert u.temperature(32) == 0 and u.temperature(-40) == pytest.approx(-40)
    assert u.elevation(1000) == pytest.approx(304.8)


def test_a_quantity_measured_per_distance_gets_smaller_not_larger():
    # The one that is easy to get backwards: 300 Wh per mile is 186 Wh per kilometre,
    # because the kilometre is the shorter distance. Multiplying here would read as a
    # plausible number and be wrong by a factor of 2.6.
    u = Units(metric=True)
    assert u.per_distance(300) == pytest.approx(300 / KM_PER_MI)
    assert u.per_distance(300) < 300


@pytest.mark.parametrize("value", [None])
def test_nothing_stays_nothing(value):
    for u in (Units(False), Units(True)):
        assert u.distance(value) is None and u.speed(value) is None and u.temperature(value) is None and u.per_distance(value) is None and u.elevation(value) is None


def test_the_labels_change_together_with_the_numbers():
    assert Units(False).labels["dist"] == "mi" and Units(True).labels["dist"] == "km"
    assert Units(False).labels["econ"] == "Wh/mi" and Units(True).labels["econ"] == "Wh/km"
    assert Units(False).labels["elev"] == "ft" and Units(True).labels["elev"] == "m"
    assert Units(False).labels["energy"] == Units(True).labels["energy"] == "kWh", "kWh is kWh everywhere"


# ---- the filters, which is what a template actually calls -------------------


def test_the_filters_follow_the_active_system(env):
    # Through context variables, not literals: Jinja constant-folds a filter applied to a
    # literal at compile time, which would freeze the answer to whichever system was
    # active when the template was parsed. Real templates always render variables.
    t = env.from_string("{{ d|dist(1) }} {{ u.dist }} · {{ e|econ }} {{ u.econ }} · {{ f|temp }}{{ u.temp }}")
    vals = {"d": 100, "e": 300, "f": 72}
    units.use_units(False)
    assert t.render(**vals) == "100.0 mi · 300 Wh/mi · 72°F"
    units.use_units(True)
    assert t.render(**vals) == "160.9 km · 186 Wh/km · 22°C"


def test_switching_units_needs_no_re_registration(env):
    """The label object reads the active system on every access, so a settings save takes
    effect on the next page rather than on the next restart."""
    t = env.from_string("{{ u.dist }}")
    units.use_units(True)
    assert t.render() == "km"
    units.use_units(False)
    assert t.render() == "mi"


# ---- the scan --------------------------------------------------------------

# A value whose name says it carries a unit. If one of these is rendered with |num it is
# being shown unconverted, and almost certainly beside a label that has been converted.
UNIT_BEARING = re.compile(
    r"(?:^|[._])(?:miles?|mi|odometer|mph|wh_per_mi|cost_per_mi|climb_ft|descent_ft|rated_used|range_added|due_miles|temp_f|inside_start|inside_end|outside_f|inside_f|battery_f)$", re.I
)
NUM_FILTER = re.compile(r"\{\{\s*([^{}]*?)\|num(?:\(\d\))?\s*\}\}")


def unitless(expr: str) -> bool:
    """A ratio of two like quantities has no unit, so miles/miles*100 is a percentage."""
    return "/" in expr or "*" in expr or "|" in expr


def test_no_template_shows_a_unit_bearing_value_unconverted():
    offenders = []
    for path in sorted(TEMPLATES.glob("*.html")):
        for i, line in enumerate(path.read_text().split("\n"), 1):
            for m in NUM_FILTER.finditer(line):
                expr = m.group(1).strip()
                if not unitless(expr) and UNIT_BEARING.search(expr):
                    offenders.append(f"{path.name}:{i}  {{{{ {expr}|num }}}} — use |dist, |speed, |temp, |econ or |elev")
    assert not offenders, "values shown without converting to the active unit system:\n  " + "\n  ".join(offenders)


def test_the_scan_would_actually_catch_one():
    """A guard nobody has seen fail is a guard nobody should trust."""
    assert UNIT_BEARING.search("d.miles") and UNIT_BEARING.search("trip.start_odometer") and UNIT_BEARING.search("stats.max_mph")
    assert UNIT_BEARING.search("r.climb_ft") and UNIT_BEARING.search("t.wh_per_mi")
    assert not UNIT_BEARING.search("d.kwh") and not UNIT_BEARING.search("c.cost") and not UNIT_BEARING.search("b.soc")
    assert unitless("(trip.fsd_miles / trip.miles * 100)"), "a ratio of two distances is a percentage"


# ---- a whole page, in both systems -----------------------------------------


def test_a_drive_page_reads_correctly_in_both_systems(env):
    t0 = datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc)
    ctx = {
        "root": "",
        "now": t0,
        "car": {"name": "Alpha", "model": "Example EV", "updated_age": "just now", "vin": "V1", "live": True},
        "active": "",
        "vehicles": [],
        "q": "",
        "colors": {},
        "names": {},
        "cur": "$",
        "csrf": "t",
        "auth_on": False,
        "trip": {
            "id": 1,
            "start_date": t0,
            "destination_date": t0 + timedelta(minutes=42),
            "start_odometer": 1000.0,
            "destination_odometer": 1100.0,
            "miles": 100.0,
            "duration": timedelta(minutes=42),
            "from": "A",
            "to": "B",
            "from_named": True,
            "to_named": True,
            "temp_f": 32.0,
        },
        "pos": {},
        "stats": {"avg_mph": 65.0, "used_pct": 9.0, "kwh": 10.5, "wh_per_mi": 300.0, "max_mph": 80.0, "climb_ft": 1000.0, "descent_ft": 500.0, "kwh_basis": "rated range used"},
        "lvl_start": 74.0,
        "lvl_end": 65.0,
        "route": [],
        "tags": [],
        "chart": {"t0": 0, "t1": 1, "primary": {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": []}, "extras": [], "lane": [], "events": [], "ticks": []},
        "hidden": False,
        "samples": [],
        "every": 1,
        "fixes": 0,
    }
    tpl = env.get_template("drive.html")

    units.use_units(False)
    imperial = tpl.render(**ctx)
    assert "100.0 mi" in imperial and "65<small>mph</small>" in imperial and "300 Wh/mi" in imperial
    assert "1,000<small>ft</small>" in imperial and "500 ft down" in imperial and "32<small>°F</small>" in imperial

    units.use_units(True)
    metric = tpl.render(**ctx)
    assert "160.9 km" in metric, "100 miles is 160.9 km"
    assert "105<small>km/h</small>" in metric, "65 mph is 104.6 km/h"
    assert "186 Wh/km" in metric, "300 Wh/mi is 186 Wh/km"
    assert "305<small>m</small>" in metric and "152 m down" in metric
    assert "0<small>°C</small>" in metric, "32°F is freezing"
    assert "mph" not in metric and "Wh/mi" not in metric and "°F" not in metric, "no imperial label survives a metric render"


def test_a_typed_distance_comes_back_as_the_number_that_was_typed():
    # Round-tripping matters more than the constant: an operator who types 100,000 on the
    # odometer must see 100,000 when the page redraws, whichever system they are in.
    for metric in (False, True):
        u = Units(metric=metric)
        assert u.distance(u.to_stored_distance(100000)) == pytest.approx(100000)
    assert Units(metric=True).to_stored_distance(160.9344) == pytest.approx(100), "160.9 km typed is 100 miles stored"
    assert Units(metric=False).to_stored_distance(100) == 100
    assert Units(metric=True).to_stored_distance(None) is None


IMPERIAL_LITERAL = re.compile(r"Wh/mi\b|°F\b|\bmph\b|<small>mi</small>|>Miles<|¢/mi\b")


def test_no_template_hard_codes_an_imperial_label():
    """Settings › Units and look is the exception: its picker names the system you are choosing."""
    offenders = []
    for path in sorted(TEMPLATES.glob("*.html")):
        if path.name == "settings_look.html":
            continue
        for i, line in enumerate(path.read_text().split("\n"), 1):
            for m in IMPERIAL_LITERAL.finditer(line):
                offenders.append(f"{path.name}:{i}  {m.group(0)!r} — use {{{{ u.dist }}}}, {{{{ u.econ }}}}, {{{{ u.temp }}}} or {{{{ u.speed }}}}")
    assert not offenders, "labels that stay imperial when the operator picked metric:\n  " + "\n  ".join(offenders)


def test_a_column_header_uses_the_word_not_the_abbreviation():
    # "Mi" as a column header reads worse than the "Miles" it replaced; the unit
    # abbreviation belongs beside a number, the word belongs above a column of them.
    assert Units(False).labels["distname"] == "miles" and Units(True).labels["distname"] == "kilometres"
    for name in ("drives.html", "now.html", "stats.html", "tags.html"):
        assert "u.dist|capitalize" not in (TEMPLATES / name).read_text(), f"{name} capitalises the abbreviation"


def test_stats_and_battery_read_in_metric():
    import jinja2
    from datetime import date, datetime, timezone

    import filters
    import units
    from statspage import bars, by_month, compare

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)), autoescape=True)
    filters.register(env)
    base = {
        "root": "",
        "now": datetime(2026, 9, 30, tzinfo=timezone.utc),
        "car": {"name": "Alpha", "vin": "V1", "garage": False, "live": True, "updated_age": "", "model": ""},
        "active": "stats",
        "vehicles": [],
        "q": "",
        "colors": {"V1": 0},
        "names": {"V1": "Alpha"},
        "cur": "$",
        "csrf": "t",
        "auth_on": False,
    }
    r = {"period": date(2026, 9, 1), "miles": 100.0, "drives": 10, "added_kwh": 30.0, "cost": 9.0, "used_kwh": 30.0, "drive_h": 3.0, "charges": 2}
    months = by_month([r, dict(r, period=date(2026, 8, 1))])
    units.use_units(True)
    try:
        u = units.Units(metric=True)
        html = env.get_template("stats.html").render(
            **base,
            rows=[dict(r, wh_per_mi=300.0, cost_per_mi=0.09, drive_cost=3.0, fsd_miles=0.0)],
            totals={"drives": 10, "miles": 100.0, "drive_h": 3.0, "used_kwh": 30.0, "charges": 2, "added_kwh": 30.0, "cost": 9.0, "wh_per_mi": 300.0},
            by="week",
            over=1,
            rate_plan="",
            bins={"temp": [], "speed": []},
            odometer=None,
            range_from=None,
            range_to=None,
            per_car=[],
            month_window={"label": "September 2026", "ym": "2026-09", "prev": "2026-08", "prev_label": "Aug 2026", "next": "2026-10", "next_label": "Oct 2026", "is_current": False},
            fraction=1.0,
            cmp=compare(months, (2026, 9), 1.0, (2026, 8)),
            mbars=bars({"V1": months}, ["V1"], (2026, 9), (2026, 8), u),
            mbars_phone=bars({"V1": months}, ["V1"], (2026, 9), (2026, 8), u, w=360, short=True),
        )
        assert "160.9" in html or "161<small>km</small>" in html, "100 mi is 161 km"
        assert "Per km" in html and "Per mile" not in html and "¢/km" in html
        assert " mi<" not in html and "mi</text>" not in html
        lv = {"t0": 0, "t1": 1, "primary": {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": []}, "extras": [], "lane": [], "events": [], "ticks": []}
        html = env.get_template("battery.html").render(
            **dict(base, active="battery"),
            blocks=[
                {"car": {"vin": "V1", "name": "Alpha", "soc": 50.0, "range_mi": 100.0, "range_full_mi": 200.0, "battery_f": 32.0}, "chart": lv, "full": None, "tires": {}, "firsts": {}, "points": 0}
            ],
        )
        assert "161 km" in html and "322<small>km</small>" in html and "0<small>°C</small>" in html and "°F" not in html
    finally:
        units.use_units(False)
