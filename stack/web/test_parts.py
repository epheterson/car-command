"""The shared parts render what they are given, and nothing they are not."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jinja2

sys.path.insert(0, str(Path(__file__).parent))
import filters  # noqa: E402

ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
filters.register(ENV)


def render(src: str, **ctx) -> str:
    return ENV.from_string('{% import "_parts.html" as parts %}' + src).render(**ctx)


def test_row_is_one_line_title_three_pills_and_the_big_figure():
    html = render("{{ parts.row('/cc/drives/7', '8:49 AM', 'Home → Elm Street', ['9 min', '479 Wh/mi', '$0.46'], '3.1 mi', '59→57%') }}")
    assert 'href="/cc/drives/7"' in html and 'class="row-title"' in html and 'class="pill"' not in html and html.count("</span> · <span>") == 2
    assert "3.1 mi" in html and "59→57%" in html and "thumb" not in html
    assert "row-thumb" in render("{{ parts.row('/x', 't', 'a', [], '1', '', '<svg></svg>') }}")
    assert "Asleep 2 h" in render("{{ parts.gap_row('Asleep 2 h') }}")


def test_gap_row_makes_its_place_open_the_naming_card():
    ref = {"name": "Elm Street, Springfield", "lat": 39.8, "lon": -89.65, "new": True}
    html = render("{{ parts.gap_row('Parked 18 min at Elm Street, Springfield', refs=[ref]) }}", ref=ref)
    assert 'Parked 18 min at <button class="pn pn-new" type="button" aria-haspopup="dialog" data-lat="39.8" data-lon="-89.65">Elm Street, Springfield</button>' in html
    named = render("{{ parts.gap_row('Parked 2 h at Home', refs=[dict(ref, name='Home', new=False)]) }}", ref=ref)
    assert '<button class="pn" type="button"' in named and 'class="pl-ic pl-home"' in named, "a named place opens it too, unmarked, with its glyph"


def test_a_row_title_names_both_ends_as_spans_inside_its_link():
    """A row is one link; a button cannot sit in a link, so its names are spans that act as
    buttons (role, tabindex), which the card's script keeps from following the link."""
    refs = [{"name": "Home", "lat": 39.8, "lon": -89.65, "new": False}, {"name": "Home", "lat": 39.9, "lon": -89.7, "new": False}]
    html = render("{{ parts.row('/d/1', '8:49 AM', 'Home → Home', [], '', '', refs=refs) }}", refs=refs)
    assert html.count('<span class="pn" role="button" tabindex="0" aria-haspopup="dialog"') == 2, "the same name twice is two places"
    assert 'data-lat="39.8"' in html and 'data-lat="39.9"' in html and "<button" not in html


def test_gap_row_stays_plain_text_when_its_place_is_not_in_the_text():
    """A mismatch (day.py's `where` and the sentence `day_gap` built from a different field)
    falls through to the ordinary text|places rendering instead of raising."""
    ref = {"name": "Elm Street, Springfield", "lat": 39.8, "lon": -89.65, "new": True}
    html = render("{{ parts.gap_row('Parked 18 min', refs=[ref, none]) }}", ref=ref)
    assert html == '<div class="row-gap">Parked 18 min</div>'


def test_gauge_marks_the_limit_only_when_known():
    assert "limit 90%" in render("{{ parts.gauge(58, 90) }}") and 'style="width:58.0%"' in render("{{ parts.gauge(58, 90) }}")
    assert "limit" not in render("{{ parts.gauge(58, None) }}")


def test_car_card_leads_with_state_and_greys_what_did_not_happen():
    end = datetime(2026, 9, 30, 5, 25, tzinfo=timezone.utc)
    c = {
        "vin": "A",
        "name": "Alpha",
        "status": "Sleeping",
        "soc": 43.0,
        "range_mi": 119.0,
        "charge_target": None,
        "place": "Home",
        "live": False,
        "updated_age": "45 min ago",
        "ribbon": [{"label": "sleeping", "width": 1.0, "start": end - timedelta(hours=24), "end": end}],
        "last_drive": None,
    }
    html = render(
        "{{ parts.car_card(c, '/cc', {'A': 1}, u, today, ticks) }}", c=c, today={"drives": 0, "miles": 0.0, "kwh": 0.0}, ticks=[{"left": 0.0, "label": "10 PM"}, {"left": 100.0, "label": "now"}]
    )
    assert ">Asleep<" in html and "43" in html and "no drives" in html and "no charge" in html and 'href="/cc/?vin=A"' in html
    blank = dict(c, soc=None, range_mi=None, updated_age=None, place=None, ribbon=[])
    html = render("{{ parts.car_card(c, '', {}, u, today, []) }}", c=blank, today={})
    assert "None" not in html and "—" in html


def test_a_car_record_missing_optional_fields_still_renders():
    """A history-only car's record has no charge limit and may lack a place or an age;
    the card crashed on the first missing key (2026-09-30)."""
    html = render("{{ parts.car_card(c, '', {}, u, None, []) }}", c={"vin": "A", "name": "Alpha", "status": "Idle"})
    assert ">Parked<" in html and "None" not in html and "—" in html


def test_chart_embeds_its_data_and_offers_extras_only_when_there_are_some():
    import json
    import re

    data = {
        "t0": 0,
        "t1": 86400,
        "primary": {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": [[0, 59.0], [86400, 23.0]]},
        "extras": [{"key": "range", "label": "Range", "unit": " mi", "lo": 0, "hi": 400, "points": [[0, 220.0]]}],
        "lane": [],
        "events": [{"a": 1, "b": 2, "kind": "drive", "title": "<b>x</b>", "figs": ""}],
    }
    html = render("{{ parts.chart('day-V1', data, 'now · 225 mi') }}", data=data)
    blob = re.search(r'<script type="application/json" class="chart-data">(.*?)</script>', html, re.S).group(1)
    assert json.loads(blob)["primary"]["points"][1] == [86400, 23.0]
    assert "<b>x</b>" not in blob, "a title with markup cannot close the script block"
    assert ">Range<" in html and 'data-extra="range"' in html and "23%" in html
    none = render("{{ parts.chart('c', data) }}", data=dict(data, extras=[]))
    assert "data-extra" not in none
    empty = render("{{ parts.chart('c', data) }}", data=dict(data, primary=dict(data["primary"], points=[])))
    assert "No battery readings logged in this window." in empty


def test_a_chart_with_no_readings_is_one_quiet_line():
    data = {"t0": 0, "t1": 86400, "primary": {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": []}, "extras": [], "lane": [], "events": []}
    html = render("{{ parts.chart('day-V1', data) }}", data=data)
    assert "No battery readings" in html
    assert "chart-svg" not in html and "chart-hov" not in html and "chart-key" not in html, "no empty frame, no floating card"


def test_a_rows_detail_parts_wrap_whole():
    html = render("{{ parts.row('/d/1', '9:20 AM', 'Home → Work', ['40 min'], '31.7 mi', '90→81%', extra=['11.0 kWh', '36.7 mi rated']) }}")
    assert '<span class="row-x"><span>11.0 kWh</span> · <span>36.7 mi rated</span></span>' in html


def test_bars_draw_every_month_with_the_focus_solid():
    from datetime import date

    from statspage import bars, by_month
    from units import Units

    months = by_month([{"period": date(2026, 9, 1), "miles": 100.0, "drives": 3}, {"period": date(2026, 8, 1), "miles": 200.0, "drives": 5}])
    g = bars({"V1": months}, ["V1"], (2026, 9), (2026, 8), Units(), n=2)
    html = render("{{ parts.bars(g, 'mi', {'V1': 'Alpha'}, {'V1': 0}) }}", g=g)
    assert html.count('class="mb ') == 2 and 'class="mb c0 on"' in html and "Alpha" in html and "stroke-dasharray" in html and ">Sep<" in html


def test_the_phone_drawing_carries_its_own_class_and_no_axis_unit():
    from datetime import date

    from statspage import bars, by_month
    from units import Units

    g = bars({"V1": by_month([{"period": date(2026, 9, 1), "miles": 100.0}])}, ["V1"], (2026, 9), (2026, 9), Units(), w=360, short=True)
    html = render("{{ parts.bars(g, 'mi', {}, {}, 'phone') }}", g=g)
    assert 'class="mbars phone"' in html and " mi</text>" not in html
