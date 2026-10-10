"""The Stats page as rendered: a month in progress read span for span, the range and compare
controls as links, and no paragraph of explanation anywhere on it or its sub-pages."""

import re
from datetime import date, datetime
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import jinja2
import pytest

import filters
import statspage
from units import Units

TEMPLATES = Path(__file__).parent / "templates"
TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 5, 14, 0, tzinfo=TZ)  # the 5th: October is five days in
OLD = "added up. Energy per drive is rated range used at what a rated mile costs this car"


@pytest.fixture
def env():
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)), autoescape=True)
    filters.register(e)
    return e


def month(y, m, miles, kwh=0.0, cost=0.0):
    return {
        "period": date(y, m, 1),
        "miles": miles,
        "drives": int(miles / 10),
        "added_kwh": kwh,
        "cost": cost,
        "used_kwh": miles * 0.3,
        "drive_h": miles / 30,
        "charges": 1 if kwh else 0,
        "priced": 1 if cost else 0,
    }


def context(vs="prev"):
    whole = statspage.by_month([month(2026, 10, 51, 79, 23.76), month(2026, 9, 382, 212, 66.04), month(2026, 8, 900, 300, 80.0)])
    like = statspage.by_month([month(2026, 10, 51, 79, 23.76), month(2026, 9, 60, 20, 6.0), month(2026, 8, 120, 60, 15.0)])
    fraction = (NOW - datetime(2026, 10, 1, tzinfo=TZ)) / (datetime(2026, 11, 1, tzinfo=TZ) - datetime(2026, 10, 1, tzinfo=TZ))
    cmp = statspage.compare(whole, (2026, 10), fraction, (2026, 8), like)
    proj = next(m["proj"] for m in cmp if m["key"] == "miles")
    rng = statspage.period("month", None, None, None, None, NOW)
    state = {"period": "month", "month": None, "year": None, "from": None, "to": None, "vs": vs, "by": "day", "over": "1", "vin": "V1"}
    other = None  # a month is set beside the month before and the average, not a second range
    row = {
        "period": date(2026, 10, 5),
        "drives": 3,
        "miles": 51.0,
        "drive_h": 1.5,
        "used_kwh": 19.0,
        "wh_per_mi": 373.0,
        "drive_cost": 3.0,
        "charges": 1,
        "added_kwh": 79.0,
        "cost": 23.76,
        "cost_per_mi": 0.46,
    }
    totals = {k: row[k] for k in ("drives", "miles", "drive_h", "used_kwh", "charges", "added_kwh", "cost", "wh_per_mi")} | {"priced": 1, "avg_cost": 0.0}
    return {
        "root": "",
        "now": NOW,
        "car": {"name": "Alpha", "vin": "V1", "garage": False, "live": True, "updated_age": "", "model": ""},
        "active": "stats",
        "vehicles": [],
        "q": "?vin=V1",
        "colors": {"V1": 0},
        "names": {"V1": "Alpha"},
        "cur": "$",
        "csrf": "t",
        "auth_on": False,
        "rows": [row],
        "totals": totals,
        "per_car": [],
        "by": "day",
        "over": 1,
        "rate_plan": "Springfield EV",
        "bins": {"temp": [], "speed": []},
        "odometer": None,
        "month_window": {"label": "October 2026", "ym": "2026-10", "prev": "2026-09", "prev_label": "Sep 2026", "next": "2026-11", "next_label": "Nov 2026", "is_current": True},
        "fraction": fraction,
        "cmp": cmp,
        "heads": statspage.heads((2026, 10), fraction, NOW.day),
        "mbars": statspage.bars({"V1": whole}, ["V1"], (2026, 10), (2026, 8), Units(), proj=proj),
        "mbars_phone": statspage.bars({"V1": whole}, ["V1"], (2026, 10), (2026, 8), Units(), w=360, h=190, short=True, proj=proj),
        "rng": rng,
        "ctl": statspage.controls(state, rng),
        "compare": {**other, "totals": dict(totals, miles=60.0)} if other else None,
        "period": "month",
        "estimate": None,
        "petrol": None,
    }


def text(html):
    return unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)))


def test_a_month_five_days_in_reads_like_spans_with_a_projection(env):
    html = env.get_template("stats.html").render(**context())
    head = html[html.index('<table class="cmp">') : html.index("</thead>")]
    assert "Oct 1–5" in head and "Sep 1–5" in head and "12-mo avg, 1–5" in head and "September" not in head
    assert "vs Sep 1–5" in html and "so far" not in html and " pace<" not in html
    assert "on pace for ~345" in text(html), "51 mi in 4.58 of 31 days"
    assert 'class="mp"' in html and "on pace for ~345 mi" in text(html), "the October bar carries its lighter extension"
    # Oct 1–5 against Sep 1–5 is 51 against 60, not 51 against all of September's 382
    assert "▼ 15% vs Sep 1–5" in text(html) and "87%" not in text(html)


def test_vs_average_bars_never_exceed_their_half_of_the_track(env):
    html = env.get_template("stats.html").render(**context())
    widths = [float(w) for w in re.findall(r'<i class="(?:up|down|flat)(?: cap)?" style="width:([\d.]+)%"', html)]
    assert widths and max(widths) <= 50.0
    assert 'class="up cap"' in html, "charging cost $23.76 against an average of $10.50 is +126%: capped at the end"


def test_no_explanatory_paragraph_on_stats(env):
    html = env.get_template("stats.html").render(**context())
    assert OLD not in text(html) and "Since Oct 1, 2025" not in html
    for p in re.findall(r"<p\b[^>]*>(.*?)</p>", html, re.S):
        assert len(text(p).strip()) <= 80, text(p)
    assert "How the cost is worked out" in html and "priced on Springfield EV by the hour" in html, "the method moved into an (i)"


def test_stats_sub_pages_carry_no_long_paragraphs():
    """Labels, units and an (i); never a paragraph. Each <p> in the Stats templates, with its
    template expressions taken out, is short."""
    for name in ("stats.html", "charging.html", "battery.html", "sleep.html", "software.html"):
        src = (TEMPLATES / name).read_text()
        for p in re.findall(r"<p\b[^>]*>(.*?)</p>", src, re.S):
            bare = re.sub(r"\{%.*?%\}|\{\{.*?\}\}|<[^>]+>", "", p, flags=re.S)
            assert len(" ".join(bare.split())) <= 80, (name, bare)


def test_one_period_control_and_its_stepper(env):
    """The page had a month stepper over its top half and seven range presets, Compare and
    By over the rest, three control styles in 200 px. Now the header holds one: Month, Year,
    All or Custom, with a stepper for a month or a year; every control is a .segmented."""
    html = env.get_template("stats.html").render(**context())
    main = html[html.index("<main") :]
    assert 'aria-label="Period"' in main and main.count('aria-label="Range"') == 0 and 'class="segc' not in main and "segs2" not in main
    assert 'href="/stats?vs=prev&amp;by=day&amp;vin=V1" aria-current="page">Month' in main, "this month is the default and leaves the URL"
    assert 'href="/stats?period=year&amp;vs=prev&amp;by=day&amp;vin=V1">Year' in main
    assert 'href="/stats?period=custom&amp;from=2026-10-01&amp;to=2026-10-05&amp;vs=prev&amp;by=day&amp;vin=V1">Custom' in main, "Custom opens on the period's own dates"
    assert 'href="/stats?month=2026-09&amp;vs=prev&amp;by=day&amp;vin=V1" aria-label="Sep 2026"' in main and '<span class="off" aria-hidden="true">›</span>' in main, "back a month; none ahead of now"
    assert 'href="/stats?vs=prev&amp;vin=V1">Week' in main, "week is the default grain and leaves the URL"
    assert 'aria-label="Compare with"' not in main, "a month is already set beside the month before and its average"
    assert 'class="dates"' not in main, "the date fields only appear for Custom"


def test_a_year_steps_by_year_and_can_be_set_beside_the_last(env):
    ctx = context()
    rng = statspage.period("year", None, "2025", None, None, NOW)
    state = {"period": "year", "month": None, "year": "2025", "from": None, "to": None, "vs": "prev", "by": "month", "over": "1", "vin": "V1"}
    other = statspage.versus(rng, "prev")
    ctx |= {"rng": rng, "ctl": statspage.controls(state, rng), "period": "year", "compare": {**other, "totals": dict(ctx["totals"], miles=60.0)}}
    html = env.get_template("stats.html").render(**ctx)
    main = html[html.index("<main") :]
    assert '<span class="now">2025' in main and 'href="/stats?period=year&amp;year=2024&amp;vs=prev&amp;by=month&amp;vin=V1"' in main
    assert 'href="/stats?period=year&amp;vs=prev&amp;by=month&amp;vin=V1" aria-label="2026"' in main, "forward to this year, which leaves the URL bare of it"
    assert 'aria-label="Compare with"' in main and "Jan 1 – Dec 31, 2024" in main
    assert '<table class="cmp">' not in main, "the month comparison is the month view's"


def test_custom_reveals_two_date_fields_that_keep_the_rest(env):
    ctx = context()
    rng = statspage.resolve("custom", date(2026, 9, 1), date(2026, 9, 20), NOW)
    ctx |= {
        "rng": rng,
        "ctl": statspage.controls({"period": "custom", "month": None, "year": None, "from": "2026-09-01", "to": "2026-09-20", "vs": None, "by": "week", "over": "1", "vin": "V1"}, rng),
        "period": "custom",
        "compare": None,
    }
    html = env.get_template("stats.html").render(**ctx)
    form = html[html.index('class="dates"') : html.index("</form>", html.index('class="dates"'))]
    assert 'name="from" value="2026-09-01"' in form and 'name="to" value="2026-09-20"' in form and 'name="period" value="custom"' in form
    assert 'name="vin" value="V1"' in form and 'name="by"' not in form, "the rest of the state rides along, defaults left out"
