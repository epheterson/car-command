"""Every page that shows a cost priced on a regional average marks it ≈, with a title
naming the average; drive costs are marked when the home rate behind them was partly an
average. Render tests with an average-priced fixture."""

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import jinja2
import pytest

import filters
from units import Units

T0 = datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc)
CAR = {"name": "Alpha", "model": "Example EV", "updated_age": "just now", "capacity_kwh": 75.0, "vin": "V1", "live": False}
BASE = {"root": "/cc", "now": T0, "car": CAR, "active": "", "vehicles": [{"vin": "V1", "name": "Alpha"}], "q": "", "colors": {"V1": 0}, "names": {"V1": "Alpha"}, "cur": "$"}
CHARGE = {
    "id": 3,
    "vin": "V1",
    "session_start_date": T0,
    "session_end_date": T0 + timedelta(hours=3),
    "duration": timedelta(hours=3),
    "where": "Home",
    "kwh": 13.8,
    "cost": 2.64,
    "cost_source": "average",
    "rate_plan": "Illinois average",
    "rate_month": "2026-07",
    "start_level": 50,
    "end_level": 70,
    "charging_type": "AC",
    "at_home": True,
}
DRIVE = {
    "id": 1,
    "vin": "V1",
    "start_date": T0,
    "destination_date": T0 + timedelta(minutes=20),
    "duration": timedelta(minutes=20),
    "miles": 12.0,
    "from": "Home",
    "to": "Elm Street",
    "kwh_used": 3.5,
    "wh_per_mi": 290.0,
    "cost": 0.67,
    "cost_estimate": True,
    "level_start": 70,
    "level_end": 65,
}


def plain(html: str) -> str:
    """The page with each estimate mark reduced to its ≈, so "≈$2.64" can be looked for."""
    return re.sub(r'<span class="est" title="[^"]*">≈</span>', "≈", html)


@pytest.fixture
def env():
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(e)
    return e


def test_the_day_tile_and_rows_mark_estimates():
    import daypage

    parts = daypage.row_parts(daypage.as_charge(CHARGE) | {"kind": "charge", "cost_estimate": True}, Units(), "$")
    assert "≈$2.64" in parts["pills"]
    parts = daypage.row_parts({"kind": "drive", "cost": 0.67, "cost_estimate": True, "miles": 12.0}, Units(), "$")
    assert "≈$0.67" in parts["pills"]


def test_the_day_totals_carry_whether_any_cost_is_an_estimate(env):
    from day import day_totals

    t = day_totals([dict(DRIVE, kind="drive"), dict(CHARGE, kind="charge", cost_estimate=True)], None)
    assert t["cost_estimate"] and t["drive_cost_estimate"]
    blocks = [{"vin": "V1", "name": "Alpha", "totals": {"charges": 1, "kwh": 13.8, "cost": 2.64, "cost_estimate": True, "battery_used": None}, "rows": []}]
    html = plain(env.get_template("day.html").render(**BASE, day=T0.date(), blocks=blocks, prev=date(2026, 9, 3), next=None, month="2026-09", show="all", today=date(2026, 9, 5)))
    assert "≈$2.64" in html.split('<span class="l">Charged</span>')[1].split("</div>")[0]


def test_the_calendar_month_marks_its_estimated_cost(env):
    from stats import summarize

    (month,) = summarize([dict(DRIVE, destination_date=T0 + timedelta(minutes=20))], [CHARGE], "month", timezone.utc, None)
    assert month["avg_cost"] == pytest.approx(2.64) and month["drive_cost_estimate"]
    window = {"start": T0, "end": T0, "label": "September 2026", "ym": "2026-09", "prev": "2026-08", "prev_label": "Aug 2026", "next": "2026-10", "next_label": "Oct 2026", "is_current": True}
    html = env.get_template("calendar.html").render(
        **BASE, weeks=[], window=window, totals={"drives": 1, "miles": 12.0, "charges": 1, "kwh": 13.8}, month=dict(month, added_kwh=13.8), center=[40.0, -89.0]
    )
    assert plain(html).count("≈$2.64") >= 2, "the Charged tile and the Cost row"


def test_tags_and_the_drive_page_mark_an_estimated_drive_cost(env):
    window = {"start": T0, "end": T0, "label": "September 2026", "ym": "2026-09", "prev": "2026-08", "prev_label": "Aug 2026", "next": "2026-10", "next_label": "Oct 2026", "is_current": True}
    totals = {"drives": 1, "miles": 12.0, "hours": 0.3, "kwh": 3.5, "cost": 0.67, "cost_estimate": True}
    html = plain(env.get_template("tags.html").render(**BASE, tags=["Work"], tag="Work", drives=[DRIVE], totals=totals, window=window, routes=[], away=[]))
    assert "≈$0.67" in html
    mark = str(env.globals["est"](True, "the home rate is partly the Illinois average"))
    assert mark.startswith("<span") and 'title="the home rate is partly the Illinois average"' in mark and "≈" in mark
    assert str(env.globals["est"](False, "x")) == ""
    source = (Path(__file__).parent / "templates" / "drive.html").read_text()
    assert "est(trip.cost_estimate" in source, "the drive page's cost tile carries the mark"


def test_stats_marks_an_estimated_drive_cost(env):
    from statspage import bars, compare

    row = {"period": T0.date(), "drives": 1, "miles": 12.0, "drive_h": 0.3, "used_kwh": 3.5, "wh_per_mi": 290.0, "drive_cost": 0.67, "drive_cost_estimate": True, "charges": 1, "added_kwh": 13.8}
    row |= {"cost": 2.64, "priced": 1, "avg_cost": 2.64, "cost_per_mi": 0.22}
    mw = {"label": "September 2026", "ym": "2026-09", "prev": "2026-08", "prev_label": "Aug 2026", "next": "2026-10", "next_label": "Oct 2026", "is_current": False}
    ctx = dict(rows=[row], totals=dict(row), by="week", over=1, rate_plan=None, bins={"temp": [], "speed": []}, odometer=None, range_from=None, range_to=None, month_window=mw, fraction=1.0)
    ctx |= dict(
        cmp=compare({(2026, 9): row}, (2026, 9), 1.0, (2026, 9)),
        mbars=bars({"V1": {(2026, 9): row}}, ["V1"], (2026, 9), (2026, 9), Units()),
        mbars_phone=bars({"V1": {(2026, 9): row}}, ["V1"], (2026, 9), (2026, 9), Units(), short=True),
    )
    html = plain(env.get_template("stats.html").render(**BASE, **ctx))
    assert "≈$0.67" in html


def test_the_drive_cost_says_estimate_when_an_average_fed_the_home_rate():
    from stats import drive_cost

    assert drive_cost(3.5, (0.19, True)) == (pytest.approx(0.665), True)
    assert drive_cost(3.5, (0.19, False)) == (pytest.approx(0.665), False)
    assert drive_cost(None, (0.19, True)) == (None, False) and drive_cost(3.5, None) == (None, False)


def test_the_home_rate_records_whether_an_average_fed_it():
    from facts import fill_vehicle_facts

    class Conn:
        def __init__(self):
            self.sql, self.args = [], []

        def execute(self, sql, args=()):
            self.sql.append(sql)
            self.args.append(args)
            row = {"n": 30, "k": 0.25} if "RATED" in sql or "SUM(c.real_charged)" in sql else {"rate": 0.19, "est": True}

            class R:
                def fetchone(self):
                    return row

            return R()

    conn = Conn()
    fill_vehicle_facts(conn, "V1")
    assert any("cost_source = 'average'" in s for s in conn.sql)
    assert conn.args[-1][-1] is True and "home_rate_estimate" in conn.sql[-1]
