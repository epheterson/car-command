"""The clock and date order chosen in Settings reach every place a time or date is worded."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import jinja2
import pytest

import chartdata
import clockfmt
import filters
import nowpage

TZ = ZoneInfo("America/Chicago")
AT = datetime(2026, 10, 21, 19, 5, 9, tzinfo=TZ)


@pytest.fixture(autouse=True)
def reset():
    yield
    clockfmt.PREFS.update(clock="12", date_order="mdy")


def env(clock, order):
    e = jinja2.Environment()
    filters.register(e, TZ, clock, order)
    return e


@pytest.mark.parametrize(
    ("pattern", "want"),
    [
        ("%-I:%M %p", "19:05"),
        ("%-I:%M:%S %p", "19:05:09"),
        ("%-I:%M", "19:05"),
        ("%-I %p", "19:00"),
        ("%a %-I:%M %p", "Wed 19:05"),
        ("%A, %B %-d", "Wednesday, 21 October"),
        ("%b %-d, %Y", "21 Oct 2026"),
        ("%a %b %-d, %-I:%M %p", "Wed 21 Oct, 19:05"),
    ],
)
def test_the_24_hour_clock_and_day_first_dates_cover_every_pattern_the_site_uses(pattern, want):
    assert AT.strftime(clockfmt.adapt(pattern, "24", "dmy")) == want


def test_templates_say_it_through_local_and_dfmt():
    e = env("24", "dmy")
    out = e.from_string("{{ at|local('%-I:%M:%S %p') }}|{{ at|local('%-I:%M') }}|{{ at|dfmt('%A, %B %-d') }}|{{ at|dfmt('%b %-d') }}").render(at=AT)
    assert out == "19:05:09|19:05|Wednesday, 21 October|21 Oct"
    assert env("12", "mdy").from_string("{{ at|dfmt('%A, %B %-d') }}").render(at=AT) == "Wednesday, October 21"


def test_no_template_words_a_date_with_a_bare_strftime():
    """Display dates go through dfmt; strftime stays for machine patterns (ISO dates, input values)."""
    import re
    from pathlib import Path

    for p in sorted((Path(__file__).parent / "templates").glob("*.html")):
        for pat in re.findall(r"\.strftime\('([^']*)'\)", p.read_text()):
            assert not re.search(r"%[bBaAI]|%-d|%p", pat), (p.name, pat)


def test_the_now_strip_labels_follow_the_clock():
    end = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc)
    clockfmt.PREFS.update(clock="24")
    labels = [t["label"] for t in nowpage.strip_ticks(end - timedelta(hours=24), end, TZ)]
    hours = [t["label"] for t in nowpage.strip_ticks(end - timedelta(hours=24), end, TZ) if t["kind"] == "hour"]
    assert labels[-1] == "now" and hours and all(":" in x and "M" not in x and x != "noon" for x in hours)
    clockfmt.PREFS.update(clock="12")
    assert any("M" in t["label"] for t in nowpage.strip_ticks(end - timedelta(hours=24), end, TZ) if t["kind"] == "hour")


def test_chart_ticks_and_the_hover_card_follow_the_clock():
    t0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    clockfmt.PREFS.update(clock="24")
    labels = [x[1] for x in chartdata.ticks(t0, t0 + timedelta(hours=10), TZ)]
    assert labels and all("M" not in x for x in labels)
    pay = chartdata.payload(t0, t0 + timedelta(hours=1), {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": []}, [], [], [], tz=TZ)
    assert pay["h12"] is False
    clockfmt.PREFS.update(clock="12")
    assert chartdata.payload(t0, t0 + timedelta(hours=1), {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": []}, [], [], [], tz=TZ)["h12"] is True


def test_the_scripts_take_the_clock_from_the_page_not_the_browser():
    from pathlib import Path

    static = Path(__file__).parent / "static"
    for script in ("chart.js", "track.js"):
        assert "CCClock.hour(" in (static / script).read_text(), script
    assert "CCClock.hour(h12)" in (Path(__file__).parent / "templates" / "drive.html").read_text(), "the split-point label too"
    assert '"hour12"' not in "".join(p.read_text() for p in static.glob("*.js") if p.name != "clockfmt.js") and "hour12: true" in (static / "clockfmt.js").read_text()
    assert "h12:" in (Path(__file__).parent / "templates" / "drive.html").read_text()


@pytest.mark.parametrize(
    "stored, clock, said",
    [
        ("21:00", "12", "9:00 PM"),
        ("00:00", "12", "12:00 AM"),
        ("12:05", "12", "12:05 PM"),
        ("03:00", "12", "3:00 AM"),
        ("21:00", "24", "21:00"),
        ("3:00", "24", "03:00"),
        ("", "12", ""),
        (None, "24", ""),
        ("junk", "12", ""),
    ],
)
def test_a_stored_time_of_day_reads_in_the_chosen_clock_with_one_digit_rule(stored, clock, said):
    """12-hour never pads, 24-hour always does: "3:00 AM" and "03:00", never "03:00 AM" or "3:00"."""
    assert clockfmt.tod(stored, clock) == said


@pytest.mark.parametrize(
    "typed, got",
    [
        ("21:00", (21, 0)),
        ("9:30 pm", (21, 30)),
        ("9pm", (21, 0)),
        ("9 PM", (21, 0)),
        ("12 am", (0, 0)),
        ("12:15pm", (12, 15)),
        ("0930", (9, 30)),
        ("7", (7, 0)),
        ("7.15", (7, 15)),
        ("13pm", None),
        ("24:00", None),
        ("9:75", None),
        ("soon", None),
        ("", None),
        (None, None),
    ],
)
def test_a_time_typed_any_way_a_person_types_it_is_understood(typed, got):
    assert clockfmt.parse_tod(typed) == got


def test_the_time_boxes_show_and_read_the_chosen_clock():
    import settingsform

    for clock, shown in (("12", "9:00 PM"), ("24", "21:00")):
        e = env(clock, "mdy")
        assert e.from_string("{{ '21:00'|tod }}").render() == shown
    assert env("12", "mdy").from_string("{{ 6|hour }} {{ 18|hour }}").render() == "6 AM 6 PM"
    assert env("24", "mdy").from_string("{{ 6|hour }} {{ 18|hour }}").render() == "06:00 18:00"
    assert settingsform.hhmm("9:30 pm").strftime("%H:%M") == "21:30" and settingsform.hhmm("21:30").strftime("%H:%M") == "21:30" and settingsform.hhmm("later") is None


def test_the_plan_hours_and_a_scheduled_charges_answer_follow_the_clock():
    import commands
    import cost
    import planview

    assert cost._h(6) == "6am" and cost._h(24) == "12am" and dict(planview.hours_from())[18] == "6 pm"
    assert commands.said(commands.Command("sched_charge", "Scheduled charging", "Scheduled charging set", tier=2, arg=(0, 1439)), 180, True, None, "now") == "Scheduled charging set to 3:00 AM now"
    clockfmt.PREFS.update(clock="24")
    assert cost._h(6) == "06:00" and cost._h(24) == "00:00" and dict(planview.hours_from())[18] == "18:00"
    assert commands.said(commands.Command("sched_charge", "Scheduled charging", "Scheduled charging set", tier=2, arg=(0, 1439)), 180, True, None, "now") == "Scheduled charging set to 03:00 now"


def test_a_time_box_placeholder_follows_the_clock_set_after_the_template_compiled():
    """'21:00'|tod on a constant was folded once, at compile time, so the 24-hour setting never reached it."""
    env = jinja2.Environment(loader=jinja2.FileSystemLoader("templates"))
    filters.register(env, clock="12")
    page = env.from_string('{% import "_settings.html" as s %}{{ s.time("t", "07:00", "Quiet") }}{{ "13:05"|tod }}{{ 14|hour }}')
    assert 'placeholder="9:00 PM"' in page.render() and 'value="7:00 AM"' in page.render()
    filters.register(env, clock="24")
    out = page.render()
    assert 'placeholder="21:00"' in out and 'value="07:00"' in out and out.endswith("13:0514:00")
    filters.register(env, clock="12")
