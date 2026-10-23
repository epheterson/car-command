"""The app must import with only environment configuration; every page route must be mounted at / and /cc."""

import importlib
from datetime import datetime, timezone
from types import SimpleNamespace

import re
import sys
from pathlib import Path

import pytest


def test_charge_pins_carry_the_car_and_speed(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    monkeypatch.setenv("REST_USER", "u")
    monkeypatch.setenv("REST_PASSWORD", "p")
    sys.path.insert(0, str(Path(__file__).parent))
    app_module = importlib.import_module("app")
    pins = app_module.charge_pins([{"session_position_latitude": 40.7001, "session_position_longitude": -74.0, "kwh": 30.0, "where": "Home", "charging_type": "DC", "vin": "V2"}])
    assert pins[0]["v"] == "V2" and pins[0]["fast"] is True


def test_app_imports_and_mounts_every_page(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    monkeypatch.setenv("REST_USER", "u")
    monkeypatch.setenv("REST_PASSWORD", "p")
    sys.path.insert(0, str(Path(__file__).parent))
    app_module = importlib.import_module("app")
    paths = {r.path for r in app_module.site.routes}
    for p in (
        "/",
        "/drives",
        "/charges",
        "/calendar",
        "/timeline",
        "/day",
        "/charging",
        "/software",
        "/tags",
        "/service",
        "/settings",
        "/settings/{section}",
        "/api/month-routes",
        "/map",
        "/api/driveprint",
        "/battery",
        "/sleep",
        "/stats",
        "/places",
        "/drives.csv",
        "/charges.csv",
        "/health",
        "/api/now",
        "/api/drives",
        "/api/charges",
    ):
        assert p in paths, p
    # Starlette spells a mount at the root "" on some versions and "/" on others.
    mounts = {r.path or "/" for r in app_module.app.routes}
    assert {"/carcommand", "/cc", "/"} <= mounts


def test_dockerfile_copies_every_module():
    here = Path(__file__).parent
    dockerfile = (here / "Dockerfile").read_text()
    modules = {p.name for p in here.glob("*.py") if not p.name.startswith("test_") and p.name != "conftest.py"}
    copied = {tok for line in dockerfile.splitlines() if line.startswith("COPY") for tok in line.split()[1:-1]}
    # Data files the modules read beside themselves (demo_routes.json, connectors.json).
    data = {p.name for p in here.glob("*.json")}
    missing = (modules | data) - copied
    assert not missing, f"Dockerfile does not COPY {sorted(missing)}"


def test_every_template_compiles():
    import jinja2

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    sys.path.insert(0, str(Path(__file__).parent))
    import filters

    filters.register(env)
    for name in env.list_templates():
        env.get_template(name)


def test_detail_templates_render_with_plausible_rows():
    import jinja2
    from datetime import datetime, timedelta, timezone

    sys.path.insert(0, str(Path(__file__).parent))
    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    t0 = datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc)
    car = {"name": "Alpha", "model": "Example EV", "updated_age": "just now", "capacity_kwh": 117.2, "vin": "V1", "live": True}
    vehicles = [{"vin": "V1", "name": "Alpha", "live": True}, {"vin": "V2", "name": "Bravo", "live": False}]
    base = {"root": "/cc", "now": t0, "car": car, "active": "", "vehicles": vehicles, "q": "?vin=V1", "colors": {"V1": 0, "V2": 1}, "names": {"V1": "Alpha", "V2": "Bravo"}, "cur": "$"}
    trip = {
        "id": 1,
        "start_date": t0,
        "destination_date": t0 + timedelta(minutes=42),
        "start_odometer": 1015.0,
        "destination_odometer": 1041.3,
        "miles": 26.3,
        "duration": timedelta(minutes=42),
        "from": "Elm Street, Springfield",
        "to": "Main Street, Shelbyville",
        "from_named": True,
        "to_named": False,
        "from_ref": {"name": "Elm Street, Springfield", "lat": 40.71, "lon": -74.0, "new": False},
        "to_ref": {"name": "Main Street, Shelbyville", "lat": 40.76, "lon": -73.99, "new": True},
        "temp_f": 68.0,
    }
    pos = {"start_position_latitude": 40.71, "start_position_longitude": -74.00, "destination_position_latitude": 40.76, "destination_position_longitude": -73.99}
    stats = {"avg_mph": 37.6, "used_pct": 9.0, "kwh": 10.5, "wh_per_mi": 401.0, "max_mph": 71.0, "rated_used": 28.4, "rated_efficiency": 0.93, "kwh_basis": "rated range used"}
    chart = {
        "t0": 0,
        "t1": 2400,
        "primary": {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": [[0, 74.0], [2400, 65.0]]},
        "extras": [],
        "lane": [[0, 2400, "driving"]],
        "events": [],
        "ticks": [],
    }
    html = env.get_template("drive.html").render(
        **base,
        trip=dict(trip, by_hand=False),
        pos=pos,
        stats=dict(stats, outside_start=68.0, outside_end=66.0),
        lvl_start=74.0,
        lvl_end=65.0,
        route=[[40.71, -74.00], [40.74, -74.00], [40.76, -73.99]],
        tags=["Work"],
        chart=chart,
        hidden=False,
        samples=[],
        every=1,
        fixes=0,
    )
    assert 'class="chartbox"' in html and 'class="tiles' in html
    assert "Elm Street" in html and ">38<" in html and "401" in html and 'id="drv-maps"' in html and ">71<" in html
    assert 'class="pn pn-new" type="button" aria-haspopup="dialog" data-lat="40.76" data-lon="-73.99"' in html, "the unnamed end opens the naming card, marked as wanting a name"
    assert 'class="pn" type="button" aria-haspopup="dialog" data-lat="40.71"' in html, "the named start opens it too, to rename it"
    assert "68→66 °F" in html, "outside temperature at both ends of the drive"
    assert "Every reading" in html and "No positions recorded inside this drive" in html, "the readings table survives the redesign"
    assert "Navigated to" not in html, "a drive with no route set in the car says nothing of one"
    html = env.get_template("drive.html").render(
        **base,
        trip=dict(trip, by_hand=False),
        pos=pos,
        stats=stats,
        lvl_start=74.0,
        lvl_end=65.0,
        route=[[40.71, -74.00], [40.76, -73.99]],
        navigated="Bravo Market",
        tags=[],
        chart=chart,
        hidden=False,
        samples=[],
        every=1,
        fixes=0,
    )
    assert 'Navigated to <b>Bravo Market</b>' in html and html.index('id="drv-maps"') < html.index("Navigated to"), "under the route"
    # the speed key, the replay and previous / next, when the route has times and speeds
    from datetime import datetime as _dt, timezone as _tz

    near = {"prev": {"kind": "drive", "id": 7, "start": _dt(2026, 10, 6, 8, tzinfo=_tz.utc), "end": None, "href": "/drives/7", "miles": 4.2, "where": "Alpha Park"}, "next": None}
    shares = [{"label": lb, "share": sh, "minutes": sh * 40} for lb, sh in zip(["0–25", "25–45", "45–60", "60–75", "75+"], [0.2, 0.5, 0.3, 0.0, 0.0])]
    html = env.get_template("drive.html").render(
        **base,
        trip=dict(trip, by_hand=False, fsd_miles=30.0),
        pos=pos,
        stats=dict(stats, climb_ft=400.0, descent_ft=120.0, elev_lo=10.0, elev_hi=300.0, cells_new=12, cells_covered=80, ground=True),
        lvl_start=74.0,
        lvl_end=65.0,
        route=[[40.71, -74.00], [40.76, -73.99]],
        timed=[[40.71, -74.0, 0, 20.0], [40.76, -73.99, 2400, 50.0]],
        steps=(0, 25, 45, 60, 75),
        shares=shares,
        near=near,
        tags=[],
        chart=dict(chart, tz="America/Chicago"),
        hidden=False,
        samples=[],
        every=1,
        fixes=0,
    )
    assert (
        'class="tr-play"' in html and 'data-band="2"' in html and 'data-band="4"' not in html and "<b>50%</b>" in html and "flex-grow:500" in html
    ), "one bar in proportion, the unused bands left out"
    assert 'rel="prev" href="' in html and '/drives/7?vin=' in html and "Alpha Park" in html and 'rel="next"' not in html
    assert "FSD <b>26.3</b> of 26.3" in html and "Roads new to the car" in html and "15%" in html and "10 to 300 ft" in html and "↑400 ↓120" in html and "net +280 ft" in html
    assert ">terrain</span>" in html, "the climb says it was measured on the terrain model"
    assert "class=\"chart-big\"" not in html and 'class="drv-bat"' in html and "pn-in" in html, "the battery said once, in the headline; the neighbours as small links"
    assert 'tz: "America/Chicago"' in html and "start: null" in html, "the line's own ends; the replay's clock on the site's zone"
    html = env.get_template("drive.html").render(
        **base,
        trip=dict(trip, destination_date=None, miles=None, duration=None, by_hand=True),
        pos=None,
        stats={},
        lvl_start=None,
        lvl_end=None,
        route=[],
        tags=[],
        chart=dict(chart, primary=dict(chart["primary"], points=[])),
        hidden=False,
        samples=[],
        every=1,
        fixes=0,
    )
    assert "in progress" in html and "No battery readings" in html and "/remove" in html, "a drive still going, with nothing recorded, renders; a hand-entered one can be removed here"
    session = {
        "id": 1,
        "session_start_date": t0,
        "session_end_date": t0 + timedelta(hours=2),
        "plug_connected_date": t0,
        "plug_disconnected_date": None,
        "start_level": 40.0,
        "end_level": 80.0,
        "charging_type": "AC",
        "where": "Home",
        "gained": 40.0,
        "duration": timedelta(hours=2),
        "cost": 18.80,
        "rate_plan": "Springfield EV",
        "cost_parts": {"off_peak": 47.0, "part_peak": 0.0, "peak": 0.0},
    }
    cstats = {"kwh": 47.0, "peak_kw": 24.0, "avg_kw": 23.5, "gained_pct": 40.0, "kwh_per_100": 117.5}
    cchart = {"t0": 0, "t1": 7200, "primary": {"key": "power", "label": "Power", "unit": " kW", "lo": 0, "hi": 30, "points": []}, "extras": [], "lane": [], "events": [], "ticks": []}
    html = env.get_template("charge.html").render(
        **base,
        session=dict(session, by_hand=False, range_added=160.0),
        stats=dict(cstats, kwh_profile=49.1, avg_volts=240, max_volts=246, avg_amps=48, max_amps=50),
        chart=cchart,
        hidden=False,
        samples=[],
        every=1,
        readings=0,
    )
    assert "Home" in html and "47.0" in html and "No power readings" in html and "$18.80" in html
    assert "+160 mi rated" in html and "49.1 kWh" in html and "96% into the battery" in html and "240 V" in html and "246 at most" in html and "50 at most" in html
    assert "Every reading" in html and "No charging-power readings recorded" in html, "the readings table survives the redesign"
    html = env.get_template("charge.html").render(
        **base, session=dict(session, session_end_date=None, duration=None, cost=None, by_hand=True), stats={"kwh": None}, chart=cchart, hidden=False, samples=[], every=1, readings=0
    )
    assert "still charging" in html and "/remove" in html
    html = env.get_template("charge.html").render(
        **base,
        session=dict(session, cost=19.95, cost_source="billed", rate_plan="Billed by Tesla, Springfield, IL", cost_parts={}, by_hand=False),
        bill={"invoice_id": "c0ffee 1234"},
        stats=cstats,
        chart=cchart,
        hidden=False,
        samples=[],
        every=1,
        readings=0,
    )
    assert "$19.95" in html and "Billed by Tesla, Springfield, IL" in html and '/dx/charging/invoice/c0ffee%201234"' in html, "a billed charge links its invoice through the site"
    # what it bought until the next charge, and the arrows either side
    nxt = {"kind": "charge", "id": 2, "start": t0 + timedelta(days=2), "end": None, "href": "/charges/2", "gained": 30.0, "where": "Home"}
    paid = {
        "ago": timedelta(days=1, hours=22),
        "miles": 120.0,
        "battery_used": 45.0,
        "rated_used": 150.0,
        "efficiency": 0.8,
        "used_kwh": 36.0,
        "wh_per_mi": 300.0,
        "cost_per_mi": 0.12,
        "until": nxt,
        "end_level": 80.0,
        "next_level": 35.0,
    }
    html = env.get_template("charge.html").render(**base, session=session, stats=cstats, chart=cchart, hidden=False, samples=[], every=1, readings=0, near={"prev": None, "next": nxt}, paid=paid)
    assert "What this charge bought" in html and "120.0<small>mi</small>" in html and "left:35.0%; width:45.0%" in html and ">35%</span>" in html and ">80%</span>" in html
    assert "300 Wh/mi" in html and "$0.12" in html and "80% of rated" in html and "until the next charge" in html and 'rel="next"' in html
    assert 'id="chg-map"' not in html, "no map base, no map strip"
    html = env.get_template("charge.html").render(**base, session=session, stats=cstats, chart=cchart, hidden=False, samples=[], every=1, readings=0, paid=dict(paid, until=None, next_level=None))
    assert "so far, no charge since" in html and ">35%</span>" in html, "the battery now, when no charge has followed"
    rows = [
        {
            "period": t0.date(),
            "drives": 3,
            "miles": 61.2,
            "drive_h": 1.9,
            "used_kwh": 21.0,
            "wh_per_mi": 343.0,
            "charges": 1,
            "added_kwh": 30.0,
            "cost": 12.0,
            "cost_per_mi": 0.196,
            "priced": 1,
            "avg_cost": 0.0,
        }
    ]
    totals = {"drives": 3, "miles": 61.2, "drive_h": 1.9, "used_kwh": 21.0, "charges": 1, "added_kwh": 30.0, "cost": 12.0, "wh_per_mi": 343.0, "priced": 1, "avg_cost": 0.0}
    bins = {"temp": [{"lo": 40, "hi": 50, "drives": 3, "miles": 60.0, "kwh": 27.0, "wh_per_mi": 450.0, "share": 1.0}], "speed": []}
    from datetime import date as _date

    from statspage import bars as sbars, by_month, compare as scompare
    from units import Units

    mrows = by_month([dict(rows[0], period=_date(2026, 9, 1)), dict(rows[0], period=_date(2026, 8, 1), miles=100.0)])
    mwin = {"label": "September 2026", "ym": "2026-09", "prev": "2026-08", "prev_label": "Aug 2026", "next": "2026-10", "next_label": "Oct 2026", "is_current": True}
    odo = {
        "t0": 0,
        "t1": 86400 * 30,
        "primary": {"key": "odo", "label": "Odometer", "unit": " mi", "lo": 1000, "hi": 1100, "points": [[0, 1000.0], [86400 * 30, 1061.2]]},
        "extras": [],
        "lane": [],
        "events": [],
        "ticks": [],
    }
    stats_ctx = dict(
        rows=rows,
        totals=totals,
        by="week",
        over=1,
        rate_plan="Springfield EV",
        bins=bins,
        odometer=odo,
        range_from=None,
        range_to=None,
        month_window=mwin,
        fraction=1.0,
        cmp=scompare(mrows, (2026, 9), 1.0, (2026, 8)),
        mbars=sbars({"V1": mrows}, ["V1"], (2026, 9), (2026, 8), Units(), n=12),
        mbars_phone=sbars({"V1": mrows}, ["V1"], (2026, 9), (2026, 8), Units(), w=360, short=True),
    )
    html = env.get_template("stats.html").render(**base, **stats_ctx)
    assert 'id="ptitle"' in html and html.index('id="ptitle"') < html.index("<h1>Stats</h1>") < html.index(
        'document.getElementById("ptitle")'
    ), "the bar's title, then the h1 it is taken from, then the script"
    assert 'class="tiles' in html and "vs Aug" in html and 'class="mbars"' in html and 'class="cmp"' in html and "12-month average" in html and 'id="odo"' in html
    assert "61.2" in html and "$12.00" in html and "343" in html and "40–50" in html and "450" in html, "the window table and the efficiency bins stay"
    stats_ctx["period"] = "year"  # the window's own tiles: a year, all time or two dates
    html = env.get_template("stats.html").render(**base, **dict(stats_ctx, rate_plan=None))
    assert "no rate plan set" in html and "until you set a plan in Settings" in html and "priced on None" not in html
    # No plan, home charges priced at the regional average: every figure says it is an
    # estimate and whose average, and the footnote names the source and the months.
    estimate = {"cost": 12.0, "charges": 1, "areas": ["Illinois"], "sources": ["US EIA, average residential electricity price"], "from": "2026-07", "to": "2026-07", "lagged": True}
    petrol = {"ok": True, "saved": 5.5, "mpg": 30.0, "l_per_100km": 7.84, "gallons": 3.9, "litres": 14.8, "petrol": 17.5, "charging": 12.0, "miles": 117.0, "charges": 1}
    petrol.update(own_price=None, sources=["US EIA, regular gasoline retail price"], areas=["Midwest (PADD 2)"], skipped={"unpriced": 2, "petrol": 0, "efficiency": 0})
    petrol.update({"from": "2026-09", "to": "2026-09"})
    est_rows = [dict(rows[0], avg_cost=12.0)]
    html = env.get_template("stats.html").render(**base, **dict(stats_ctx, rate_plan=None, rows=est_rows, totals=dict(totals, avg_cost=12.0), estimate=estimate, petrol=petrol))
    assert "≈$12.00" in html and "estimate from the Illinois average" in html and "estimated at the Illinois average" in html
    assert "Illinois average (US EIA, average residential electricity price), Jul 2026 figures" in html and "take the newest figure" in html
    assert "≈$6" in html and "saved vs a 30 mpg car" in html and "Midwest (PADD 2) average (US EIA, regular gasoline retail price), Sep 2026" in html
    assert "2 charges left out (2 with no price)" in html
    # No plan and no average: no bare $0 dressed as a real figure.
    bare = dict(rows[0], cost=0.0, priced=0, cost_per_mi=None)
    html = env.get_template("stats.html").render(
        **base, **dict(stats_ctx, rate_plan=None, rows=[bare], totals=dict(totals, cost=0.0, priced=0), cmp=scompare(by_month([dict(bare, period=_date(2026, 9, 1))]), (2026, 9), 1.0, (2026, 8)))
    )
    assert "No price yet" in html and "Set a plan in Settings › Money" in html and '<span class="v">$0' not in html
    bare_ctx = dict(
        stats_ctx, period="month", rate_plan=None, rows=[bare], totals=dict(totals, cost=0.0, priced=0), cmp=scompare(by_month([dict(bare, period=_date(2026, 9, 1))]), (2026, 9), 1.0, (2026, 8))
    )
    html = env.get_template("stats.html").render(**base, **bare_ctx)
    assert "no price<" in html and "No price yet" in html and '<span class="v">$0' not in html, "the month view says it too"
    # With a plan set, the unpriced charges are away ones: no plan to set, a place to price.
    for ctx in (dict(bare_ctx, rate_plan="PG&E EV2A"), dict(bare_ctx, period="year", rate_plan="PG&E EV2A")):
        html = env.get_template("stats.html").render(**base, **ctx)
        assert "Not priced" in html and "Away charges · price a place" in html and "No price yet" not in html and "Set a plan" not in html
    window = {"start": t0, "end": t0, "label": "September 2026", "ym": "2026-09", "prev": "2026-08", "prev_label": "Aug 2026", "next": "2026-10", "next_label": "Oct 2026", "is_current": True}
    import daypage

    html = env.get_template("drives.html").render(
        **base, drives=[trip], days=daypage.by_day([trip], timezone.utc, "start_date"), window=window, sort="start", desc=True, sort_base="?", flt={"raw": {}}
    )
    assert "September 2026" in html and "month=2026-08" in html and "month=2026-10" not in html and "Elm Street" in html
    assert html.count('class="row-b') == 1 and 'class="day-h"' in html and 'class="fsheet"' in html and "<table" not in html
    assert "sort=miles" in html and "drives.csv" in html and "show=hidden" in html
    assert "Show older" not in html
    more = {"left": 6625, "label": "Show older", "href": "?from=2000-01-01&rows=1000"}
    html = env.get_template("drives.html").render(
        **base, drives=[trip], days=daypage.by_day([trip], timezone.utc, "start_date"), window=window, sort="start", desc=True, sort_base="?", flt={"raw": {}}, more=more
    )
    assert '6,625 more<a class="btn sm" href="?from=2000-01-01&' in html and 'rows=1000" data-no-prefetch>Show older</a>' in html
    html = env.get_template("charges.html").render(**base, charges=[], days=[], window=window, sort="start", desc=True, sort_base="?", flt={}, more=None)
    assert "Show older" not in html and "No charges in September 2026" in html
    html = env.get_template("drives.html").render(**base, drives=[], days=[], window=window, sort="start", desc=True, sort_base="?", flt={"raw": {"min": "500"}}, hidden=True)
    assert "No drives in September 2026" in html and "Back to the list" in html and "Filter · 1 on" in html
    assert "marked “by hand”" in html and "can be removed from here" not in html
    charge_rec = dict(session, id=9, vin="V1", kwh=30.0, range_added=120.0, by_hand=False)
    html = env.get_template("charges.html").render(
        **base, charges=[charge_rec], days=daypage.by_day([charge_rec], timezone.utc, "session_start_date"), window=dict(window, is_current=False), sort="start", desc=True, sort_base="?", flt={}
    )
    assert (
        "month=2026-10" in html and html.count('class="row-b') == 1 and "30.0 kWh" in html and "+120 mi rated" not in html and "<table" not in html
    ), "the detail lives on the charge's page; a list row is two lines"
    live = dict(charge_rec, id=11, session_end_date=None, kwh=None, cost=None, duration=None, open=True, live=True)
    html = env.get_template("charges.html").render(
        **base, charges=[live], days=daypage.by_day([live], timezone.utc, "session_start_date"), window=window, sort="start", desc=True, sort_base="?", flt={}
    )
    assert html.count("Charging now") == 2 and "0.0 kWh" not in html, "the day and the row say it is still going, not that it added nothing"
    html = env.get_template("charges.html").render(**base, charges=[], days=[], window=dict(window, is_current=False), sort="kwh", desc=True, sort_base="?", flt={"kind": "fast"})
    assert "No charges in September 2026" in html and "Filter · 1 on" in html
    odd = dict(charge_rec, id=10, session_start_date=None)
    html = env.get_template("charges.html").render(
        **base, charges=[odd], days=daypage.by_day([odd], timezone.utc, "session_start_date"), window=window, sort="start", desc=True, sort_base="?", flt={}, hidden=True
    )
    assert html.count('class="row-b') == 1 and "marked “by hand”" in html, "a hidden charge that never started still lists"
    from datetime import date

    weeks = [
        [{"date": date(2026, 8, 31), "other": True, "drives": 0, "miles": 0.0, "charges": 0, "kwh": 0.0, "home_kwh": 0.0}]
        + [{"date": date(2026, 9, i), "other": False, "drives": i % 2, "miles": 12.6 * (i % 2), "charges": 1 if i == 4 else 0, "kwh": 30.0 if i == 4 else 0.0, "home_kwh": 0.0} for i in range(1, 7)]
    ]
    import calview
    from units import Units

    ms = calview.month_strips(2026, 9, calview.day_values(weeks[0], {}, Units()), date(2026, 9, 6), Units(), "$", lambda d: f"/cc/day?date={d}")
    bat = calview.month_chart([(t0, 80.0), (t0 + timedelta(days=2), 60.0)], [], [], t0, t0 + timedelta(days=30), t0 + timedelta(days=4), timezone.utc, Units())
    html = env.get_template("calendar.html").render(
        **base, weeks=weeks, window=window, totals={"drives": 3, "miles": 37.8, "charges": 1, "kwh": 30.0}, max_miles=12.6, center=[40.71, -74.0], strips=ms, battery=[{"vin": "V1", "chart": bat}]
    )
    assert 'href="/cc/calendar?year=2026&vin=V1">Year</a>' in html and html.index('class="calsum"') < html.index('class="cal"') < html.index(
        'id="calbat-V1"'
    ), "Month and Year in one control; totals, grid, battery"
    assert html.count('class="calsum-tab"') == 3 and ">38 mi<" in html and "/cc/day?date=2026-09-03" in html and html.count("calsum-s") >= 3
    html = env.get_template("calendar.html").render(
        **base,
        weeks=weeks,
        window=window,
        totals={"drives": 3, "miles": 37.8, "charges": 1, "kwh": 30.0},
        max_miles=12.6,
        center=[40.71, -74.0],
        charge_pins=[{"lat": 39.8, "lon": -89.65, "n": 2, "kwh": 41.2, "where": "Home", "fast": False}],
    )
    assert '"where": "Home"' in html and "CCMap.charge" in html
    from stats import year_grids

    from datetime import datetime as _d, timezone as _tz

    tz = _tz.utc
    ds = [{"start_date": _d(2026, 9, d, 16, tzinfo=tz), "miles": 12.6, "vin": "V1"} for d in (1, 3, 5)]
    cs = [{"session_start_date": _d(2026, 9, 4, 20, tzinfo=tz), "kwh": 30.0, "vin": "V1", "at_home": True}]
    months = year_grids(ds, cs, 2026, tz, 0)
    assert len(months) == 12 and months[8]["label"] == "September" and months[8]["totals"]["drives"] == 3 and months[0]["totals"]["drives"] == 0
    yhtml = env.get_template("calendar_year.html").render(
        **base,
        year=2026,
        months=months,
        totals={"drives": 3, "miles": 37.8, "charges": 1, "kwh": 30.0, "days_driven": 2},
        best=None,
        max_miles=12.6,
        this_year=2026,
        strips=calview.year_strips(2026, calview.day_values([c for m in months for w in m["weeks"] for c in w], {}, Units()), date(2026, 10, 1), Units(), "$"),
    )
    assert (
        yhtml.count('class="calsum-tab"') == 3
        and yhtml.count('class="strip wk"') == 3
        and '<span class="m-dist">38 mi</span><span class="m-kwh">30 kWh</span><span class="m-cost">$0.00</span>' in yhtml
        and "lg-dot" in yhtml
    )
    assert 'href="/cc/calendar?month=2026-09&vin=V1">September' in yhtml and ">January<" not in yhtml and ">October<" not in yhtml, "the months from the first logged one up to this one"
    assert 'href="/cc/calendar?year=2025&vin=V1" aria-label="2025">‹' in yhtml and 'aria-label="2027"' not in yhtml
    assert yhtml.count("ym-day busy") == 3 and yhtml.count("charged") >= 1, "three days drove, one charged"
    assert 'class="pbar"' in yhtml and 'class="calsum"' in yhtml and "seg-links" not in yhtml and ">Month</a>" in yhtml
    assert "⚡30<span class=\"xu\"> kWh" in html and "13 mi" in html and html.count('class="cal-day') == 7 and "date=2026-09-04" in html
    month = {"drives": 3, "days_driven": 3, "miles": 37.8, "drive_h": 1.0, "used_kwh": 10.0, "wh_per_mi": 265.0, "drive_cost": 3.0, "charges": 1, "added_kwh": 30.0, "cost": 4.0, "fsd_miles": 0}
    rows = {date(2026, 9, 1): {"wh_per_mi": 330.0, "drive_cost": 1.2}, date(2026, 9, 3): {"wh_per_mi": 270.0, "drive_cost": 1.2}}
    petrol = {"ok": True, "saved": 12.5, "mpg": 30, "l_per_100km": 7.8, "petrol": 16.5, "own_price": None, "areas": ["US"]}
    html = env.get_template("calendar.html").render(
        **base,
        weeks=weeks,
        window=window,
        totals={"drives": 3, "miles": 37.8, "charges": 1, "kwh": 30.0},
        max_miles=12.6,
        center=[40.71, -74.0],
        month=month,
        day_rows=rows,
        day_temps={date(2026, 9, 1): 61.0},
        split=[{"key": "home", "label": "Home", "n": 1, "kwh": 30.0, "cost": 4.0}],
        petrol=petrol,
    )
    assert '<span class="xw odd"> · 330</span>' in html and '<span class="xw"> · 270</span>' in html, "Wh/mi dark only when 10% off the month's"
    assert '<span class="xt">61°</span>' in html and '<td class="sub-r">Home</td>' in html and "1 · 30.0 kWh · $4.00" in html and "≈$12.50 saved" in html
    assert ">Distance<" not in html and ">Longest day<" not in html, "the line under the strip says them once"
    assert 'class="pbar"' in html and 'class="calsum"' in html and "seg-links" not in html
    mwin = {"start": t0, "end": t0, "label": "September 2026", "ym": "2026-09", "prev": "2026-08", "prev_label": "Aug 2026", "next": "2026-10", "next_label": "Oct 2026", "is_current": True}
    html = env.get_template("map.html").render(**base, positions=336598, since=t0, center=[40.71, -74.0], window=mwin, pins=[])
    chg = html[html.index('data-layer="charges"') :].split("</button>")[0]
    assert '<svg class="ic k-charge"' in chg and "<i " not in chg, "the Charges chip shows the charge bolt, not a dot of two car colours"
    assert "336,598" in html and "/cc/api/driveprint?vin=V1" in html and "September 2026" in html and 'class="pbar"' in html
    assert 'data-car=' not in html and 'data-layer="charges"' in html and 'data-layer="cells"' in html, "one car: no car chips, the Charges and all-time chips still there"
    garage = dict(base, car={"vin": "all", "name": "All cars", "garage": True, "live": False, "updated_age": "", "model": ""}, q="?vin=all")
    days = [
        {
            "date": t0.date(),
            "miles": 26.3,
            "kwh": 30.0,
            "events": [
                {
                    "at": t0,
                    "kind": "drive",
                    "vin": "V2",
                    "car": "Bravo",
                    "id": 5,
                    "title": "Home → the office",
                    "miles": 26.3,
                    "duration": timedelta(minutes=42),
                    "level_start": 74.0,
                    "level_end": 65.0,
                },
                {
                    "at": t0,
                    "kind": "charge",
                    "vin": "V1",
                    "car": "Alpha",
                    "id": 9,
                    "title": "Home",
                    "kwh": 30.0,
                    "cost": 12.0,
                    "duration": timedelta(hours=2),
                    "level_start": 40.0,
                    "level_end": 80.0,
                    "charging_type": "AC",
                },
                {"at": t0, "kind": "software", "vin": "V1", "car": "Alpha", "id": None, "title": "Software 2026.26.6"},
                {"at": t0, "kind": "parked", "vin": "V1", "car": "Alpha", "id": None, "title": "Home", "duration": timedelta(hours=1, minutes=21), "lost": 15.0},
            ],
        }
    ]
    html = env.get_template("timeline.html").render(
        **garage,
        days=days,
        window=window,
        kind="all",
        kinds=("all", "drives", "charges", "software"),
        counts={"drives": 1, "charges": 1, "software": 1},
        thumbs={5: '<svg class="route" viewBox="0 0 96 48"></svg>'},
    )
    assert "Home</span> → the office" in html and "pl-home" in html and "Software 2026.26.6" in html and "$12.00" in html and 'href="/cc/drives/5?vin=all"' in html
    assert html.count('class="row-b') == 3, "the drive, the charge and the update are rows"
    assert 'class="row-b k-drive"' in html and 'class="row-b k-charge"' in html, "a drive and a charge tell apart by kind"
    assert '<span class="row-thumb"><svg class="route"' in html and 'class="ic"' in html, "the drive's chip holds its route; the charge's holds the bolt"
    assert 'row-thumb fast' not in html, "an ordinary charge is not marked fast"
    assert 'class="row-x"' not in html and "avg 38 mph" not in html and 'aria-label="Show"' not in html, "two lines a row, one filter row (the sub-navigation); the detail is the drive's page"
    assert (
        "Parked 1 h 21 min at <span class=\"pl\">" in html and "Home</span> · lost 15.0%" in html and 'class="row-gap go"><span class="dot c' in html and "/cc/parked/" in html
    ), "parked time is the thin line, with the car's dot in the garage, and opens its stretch"
    assert (
        'href="/cc/software?vin=all"' in html and 'class="route"' in html and 'class="dot c1"' in html
    ), "the update links to Software; the route is the drive's thumbnail; the car's dot leads in the garage"
    assert "All cars</a>" not in html.split('class="stage"')[1], "the shell switches car, not the page"
    assert 'class="day-h"' in html and 'class="segs2"' not in html and 'aria-label="Aug 2026"' in html and 'class="tday"' in html
    html = env.get_template("timeline.html").render(
        **garage, days=[dict(days[0], miles=0, kwh=0.1)], window=window, kind="all", kinds=("all", "drives", "charges", "software"), counts={"drives": 1, "charges": 1, "software": 1}
    )
    assert "0.1 kWh</span></h2>" in html, "a small charge is not 0 kWh in the day header"
    with_door = [dict(e, garage=["Garage closed", "Garage opened"]) if e["kind"] == "drive" else e for e in days[0]["events"]]
    html = env.get_template("timeline.html").render(
        **garage, days=[dict(days[0], events=with_door)], window=window, kind="all", kinds=("all", "drives", "charges", "software"), counts={"drives": 1, "charges": 1, "software": 1}
    )
    assert (
        html.count('class="row-g"') == 2 and "</svg>Garage closed</span>" in html and "</svg>Garage opened</span>" in html
    ), "a drive the door acted for carries its lines, each with the garage glyph"
    html = env.get_template("drives.html").render(
        **garage, drives=[dict(trip, vin="V2"), dict(trip, id=6, vin="V3", by_hand=True)], days=None, window=window, sort="miles", desc=True, sort_base="?vin=all&", flt={"raw": {"q": "Commute"}}
    )
    assert (
        'class="dot c1"' in html and 'class="day-h"' not in html and "by hand" in html and "Filter · 1 on" in html
    ), "sorted by distance: no day headers, a date on each row; the car's dot in the garage"
    cal = [
        [
            {
                "date": date(2026, 9, 4),
                "other": False,
                "drives": 2,
                "miles": 30.0,
                "charges": 1,
                "kwh": 30.0,
                "home_kwh": 0.0,
                "cars": {"V1": {"drives": 1, "miles": 12.0, "charges": 1, "kwh": 30.0}, "V2": {"drives": 1, "miles": 18.0, "charges": 0, "kwh": 0.0}},
            }
        ]
        * 7
    ]
    html = env.get_template("calendar.html").render(**garage, weeks=cal, window=window, totals={"drives": 2, "miles": 30.0, "charges": 1, "kwh": 30.0}, max_miles=30.0, center=[40.71, -74.0])
    assert "Alpha 12 mi ⚡30" in html and "Bravo 18 mi" in html
    rows = [
        {
            "kind": "parked",
            "sub": "sleep",
            "start": t0 - timedelta(hours=8),
            "end": t0,
            "id": None,
            "title": "Asleep",
            "kwh_loss": None,
            "sentry_h": 0.0,
            "duration": timedelta(hours=8),
            "asleep_share": 0.95,
            "level_start": 81.0,
            "level_end": 80.0,
            "drop": 1.0,
        },
        {
            "kind": "drive",
            "start": t0,
            "end": t0 + timedelta(minutes=42),
            "id": 5,
            "title": "Home → the office",
            "miles": 26.3,
            "duration": timedelta(minutes=42),
            "level_start": 80.0,
            "level_end": 71.0,
            "temp_f": 68.0,
            "wh_per_mi": 257.0,
        },
        {
            "kind": "charge",
            "start": t0 + timedelta(hours=10),
            "end": None,
            "id": 9,
            "title": "Home",
            "kwh": 20.0,
            "cost": 8.0,
            "duration": None,
            "level_start": 60.0,
            "level_end": 82.0,
            "charging_type": "AC",
        },
    ]
    blocks = [
        {
            "vin": "V2",
            "name": "Bravo",
            "live": False,
            "ribbon": [{"label": "sleeping", "width": 0.5, "start": t0, "end": t0}, {"label": "driving", "width": 0.5, "start": t0, "end": t0}],
            "rows": rows,
            "totals": {
                "drives": 1,
                "miles": 26.3,
                "used_kwh": 6.75,
                "wh_per_mi": 257.0,
                "rated_used": 27.4,
                "drive_h": 0.7,
                "charges": 1,
                "kwh": 20.0,
                "cost": 8.0,
                "parked_h": 8.0,
                "asleep_h": 7.6,
                "idle_h": 0.4,
                "parked_drop": 1.0,
                "parked_range_loss": 2.4,
                "parked_kwh_loss": 0.6,
                "phantom_mi_per_h": 0.3,
                "sentry_h": 0.0,
                "climate_h": 0.1,
                "battery_used": 10.0,
            },
            "routes": [[[40.71, -74.00], [40.76, -73.99]]],
            "chart": {
                "t0": 0,
                "t1": 86400,
                "primary": {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": [[0, 81.0], [86400, 82.0]]},
                "extras": [],
                "lane": [],
                "events": [],
            },
            "since_charge": {"ago": timedelta(days=7, hours=8), "battery_used": 8.26, "miles": 6.5, "rated_used": 22.3, "efficiency": 0.29, "kwh": 5.5},
        }
    ]
    html = env.get_template("day.html").render(
        **garage,
        day=t0.date(),
        blocks=blocks,
        prev=date(2026, 9, 3),
        next=date(2026, 9, 5),
        month="2026-09",
        show="all",
        today=date(2026, 9, 5),
    )
    assert 'class="chartbox"' in html and 'class="chart-data"' in html, "the Day battery chart is the shared chart"
    assert html.count('class="row-b') == 2 and 'class="row-gap go"' in html and '/parked/' in html, "drives and charges are rows; parked time a thin line that opens its stretch"
    assert 'type="date"' in html and "data-autosubmit" in html, "a date picker in the bar"
    assert 'class="week"' not in html and 'class="tiles pair"' in html and "Since the last charge" in html
    assert "Phantom drain" in html and "Battery used" in html and "show=parked" in html and "29% of rated" in html
    assert "Home</span> → the office" in html and "Asleep 8 h · lost 1.0%" in html and "257 Wh/mi" in html and "60→82%" in html and "$8.00" in html
    assert "68°F out" in html and "until 9:42 AM" in html and "avg 38 mph" in html, "the Day row carries the second tier too"
    assert 'id="map"' in html and "date=2026-09-03" in html and "Thu Sep 3" in html and 'href="/cc/drives/5?vin=all"' in html
    gap_day = [dict(blocks[0], totals=dict(blocks[0]["totals"], asleep_h=None, idle_h=None), since_charge=dict(blocks[0]["since_charge"], battery_used=None))]
    html = env.get_template("day.html").render(**garage, day=t0.date(), blocks=gap_day, prev=date(2026, 9, 3), next=None, month="2026-09", show="all", today=date(2026, 9, 5))
    assert "asleep" not in html.split('<span class="l">Parked</span>')[1].split("</div>")[0], "unknown sleep is not 0.0 h asleep"
    sc_row = html.split("Since the last charge")[1].split("</div></div></div>")[0]
    assert "6.5 mi" in sc_row and "None" not in sc_row and "% ·" not in sc_row, "the line stays; the unknown battery figure goes"
    tile_day = [dict(blocks[0], totals=dict(blocks[0]["totals"], parked_kwh_loss=0, parked_drop=3.0, sentry_h=2.0, phantom_mi_per_h=None, parked_range_loss=0))]
    vin = tile_day[0]["vin"]
    html = env.get_template("day.html").render(**garage, day=t0.date(), blocks=tile_day, prev=date(2026, 9, 3), next=None, month="2026-09", show="all", today=date(2026, 9, 5), guard={vin: "Sentry"})
    parked_tile = html.split('<span class="l">Parked</span>')[1].split("</div>")[0]
    lucid = env.get_template("day.html").render(
        **garage, day=t0.date(), blocks=tile_day, prev=date(2026, 9, 3), next=None, month="2026-09", show="all", today=date(2026, 9, 5), guard={vin: "Halo Secure"}
    )
    assert "Halo Secure 2.0 h" in lucid.split('<span class="l">Parked</span>')[1].split("</div>")[0] and "Sentry" not in lucid, "a Lucid's tile says Halo Secure"
    assert "lost 3%" in parked_tile and "Sentry 2.0 h" in parked_tile, "the Parked tile says the loss without a kWh figure, and carries Sentry when there is no drain tile"
    cars = [
        {
            "vin": "V1",
            "name": "Alpha",
            "status": "Sleeping",
            "status_class": "sleeping",
            "soc": 72.0,
            "range_mi": 280.0,
            "place": "Home",
            "address": None,
            "live": True,
            "updated_age": "just now",
            "ribbon": [{"label": "sleeping", "width": 1.0}],
            "last_drive": trip,
        },
        {
            "vin": "V2",
            "name": "Bravo",
            "status": "Idle",
            "status_class": "idle",
            "soc": 71.0,
            "range_mi": 177.0,
            "place": None,
            "address": "Elm St, Springfield",
            "live": False,
            "updated_age": "2 h ago",
            "ribbon": [],
            "last_drive": None,
        },
    ]
    html = env.get_template("garage.html").render(**garage, cars=cars)
    assert html.count('class="cc"') == 2 and "Elm St" in html and "Elm Street, Springfield → Main Street" in html, "two cards, the address when there is no place, the last drive as a row"
    summary = {
        "places": [
            {
                "where": "Home",
                "charges": 3,
                "kwh": 90.0,
                "cost": 27.0,
                "rate": 0.3,
                "dc": 0,
                "hours": 9.0,
                "avg_time": timedelta(hours=3),
                "saving": {"saved": 31.5, "compared": 3, "of": 3, "skipped": {}, "note": None},
            },
            {
                "where": "Shelbyville Supercharger",
                "charges": 2,
                "kwh": 60.0,
                "cost": 24.0,
                "dc": 2,
                "hours": 0.0,
                "avg_time": None,
                "saving": {"saved": None, "compared": 0, "of": 2, "note": "Nothing to compare: 2 with no petrol price for where and when"},
                "estimate": True,
            },
        ],
        "saved_ready": True,
        "saving_tail": None,
        "saving_all": {"saved": 31.5, "compared": 3, "of": 5, "note": "3 of 5 charges compared: 2 with no petrol price for where and when"},
        "ends": [{"level": 80, "charges": 3, "share": 1.0, "bar": 1.0}],
        "superchargers": [{"where": "Shelbyville Supercharger", "charges": 2, "kwh": 60.0, "cost": 24.0, "minutes": 50.0}],
        "total_kwh": 150.0,
        "total_charges": 5,
        "total_cost": 51.0,
        "tou": {"periods": []},
    }
    html = env.get_template("charging.html").render(**garage, s=summary, days=90)
    assert 'class="pbar"' in html and 'aria-label="Period"' in html and 'class="tiles pair"' in html and "150<small>kWh</small>" in html and "Home" in html and "seg-links" not in html
    assert 'href="/cc/charging?days=90&vin=all" aria-current="page"' in html and "Shelbyville Supercharger" in html
    assert html.count('<td class="st-p">') == 3 and "3 charges · 30.0 kWh each · 3 h a session" in html, "a row a place and the All row; on a phone the count and the session sit under the name"
    assert '<td class="r st-o">—</td>' in html and "0 min" not in html, "a place whose charges carry no length shows a dash, not 0 min"
    assert '<td class="r">≈$24</td>' in html and '<td class="r st-v">$32</td>' in html, "the estimate marked; a whole saving plain"
    assert 'title="Nothing to compare: 2 with no petrol price for where and when"><span class="st-nil">—</span>' in html, "a blank saving says why"
    assert '<td class="r st-v part" title="3 of 5 charges compared: 2 with no petrol price for where and when">$32</td>' in html, "a partial All is marked and says which"
    waiting = env.get_template("charging.html").render(**garage, s=dict(summary, saved_ready=False), days=90)
    assert waiting.count('title="Worked out in the background; here in a few minutes"') == 3 and "$32" not in waiting.split("<tfoot>")[0].split('class="sites"')[1]
    assert "Most energy" in html and "Most used" not in html, "the tile ranks places by kWh"
    home = dict(summary["places"][0], charges=2, topups=1, kwh=90.4, topup_kwh=0.4)
    html = env.get_template("charging.html").render(**garage, s=dict(summary, places=[home], total_charges=2, total_topups=1, topup_kwh=0.4, total_kwh=90.4, charging_now=1), days=90)
    assert "2 charges · 45.0 each · 1 top-up · charging now" in html, "the average over the charges; the top-up and the live one apart"
    assert "2 charges · 45.0 kWh each · 1 top-up · 3 h a session" in html
    unpriced = dict(summary, priced=0, total_cost=0.0)
    assert "Set a plan in Settings › Money" in env.get_template("charging.html").render(**garage, s=unpriced, days=90)
    html = env.get_template("charging.html").render(**garage, s=unpriced, days=90, rate_plan="PG&E EV2A")
    assert "Not priced" in html and "Away charges · price a place" in html and "Set a plan" not in html, "a plan is set: the charges with no price are away"
    sblock = {
        "vin": "V1",
        "name": "Alpha",
        "days": [],
        "summary": {"asleep_pct": 82.0, "wakeups_per_day": 3.1, "driving_h": 5.0, "charging_h": 9.0, "days": 14},
        "known": True,
        "streak": ("sleeping", timedelta(hours=7)),
        "idles": [],
    }
    html = env.get_template("sleep.html").render(**base, blocks=[sblock], window=None, parked_rates=[], search={}, searching=False)
    assert 'class="pbar"' in html and 'class="tiles"' in html and "82<small>%</small>" in html and "sleeping for 7 h" in html and "seg-links" not in html and ">Month</a>" in html
    html = env.get_template("sleep.html").render(**garage, blocks=[sblock], window=dict(window, is_current=False), parked_rates=[], search={}, searching=False)
    assert 'class="daycar-h"' in html and "month=2026-10" in html and ">14 days</a>" in html
    # a stop that ends a week later, on the same weekday, says which day it ended
    stop = {"start": t0, "end": t0 + timedelta(days=7), "open": False, "place": "Home", "hours": 168.0, "asleep_share": 0.9, "soc_start": 80.0, "soc_end": 74.0, "drop_per_day": 0.9, "sentry_share": 0}
    day = {
        "date": t0.date(),
        "segments": [],
        "span_h": 24.0,
        "known_h": 24.0,
        "hours": {"idle": 1.0, "driving": 0.0, "charging": 0.0, "sleeping": 23.0, "unknown": 0.0},
        "asleep_pct": 96.0,
        "wakeups": 1,
        "idles": [stop],
    }
    html = env.get_template("sleep.html").render(**base, blocks=[dict(sblock, days=[day], idles=[stop])], window=None, parked_rates=[], search={}, searching=False)
    assert (t0 + timedelta(days=7)).strftime("%a") in html and "→ Fri 9:00 AM" in html, "the end's weekday, not the time alone"
    charged = dict(stop, end=t0 + timedelta(hours=2), hours=2.1, soc_start=47.0, soc_end=57.0, drop_per_day=None, charged=True)
    html = env.get_template("sleep.html").render(**base, blocks=[dict(sblock, days=[dict(day, idles=[charged])], idles=[charged])], window=None, parked_rates=[], search={}, searching=False)
    assert "47→57% · charged" in html and "/day" not in html, "a stop with a charge in it says so and gives no drain rate"
    lv = {
        "t0": 0,
        "t1": 86400 * 30,
        "primary": {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": [[0, 80.0], [86400 * 30, 72.0]]},
        "extras": [],
        "lane": [],
        "events": [],
        "ticks": [],
    }
    bblock = {"car": dict(car, vin="V1", soc=72.0, range_mi=280.0), "chart": lv, "full": None, "tires": {}, "firsts": {}, "points": 2}
    html = env.get_template("battery.html").render(**garage, blocks=[bblock])
    assert 'class="chartbox"' in html and "Alpha" in html and 'class="tiles pair"' in html and '<span class="chart-big">72%</span>' in html and "280 mi" in html and 'class="chart-key"' not in html

    import calview
    from units import Units

    tdays = [{"day": date(2026, 1, 1) + timedelta(days=i), "fl": 40.0, "fr": 41.0, "rl": 39.0 - i, "rr": 41.0} for i in range(3)]
    th = calview.tire_days(tdays, [{"day": date(2026, 1, 1), "f": 40.0}], Units(), timezone.utc)
    tblock = {"vin": "V1", "name": "Alpha", "tires": {}, "tire_history": th, "tire_now": None}
    svc = lambda **tb: env.get_template("service.html").render(**garage, items=[], odos={}, today=_date(2026, 9, 5), tire_sets=[], tread=[], tread_today="2026-09-05", tire_blocks=[dict(tblock, **tb)])
    html = svc()
    assert 'id="tires-V1"' in html and "Every day, 3 days" in html and 'data-extra="rl"' in html and "tires30-V1" not in html
    assert html.index("Tire pressure") < html.index("Tire sets"), "the pressure sits above the tire sets"
    assert 'id="tires-V1"' not in env.get_template("battery.html").render(**garage, blocks=[dict(bblock, tire_history=th)]) and "Tire pressure" not in env.get_template("battery.html").render(
        **garage, blocks=[dict(bblock, tire_history=th)]
    ), "not on Battery"
    now = calview.tire_now({"fl": [[0, 41.0]], "fr": [[0, 41.0]], "rl": [[0, 38.0]], "rr": [[0, 41.0]]})
    html = svc(tire_now=now)
    assert html.count('class="tc tc-') == 4 and 'class="tc tc-rl low"' in html and "<b>38.0</b>" in html
    assert "Tire pressure" not in svc(tire_history=None, tires=None)
    html = env.get_template("battery.html").render(
        **base,
        blocks=[
            dict(
                bblock,
                car=dict(bblock["car"], pack_capacity_kwh=112.0, pack_energy_kwh=80.5, battery_warning=True, range_full_mi=390.0, battery_f=77.0),
                chart=dict(lv, primary=dict(lv["primary"], points=[])),
            )
        ],
        packs={"V1": __import__("carspage").pack(112.0, 112.0, "Tesla", 75.0)},
    )
    assert (
        "112.0<small>kWh</small>" in html
        and "usable, reported by Tesla · 80.5 kWh in it now" in html
        and "390<small>mi</small>" in html
        and "77<small>°F</small>" in html
        and ">Health</span><span class=\"v warn\">Warning<" in html
        and "No battery readings" in html
    )
    srows = [{"period": t0.date(), "drives": 3, "miles": 61.2, "drive_h": 1.9, "used_kwh": 21.0, "wh_per_mi": 343.0, "charges": 1, "added_kwh": 30.0, "cost": 12.0, "cost_per_mi": 0.196}]
    html = env.get_template("stats.html").render(
        **garage,
        rows=srows,
        totals=totals,
        by="month",
        rate_plan="Springfield EV",
        bins={"temp": [], "speed": []},
        odometer=None,
        per_car=[{"vin": "V1", "name": "Alpha", "drives": 3, "miles": 61.2, "drive_h": 1.9, "used_kwh": 21.0, "wh_per_mi": 343.0, "charges": 1, "added_kwh": 30.0, "cost": 12.0}],
        range_from=None,
        range_to=None,
        month_window=dict(stats_ctx["month_window"], is_current=False),
        fraction=1.0,
        cmp=stats_ctx["cmp"],
        mbars=stats_ctx["mbars"],
        mbars_phone=stats_ctx["mbars_phone"],
    )
    assert "By car" in html and "vin=V1" in html and "so far" not in html and '<table class="cmp pair bycar">' in html, "the cars side by side, metrics as rows"
    named = [
        {
            "id": 3,
            "name": "Home",
            "latitude": 40.71,
            "longitude": -74.0,
            "radius_m": 150,
            "auto": False,
            "visits": 120,
            "address": None,
            "icon": "home",
            "home": True,
            "city": "Springfield",
            "price_text": "Home plan",
        }
    ]
    found = [
        {"id": 7, "name": "Elm Street, Springfield", "latitude": 40.7, "longitude": -74.0, "radius_m": 150, "auto": True, "visits": 14, "address": "Elm Street, Springfield", "city": "Springfield"},
        {"id": 8, "name": "Oak Lane", "latitude": 40.8, "longitude": -74.1, "radius_m": 150, "auto": True, "visits": 3, "address": None, "city": ""},
    ]
    pstays = {7: {"points": [[40.7, -74.0]], "by_car": {"V1": 11, "V2": 3}, "n": 14, "median": timedelta(hours=2), "usual": "weekday mornings"}}
    html = env.get_template("places.html").render(**dict(garage, car=dict(car, lat=40.7, lon=-74.0)), named=len(named), found=len(found), merge=None)
    assert 'class="pbar"' in html and ">1 place · 2 to name<" in html and 'href="/cc/places/new"' in html and "Find a place" in html
    assert html.index('id="pl-named"') < html.index('id="pl-need"'), "the named places first, then the found ones to name"
    assert 'id="maps-offer"' not in html, "no offer where the settings are not known"
    st_off = {"maps_offer": None, "public_tiles": False, "public_geocoder": False}
    offered = env.get_template("places.html").render(**dict(garage, car=dict(car, lat=40.7, lon=-74.0), settings=st_off, here="/places"), named=1, found=2, merge=None)
    assert (
        'id="maps-offer"' in offered
        and 'name="answer" value="on"' in offered
        and 'name="answer" value="no"' in offered
        and 'action="/cc/settings/maps/offer"' in offered
        and 'name="back" value="/places"' in offered
    )
    assert "OpenFreeMap sees the area you view" in offered and "OpenStreetMap sees each stop" in offered, "what each service sees, up front"
    for gone in ({"maps_offer": "no"}, {"public_tiles": True}, {"public_geocoder": True}):
        assert 'id="maps-offer"' not in env.get_template("places.html").render(
            **dict(garage, car=dict(car, lat=40.7, lon=-74.0), settings={**st_off, **gone}, here="/places"), named=1, found=2, merge=None
        ), gone
    assert 'data-index="/cc/places/index.json"' in html and 'id="pl-map"' in html and "data-merge" not in html
    assert html.count('class="plr skel"') == 3, "the rows' room is held until the places come (the named, and up to ten to name); no row is drawn into the page"
    assert '<h2 id="pl-need-h">To name <span class="n">2</span></h2>' in html
    assert '<template id="pl-maphead">' in html and "static/placesmap.js" in html and "static/placecard.js" not in html, "the card's script comes when it is used"
    assert "/rename" not in html and "/delete" not in html and ">Save<" not in html, "no forms: one list and one map"
    p = dict(named[0], user_address=None, street="Elm Street, Springfield", price=None, parts=[{"id": 9, "latitude": 40.72, "longitude": -74.0, "radius_m": 100}])
    choices = [{"id": 7, "name": "Elm Street, Springfield", "auto": True, "m": 900.0, "far": "900 m"}]
    html = env.get_template("place.html").render(**garage, p=p, st=pstays[7], choices=choices, maps_on=True, new=False)
    assert (
        'action="/cc/places/3/edit"' in html
        and 'name="latitude" value="40.71"' in html
        and 'type="range" name="radius_m" min="25" max="1000"' in html
        and 'id="pd-r-n" type="number" step="1" inputmode="numeric" value="150"' in html
    )
    assert 'href="/cc/places?merge=3"><span class="set-l">Merge with…</span><span class="set-v">Elm Street, Springfield, 900 m</span>' in html, "Merge… picks on the Places map"
    assert 'action="/cc/places/3/delete"' in html and 'id="pd-confirm" class="pd-confirm" hidden' in html and "confirm(" not in html, "Remove asks on the page, not in a browser dialog"
    assert 'data-suggest role="combobox"' in html and "Alpha 11" in html and "usually 2 h" in html and '"radius_m": 100' in html and "CCMap.base(" in html
    html = env.get_template("place.html").render(**garage, p=dict(p, id=None, name="", parts=[], latitude=None, longitude=None), st=None, choices=[], maps_on=False, new=True)
    assert 'action="/cc/places/new"' in html and 'name="lat" value=""' in html and "data-suggest " not in html and "data-suggest>" not in html and "Merge with" not in html
    mwin = {"start": t0, "end": t0, "label": "September 2026", "ym": "2026-09", "prev": "2026-08", "prev_label": "Aug 2026", "next": "2026-10", "next_label": "Oct 2026", "is_current": True}
    pins = [{"lat": 40.7, "lon": -74.0, "n": 2, "kwh": 30.0, "where": "Home", "fast": False, "v": "V1"}]
    html = env.get_template("map.html").render(
        **dict(garage, vehicles=[{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}]), vins=["V1", "V2"], positions=500000, since=t0, center=[40.71, -74.0], window=mwin, pins=pins
    )
    assert 'class="chips"' in html and 'data-car="V1"' in html and 'data-car="V2"' in html and "palette[colours[v]" in html and "order[c[3]" in html
    assert "/cc/api/month-routes?month=2026-09" in html and "CCMap.route" in html and "CCMap.charge" in html and '"v": "V1"' in html
    html = env.get_template("tags.html").render(
        **garage,
        tags=["Work", "Airport"],
        tag="Work",
        drives=[dict(trip, vin="V2", note="client visit")],
        totals={"drives": 1, "miles": 26.3, "hours": 0.7, "kwh": 6.7, "cost": 1.6, "charge_kwh": 40.0, "charge_cost": 0},
        window=window,
        routes=[{"c": 1, "pts": [[40.7, -74.0], [40.8, -74.1]]}],
        away=[dict(session, kwh=47.0)],
    )
    assert "client visit" in html and "tagged Work" in html and "tag=Airport" in html and 'id="map"' in html and "Charges along the way" in html
    # the rules are one-line rows in the house groups and the tags are pills, as on Drives
    rules = [{"id": 3, "from_name": "Home", "to_name": "Work", "tag": "Work", "both_ways": True}, {"id": 4, "from_name": None, "to_name": "Springfield Airport", "tag": "Airport", "both_ways": False}]
    html = env.get_template("tags.html").render(**garage, tags=["Work", "Airport"], tag="Work", drives=[], totals={"drives": 0, "miles": 0, "hours": 0}, window=window, routes=[], away=[], rules=rules)
    assert "Home → Work<span class=\"set-s\">both ways</span>" in html and "anywhere → Springfield Airport</span>" in html and 'form="unrule-4"' in html
    assert '<nav class="segs2" aria-label="Tag">' in html and 'class="stepper"' in html and "seg-links" not in html and '<table class="cards"><thead><tr><th>From' not in html
    assert "Tag past drives" in html and 'role="switch" name="both_ways"' in html
    assert "leaflet.js" not in html, "no routes, no map files"
    from datetime import date as _date

    items = [
        {
            "id": 1,
            "vin": "V1",
            "what": "Tire rotation",
            "done": False,
            "due_date": _date(2026, 9, 20),
            "due_miles": 1500.0,
            "notes": "",
            "status": "soon",
            "done_on": None,
            "odometer": None,
            "cost": None,
        },
        {
            "id": 2,
            "vin": "V1",
            "what": "Cabin filter",
            "done": True,
            "done_on": _date(2026, 6, 1),
            "odometer": 800.0,
            "cost": 45.0,
            "notes": "OEM",
            "due_date": None,
            "due_miles": None,
            "status": None,
        },
    ]
    html = env.get_template("service.html").render(**garage, items=items, odos={"V1": 1015.0}, today=_date(2026, 9, 5))
    assert "Tire rotation" in html and "485 mi to go" in html and "$45.00" in html and ">Soon<" in html
    assert 'class="pbar"' in html and 'class="tiles t3"' in html and "<table" not in html.split("<main")[1] and "Spent" in html
    sw = [
        {"id": 1, "version": "2026.26.6", "seen_at": t0, "until": None, "days": 12, "secs": 12 * 86400.0, "brief": None, "odometer": 1234.0, "by_hand": False},
        {"id": 2, "version": "2026.20.1", "seen_at": t0 - timedelta(days=30), "until": t0, "days": 30, "secs": 30 * 86400.0, "brief": None, "odometer": 1000.0, "by_hand": True},
        {
            "id": 3,
            "version": "2026.20.0",
            "seen_at": t0 - timedelta(days=30, hours=5),
            "until": t0 - timedelta(days=30),
            "days": 0,
            "secs": 5 * 3600.0,
            "brief": (5, "h"),
            "odometer": None,
            "by_hand": False,
        },
    ]
    html = env.get_template("software.html").render(**base, blocks=[{"vin": "V1", "name": "Alpha", "rows": sw}], add_cars=[{"vin": "V1", "name": "Alpha"}], today=t0.date())
    assert 'class="pbar"' in html and 'class="tiles t3"' in html and "2026.26.6" in html and "/software/add" in html and "/software/2/delete" in html and "30<small>days</small>" in html
    assert "5<small>h</small>" in html and ">0<small>day" not in html, "a version that ran hours says hours, not 0 days"
    # Data checks in the three shapes the worker leaves: a feed car with a finding, a
    # history-only car whose facts have no odometer keys (this one answered 500 on the
    # Mini for a quarter of an hour), and a car not checked yet.
    checks = [
        {
            "vin": "V1",
            "name": "Alpha",
            "source": "TeslaFi (API)",
            "facts": {"raw_newest": datetime(2026, 9, 28, 18, tzinfo=timezone.utc), "derived_newest": datetime(2026, 9, 24, tzinfo=timezone.utc), "odo_7d": 114.6, "trips_7d": 0.0},
            "findings": [{"kind": "miles", "level": "warn", "text": "115 of the 115 mi Alpha drove this week are not in any drive."}],
        },
        {"vin": "V2", "name": "Bravo", "source": "imported history only", "facts": {"derived_newest": datetime(2026, 9, 4, tzinfo=timezone.utc), "trips_7d": 0.0}, "findings": []},
        {"vin": "V3", "name": "Charlie", "source": "Lucid (CarConnectivity)", "facts": {}, "findings": []},
    ]
    import cost
    from planview import money_context as money_ctx

    st = {"currency": "€"}
    sec = dict(base, st=st, conv={}, saved=True)
    # No plan: the rows editor, empty, and a line saying home charges have no cost yet.
    html = env.get_template("settings_money.html").render(**sec, section="money", label="Money", **money_ctx(None))
    assert "No rate plan yet" in html and 'name="from0"' in html and 'name="from1"' not in html and "<h1>Money</h1>" in html and "Saved." in html and "/settings/money" in html
    # A typed-in plan: its rows and schedule in the editor.
    manual = cost.compile_rows(
        "Springfield EV", [{"from": "2026-01-01", "off_peak": 0.1, "part_peak": 0.2, "peak": 0.3}], [{"period": "peak", "days": "weekdays", "start": 16, "end": 21, "months": None}]
    )
    html = env.get_template("settings_money.html").render(**sec, section="money", label="Money", **money_ctx(manual))
    assert "Springfield EV" in html and 'name="from1"' in html and "No rate plan yet" not in html
    ss0 = html[html.index('name="ss0"') : html.index("</select>", html.index('name="ss0"'))]
    assert '<option value="16" selected>4 pm</option>' in ss0 and "Peak · 4 pm–9 pm" in html and "Weekdays · all year" in html
    assert html.count('<table class="tou"') == 2 and 'aria-label="Weekends: period by month and hour"' in html, "a typed-in plan's hours read as the grid too"
    # A plan whose prices have always applied (its first row dated 2000-01-01) says so, never "Jan 1, 2000"
    always = cost.compile_rows("Springfield EV", [{"from": "2000-01-01", "off_peak": 0.1, "part_peak": 0.2, "peak": 0.3}], [])
    html = env.get_template("settings_money.html").render(**sec, section="money", label="Money", **money_ctx(always))
    assert "2000" not in html.replace('value="2000-01-01"', "") and "Since the start" in html and "since Since" not in html
    # Four periods: the grid, read-only, with a price field per version and period.
    four = dict(
        manual,
        name="Springfield Solar TOU",
        source="urdb:test",
        versions=[
            dict(manual["versions"][0], periods=[{"key": f"p{i}", "label": f"Band {i}", "price": 0.1 * (i + 1)} for i in range(4)], weekday=[[0] * 9 + [1] * 7 + [3] * 5 + [2] * 3 for _ in range(12)])
        ],
    )
    html = env.get_template("settings_money.html").render(**sec, section="money", label="Money", **money_ctx(four))
    assert html.count('<table class="tou"') == 2 and html.count("<td style=\"--heat:") == 2 * 12 * 24 and 'name="pv0_3"' in html and "Band 3" in html and 'name="from0"' not in html
    many = cost.compile_rows("Springfield EV", [{"from": f"{y}-01-01", "off_peak": 0.1, "part_peak": 0.2, "peak": 0.3} for y in (2021, 2022, 2023, 2024)], [])
    html = env.get_template("settings_money.html").render(**sec, section="money", label="Money", **money_ctx(many))
    head, _, rest = html.partition('class="set-x rate-more"')
    assert (
        "Jan 1, 2024" in head and "Jan 1, 2023" not in head and "Earlier prices" in rest and rest.index("Jan 1, 2023") < rest.index("Jan 1, 2021")
    ), "one set up front, the older ones newest first behind a disclosure"
    assert 'name="from3"' in head and 'name="from0"' in rest
    html = env.get_template("settings_look.html").render(**sec, section="look", label="Units and look", tz="Europe/London")
    assert "Europe/London" in html and 'name="units"' in html and 'name="currency" value="€"' in html
    html = env.get_template("settings_data.html").render(**sec, section="data", label="Data", data_checks=checks)
    assert "Fine<" not in html and "All good" in html, "a passing check says so, not just fine"
    assert "115 of the 115 mi" in html and "not checked yet" in html and "115 mi on the odometer, 0 in drives" in html
    assert 'name="weather_shown"' in html and 'name="min_drive_mi"' in html and "/drives.csv" in html
    assert "A day's readings" not in html, "no raw readings for this car: no link"
    html = env.get_template("settings_data.html").render(**sec, section="data", label="Data", data_checks=checks, raw_readings=True)
    assert "A day's readings" in html and 'href="/data' in html.replace("/cc/data", "/data")
    html = env.get_template("settings_alerts.html").render(
        **sec,
        section="alerts",
        label="Alerts",
        channels=["discord", "pushover"],
        routes={"parked": {"pushover"}, "login": set()},
        alert_kinds=__import__("notify").KINDS,
        alert_groups=__import__("notify").GROUPS,
    )
    assert 'name="route_parked_pushover" id="route_parked_pushover" aria-label="Parked on Pushover" checked data-autosave>' in html
    assert 'name="route_parked_discord" id="route_parked_discord" aria-label="Parked on Discord" data-autosave>' in html
    assert 'name="route_login_discord" id="route_login_discord" aria-label="Sign-in on Discord" data-autosave>' in html and 'aria-label="Set off on Discord" checked data-autosave>' in html
    assert 'name="routes_shown"' in html
    assert "ha/start" not in html and 'id="ha"' not in html and "Garage door" not in html, "no link yet: the Garage door section is not on the page at all"
    data_html = lambda ha: env.get_template("settings_data.html").render(**sec, section="data", label="Data", data_checks=checks, raw_readings=False, ha=ha)  # noqa: E731
    unl = data_html({"linked": False, "env": False})
    assert '/settings/alerts/ha/start"' in unl and ">Connect<" in unl and "Integrations" in unl, "linking lives under Settings › Data"
    typo = env.get_template("settings_data.html").render(
        **sec, section="data", label="Data", data_checks=checks, raw_readings=False, ha={"linked": False, "env": False, "problem": "x"}, ha_typed="http://homeasistant.local:8123"
    )
    assert 'value="http://homeasistant.local:8123"' in typo, "a refused address comes back to be fixed, not typed again"
    linked = {
        "linked": True,
        "url": "http://ha.springfield:8123",
        "entity": "cover.b",
        "door": {"state": "closed"},
        "doors": [{"id": "cover.a", "name": "Shed"}, {"id": "cover.b", "name": "Garage"}],
        "arrival": {"mode": "watch", "vin": "V2", "aim": 8},
        "aims": [5, 8, 13],
        "model": {"trips": 11, "directions": [{"bearing": 161, "trips": 6}, {"bearing": 7, "trips": 5}], "stops": [{"d": 220}]},
        "cars": [{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}],
        "homes": [{"id": 1, "name": "Home"}],
        "log": [
            {"when": "Oct 18 18:02:10", "kind": "schedule", "d": 340, "eta": 41},
            {"when": "Oct 18 18:02:25", "kind": "start", "did": "would start the door"},
            {"when": "Oct 18 18:02:31", "kind": "you_opened", "off_s": -6, "cam_s": 2},
            {"when": "Oct 18 18:03:00", "kind": "short", "why": "stopped short", "d": 220},
        ],
    }
    html = env.get_template("settings_alerts.html").render(**sec, section="alerts", label="Alerts", channels=[], routes={}, ha=linked, alert_kinds=__import__("notify").KINDS)
    assert '<option value="cover.b" selected>Garage</option>' in html and "http://ha.springfield:8123" in html and ">closed<" in html and "ha/forget" not in html
    assert '<option value="watch" selected>Watch only</option>' in html and '<option value="V2" selected>Bravo</option>' in html and 'name="place"' not in html
    assert "340 m out · driveway in 41 s" in html and "would start the door" in html and "you opened it · model −6 s · camera +2 s" in html and "stopped short 220 m out after the door started" in html
    assert '<option value="8" selected>open 8 s before</option>' in html and "from 161° · 6" in html and "1 stop near home" in html
    assert "Open on arrival" in html and "Close behind it" not in html, "no driveway service: the camera row waits for it"
    html = env.get_template("settings_alerts.html").render(**sec, section="alerts", label="Alerts", channels=[], routes={}, ha={**linked, "camera": True}, alert_kinds=__import__("notify").KINDS)
    assert "Close behind it" in html and "once it is out of the garage" in html and "Teslas" not in html
    unlinked = env.get_template("settings_alerts.html").render(
        **sec,
        section="alerts",
        label="Alerts",
        channels=[],
        routes={},
        ha={"linked": False, "env": False, "cars": [{"vin": "V1", "name": "Alpha"}], "aims": [5, 8]},
        alert_kinds=__import__("notify").KINDS,
    )
    assert 'id="ha"' not in unlinked and "ha/start" not in unlinked and "Open on arrival" not in unlinked and "Garage door" not in unlinked, "unlinked: the section is hidden entirely"
    assert (
        "ha/forget" in data_html(linked)
        and "ha/start" not in data_html(linked)
        and "linked in .env" in data_html({"linked": False, "env": True})
        and "ha/start" not in data_html({"linked": False, "env": True})
    )
    by_env = env.get_template("settings_alerts.html").render(
        **sec, section="alerts", label="Alerts", channels=[], routes={}, ha={"linked": False, "env": True, "cars": [], "aims": [8]}, alert_kinds=__import__("notify").KINDS
    )
    assert "linked in .env" in by_env and "ha/start" not in by_env and "Open on arrival" in by_env, "linked by .env: no Connect, the rest shows"
    notify = __import__("notify")
    html = env.get_template("settings_alerts.html").render(
        **sec, section="alerts", label="Alerts", channels=["discord"], routes={}, alert_kinds=notify.KINDS, alert_groups=notify.visible_groups({"keyfob", "garage_open"})
    )
    assert "Key fob battery low" not in html and "Garage opened or closed" not in html and "Parked" in html
    assert 'name="routes_kinds" value="charge_start' in html and "keyfob" not in html.split('name="routes_kinds"')[1].split(">")[0]
    assert f"{len(notify.KINDS) - 2} of {len(notify.KINDS) - 2} alerts" in html, "the count is of the alerts listed"

    import carspage

    rows = [{"id": 4, "kind": "teslafi", "vin": "V1", "label": "Alpha", "config": {}, "readable": True, "status": (t0, None), "last_ok_at": None, "last_error": None}]
    accts = carspage.accounts(rows, [{"kind": "teslamate", "vin": None, "car": "a car", "status": None, "used": True}], True, {"V1": "Alpha", "V3": "Charlie"}, ["V3"], [], (t0, None), ["V2"])
    packs = {"V1": carspage.pack(81.234, None, None, 75.0), "V3": carspage.pack(117.1, 117.1, "Lucid", None)}
    cars = carspage.cards(
        [{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}, {"vin": "V3", "name": "Charlie"}],
        {"V1": ("Tesla", "Model 3"), "V3": ("Lucid", "Air")},
        {"V1": "feed", "V2": "teslamate", "V3": "live"},
        {"V1": (t0, None), "V2": (None, "TeslaMate has nothing newer for Bravo."), "V3": (t0, None)},
        {"V1": t0},
        packs,
        accts,
    )
    html = env.get_template("settings_cars.html").render(
        **sec, section="cars", label="Cars and sources", cars=cars, accounts=accts, sources=rows, env_sources=[{"kind": "teslamate", "used": True}], vault_ready=True
    )
    for name in ("TeslaFi (API)", "TeslaMate", "Lucid", "Tesla Model 3"):
        assert f"<b>{name}</b>" in html or name in html, name
    assert "the connector" not in html and "the TeslaFi feed" not in html
    assert "81.2 kWh" in html and "set by you" in html and "s figure (75.0 kWh)" in html and "117.1 kWh" in html and "reported by Lucid" in html and ">unknown<" in html
    for vin, value in (("V1", "81.2"), ("V3", "117.1"), ("V2", "")):
        field = html.index(f'name="capacity_{vin}" id="cap-{vin}" value="{value}"')
        opened = html.rfind('<details class="set-x pack">', 0, field)
        assert opened > html.rfind('<details class="set-x">', 0, field) and "</form>" not in html[opened:field], "the box opens beneath its Pack row"
    assert html.count('name="capacity_') == 4 and html.count('<details class="set-x pack">') == 3, "no pack box outside its row"
    assert 'id="pack-reset-V1"' in html and 'id="pack-reset-V3"' not in html, "a reset only where you set it"
    assert html.count('class="set-row acct') == 3 and "TeslaFi token" in html and "Lucid account" in html and "TeslaMate database" in html
    assert "from a sign-in file" in html and "secrets/lucid-token.json" in html and "kept in the app" in html and "set in .env" in html and "older token setting" not in html
    for k in ("tesla", "teslafi", "teslamate", "lucid"):
        assert f'id="add-{k}"' in html and f'id="pane-{k}"' in html
    assert html.index('id="pane-teslafi"') < html.index('name="token"') < html.index('id="pane-teslamate"') < html.index('name="url"') < html.index('id="pane-lucid"') < html.index('name="password"')
    assert 'id="accts" data-waiting="0"' in html, "nothing under way: the rows do not poll"
    under_way = [
        {"where": "app", "id": 7, "kind": "lucid", "what": "Lucid account", "cars": [], "kept": "x", "retry": False, "config": {"pending": True}, "state": "wait", "text": "Signing in…", "at": None},
        {"where": "app", "id": 8, "kind": "carconnectivity", "what": "Cupra", "cars": [], "kept": "x", "retry": False, "config": {"state": "remove"}, "state": "wait", "text": "Removing", "at": None},
    ]
    html = env.get_template("settings_cars.html").render(**sec, section="cars", label="Cars and sources", cars=[], accounts=under_way, sources=[], env_sources=[], vault_ready=True)
    assert 'id="accts" data-waiting="1"' in html and "Signing in…" in html
    assert 'form="src-rm-7"' in html and 'form="src-rm-8"' not in html, "no Remove button on a row that is already being removed"
    html = env.get_template("settings_access.html").render(**dict(sec, saved=False), section="access", label="Sign-in and API", tokens=[], auth_enabled=True)
    assert "Mint a token" in html and "Sign-in is on" in html
    assert 'form="everywhere"' in html and '/settings/signin/everywhere"' in html and "Sign out everywhere else" in html
    assert 'name="kind"' in html and "Allow admin tokens from anywhere" in html and "No admin calls yet" in html
    made = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    html = env.get_template("settings_access.html").render(
        **dict(sec, saved=False),
        section="access",
        label="Sign-in and API",
        auth_enabled=True,
        tokens=[{"hash": "ab" * 32, "label": "assistant", "kind": "admin", "last4": "zz99", "car": "every car", "created_at": made, "last_used_at": None}],
        api_log=[{"at": made, "label": "assistant", "last4": "zz99", "method": "POST", "path": "/api/admin/tesla/register", "status": 200, "client_ip": "192.168.1.40"}],
        admin_anywhere=True,
        minted="raw-token-shown-once",
        minted_label="assistant",
        minted_kind="admin",
        request=SimpleNamespace(url=SimpleNamespace(scheme="https", netloc="example.test")),
    )
    assert '<span class="badge kind admin">Admin</span>' in html and "…zz99" in html and "can change setup" in html, "an admin token is marked, and minting one warns"
    assert "POST /api/admin/tesla/register" in html and "192.168.1.40" in html and "now allowed from anywhere" in html
    assert 'name="anywhere" aria-label="Allow admin tokens from anywhere" checked' in html
    assert 'data-copy-token="raw-token-shown-once"' in html and ">Copy</button>" in html, "a new token has a Copy button"
    says = html[html.index('class="set-row kind-says"') :].split("</div>")[0]
    assert all(f'data-k="{k}"' in says for k in ("read", "status", "control", "mcp", "admin")), "one plain line for each kind of token"
    html = env.get_template("error.html").render(**base, status=404, detail="No such drive.")
    assert "No such drive." in html
    assert "Bravo" in html and "(history)" not in html and 'href="/cc/timeline?vin=V1"' in html
    assert html.count('class="side-car') == 3, "All cars, Alpha, Bravo in the sidebar"
    assert 'href="/cc/?vin=V2"' in html, "switching car keeps the page: the error page has no here, so it falls back to Now"


def _app(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    monkeypatch.setenv("REST_USER", "u")
    monkeypatch.setenv("REST_PASSWORD", "p")
    sys.path.insert(0, str(Path(__file__).parent))
    return importlib.import_module("app")


def test_every_json_endpoint_can_serialise_what_the_pages_hold(monkeypatch):
    """/api/now?vin=<the Tesla> answered 500 and no page showed it.

    That car's payload carries a nested "meters" dict, and its "at" is a datetime one
    level below where the conversion used to stop. The HTML pages render a datetime
    without complaint, so nothing showed it until the JSON endpoints were fetched on
    purpose, which had never been done for any car but the default one."""
    import json
    from datetime import date, datetime, timedelta, timezone
    from decimal import Decimal

    jsonable = _app(monkeypatch).jsonable
    payload = {
        "updated": datetime(2026, 9, 11, 9, 22, tzinfo=timezone.utc),
        "meters": {"trip": 12.4, "at": datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)},
        "drives": [{"duration": timedelta(minutes=19), "day": date(2026, 9, 11)}],
        "kwh": Decimal("10.80"),
        "place": "Home",
        "soc": None,
    }
    out = jsonable(payload)
    assert json.loads(json.dumps(out)) == out, "the endpoint would 500 on this"
    assert out["meters"]["at"] == "2026-09-11T09:00:00+00:00"
    assert out["drives"][0] == {"duration": 1140.0, "day": "2026-09-11"}
    assert out["kwh"] == 10.8


def test_each_list_route_points_at_its_page_not_a_helper(monkeypatch):
    """A helper inserted between a route decorator and its function takes the route.
    /drives once served a 37-byte query string with a 200, because sort_base() had been
    placed under @site.get("/drives") and the suite only checked that the path existed."""
    routes = {r.path: r.endpoint.__name__ for r in _app(monkeypatch).site.routes if hasattr(r, "endpoint")}
    assert routes["/drives"] == "drives_page"
    assert routes["/charges"] == "charges_page"
    assert routes["/calendar"] == "calendar_page"


def test_the_time_zone_switch_tells_everything_that_captured_it(monkeypatch):
    """TZ is read at call time by every route, so rebinding it is most of the job. Two
    things captured it at import: the template filters close over their zone, and the
    TeslaFi poller holds its own. Both are told. If a third copy ever appears, this
    is the test that should grow."""
    import inspect

    app_module = _app(monkeypatch)
    src = inspect.getsource(app_module.use_timezone)
    assert "register_filters(templates.env, TZ, CLOCK, DATE_ORDER, place_icons=" in src and "teslafi.tz = TZ" in src
    app_module.use_timezone("Europe/London")
    assert app_module.TZ.key == "Europe/London" and app_module.teslafi.tz.key == "Europe/London"
    assert app_module.templates.env.filters["local"](datetime(2026, 1, 1, 12, tzinfo=timezone.utc)).endswith("12:00 PM")
    app_module.use_timezone(None)
    assert app_module.TZ.key == "America/Los_Angeles"
    assert app_module.templates.env.filters["local"](datetime(2026, 1, 1, 12, tzinfo=timezone.utc)).endswith("4:00 AM")


def test_custom_icons_reach_context_free_macro_imports_and_the_live_stream_path(monkeypatch, request):
    """A custom icon used to be invisible outside page(): _parts.html's row/gap_row/
    car_card macros are `{% import %}`-ed without context by garage.html, now.html,
    drives.html, charges.html, day.html, stats.html, timeline.html and battery.html, and
    the live-stream garage card (app._garage_card) is an env.from_string() template
    rendered with explicit kwargs and no context at all. place_icon_map is now a Jinja
    *global* (filters.py), and Jinja merges globals into every context it creates — a
    context-free import included — so both paths see it without wiring anything through
    page()."""
    app_module = _app(monkeypatch)
    # a fast, DB-free icon source, swapped in over the real (DB-backed) one so this test
    # never touches a database
    monkeypatch.setitem(app_module.templates.env.globals, "place_icon_map", lambda: {"gym": "gym"})
    # A template takes the environment's globals when it is first loaded and keeps them, so any
    # test that rendered a page before this one had _parts.html cached with the real icon source.
    # Drop the cache now, and again on the way out (this runs before monkeypatch puts the global
    # back), so neither this test nor the ones after it see another's.
    app_module.templates.env.cache.clear()
    request.addfinalizer(app_module.templates.env.cache.clear)

    gap = app_module.templates.env.from_string('{% import "_parts.html" as parts %}{{ parts.gap_row("Parked at Gym") }}').render()
    assert gap.count("<svg") == 1 and 'class="pl-ic pl-gym"' in gap

    row = app_module.templates.env.from_string('{% import "_parts.html" as parts %}{{ parts.row("/d/1", "8:49 AM", "Gym", [], "", "") }}').render()
    assert row.count("<svg") == 1 and 'class="pl-ic pl-gym"' in row

    # the live-stream card path itself: a template compiled once, rendered with no
    # context kwargs — exactly app.draw_live_cards()'s call shape
    car = {"vin": "V1", "name": "Alpha", "status": "parked", "soc": 80.0, "range_mi": 210.0, "charge_target": None, "place": "Gym", "updated_age": "1 min ago", "live": True}
    card = app_module._garage_card().render(c=car, root="", colors={"V1": 0}, today={}, ticks=[])
    assert card.count("<svg") >= 1 and 'class="pl-ic pl-gym"' in card


def test_valid_icon_accepts_known_keys_none_and_blank_but_rejects_anything_else(monkeypatch):
    """A posted icon that isn't a real key, "none", or blank is a 400 — not a value that
    quietly sits in the column drawing nothing forever."""
    from fastapi import HTTPException

    app_module = _app(monkeypatch)
    assert app_module._valid_icon(None) is None
    assert app_module._valid_icon("") is None
    assert app_module._valid_icon("   ") is None
    assert app_module._valid_icon("none") == "none"
    for key in app_module.ICON_KEYS:
        assert app_module._valid_icon(key) == key
    for bad in ("sparkle", "HOME", "<script>", "Gym"):
        with pytest.raises(HTTPException) as exc:
            app_module._valid_icon(bad)
        assert exc.value.status_code == 400, bad


def test_an_install_that_never_had_a_connector_is_told_calmly():
    import app as app_module

    app_module._cache.pop("connector_seen", None)
    app_module._cache["connector_down"] = (app_module.datetime.now(app_module.timezone.utc), "CarConnectivity is not answering.")
    assert app_module.connector_down() == app_module.NO_CONNECTOR
    app_module._cache["connector_seen"] = True
    assert app_module.connector_down() is None, "one that answered before and just stopped is restarting, most likely: no warning yet"
    app_module._cache["connector_down_since"] = app_module.datetime.now(app_module.timezone.utc) - app_module.timedelta(seconds=app_module.CONNECTOR_GRACE_S + 5)
    assert "not answering" in app_module.connector_down(), "quiet past the grace: the warning shows"
    for k in ("connector_down", "connector_seen", "connector_down_since"):
        app_module._cache.pop(k, None)


def test_here_is_relative_to_the_mount_root(monkeypatch):
    app_module = _app(monkeypatch)
    assert app_module.here_of("/cc/stats", "/cc", "by=month&vin=V1") == "/stats?by=month&vin=V1"
    assert app_module.here_of("/stats", "", "") == "/stats"
    assert app_module.here_of("/cc", "/cc", "") == "/"


def test_car_switch_keeps_the_page():
    import jinja2

    sys.path.insert(0, str(Path(__file__).parent))
    import filters
    from datetime import datetime, timezone

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    car = {"name": "Alpha", "model": "Example EV", "updated_age": "just now", "vin": "V1", "live": True}
    vehicles = [{"vin": "V1", "name": "Alpha", "live": True}, {"vin": "V2", "name": "Bravo", "live": False}]
    html = env.get_template("error.html").render(
        root="/cc",
        now=datetime(2026, 9, 4, tzinfo=timezone.utc),
        car=car,
        active="battery",
        vehicles=vehicles,
        q="?vin=V1",
        colors={"V1": 0, "V2": 1},
        names={"V1": "Alpha", "V2": "Bravo"},
        cur="$",
        here="/battery?days=30&vin=V1",
        status=404,
        detail="x",
    )
    assert 'href="/cc/battery?days=30&vin=V2"' in html, "the test environment does not escape; the app's does (&amp;), which the browser reads the same"
    assert 'class="side-page on"' in html and ">Battery<" in html, "the section's pages are listed with the current one marked"
    one = env.get_template("error.html").render(
        root="", now=datetime(2026, 9, 4, tzinfo=timezone.utc), car=car, active="now", vehicles=vehicles[:1], q="", cur="$", here="/", colors={}, names={}, status=404, detail="x"
    )
    assert "side-car" not in one and "carsheet" not in one, "one car: no switcher"


def test_pages_prefetch_and_never_downloads():
    import json
    import re

    import jinja2

    sys.path.insert(0, str(Path(__file__).parent))
    import filters
    from datetime import datetime, timezone

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    html = env.get_template("error.html").render(
        root="/cc", now=datetime(2026, 9, 4, tzinfo=timezone.utc), car={"vin": "V1", "name": "Alpha"}, active="", vehicles=[], q="", cur="$", colors={}, names={}, status=404, detail="x"
    )
    rules = json.loads(re.search(r'<script type="speculationrules">(.*?)</script>', html, re.S).group(1))
    text = json.dumps(rules)
    assert '"/cc/*"' in text and "*.csv" in text and "data-no-prefetch" in text
    assert 'src="/cc/static/prefetch.js' in html
    js = (Path(__file__).parent / "static" / "prefetch.js").read_text()
    assert ".csv" in js and "data-no-prefetch" in js and "speculationrules" in js


def test_manifest_carries_the_mount_prefix(monkeypatch):
    app_module = _app(monkeypatch)
    m = app_module.manifest_for("/cc")
    assert m["start_url"] == "/cc/" and m["scope"] == "/cc/" and m["display"] == "standalone"
    assert [(i["src"], i.get("purpose", "any")) for i in m["icons"]] == [
        ("/cc/static/icon-192.png", "any"),
        ("/cc/static/icon-512.png", "any"),
        ("/cc/static/icon-192.png", "maskable"),
        ("/cc/static/icon-512.png", "maskable"),
    ], "the icon keeps the mark inside the maskable safe circle, so the same files serve both"
    assert app_module.manifest_for("")["start_url"] == "/"
    for name in ("icon-192.png", "icon-512.png", "apple-touch-icon.png"):
        assert (Path(__file__).parent / "static" / name).stat().st_size > 500, name


def test_the_shell_survives_the_apps_own_escaping(monkeypatch):
    """The render tests use a plain environment; the app's escapes. The prefetch rules must
    still be JSON, and a switcher link with two parameters must still be one link."""
    import json
    import re
    from datetime import datetime, timezone

    app_module = _app(monkeypatch)
    env = app_module.templates.env
    car = {"name": "Alpha", "vin": "V1", "live": True, "updated_age": "now"}
    vehicles = [{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}]
    html = env.get_template("error.html").render(
        root="/cc",
        now=datetime(2026, 9, 4, tzinfo=timezone.utc),
        car=car,
        active="battery",
        vehicles=vehicles,
        q="?vin=V1",
        colors={"V1": 0, "V2": 1},
        names={},
        cur="$",
        here="/battery?days=30&vin=V1",
        status=404,
        detail="x",
        js_v="x",
        css_v="x",
    )
    rules = json.loads(re.search(r'<script type="speculationrules">(.*?)</script>', html, re.S).group(1))
    assert "/cc/*.csv" in json.dumps(rules), "the csv pattern carries the mount, or it resolves against the page's folder"
    assert 'href="/cc/battery?days=30&amp;vin=V2"' in html


def test_the_sidebar_shows_each_cars_battery(monkeypatch):
    import jinja2
    from datetime import datetime, timezone

    app_module = _app(monkeypatch)
    sql = app_module.car_levels_sql()
    assert "drive_levels" in sql and "LIMIT 1" in sql and "%" not in sql

    class Conn:
        def execute(self, q, params=None):
            return self

        def fetchall(self):
            return [{"vin": "V1", "level": 58.3}, {"vin": "V2", "level": None}]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(app_module, "db", lambda: Conn())
    app_module._cache.pop("car_levels", None)
    assert app_module.car_levels() == {"V1": 58.3}, "a car with no reading is left out, not shown as None"

    sys.path.insert(0, str(Path(__file__).parent))
    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    html = env.get_template("error.html").render(
        root="",
        now=datetime(2026, 9, 4, tzinfo=timezone.utc),
        car={"vin": "V1", "name": "Alpha"},
        active="stats",
        vehicles=[{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}],
        q="",
        colors={},
        names={},
        cur="$",
        levels={"V1": 58.3},
        status=404,
        detail="x",
    )
    assert '<span class="side-lv">58%</span>' in html and html.count('class="side-lv"') == 2, "once in the sidebar, once in the phone's car sheet"
    tabbar = html[html.index('class="tabbar"') : html.index('<div class="sheet up">')]
    assert tabbar.count("<svg") == 5, "four sections and More in the bar, each with its icon (the More sheet's rows have their own)"


def test_today_is_one_query_for_every_car(monkeypatch):
    app_module = _app(monkeypatch)
    sql = app_module.today_sql()
    assert sql.count("FROM trips") == 1 and sql.count("FROM charging_sessions") == 1 and "GROUP BY t.vin" in sql and "GROUP BY c.vin" in sql, "one statement: drives and charges grouped by car, joined"
    assert "cc_hidden" in sql, "hidden drives and charges do not count"
    assert "GROUP BY" in sql and "%(since)s" in sql and "%" not in sql.replace("%(since)s", "")
    calls = []

    class Conn:
        def execute(self, q, params=None):
            calls.append(params)
            return self

        def fetchall(self):
            return [{"vin": "A", "drives": 3, "miles": 98.2, "kwh": 9.2}]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(app_module, "db", lambda: Conn())
    app_module._cache.pop("today", None)
    got = app_module.today_by_car()
    assert got == {"A": {"drives": 3, "miles": 98.2, "kwh": 9.2}}
    since = calls[0]["since"]
    local = since.astimezone(app_module.TZ)
    assert (local.hour, local.minute) == (0, 0), "today starts at local midnight"
    app_module.today_by_car()
    assert len(calls) == 1, "cached for a minute"


def test_garage_now_says_what_each_car_did():
    import jinja2
    from datetime import datetime, timedelta, timezone

    sys.path.insert(0, str(Path(__file__).parent))
    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    end = datetime(2026, 9, 30, 5, 25, tzinfo=timezone.utc)
    rib = [{"label": "sleeping", "width": 1.0, "start": end - timedelta(hours=24), "end": end}]
    cars = [
        {
            "vin": "V1",
            "name": "Alpha",
            "status": "Ready",
            "soc": 58.0,
            "range_mi": 225.0,
            "charge_target": 90.0,
            "place": "Home",
            "live": True,
            "updated_age": "just now",
            "ribbon": rib,
            "last_drive": None,
        },
        {
            "vin": "V2",
            "name": "Bravo",
            "status": "Sleeping",
            "soc": 43.0,
            "range_mi": 119.0,
            "charge_target": None,
            "place": "Home",
            "live": False,
            "updated_age": "45 min ago",
            "ribbon": rib,
            "last_drive": None,
        },
    ]
    line = [{"vin": "V1", "name": "Alpha", "text": "drove 11 mi in 5 drives", "moved": True}, {"vin": "V2", "name": "Bravo", "text": "stayed home", "moved": False}]
    html = env.get_template("garage.html").render(
        root="/cc",
        now=end,
        car={"vin": "all", "name": "Garage", "garage": True},
        active="now",
        vehicles=[{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}],
        q="?vin=all",
        colors={"V1": 0, "V2": 1},
        names={},
        cur="$",
        cars=cars,
        today={"V1": {"drives": 5, "miles": 11.2, "kwh": 0.0}},
        line=line,
        ticks=[{"left": 0.0, "label": "10 PM"}, {"left": 100.0, "label": "now"}],
    )
    assert "drove 11 mi in 5 drives" in html and "stayed home" in html
    assert html.count('class="cc"') == 2 and ">Idling<" in html and ">Asleep<" in html and "limit 90%" in html and "5 drives · 11 mi" in html


def test_single_car_now_leads_with_state_and_lists_rows():
    import jinja2
    from datetime import datetime, timedelta, timezone

    sys.path.insert(0, str(Path(__file__).parent))
    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    t0 = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)
    car = {
        "vin": "V1",
        "name": "Alpha",
        "status": "Charging",
        "status_class": "charging",
        "charging_power_kw": 7.2,
        "soc": 58.0,
        "range_mi": 225.0,
        "range_full_mi": 385.0,
        "charge_target": 90.0,
        "place": "Home",
        "live": True,
        "updated_age": "just now",
        "lat": None,
        "lon": None,
    }
    drive = {
        "id": 5,
        "start_date": t0,
        "destination_date": t0 + timedelta(minutes=9),
        "from": "Home",
        "to": "Elm Street, Springfield",
        "miles": 3.1,
        "duration": timedelta(minutes=9),
        "wh_per_mi": 479.0,
        "cost": 0.46,
        "level_start": 59.0,
        "level_end": 57.0,
    }
    charge = {"id": 9, "session_start_date": t0, "where": "Home", "duration": timedelta(hours=2), "charging_type": "AC", "cost": 5.2, "kwh": 17.4, "start_level": 40.0, "end_level": 58.0}
    ribbon = [{"label": "charging", "width": 1.0, "start": t0 - timedelta(hours=24), "end": t0}]
    ctx = dict(
        root="/cc",
        now=t0,
        car=car,
        active="now",
        vehicles=[{"vin": "V1", "name": "Alpha"}],
        q="",
        colors={},
        names={},
        cur="$",
        ribbon=ribbon,
        spark="",
        software=[],
        due=[],
        spark_first=None,
        drives=[drive],
        charges=[charge],
        thumbs={5: "<svg class='route'></svg>"},
        tread_last=None,
        ticks=[{"left": 0.0, "label": "4 PM"}, {"left": 100.0, "label": "now"}],
    )
    html = env.get_template("now.html").render(**ctx, today={"drives": 1, "miles": 3.1, "kwh": 17.4})
    offer = {"maps_offer": None, "public_tiles": False, "public_geocoder": False}
    asked = env.get_template("now.html").render(**dict(ctx, settings=offer, here="/"), today=None)
    assert 'id="maps-offer"' in asked and asked.index('id="maps-offer"') < asked.index("</main>") and 'id="maps-offer"' not in html, "Now carries the one-time offer until it is answered"
    est = env.get_template("now.html").render(**dict(ctx, drives=[dict(drive, cost_estimate=True)], charges=[dict(charge, cost_source="average")]), today=None)
    assert "≈$0.46" in est and "≈$5.20" in est and "≈" not in html, "an estimated cost is marked on Now's rows, a real one is not"
    assert html.count('class="row-b') == 2, "recent drives and charges are one-line rows"
    assert 'class="gauge"' in html and "limit 90%" in html and "</i>Charging</span>" in html and '<span class="ft-b">7.2 kW</span>' in html
    assert "1 drive today · 3 mi" in html and "charged 17 kWh today" in html and "route" in html and "$0.46" in html and "17.4 kWh" in html
    quiet = env.get_template("now.html").render(**dict(ctx, drives=[], charges=[]), today=None)
    assert "no drives today" in quiet and "No drives yet" in quiet
    # a parked car with drives today: the car close up, and beside it a second map of the day's drives
    here = dict(car, lat=39.78, lon=-89.65)
    pathed = env.get_template("now.html").render(**dict(ctx, car=here, day_path=[[[39.7, -89.6], [39.78, -89.65]]], path_kind="today"), today={"drives": 2, "miles": 9.0})
    assert 'id="map-day"' in pathed and "[[[39.7, -89.6], [39.78, -89.65]]]" in pathed and "Today · 2 drives · 9 mi" in pathed
    last = env.get_template("now.html").render(**dict(ctx, car=here, day_path=[[[39.7, -89.6], [39.78, -89.65]]], path_kind="last"), today=None)
    assert "<figcaption>Last drive" in last
    still = env.get_template("now.html").render(**dict(ctx, car=here, day_path=[]), today=None)
    assert 'id="map"' in still and 'id="map-day"' not in still and 'class="maps2 one"' in still, "no route, one map"
    assert "leaflet.js" in still and "leaflet.js" not in html, "the map files load only when Now has a position to draw"
    # asleep, plug state unknown, no commands: no Charge tile saying only "—"
    asleep = env.get_template("now.html").render(**dict(ctx, car=dict(car, status="Asleep", charging_power_kw=None, plugged=None)), today=None)
    assert "Charge</span>" not in asleep and 'class="fts"' in asleep and "Charging</span>" in html, "an empty Charge tile is dropped"
    unplugged = env.get_template("now.html").render(**dict(ctx, car=dict(car, status="Asleep", charging_power_kw=None, plugged=False)), today=None)
    assert "Unplugged" in unplugged
    css = (Path(__file__).parent / "static" / "style.css").read_text()
    assert ".maps2 { grid-template-columns:1fr; }" in css and ".fts > .ft:last-child:nth-child(odd) { grid-column:1 / -1; }" in css, "a phone stacks the maps; an odd last tile spans its row"
    import re

    # out of reach: the pill says Offline with the last reading's time, the dot is grey, and no "driving now"
    gone = dict(car, status="Driving", offline=True, last_seen=t0 - timedelta(minutes=80))
    out = env.get_template("now.html").render(**dict(ctx, car=gone, win="24h"), today={"drives": 1, "miles": 3.1})
    assert 'class="state offline"' in out and "<i></i>Offline" in out and re.search(r"last seen [^<]*\d{1,2}:\d\d", out)
    assert "driving now" not in out and "<i></i>Driving" not in out and "<title>Alpha · Offline" in out
    assert "last seen" not in html and ">Offline<" not in html, "a reachable car shows neither"
    # the window toggle: the strip and the chart share it; the chosen one is marked
    week = env.get_template("now.html").render(**dict(ctx, win="7d", spark="0,1 600,2"), today=None)
    assert "Last 7 days" in week and 'href="/cc/?win=24h"' in week and 'href="/cc/?win=7d" data-keep-scroll data-win-to="7d" aria-current="true"' in week and "over the last seven days" in week
    assert "Last 24 hours" in env.get_template("now.html").render(**dict(ctx, win="24h"), today=None)
    assert 'href="/cc/?win=24h" data-keep-scroll data-win-to="24h"' in week, "without the script the link loads the page where the reader was"
    assert 'href="/cc/?vin=V1&win=7d"' in env.get_template("now.html").render(**dict(ctx, win="24h", q="?vin=V1"), today=None), "the toggle keeps the car"
    card = env.get_template("_parts.html").module.car_card(dict(gone, lat=None), "/cc", {}, {"dist": "mi"}, None, [])
    assert 'class="state offline"' in card and "last seen" in card
    yday = dict(gone, lat=None, last_seen=datetime.now(timezone.utc) - timedelta(days=1))
    assert re.search(
        r"last seen Yesterday\W+\d{1,2}:\d\d", env.get_template("_parts.html").module.car_card(yday, "/cc", {}, {"dist": "mi"}, None, []).replace("\u202f", " ")
    ), "a sighting from another day carries the day"
    today = dict(gone, lat=None, last_seen=datetime.now(timezone.utc))
    assert "last seen Yesterday" not in env.get_template("_parts.html").module.car_card(today, "/cc", {}, {"dist": "mi"}, None, [])


def test_a_battery_chart_sits_on_the_strips_time_axis():
    from datetime import datetime, timedelta, timezone

    import app as app_module

    end = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)
    start = end - timedelta(hours=24)
    pts = app_module.sparkline([(start + timedelta(hours=6), 80.0), (end, 60.0)], window=(start, end)).split()
    assert pts[0].startswith("150.0,") and pts[1].startswith("600.0,"), "a quarter of the way in sits a quarter of the way across, not at the left edge"
    assert app_module.sparkline([(start - timedelta(hours=1), 50.0), (end, 50.0)], window=(start, end)).startswith("0.0,"), "a point from before the window stays on the chart"
    assert app_module.sparkline([(start, 50.0)], window=(start, end)) == ""


def test_an_unfinished_drive_does_not_count_as_driven_today(monkeypatch):
    app_module = _app(monkeypatch)
    assert "COUNT(t.destination_odometer)" in app_module.today_sql(), "a drive still under way has no end odometer and would read 'drove 0 mi'"


def test_saving_thresholds_or_hiding_a_row_forgets_today():
    src = (Path(__file__).parent / "app.py").read_text()
    save = src[src.index("async def settings_save") : src.index("@site.get(\"/charging\"")]
    assert '_cache.pop("today", None)' in save
    hide = src[src.index("def set_hidden") : src.index("@site.post(\"/drives/{trip_id}/hide\")")]
    assert '_cache.pop("today", None)' in hide


def test_single_car_now_small_things():
    import jinja2
    from datetime import datetime, timezone

    sys.path.insert(0, str(Path(__file__).parent))
    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    t0 = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)
    car = {"vin": "V1", "name": "Alpha", "status": "Sleeping", "soc": 58.0, "range_mi": 225.0, "place": "Home", "live": True, "updated_age": "just now", "lat": None, "lon": None}
    running = {"id": 9, "session_start_date": t0, "where": "Home", "duration": None, "charging_type": "AC", "cost": None, "kwh": None, "start_level": 40.0, "end_level": None}
    html = env.get_template("now.html").render(
        root="",
        now=t0,
        car=car,
        active="now",
        vehicles=[],
        q="",
        colors={},
        names={},
        cur="$",
        ribbon=[],
        spark="",
        software=[],
        due=[],
        spark_first=None,
        drives=[],
        charges=[running],
        thumbs={},
        tread_last=None,
        ticks=[],
        today={"drives": 0, "miles": 0.0, "kwh": 0.0},
    )
    assert "no charge today" in html, "the same two pills as the garage card"
    assert "<b> kWh</b>" not in html and "<b>—</b>" in html, "a charge still running has no kWh yet"
    assert "<title>Alpha · Asleep · Car Command</title>" in html


def test_the_garage_now_route_builds_its_page(monkeypatch):
    """The route referenced a module name app.py never imported and answered 500
    when deployed; the template tests could not see it. Run the route itself."""
    from datetime import datetime, timedelta, timezone

    app_module = _app(monkeypatch)
    end = datetime.now(timezone.utc)
    rib = [{"label": "sleeping", "width": 1.0, "start": end - timedelta(hours=24), "end": end}]
    monkeypatch.setattr(app_module, "pick", lambda request: {"vin": "all", "name": "All cars", "garage": True})
    monkeypatch.setattr(app_module, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha", "live": True}])
    monkeypatch.setattr(app_module, "live_vehicle", lambda vin=None: {"vin": "V1", "name": "Alpha", "live": True, "status": "Idle", "soc": 50.0, "range_mi": 150.0, "updated": end, "place": "Home"})
    monkeypatch.setattr(app_module, "ribbon", lambda vin, hours=24: rib)
    monkeypatch.setattr(app_module, "drives", lambda vin, limit=50, start=None, end=None: [])
    monkeypatch.setattr(app_module, "today_by_car", lambda: {"V1": {"drives": 2, "miles": 12.0, "kwh": 0.0}})
    seen = {}
    monkeypatch.setattr(app_module, "page", lambda request, name, **ctx: seen.update(name=name, **ctx) or "ok")

    class Req:
        query_params = {"vin": "all"}

    assert app_module.now(Req()) == "ok"
    assert seen["name"] == "garage.html" and seen["line"][0]["text"] == "drove 12 mi in 2 drives" and seen["ticks"][-1]["label"] == "now"
    assert seen["live_vin"] == "all", "the garage streams every card"


def test_the_window_switch_gets_the_strip_and_battery_line_alone(monkeypatch):
    """/now/window answers with only #now-win (what 24 h / 7 d changes), remembers the choice in a
    cookie, and reads nothing the page's other cards need."""
    from datetime import datetime, timedelta, timezone

    app_module = _app(monkeypatch)
    end = datetime.now(timezone.utc)
    seen = []
    rib = [{"label": "sleeping", "width": 0.5, "start": end - timedelta(hours=84), "end": end - timedelta(hours=42)}, {"label": "caron", "width": 0.5, "start": end - timedelta(hours=42), "end": end}]
    monkeypatch.setattr(app_module, "pick", lambda request: {"vin": "V1", "name": "Alpha", "live": True, "soc": 50.0})
    monkeypatch.setattr(app_module, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}])
    monkeypatch.setattr(app_module, "ribbon", lambda vin, hours=24, end=None: seen.append(("ribbon", hours)) or rib)
    monkeypatch.setattr(app_module, "battery_series", lambda vin, days: [(end - timedelta(hours=5), 60.0)])
    monkeypatch.setattr(app_module, "drives", lambda vin, limit=50, start=None, end=None: [])
    monkeypatch.setattr(app_module, "charges", lambda vin, limit=50, start=None, end=None: [])

    class Req:
        query_params = {"vin": "V1"}
        scope = {"root_path": "/cc"}

    r = app_module.now_window_page(Req(), "7d")
    html = r.body.decode()
    assert seen == [("ribbon", 168)] and html.lstrip().startswith('<div id="now-win" data-win="7d" data-win-src="/cc/now/window?vin=V1"')
    assert "Last 7 days" in html and 'class="strip-bar"' in html and 'class="spark"' in html and 'class="seg caron"' in html
    assert 'data-win-to="24h"' in html and 'data-win-to="7d" aria-current="true"' in html and "<main" not in html and 'class="gauge"' not in html
    assert "cc_win=7d" in r.headers["set-cookie"] and "no-store" in r.headers["cache-control"]
    day = app_module.now_window_page(Req(), "24h").body.decode()
    assert 'class="strip-mid"' in day and 'class="ax-day"' in day and 'class="ax-now"' in day and 'class="ax-hour"' in day, "midnight is a line and a weekday; now ends the axis"
    with pytest.raises(Exception):
        app_module.now_window_page(Req(), "90d")


def test_the_switch_script_swaps_in_place_with_a_pending_state_and_a_fallback():
    js = (Path(__file__).parent / "static" / "shell.js").read_text()
    assert 'a[data-win-to]' in js and 'setAttribute("aria-busy", "true")' in js and "replaceWith(fresh)" in js and "AbortController" in js
    assert "location.href = a.href" in js, "a failed swap loads the page the old way"
    css = (Path(__file__).parent / "static" / "style.css").read_text()
    assert '#now-win[aria-busy="true"]' in css and "prefers-reduced-motion" in css.split("win-wait")[-1]


def test_the_day_pages_names_open_the_naming_card():
    """day.py threads refs onto every row; day.html passes them to gap_row and row, which make
    the place a tappable name: an unnamed stop underlined, a named one plain until hovered,
    still with its glyph. A row with no place has none."""
    import jinja2
    from datetime import date, datetime, timedelta, timezone

    sys.path.insert(0, str(Path(__file__).parent))
    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    t0 = datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc)
    car = {"name": "Alpha", "model": "Example EV", "updated_age": "just now", "vin": "V1", "live": True}
    base = {"root": "", "now": t0, "car": car, "active": "", "vehicles": [{"vin": "V1", "name": "Alpha", "live": True}], "q": "", "colors": {"V1": 0}, "names": {"V1": "Alpha"}, "cur": "$"}
    parked = {"kind": "parked", "sub": "idle", "id": None, "title": "Idle", "duration": timedelta(hours=2), "kwh_loss": None}
    rows = [
        dict(parked, start=t0 - timedelta(hours=2), end=t0, where="Elm Street, Springfield", refs=[{"name": "Elm Street, Springfield", "lat": 39.80, "lon": -89.65, "new": True}]),
        dict(parked, start=t0 - timedelta(hours=4), end=t0 - timedelta(hours=2), where="Home", refs=[{"name": "Home", "lat": 39.79, "lon": -89.66, "new": False}]),
        dict(parked, start=t0 - timedelta(hours=6), end=t0 - timedelta(hours=4), where=None, refs=[]),
    ]
    import day as day_module

    blocks = [{"vin": "V1", "name": "Alpha", "live": True, "ribbon": [], "rows": rows, "totals": day_module.day_totals(rows, None), "routes": [], "chart": {}, "since_charge": None}]
    html = env.get_template("day.html").render(**base, day=t0.date(), blocks=blocks, prev=date(2026, 9, 3), next=None, month="2026-09", show="all", today=date(2026, 9, 5))
    assert '<button class="pn pn-new" type="button" aria-haspopup="dialog" data-lat="39.8" data-lon="-89.65">Elm Street, Springfield</button>' in html
    assert '<button class="pn" type="button" aria-haspopup="dialog" data-lat="39.79" data-lon="-89.66"><span class="pl">' in html, "Home opens it too, glyph and all"
    assert html.count('class="pn') == 2, "the row with no place has nothing to tap"


def _maps_page(**kw):
    import jinja2
    from datetime import datetime, timezone

    sys.path.insert(0, str(Path(__file__).parent))
    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    t0 = datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc)
    car = {"name": "Alpha", "vin": "V1", "live": True, "updated_age": "now"}
    base = {"root": "/cc", "now": t0, "car": car, "active": "", "vehicles": [car], "q": "?vin=V1", "colors": {"V1": 0}, "names": {"V1": "Alpha"}, "cur": "$"}
    ctx = dict(
        base,
        st={"public_tiles": True, "public_geocoder": False},
        conv={},
        saved=False,
        section="maps",
        label="Maps",
        regions=[],
        wait_until_dt=None,
        maps_on=False,
        status=None,
        esri={"ready": False, "hint": None},
        esri_sign_up="https://example.test/sign-up",
        esri_error=None,
    )
    ctx.update(kw)
    return env.get_template("settings_maps.html").render(**ctx)


def _maps_status(building, **kw):
    return dict({"building": building, "version": 3, "disk_bytes": 2_500_000_000, "error": None, "auto": True, "refresh": "monthly", "regions": []}, **kw)


def test_settings_maps_says_what_the_builder_is_doing():
    """The builder reports idle, downloading, building or failed; only the middle two are
    "building", and an idle service with a version is on."""
    on = _maps_page(status=_maps_status("idle"), maps_on=True)
    assert "On</span> · version 3 · 2.5 GB" in on and "building" not in on
    for busy in ("downloading", "building"):
        assert "building&hellip;" in _maps_page(status=_maps_status(busy), maps_on=True)
    failed = _maps_page(status=_maps_status("failed", error="osmium: out of memory"), maps_on=True)
    assert "Build failed" in failed and "osmium: out of memory" in failed and "Try again" in failed and "building&hellip;" not in failed
    off = _maps_page()
    assert "Not running" in off and "<code>docker compose --profile maps up -d maps</code></span>" in off


def test_settings_maps_public_switches_stay_editable_with_local_maps_on():
    """Local map backgrounds do not exist yet and the public geocoder still names stops
    outside the regions, so both switches are editable whether or not local maps run."""
    for maps_on in (False, True):
        html = _maps_page(status=_maps_status("idle") if maps_on else None, maps_on=maps_on)
        assert html.count("disabled") == 1 and "Not needed" not in html, "only the satellite switch waits, for its key"
        assert 'name="public_tiles_shown"' in html and 'name="public_geocoder_shown"' in html
        assert "Map backgrounds from OpenFreeMap" in html and "OpenFreeMap sees which area you view" in html
        assert "Off: maps draw routes and places on a plain background. Local backgrounds aren't built yet." in html
        assert ("<code>docker compose --profile maps up -d maps</code></span>" in html) is (not maps_on) and "Just this region" not in html
    on = _maps_page(status=_maps_status("idle"), maps_on=True)
    assert "Public place names outside your regions" in on and "Nominatim sees that stop, rounded to about 100 m" in on
    off = _maps_page()
    assert "Place names from OpenStreetMap" in off and "Nominatim sees each stop, rounded to about 100 m" in off


def test_the_maps_loops_never_block_the_event_loop():
    """MAPS.status() can take its full 3 s timeout and the geocoder tick reads the database;
    run on the event loop, either stalls every page for that long. Both loops hand their
    blocking work to a thread."""
    src = (Path(__file__).parent / "app.py").read_text()
    geo = src[src.index("    async def geocode_loop():") : src.index("    geo_task = asyncio.create_task")]
    maps = src[src.index("    async def maps_loop():") : src.index("    maps_task = asyncio.create_task")]
    assert "asyncio.to_thread(geocoder_tick, down_streak)" in geo and "= geocoder_tick(" not in geo
    body = maps[maps.index("while True:") :]
    for blocking in ("MAPS.status()", "state_get(", "state_set(", "w.tick()"):
        assert blocking not in body, blocking
    assert "await asyncio.to_thread(maps_tick, w)" in body
    startup = src[src.index("    apply_settings(current_settings())") : src.index("    recorder.start()")]
    assert "await asyncio.to_thread(geocoder_tick, 0)" in startup


def test_map_backgrounds_reach_a_page_rendered_without_settings(monkeypatch):
    """base.html's data-tiles used to read settings.public_tiles from the page context, so
    any render that did not pass settings drew its maps with no background. It is a Jinja
    global now, set whenever the settings are applied (at startup and on every save)."""
    app_module = _app(monkeypatch)
    env = app_module.templates.env
    monkeypatch.setitem(env.globals, "place_icon_map", lambda: {})
    page = '{% extends "base.html" %}{% block body %}x{% endblock %}'
    monkeypatch.setattr(app_module, "use_timezone", lambda name: None)
    ctx = {"root": "", "vehicles": [], "now": datetime(2026, 10, 3, 12, tzinfo=timezone.utc), "car": {"vin": "V1", "name": "Alpha"}}
    app_module.apply_settings({**app_module.settings_mod.DEFAULTS, "public_tiles": True})
    assert 'data-tiles="public"' in env.from_string(page).render(**ctx)
    app_module.apply_settings({**app_module.settings_mod.DEFAULTS, "public_tiles": False})
    assert 'data-tiles="none"' in env.from_string(page).render(**ctx)


def test_satellite_needs_the_key_as_well_as_the_switch(monkeypatch):
    """Satellite on with a key shows the close-ups even with the public backgrounds off, and hands the page the key; no key, nothing."""
    app_module = _app(monkeypatch)
    env = app_module.templates.env
    monkeypatch.setitem(env.globals, "place_icon_map", lambda: {})
    monkeypatch.setattr(app_module, "ESRI_KEY", None)
    page = '{% extends "base.html" %}{% block body %}x{% endblock %}'
    monkeypatch.setattr(app_module, "use_timezone", lambda name: None)
    ctx = {"root": "", "vehicles": [], "now": datetime(2026, 10, 3, 12, tzinfo=timezone.utc), "car": {"vin": "V1", "name": "Alpha"}}
    d = app_module.settings_mod.DEFAULTS
    app_module.apply_settings({**d, "satellite": True})
    assert 'data-sat="off"' in env.from_string(page).render(**ctx), "no key, no satellite"
    monkeypatch.setattr(app_module, "ESRI_KEY", "K" * 30)
    app_module.apply_settings({**d, "satellite": True})
    html = env.from_string(page).render(**ctx)
    assert 'data-sat="on"' in html and 'data-sat-key="' + "K" * 30 + '"' in html and 'data-close="on"' in html and 'data-tiles="none"' in html
    app_module.apply_settings({**d, "public_tiles": True})
    off = env.from_string(page).render(**ctx)
    assert 'data-sat="off"' in off and "data-sat-key" not in off and 'data-close="on"' in off
    app_module.apply_settings(dict(d))
    assert 'data-close="off"' in env.from_string(page).render(**ctx)


def test_settings_maps_region_rows_read_as_words():
    """A region row shows its name, why it is here and the day it was refreshed, never a
    Geofabrik path, a coordinate, or "refreshed" twice."""
    from datetime import datetime, timezone

    t = datetime(2026, 10, 3, 19, tzinfo=timezone.utc)
    regions = [
        {"id": "us/oregon", "label": "Oregon", "why": "Your drives", "mb": 254, "added_dt": t, "refreshed_dt": t},
        {"id": "us/nevada", "label": "Nevada", "why": "Added by you", "mb": 124, "added_dt": t, "refreshed_dt": None},
    ]
    html = _maps_page(status=_maps_status("idle"), maps_on=True, regions=regions)
    assert ">Oregon<span class=\"set-s\">Your drives · 254 MB · Oct 3</span>" in html and "Nevada" in html and "not built yet" in html and "refreshed Oct" not in html
    assert 'name="auto" id="maps-auto"' in html and 'value="us/oregon" aria-label="Remove Oregon"' in html


def test_a_named_place_without_an_address_reads_as_its_street(monkeypatch):
    """Places showed "9,464 visits 39.8000, -89.6500" for a named place with no stored
    address; the cell's cached street is the same one every drive row already shows."""
    app_module = _app(monkeypatch)
    cells = {(39.8, -89.65): "Capitol Avenue, Springfield"}
    rows = [
        {"name": "Home", "latitude": 39.80012, "longitude": -89.65004, "address": None},
        {"name": "Work", "latitude": 39.81, "longitude": -89.66, "address": "1 Main Street, Springfield, Sangamon County"},
        {"name": "Lake", "latitude": 39.70, "longitude": -89.61, "address": None},
    ]
    out = app_module.with_street(rows, cells)
    assert [r["street"] for r in out] == ["Capitol Avenue, Springfield", None, None]


def test_a_slow_request_is_logged_with_its_page_and_time():
    """A page that takes over a second leaves a line naming itself, so 'sometimes slow' has evidence."""
    from app import slow_line

    assert slow_line("GET", "/drives", 1450.0) == "slow request: GET /drives took 1450 ms"
    assert slow_line("GET", "/drives", 300.0) is None
    assert slow_line("GET", "/stream", 60000.0) is None, "the live stream is long-lived on purpose"
    assert slow_line("GET", "/static/style.css", 5000.0) is None


def test_now_flows_banners_then_today_then_the_cars():
    """Top to bottom: what is happening now, what happened today, then each car. A car out
    driving has its banner; Sentry on is a red dot on that car's card, nowhere else."""
    import jinja2
    from datetime import timedelta

    sys.path.insert(0, str(Path(__file__).parent))
    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")), autoescape=True)
    filters.register(env)
    t0 = datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)
    alpha = {"vin": "V1", "name": "Alpha", "status": "Driving", "soc": 70.0, "range_mi": 200.0, "place": None, "live": False, "updated_age": "just now", "make": "Tesla", "sentry": False}
    bravo = {"vin": "V2", "name": "Bravo", "status": "Idle", "soc": 50.0, "range_mi": 150.0, "place": "Home", "live": True, "updated_age": "just now", "make": "Lucid", "sentry": True}
    drive = {"vin": "V1", "name": "Alpha", "kind": "driving", "since": timedelta(minutes=12), "dest": "Work", "minutes_left": 8, "miles": 6.14, "wh_per_mi": 251.0, "estimate": False}
    common = dict(root="", now=t0, active="now", vehicles=[{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}], q="", colors={"V1": 0, "V2": 1}, names={}, cur="$", ticks=[])
    html = env.get_template("garage.html").render(
        **common,
        car={"vin": "all", "name": "Garage", "garage": True},
        cars=[alpha, bravo],
        today={},
        line=[{"vin": "V1", "name": "Alpha", "text": "stayed put", "moved": False}, {"vin": "V2", "name": "Bravo", "text": "stayed home", "moved": False}],
        acts=[drive],
        act_sig="V1:driving",
    )
    assert html.index('class="acts"') < html.index('class="today"') < html.index('class="cc"'), "banners, then today, then the cars"
    assert "<b>Alpha</b> driving to <b>Work</b>" in html and "12 min · 6.1 mi · 251 Wh/mi · 8 min to go" in html
    assert 'href="/?vin=V1" aria-label="Alpha driving"' in html, "on the garage page a banner opens its car"
    assert html.count(' guarding"') == 1 and 'title="Halo Secure on"' in html and ">Halo Secure<" not in html, "the guard is on Bravo's state, named only in its title"
    import re as _re

    assert _re.search(r'<span class="state [a-z]+ guarding" title="Halo Secure on"><i></i>Parked</span>', html), "the guard's red dot stands in for the state's dot, with no text of its own"
    moving = env.get_template("garage.html").render(**common, car={"vin": "all", "name": "Garage", "garage": True}, cars=[dict(bravo, status="Driving")], today={}, line=[], acts=[], act_sig="")
    assert " guarding" not in moving, "a moving car reports its guard set but idle: no red dot while driving"
    quiet = env.get_template("garage.html").render(**common, car={"vin": "all", "name": "Garage", "garage": True}, cars=[alpha], today={}, line=[], acts=[], act_sig="")
    assert '<div class="acts" data-live="activity" data-sig=""></div>' in quiet and "act-scene" not in quiet, "nothing at all when no car is out"

    charging = {
        "vin": "V2",
        "name": "Bravo",
        "kind": "charging",
        "since": timedelta(hours=1, minutes=5),
        "place": "Bravo Market, 12 Elm Street, Springfield",
        "kw": 7.2,
        "level_from": 41.0,
        "level_now": 58.0,
        "kwh_added": None,
    }
    one = env.get_template("now.html").render(
        **common,
        car=dict(bravo, status="Charging", lat=None, lon=None),
        acts=[charging],
        act_sig="V2:charging",
        ribbon=[],
        spark="",
        software=[],
        due=[],
        spark_first=None,
        drives=[],
        charges=[],
        thumbs={},
        tread_last=None,
        today=None,
    )
    assert one.index('class="acts"') < one.index('class="hero"'), "the banner stands above the car's card"
    assert "<b>Bravo</b> charging at <b>Bravo Market, 12 Elm Street</b>" in one and "7.2 kW · 41→58% · 1 h 5 min" in one
    assert (
        '<section class="act charging" aria-label="Bravo charging">' in one and "act-plug" in one and "act-near" not in one.split("</defs>")[1]
    ), "a single car's banner is not a link; charging has no roadside"
    assert one.count(' guarding"') == 1, "the single car's card shows the guard too"
    assert "charging now" in one and "no charge today" not in one, "the card agrees with the banner"
    assert "driving now" in html and "On the road" in html, "a car out with no route: no 'no drives', no stale place"
    road = env.get_template("now.html").render(
        **common,
        car=dict(alpha, lat=39.7926, lon=-89.6347),
        acts=[],
        act_sig="",
        ribbon=[],
        spark="",
        software=[],
        due=[],
        spark_first=None,
        drives=[],
        charges=[],
        thumbs={},
        tread_last=None,
        today=None,
    )
    parked = env.get_template("now.html").render(
        **common,
        car=dict(alpha, status="Idle", lat=39.7926, lon=-89.6347),
        acts=[],
        act_sig="",
        ribbon=[],
        spark="",
        software=[],
        due=[],
        spark_first=None,
        drives=[],
        charges=[],
        thumbs={},
        tread_last=None,
        today=None,
    )
    import re

    text = lambda h: re.sub(r"<[^>]+>", "", h.split('id="map"')[0])  # noqa: E731
    assert "39.7926" not in text(road) and "39.7926" not in text(parked), "never raw coordinates in the card's words"
    assert "On the road" in road and "driving now" in road and "no drives today" not in road and "Unnamed spot" in parked


def test_a_parked_car_is_never_a_bare_dash():
    """The card and the hero say where a parked car is: a place, its street, its town, an
    unnamed spot to name, or "Location unknown" with no position at all. Never a dash."""
    import re

    import jinja2

    sys.path.insert(0, str(Path(__file__).parent))
    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")), autoescape=True)
    filters.register(env)
    t0 = datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)
    common = dict(root="", now=t0, active="now", vehicles=[{"vin": "V1", "name": "Alpha"}], q="", colors={"V1": 0}, names={}, cur="$", ticks=[])
    rest = dict(acts=[], act_sig="", ribbon=[], spark="", software=[], due=[], spark_first=None, drives=[], charges=[], thumbs={}, tread_last=None, today=None)
    alpha = {"vin": "V1", "name": "Alpha", "status": "Idle", "soc": 70.0, "range_mi": 200.0, "place": None, "address": None, "live": False, "updated_age": "just now", "make": "Tesla"}
    town = dict(alpha, lat=39.7926, lon=-89.6347, locality="Springfield, IL")
    street = dict(town, address="Oak Avenue, Springfield")
    bare = dict(alpha, lat=None, lon=None)
    card = lambda c: re.sub(
        r"<[^>]+>",
        "",
        env.get_template("garage.html").render(**common, car={"vin": "all", "name": "Garage", "garage": True}, cars=[c], line=[], **rest).split('class="cc-where"')[1].split("</span>")[0],
    )  # noqa: E731
    hero = lambda c: re.sub(r"<[^>]+>", "", env.get_template("now.html").render(**common, car=c, **rest).split('class="where"')[1].split("</p>")[0])  # noqa: E731
    assert "Springfield, IL" in card(town) and "Springfield, IL" in hero(town), "a Tesla at an unnamed spot shows its town"
    assert "Oak Avenue, Springfield" in card(street) and "Springfield, IL" not in card(street), "the street beats the town"
    assert "Location unknown" in card(bare) and "Location unknown" in hero(bare)
    for c in (town, street, bare):
        assert "\u2014" not in card(c) and "39.7926" not in card(c) + hero(c), "never a dash, never raw coordinates"


def test_row_marks_a_fast_charge_and_tells_kinds_apart():
    import jinja2

    sys.path.insert(0, str(Path(__file__).parent))
    import filters

    tpl = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")), autoescape=True)
    filters.register(tpl)
    tpl.globals["place_refs"] = lambda text, refs=None, inline=False: text
    parts = tpl.get_template("_parts.html").module
    fast = str(parts.row("/c/1", "4:04 PM", "Lake", ["10 min", "fast"], "8.3 kWh", "88→90%", kind="charge"))
    slow = str(parts.row("/c/2", "2:05 AM", "Home", ["1 h"], "10.1 kWh", "67→80%", kind="charge"))
    drive = str(parts.row("/d/3", "7:53 PM", "A → B", ["9 min"], "4.8 mi", "", kind="drive"))
    assert 'class="row-b k-charge"' in fast and 'row-thumb fast' in fast and 'class="ic"' in fast
    assert 'class="row-thumb"' in slow and 'fast' not in slow.replace("k-charge", "")
    assert 'class="row-b k-drive"' in drive and 'class="row-thumb"' in drive
    assert 'class="row-b"' in str(parts.row("/s", "1 PM", "Update", [], "", ""))


def test_the_site_opens_on_every_car_when_there_is_more_than_one(monkeypatch):
    """The site starts on all cars, now. No ?vin, two cars: the garage.
    One car, or a car asked for by name, keeps its own page."""
    app_module = _app(monkeypatch)
    picked = []
    monkeypatch.setattr(app_module, "pick", lambda request: picked.append(1) or {"vin": "V1", "name": "Alpha", "empty": True})
    seen = {}
    monkeypatch.setattr(app_module, "page", lambda request, name, **ctx: seen.update(name=name, car=ctx.get("car")) or "ok")
    monkeypatch.setattr(app_module, "garage", lambda: {})
    monkeypatch.setattr(app_module, "current_settings", lambda: {"timezone": "UTC"})
    monkeypatch.setattr(app_module, "live_vehicle", lambda vin=None: {"vin": vin, "name": vin, "live": False, "updated": None})
    for name in ("ribbon", "drives", "today_by_car", "activity_banners", "car_identities"):
        monkeypatch.setattr(app_module, name, lambda *a, **k: {} if name in ("today_by_car", "car_identities") else [])

    class Bare:
        query_params = {}

    class Asked:
        query_params = {"vin": "V1"}

    monkeypatch.setattr(app_module, "all_vehicles", lambda: [{"vin": "V1"}, {"vin": "V2"}])
    app_module.now(Asked())
    assert picked, "a car asked for by vin is that car"
    picked.clear()
    try:
        app_module.now(Bare())
    except Exception:  # noqa: BLE001 — the garage page's own parts are tested elsewhere; only the choice matters here
        pass
    assert not picked, "with two cars and no vin the site starts on the garage, not on one car"
    monkeypatch.setattr(app_module, "all_vehicles", lambda: [{"vin": "V1"}])
    app_module.now(Bare())
    assert picked, "one car is just that car"


def _env():
    import jinja2

    sys.path.insert(0, str(Path(__file__).parent))
    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")), autoescape=True)
    filters.register(env)
    return env


def test_the_cards_tiles_say_the_cars_facts_and_carry_its_controls():
    import commands as cm

    car = {
        "vin": "V1",
        "name": "Alpha",
        "make": "Tesla",
        "status": "Charging",
        "soc": 41.0,
        "charge_target": 90.0,
        "charging_power_kw": 7.2,
        "plugged": True,
        "live": True,
        "locked": True,
        "inside_f": 68.0,
        "outside_f": 55.0,
        "odometer_mi": 95736.0,
        "software": "2026.26.6.5",
        "updated_age": "just now",
    }
    cmd = {
        "reason": None,
        "allowed": {"charge_start", "charge_stop", "charge_limit", "climate_on", "climate_off", "lock"},
        "more": [cm.CATALOGUE["honk"]],
        "climate": False,
        "limit": 90.0,
        "soc": 41.0,
    }
    ctx = dict(root="/cc", q="", u={"dist": "mi", "temp": "°F"}, state_word=lambda s: (s, s.lower()), guard_name=lambda m: "Sentry", place_refs=lambda *a: "", tread_last=None)
    hero = _env().get_template("_now_hero.html").render(car=car, cmd=cmd, **ctx)
    assert 'data-cmd-url="/cc/cars/V1/command"' in hero, "the card's own controls send from it"
    # charging now: its tile says the power and stops it; the climate tile says the cabin and turns it on or off
    assert '<span class="ft-b">7.2 kW</span>' in hero and 'data-cmd="charge_stop">Stop charging</button>' in hero
    # the car says climate is off: the tile is the switch, saying Off, and a tap turns it on
    assert '<span class="ft-b" data-say>Off</span>' in hero and "68° inside · 55° outside" in hero
    assert '<button type="button" class="ft tap" data-cmd="climate_on" aria-pressed="false" aria-label="Turn on climate">' in hero
    assert 'data-cmd="climate_off"' not in hero and "Turn on climate</button>" not in hero
    on = _env().get_template("_now_hero.html").render(car=car, cmd=dict(cmd, climate=True), **ctx)
    assert 'class="ft tap on" data-cmd="climate_off" aria-pressed="true" aria-label="Turn off climate"' in on
    unsure = _env().get_template("_now_hero.html").render(car=car, cmd=dict(cmd, climate=None), **ctx)
    assert '<span class="ft-b" data-say>68°</span>' in unsure and 'data-cmd="climate_on">On</button>' in unsure and 'data-cmd="climate_off">Off</button>' in unsure
    # a tile without a control opens where its fact is kept
    assert 'href="/cc/stats?period=all#odometer"' in hero and 'href="/cc/software"' in hero
    # locked with no unlock allowed: the doors say so and offer nothing; the odometer and software are tiles
    assert "<span class=\"ft-b\">Locked</span>" in hero and 'data-cmd="unlock"' not in hero and 'data-cmd="lock"' not in hero
    assert "95,736 mi" in hero and "2026.26.6.5" in hero and "morefacts" not in hero
    # the battery bar is the limit control
    assert '<div class="gauge lim-bar">' in hero and 'value="90"' in hero and "Min 50%" not in hero and "Daily · 50–90%" in hero
    # commands off: the same facts, no controls, the plain gauge
    plain = _env().get_template("_now_hero.html").render(car=car, cmd=None, **ctx)
    assert "data-cmd" not in plain and '<div class="gauge"' in plain and '<span class="ft-b">7.2 kW</span>' in plain
    # a feed car reports no locks: no Doors tile when nothing can be said or done
    feed = _env().get_template("_now_hero.html").render(car=dict(car, live=False, locked=None), cmd=None, **ctx)
    assert "</svg>Doors</span>" not in feed


def test_the_dock_holds_the_one_tap_commands_as_icons_then_adjust_and_the_log():
    import re as _re

    import commands as cm

    cmd = {"reason": None, "allowed": set(), "more": [cm.CATALOGUE[k] for k in ("honk", "flash", "climate_temp", "sched_charge", "unlock", "trunk")]}
    t = _env().from_string('{% import "_commands.html" as c %}{{ c.dock(cmd, car, root) }}')
    html = t.render(cmd=cmd, car={"vin": "V1"}, root="")
    acts = _re.findall(r'class="dock-b( guarded)?" data-cmd="([a-z_]+)"', html)
    assert acts == [("", "flash"), ("", "honk"), (" guarded", "trunk")], acts
    assert "Everyday" not in html and "role=\"tab\"" not in html, "no tier tabs"
    rows = _re.findall(r'<div class="cmd-row"><span class="cmd-l">([^<]+)</span>', html)
    assert rows == ["Charge at", "Cabin"], rows
    assert "data-step-up hidden" in html and 'data-cmd-log="/cars/V1/commands"' in html and "data-minutes" in html
    # the trunk said open: the button closes it; an open frunk only shuts by hand; the windows offer the one move that changes them
    more = cmd["more"] + [cm.CATALOGUE[k] for k in ("frunk", "windows_vent", "windows_close")]
    opened = t.render(cmd={**cmd, "more": more}, car={"vin": "V1", "trunk_open": True, "frunk_open": True, "windows_open": True}, root="")
    assert "<span data-say>Close trunk</span>" in opened and "disabled><svg" in opened and "Frunk open" in opened
    assert 'data-cmd="windows_close"' in opened and 'data-cmd="windows_vent"' not in opened
    unknown = t.render(cmd={**cmd, "more": more}, car={"vin": "V1"}, root="")
    assert "<span data-say>Open trunk</span>" in unknown and 'data-cmd="windows_close"' in unknown and 'data-cmd="windows_vent"' in unknown
    # a Lucid closes its own: an open trunk offers Close, a shut frunk offers Open, nothing greyed out
    lucid = [cm.CATALOGUE[k] for k in ("trunk", "trunk_close", "frunk", "frunk_close")]
    both = t.render(cmd={**cmd, "more": lucid}, car={"vin": "V1", "make": "Lucid", "trunk_open": True, "frunk_open": False}, root="")
    keys = _re.findall(r'data-cmd="([a-z_]+)"', both)
    assert "trunk_close" in keys and "trunk" not in keys and "frunk" in keys and "frunk_close" not in keys and " disabled" not in both
    said = t.render(cmd={**cmd, "reason": "This car's connector takes no commands."}, car={"vin": "V1"}, root="")
    assert "takes no commands" in said and "data-cmd" not in said
    assert t.render(cmd=None, car={"vin": "V1"}, root="").strip() == ""


def test_a_hidden_password_step_stays_hidden():
    css = (Path(__file__).parent / "static" / "style.css").read_text()
    assert ".cmd-step[hidden] { display:none; }" in css


def test_a_pair_of_on_and_off_is_one_segmented_control():
    import commands as cm

    cmd = {"reason": None, "allowed": set(), "more": [cm.CATALOGUE[k] for k in ("defrost_on", "defrost_off", "sched_charge", "sched_charge_off")]}
    html = _env().from_string('{% import "_commands.html" as c %}{{ c.dock(cmd, car, root) }}').render(cmd=cmd, car={"vin": "V1"}, root="")
    assert '<span class="cmd-pair" role="group" aria-label="Defrost">' in html
    assert 'aria-label="Charge at"' not in html or '<span class="cmd-pair" role="group" aria-label="Charge at">' not in html, "a value and Off are not a pair"


def test_the_command_log_rows():
    from datetime import datetime, timezone

    rows = [
        {"at": datetime(2026, 10, 11, 21, 14, tzinfo=timezone.utc), "label": "Stop charging", "arg": None, "source": "hold", "ok": True, "detail": None},
        {"at": datetime(2026, 10, 11, 21, 15, tzinfo=timezone.utc), "label": "Charge limit", "arg": 85.0, "source": "site", "ok": False, "detail": "The car said no."},
    ]
    html = _env().get_template("_command_log.html").render(rows=rows)
    assert "Stop charging" in html and "Charge limit 85" in html and "sdot bad" in html and "The car said no." in html
    assert "No commands yet" in _env().get_template("_command_log.html").render(rows=[])


def _scene_path_points(d, steps=40):
    """Sample an absolute M/H/V/C path into points (the scene's paths use nothing else)."""
    import re as _re

    toks = _re.findall(r"[MHVC]|-?\d+(?:\.\d+)?", d)
    pts, i, cur, cmd = [], 0, (0.0, 0.0), None
    while i < len(toks):
        if toks[i] in "MHVC":
            cmd = toks[i]
            i += 1
        if cmd == "M":
            cur = (float(toks[i]), float(toks[i + 1]))
            i += 2
            pts.append(cur)
        elif cmd in "HV":
            v = float(toks[i])
            i += 1
            cur = (v, cur[1]) if cmd == "H" else (cur[0], v)
            pts.append(cur)
        else:
            c = [cur] + [(float(toks[i + k]), float(toks[i + k + 1])) for k in (0, 2, 4)]
            i += 6
            for s in range(1, steps + 1):
                t = s / steps
                w = ((1 - t) ** 3, 3 * t * (1 - t) ** 2, 3 * t * t * (1 - t), t**3)
                pts.append(tuple(sum(w[k] * c[k][j] for k in range(4)) for j in (0, 1)))
            cur = c[3]
    return pts


def test_activity_scene_is_drawn_true():
    """The banner's drawing: the far hills stay above the road and wrap without a step; the
    roadside stands on the road; a post ends where its sign or canopy begins; the wheels sit
    on the road line; the charging post stands on the road."""
    import re as _re

    web = Path(__file__).parent
    tpl = (web / "templates" / "_activity.html").read_text()
    css = (web / "static" / "style.css").read_text()
    road_top = float(_re.search(r"\.act-road \{[^}]*top:([\d.]+)px", css).group(1))
    road_h = float(_re.search(r"\.act-road \{[^}]*height:([\d.]+)px", css).group(1))

    far = _re.search(r'<pattern id="act-far" width="(\d+)".*?<path d="([^"]+)"[^>]*stroke-width="([\d.]+)"', tpl)
    width, hills, sw = float(far.group(1)), _scene_path_points(far.group(2)), float(far.group(3))
    assert max(y for _, y in hills) <= road_top - sw / 2 + 1e-6, "the hills never draw into or below the road"
    xs = [x for x, _ in hills]
    assert min(xs) < 0 and max(xs) > width, "the hills run past both edges of their tile"
    at = lambda x: min(hills, key=lambda p: abs(p[0] - x))[1]  # noqa: E731
    for x in (0, 5, 10):
        assert abs(at(x) - at(x + width)) < 0.05, "the hills wrap without a step"

    near = _re.search(r'<pattern id="act-near".*?</pattern>', tpl, _re.S).group(0)
    bases = [float(b) for b in _re.findall(r'd="M [\d.]+ ([\d.]+) [Vq]', near)]
    assert bases and all(road_top <= b <= road_top + road_h for b in bases), "everything by the road stands on it"
    sign = _re.search(r'd="M (\d+) [\d.]+ V ([\d.]+)"[^>]*/><rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"', near)
    assert float(sign.group(2)) == float(sign.group(4)) + float(sign.group(6)), "the sign's post ends at the sign's bottom edge"
    tree = _re.search(r'd="M (\d+) [\d.]+ V ([\d.]+)[^"]*"[^>]*/><circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"', near)
    assert float(tree.group(2)) == float(tree.group(4)) + float(tree.group(5)), "the trunk ends at the canopy's bottom"
    for x0, y0, dx, dy in _re.findall(r"M (\d+) ([\d.]+) l (-?[\d.]+) (-?[\d.]+)", near):
        ex, ey = float(x0) + float(dx), float(y0) + float(dy)
        dist = ((ex - float(tree.group(3))) ** 2 + (ey - float(tree.group(4))) ** 2) ** 0.5
        gap = dist - float(tree.group(5))
        assert abs(gap) < 0.05 or gap >= 3, "a twig meets the canopy's edge or stays well clear of it"
    assert _re.search(r"#act-near (circle|rect)[^{]*\{[^}]*fill:var\(--panel\)", css), "the canopy and the sign hide the hills behind them"

    # The car: 76 px wide over a 50 x 20 viewBox, its box's bottom `bottom` px above the scene's.
    scene_h = float(_re.search(r"\.act-scene \{[^}]*height:([\d.]+)px", css).group(1))
    car_bottom = float(_re.search(r"\.act-car \{[^}]*bottom:([\d.]+)px", css).group(1))
    vb = [float(v) for v in _re.search(r'class="act-body" viewBox="([^"]+)"', tpl).group(1).split()]
    k = float(_re.search(r"\.act-body \{[^}]*width:([\d.]+)px", css).group(1)) / vb[2]
    box_bottom = scene_h - car_bottom
    for cy, r in _re.findall(r'class="act-wheel" cx="[\d.]+" cy="([\d.]+)" r="([\d.]+)"', tpl):
        wheel_low = box_bottom - (vb[1] + vb[3] - float(cy) - float(r)) * k
        assert abs(wheel_low - (road_top + road_h / 2)) < 0.05, "the wheels' line runs along the road's"

    plug = _re.search(r'class="act-plug" viewBox="0 0 (\d+) (\d+)"', tpl)
    plug_h = float(plug.group(2))
    post = _re.search(r'class="act-post" d="M [\d.]+ ([\d.]+) V', tpl)
    lift = float(_re.search(r"\.act-plug \{[^}]*margin:0 0 (-?[\d.]+)px", css).group(1))
    assert box_bottom - lift - (plug_h - float(post.group(1))) == road_top, "the charging post stands on the road"


def test_cars_are_listed_by_name_whatever_their_source():
    import nowpage

    names = ["Delta", "C.H.A.R.", "b.r.a.v.o.", "Alpha"]
    assert sorted(names, key=nowpage.car_sort_key) == ["Alpha", "b.r.a.v.o.", "C.H.A.R.", "Delta"]


def test_the_strip_says_what_each_stretch_was_and_opens_it():
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)
    segs = [
        {"label": "driving", "width": 0.5, "start": t0, "end": t0 + timedelta(minutes=24)},
        {"label": "charging", "width": 0.3, "start": t0 + timedelta(hours=1), "end": t0 + timedelta(hours=2)},
        {"label": "sleeping", "width": 0.2, "start": t0 + timedelta(hours=2), "end": t0 + timedelta(hours=9)},
    ]
    marks = [{"drive": {"id": 5, "miles": 12.04, "to": "Work, Springfield"}}, {"charge": {"id": 9, "kwh": 17.4, "where": "Home"}}, {}]
    t = _env().from_string('{% import "_parts.html" as p %}{{ p.strip(segs, [], marks, "/cc", "V1", u) }}')
    html = t.render(segs=segs, marks=marks, u={"dist": "mi"})
    assert 'href="/cc/drives/5?vin=V1"' in html and "12.0 mi to Work" in html and 'data-tip="Driving · ' in html
    assert 'href="/cc/charges/9?vin=V1"' in html and "17.4 kWh at Home" in html
    assert 'href="/cc/day?date=' in html and "Asleep · " in html
    bare = _env().from_string('{% import "_parts.html" as p %}{{ p.strip(segs, []) }}').render(segs=segs)
    assert "<a " not in bare and 'role="img"' in bare, "without marks the strip is a picture, as on other pages"


def test_the_charge_heat_is_one_grouped_query_on_the_lists_own_rules(monkeypatch):
    app_module = _app(monkeypatch)
    sql = app_module.charge_heat_sql()
    assert "AS priced_kwh" in sql and "SUM(f.cost)" in sql, "a price per kWh over the kWh that were priced"
    assert sql.count("FROM charging_sessions") == 1 and "GROUP BY 1, 2, 3" in sql and "ANY(%s)" in sql and "cc_hidden" in sql, "every charge, grouped by car and spot, hidden ones left out"
    assert "%" not in sql.replace("%s", ""), "no stray placeholder"


def test_a_calendar_month_reads_its_own_level_runs_and_the_one_before(monkeypatch):
    app_module = _app(monkeypatch)
    sql = app_module.month_levels_sql()
    assert sql.count("LATERAL") == 2 and "ORDER BY l.first_date DESC LIMIT 1" in sql and "l.first_date >= %(a)s AND l.first_date < %(b)s" in sql, "both ranges on the index"
    assert "last_date >=" not in sql, "a bound on last_date alone would read every run before the month"


def test_a_parked_stretch_and_the_rows_that_open_it_render():
    import jinja2
    from datetime import datetime, timedelta, timezone

    sys.path.insert(0, str(Path(__file__).parent))
    import filters
    from parkedview import figures

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    t0 = datetime(2026, 10, 10, 17, 0, tzinfo=timezone.utc)
    car = {"name": "Alpha", "model": "Example EV", "updated_age": "just now", "capacity_kwh": 80.0, "vin": "V1", "live": True}
    base = {"root": "/cc", "now": t0, "car": car, "active": "", "vehicles": [{"vin": "V1", "name": "Alpha", "live": True}], "q": "?vin=V1", "colors": {"V1": 0}, "names": {"V1": "Alpha"}, "cur": "$"}
    end = t0 + timedelta(hours=4)
    f = figures(
        t0,
        end,
        [(t0, 77.0), (end - timedelta(minutes=5), 75.6)],
        [(t0, 240.0), (end - timedelta(minutes=5), 235.5)],
        [(t0, 70.0), (end - timedelta(minutes=5), 73.0)],
        [(t0, 69.0)],
        [],
        [(t0, end, (True, False))],
        0.25,
        0.4,
    )
    chart = {
        "t0": t0.timestamp(),
        "t1": end.timestamp(),
        "primary": {"key": "level", "label": "Battery", "unit": "%", "lo": 70, "hi": 80, "points": [[t0.timestamp(), 77.0]]},
        "extras": [],
        "lane": [],
        "events": [],
        "ticks": [],
    }
    near = {"prev": {"kind": "parked", "id": 1, "start": t0 - timedelta(hours=9), "end": t0 - timedelta(hours=1), "href": "/parked/1", "duration": timedelta(hours=8), "where": "Home"}, "next": None}
    rows = [{"at": t0, "level": 77.0, "rated": 240.0, "inside": 70.0, "outside": 69.0, "asleep": False}]
    html = env.get_template("parked.html").render(
        **base,
        start=t0,
        end=end,
        figs=f,
        where="Bravo Lake",
        where_ref={"name": "Bravo Lake", "lat": 39.8, "lon": -89.6, "new": False},
        lat=39.8,
        lon=-89.6,
        chart=chart,
        near=near,
        guard="Sentry",
        rates={"idle": 0.3, "sentry": 1.0, "climate": 2.0},
        samples=rows,
        every=1,
        readings=1,
    )
    assert "Parked at" in html and "Bravo Lake" in html and "4 h" in html and "4.5<small>mi</small>" in html and "$0.45" in html
    assert "Sentry on</span>" in html and "usually 1.00" in html and "with Sentry on" in html and "70→73 °F" in html and "69→69 °F" in html
    assert 'rel="prev"' in html and "Every reading" in html and 'id="pk-map"' not in html, "no map base, no strip"
    week = env.get_template("parked.html").render(
        **base, start=t0, end=t0 + timedelta(days=7), figs=f, where=None, where_ref=None, lat=None, lon=None, chart=chart, near=None, guard="Sentry", rates=None, samples=[], every=1, readings=0
    )
    assert "Sat Oct 17, " in week, "a week later on the same weekday still says which day it ended"
    html = env.get_template("_parts.html").module.gap_row("Parked 4 h at Bravo Lake", None, [{"name": "Bravo Lake", "lat": 39.8, "lon": -89.6, "new": False}], "/cc/parked/1791651780?vin=V1")
    assert 'class="row-gap go"' in html and 'href="/cc/parked/1791651780?vin=V1"' in html and 'class="pn"' in html, "the row opens the stretch; the place keeps its card"
    spark = {"d": "M0,20 L0.0,18.0 L100.0,2.0 L100,20 Z", "lo": 400.0, "hi": 700.0}
    html = env.get_template("_parts.html").module.row("/cc/drives/1", "9:00 AM", "Home → Lake", [], "8.3 mi", "", kind="drive", elev=spark)
    assert 'class="row-el"' in html and spark["d"] in html and "400–700 ft" in html


def test_a_charts_extra_lines_are_chips_with_no_label_inside_the_group():
    import jinja2

    sys.path.insert(0, str(Path(__file__).parent))
    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    data = {
        "t0": 0,
        "t1": 60,
        "primary": {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": [[0, 50]]},
        "extras": [{"key": "speed", "label": "Speed", "unit": " mph", "lo": 0, "hi": 80, "points": [[0, 10]]}],
        "lane": [],
        "events": [],
        "ticks": [],
    }
    html = env.from_string('{% import "_parts.html" as parts %}{{ parts.chart("x", data) }}').render(data=data)
    assert 'aria-label="Also show"' in html and ">Also show<" not in html, "the chips name themselves; the group's name is for screen readers"


def test_the_doors_tile_says_a_charge_port_left_open_only_once_the_cable_is_out():
    import jinja2

    sys.path.insert(0, str(Path(__file__).parent))
    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    env.globals.update(guard_name=lambda make: "Halo Secure", state_word=lambda s: ("Parked", "parked"))
    base = {"vin": "V1", "name": "Alpha", "status": "Idle", "locked": True, "live": True, "make": "Lucid"}
    render = lambda car: env.get_template("_now_tiles.html").render(car=car, cmd=None, root="", q="", vq="?", a=[], tread_last=None, u={"dist": "mi"})
    assert "charge port open" in render(dict(base, port_open=True, plugged=False))
    assert "charge port open" not in render(dict(base, port_open=True, plugged=True)), "open while plugged in is every charge"
    assert "charge port open" not in render(dict(base, port_open=None, plugged=False))


def test_the_charging_page_reads_the_efficiency_and_never_works_it_out(monkeypatch):
    """The saving against petrol needs every drive's efficiency, an aggregate over all the
    trips. A background thread keeps it (warm_efficiency); the Charging page only reads what
    it kept, and until there is something, leaves the column waiting."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    monkeypatch.setenv("REST_USER", "u")
    monkeypatch.setenv("REST_PASSWORD", "p")
    sys.path.insert(0, str(Path(__file__).parent))
    app_module = importlib.import_module("app")
    from datetime import datetime, timezone

    row = {"session_start_date": datetime(2026, 9, 12, 8, tzinfo=timezone.utc), "where": "Home", "vin": "ALPHA", "region": "US-IL", "kwh": 30.0, "cost": 5.0}
    monkeypatch.setattr(app_module, "pick", lambda request: {"vin": "ALPHA", "name": "Alpha"})
    monkeypatch.setattr(app_module, "charges", lambda vin, limit, since: [row])
    monkeypatch.setattr(app_module, "current_settings", lambda: {"gas_mpg": 30.0, "gas_price": 4.0})
    monkeypatch.setattr(app_module, "car_efficiency", lambda: pytest.fail("the page ran the all-drives aggregate"))
    monkeypatch.setattr(app_module, "_efficiency_load", lambda: pytest.fail("the page ran the all-drives aggregate"))
    monkeypatch.setattr(app_module, "page", lambda request, name, **kw: kw["s"])
    monkeypatch.setattr(app_module, "_cache", {})
    s = app_module.charging_page(None)
    assert s["saved_ready"] is False and s["places"][0]["saving"] is None, "nothing kept yet: the column waits"
    app_module._cache["car_efficiency"] = (datetime(2020, 1, 1, tzinfo=timezone.utc), {("ALPHA", None): 300.0})
    s = app_module.charging_page(None)
    assert s["saved_ready"] and s["places"][0]["saving"]["saved"] == pytest.approx(100 / 30 * 4.0 - 5.0), "an old figure is still read, never reloaded here"
    assert s["places"][0]["saving"]["note"] is None and s["saving_all"]["compared"] == 1


# ---- connector 0.4.0's fields on Now, Software and Battery

LUCID_040 = {
    "vin": "V1",
    "name": "Alpha",
    "make": "Lucid",
    "status": "Charging",
    "live": True,
    "locked": True,
    "plugged": True,
    "sentry": True,
    "charging_power_kw": 11.2,
    "charge_minutes_left": 95,
    "software": "2.9.1",
    "update_available": True,
}


def _tiles(car, cmd=None):
    return _env().get_template("_now_tiles.html").render(car=car, cmd=cmd, root="", q="", vq="?", a=[], tread_last=None, u={"dist": "mi"})


def test_a_charge_says_the_cars_own_kwh_and_when_it_will_be_full():
    from datetime import datetime, timezone

    full = datetime(2026, 10, 14, 1, 40, tzinfo=timezone.utc)  # 6:40 PM in Springfield's zone the filters default to
    html = _tiles(dict(LUCID_040, charge_session_kwh=18.42, charge_full_at=full))
    assert '<span title="Added this charge, by the car\'s meter">18.4 kWh</span> · <span title="In 1 h 35 min">full at 6:40 PM</span>' in html
    html = _tiles(dict(LUCID_040, charge_full_at=full))
    assert "kWh</span>" not in html and "full at 6:40 PM" in html, "no meter before 0.4.0 or after the charge"
    assert "full in 1 h 35 min" in _tiles(LUCID_040), "an estimate with no time to put to it"


def test_the_doors_tile_puts_someone_inside_and_the_alarm_first_and_the_key_fob_last():
    assert 'class="ft warn"' in _tiles(dict(LUCID_040, living_object="level-1", trunk_open=True)) and "someone left inside" in _tiles(dict(LUCID_040, living_object="level-1", trunk_open=True))
    assert "alarm: intrusion" in _tiles(dict(LUCID_040, alarm_status="intrusion"))
    assert "alarm warning" in _tiles(dict(LUCID_040, alarm_status="pre-alarm"))
    assert "alarm" not in _tiles(dict(LUCID_040, alarm_status="armed")).split("Doors")[1].split("</div>")[0]
    assert "trunk open" in _tiles(dict(LUCID_040, keyfob_low=True, trunk_open=True)), "the key fob waits behind anything open"
    assert "key fob battery low" in _tiles(dict(LUCID_040, keyfob_low=True))


def test_the_guard_tile_says_why_it_is_paused_and_when_it_has_no_drive():
    paused = _tiles(dict(LUCID_040, sentry_paused="usb-full"))
    assert '</svg>Halo Secure</span><span class="ft-b" data-say>Paused</span>' in paused.replace("\n    ", "") and '<span class="ft-s">USB full</span>' in paused
    out = _tiles(dict(LUCID_040, sentry_usb=False))
    assert '<span class="ft-s">no USB drive</span>' in out and 'class="ft warn"><span class="ft-h">' in out
    assert '<span class="ft-s">watching</span>' in _tiles(dict(LUCID_040, sentry_usb=None)), "the car's idle drive state says nothing"


def test_the_software_tile_names_the_waiting_version():
    html = _tiles(dict(LUCID_040, update_version="3.1.2", update_install_min=30))
    assert '<span title="About 30 min to install">3.1.2 waiting</span>' in html
    assert "update waiting" in _tiles(LUCID_040)


def test_a_car_without_the_new_keys_renders_exactly_as_one_with_them_unknown():
    """A Tesla, or connector 0.3.1: the keys are absent; a 0.4.0 car that has not said has them None."""
    new = (
        "charge_session_kwh",
        "charge_full_at",
        "alarm_status",
        "sentry_paused",
        "sentry_usb",
        "living_object",
        "occupant_warning",
        "keyfob_low",
        "window_heating",
        "battery_health",
        "update_version",
        "update_install_min",
        "update_notes",
        "update_notes_url",
        "elevation_raw",
    )
    for car in (dict(LUCID_040, make="Tesla", status="Idle"), dict(LUCID_040, update_available=None)):
        assert _tiles(car) == _tiles(dict(car, **dict.fromkeys(new)))
        hero = lambda c: _env().get_template("_now_hero.html").render(car=c, cmd=None, root="", q="", u={"dist": "mi"}, today={}, due=[], route=None, ident=None)
        assert hero(car) == hero(dict(car, **dict.fromkeys(new)))


def test_the_card_lists_what_is_wrong_with_the_cars_safety():
    hero = (
        _env()
        .get_template("_now_hero.html")
        .render(car=dict(LUCID_040, occupant_warning="stage-2", sentry_paused="usb-error", keyfob_low=True), cmd=None, root="", q="", u={"dist": "mi"}, today={}, due=[], route=None, ident=None)
    )
    assert '<p class="odd"><span class="warn">Someone left inside</span> · <span class="warn">Halo Secure paused: USB error</span> · <span class="warn">Key fob battery low</span></p>' in hero
    home = _env().get_template("_now_hero.html").render(car=dict(LUCID_040, sentry_paused="at-home"), cmd=None, root="", q="", u={"dist": "mi"}, today={}, due=[], route=None, ident=None)
    assert 'class="odd"' not in home, "paused at home is the owner's choice, not news"


def test_defrost_in_the_dock_says_whether_the_window_heaters_are_on():
    import commands as cm

    cmd = {"reason": None, "allowed": set(), "more": [cm.CATALOGUE[k] for k in ("defrost_on", "defrost_off")]}
    t = _env().from_string('{% import "_commands.html" as c %}{{ c.dock(cmd, car, root) }}')
    assert 'Defrost <small class="muted" title="Defrost and the rear window heater, as the car last said">on</small>' in t.render(cmd=cmd, car={"vin": "V1", "window_heating": True}, root="")
    assert ">off</small>" in t.render(cmd=cmd, car={"vin": "V1", "window_heating": False}, root="")
    assert '<span class="cmd-l">Defrost</span>' in t.render(cmd=cmd, car={"vin": "V1"}, root=""), "a Tesla's, or a car that has not said"


def _page_base(car):
    from datetime import datetime, timezone

    return {
        "root": "",
        "now": datetime(2026, 10, 14, 1, 0, tzinfo=timezone.utc),
        "car": car,
        "active": "",
        "vehicles": [{"vin": "V1", "name": "Alpha", "live": True}],
        "q": "",
        "colors": {"V1": 0},
        "names": {"V1": "Alpha"},
        "cur": "$",
    }


def test_the_software_page_shows_the_waiting_update_and_its_notes():
    from datetime import datetime, timezone

    import softwarepage

    rows = [{"id": 1, "version": "2.9.1", "seen_at": datetime(2026, 9, 1, tzinfo=timezone.utc), "until": None, "days": 43, "odometer": None, "by_hand": False}]
    live = dict(
        LUCID_040,
        update_version="3.1.2",
        update_install_min=30,
        update_notes="Halo Secure records to the USB drive.\n\nThe glass roof shades faster.",
        update_notes_url="https://example.com/notes/3.1.2",
    )
    block = {"vin": "V1", "name": "Alpha", "rows": rows, "waiting": softwarepage.waiting(live)}
    html = _env().get_template("software.html").render(**_page_base(LUCID_040), blocks=[block], add_cars=[], today=None)
    assert '<span class="swup-h"><b>3.1.2</b> waiting<small> · about 30 min to install</small></span>' in html
    assert "<li>Halo Secure records to the USB drive.</li><li>The glass roof shades faster.</li>" in html
    assert 'href="https://example.com/notes/3.1.2" target="_blank" rel="noopener noreferrer">Notes online</a>' in html
    gravity = dict(block, waiting=softwarepage.waiting(dict(live, update_notes=None, update_notes_url=None)))
    html = _env().get_template("software.html").render(**_page_base(LUCID_040), blocks=[gravity], add_cars=[], today=None)
    assert "<b>3.1.2</b> waiting" in html and "Release notes" not in html and "Notes online" not in html, "a Gravity's notes never come"
    html = _env().get_template("software.html").render(**_page_base(LUCID_040), blocks=[dict(block, waiting=None)], add_cars=[], today=None)
    assert "swup" not in html


def test_the_battery_page_shows_the_cars_health_figure_and_charts_it_only_once_it_moves(monkeypatch):
    from datetime import date, datetime, timezone

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    monkeypatch.setenv("REST_USER", "u")
    monkeypatch.setenv("REST_PASSWORD", "p")
    sys.path.insert(0, str(Path(__file__).parent))
    app_module = importlib.import_module("app")
    now = datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)
    flat = [(date(2026, 10, d), 100.0) for d in (11, 12, 13)]
    moved = flat + [(date(2026, 10, 14), 99.5)]
    assert app_module.health_block({"battery_health": None}, [], now) is None, "a car that never said one, a Tesla among them"
    assert app_module.health_block({"battery_health": 100.0}, flat, now) == {"now": 100.0, "chart": None, "days": 3}
    assert app_module.health_block({}, moved, now)["now"] == 99.5, "the last logged figure while the car is away"
    chart = app_module.health_block({"battery_health": 99.5}, moved, now)["chart"]
    assert chart["primary"]["key"] == "health" and len(chart["primary"]["points"]) == 4

    lv = {"t0": 0, "t1": 60, "primary": {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": [[0, 50]]}, "extras": [], "lane": [], "events": [], "ticks": []}
    block = {"car": dict(LUCID_040, battery_health=100.0), "chart": lv, "full": None, "tires": {}, "firsts": {}, "points": 2, "health": {"now": 100.0, "chart": None, "days": 3}}
    render = lambda b: _env().get_template("battery.html").render(**_page_base(LUCID_040), blocks=[b], packs={})
    html = render(block)
    assert '<span class="v">100</span><span class="d">the car\'s own figure</span>' in html and "Battery health," not in html
    html = render(dict(block, car=dict(block["car"], battery_warning=True), health={"now": 99.5, "chart": dict(lv, primary=dict(lv["primary"], key="health")), "days": 4}))
    assert '<span class="v warn">99.5</span><span class="d">the car\'s own figure · warning</span>' in html and "Battery health, 4 days" in html and 'id="health-V1"' in html


def test_live_vehicle_marks_a_driving_car_that_stopped_moving_as_offline(monkeypatch):
    from datetime import datetime, timedelta, timezone

    import app as app_module

    now = datetime.now(timezone.utc)
    car = {"vin": "V1", "status": "Driving", "updated": now - timedelta(seconds=30), "live": True}
    monkeypatch.setattr(app_module, "reported_vehicle", lambda vin=None: dict(car))
    monkeypatch.setattr(app_module, "latest_fixes", lambda: {"V1": now - timedelta(minutes=80)})
    gone = app_module.live_vehicle("V1")
    assert gone["offline"] is True and gone["last_seen"] == now - timedelta(minutes=80)
    monkeypatch.setattr(app_module, "latest_fixes", lambda: {"V1": now - timedelta(seconds=20)})
    assert app_module.live_vehicle("V1")["offline"] is False
    # a parked car never asks for the position
    monkeypatch.setattr(app_module, "reported_vehicle", lambda vin=None: dict(car, status="Idle"))
    monkeypatch.setattr(app_module, "latest_fixes", lambda: (_ for _ in ()).throw(AssertionError("read the positions")))
    assert app_module.live_vehicle("V1")["offline"] is False


def test_a_summarised_drive_uses_its_own_energy_and_drops_the_one_reading_twice():
    import app

    r = {"summary_kwh": 3.4, "level_start": 90.0, "level_end": 90.0}
    assert app.summary_kwh(r) == 3.4 and r["level_start"] is None and r["level_end"] is None, "not '90→90%'"
    kept = {"summary_kwh": 3.4, "level_start": 90.0, "level_end": 85.0}
    assert app.summary_kwh(kept) == 3.4 and kept["level_start"] == 90.0
    assert app.summary_kwh({"level_start": 90.0, "level_end": 80.0}) is None, "no summary: worked out from the levels as before"


def test_day_readings_without_the_connectors_table_is_an_empty_day_not_a_500(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    monkeypatch.setenv("REST_USER", "u")
    monkeypatch.setenv("REST_PASSWORD", "p")
    sys.path.insert(0, str(Path(__file__).parent))
    app = importlib.import_module("app")
    import psycopg

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=()):
            raise psycopg.errors.UndefinedTable("relation cc_raw does not exist")

    monkeypatch.setattr(app, "db", lambda: Conn())
    monkeypatch.setattr(app, "source_of", lambda car: "history")
    day = datetime(2026, 9, 1).date()
    assert app.day_readings({"vin": "ALPHA", "name": "Alpha"}, day) == ("history", ["at"], [])


def test_readings_links_need_a_car_with_raw_readings(monkeypatch):
    """The Day page's Readings button and Settings > Data's row open /data, which has rows only for a
    TeslaFi-feed car or a connector car; an import-only or TeslaMate car gets neither link."""
    import app

    monkeypatch.setattr(app, "source_of", lambda car: car["src"])
    assert app.has_raw_readings({"src": "feed"}) and app.has_raw_readings({"src": "live"})
    assert not any(app.has_raw_readings({"src": k}) for k in ("history", "teslamate", "tesla"))
    assert not app.has_raw_readings({})
    day = (Path(__file__).parent / "templates" / "day.html").read_text()
    assert "not car.garage and raw_readings %}<a class=\"btn sm\" href=\"{{ root }}/data?date" in day


def test_the_clock_script_loads_before_the_scripts_that_use_it_and_the_time_boxes_are_typed():
    base = (Path(__file__).parent / "templates" / "base.html").read_text()
    assert base.index("static/clockfmt.js") < base.index("static/chart.js"), "CCClock exists before the chart reads it"
    macro = (Path(__file__).parent / "templates" / "_settings.html").read_text()
    assert 'type="time"' not in macro and "|tod" in macro, "a native time box follows the browser's locale, not the setting"
    assert 'type="time"' not in (Path(__file__).parent / "templates" / "_commands.html").read_text()


def test_round_four_renders_plurals_sizes_placeholders_and_a_full_width_address_box():
    """The walkthrough's polish, in the rendered HTML: one charge, 50 KB, a 24-hour placeholder, the stopped card, a place with no visits."""
    import filters

    env = _env()
    filters.register(env, clock="24")
    try:
        _round_four(env, filters)
    finally:
        filters.register(env, clock="12")  # the clock is process-wide; later tests expect the default


def _round_four(env, filters):
    imp = env.get_template("_import.html").render(
        imp={
            "job": {"state": "done", "kind": "teslamate", "step": "", "pct": 100, "error": None, "places": 1, "result": {"drives": 4, "charges": 1, "days": 1}, "items": []},
            "staged": [],
            "busy": False,
            "max_mb": 1024,
            "notice": None,
        },
        root="",
        csrf="t",
        settings={"maps_offer": None},
    )
    assert "<b>1</b> charge " in imp and "1 charges" not in imp and "<b>4</b> drives" in imp and "1 place found" in imp and 'id="maps-offer"' not in imp
    assert env.get_template("_settings.html").module.time("quiet", "07:00", "Quiet") and 'placeholder="21:00"' in env.get_template("_settings.html").module.time("quiet", None, "Quiet")
    assert filters.filesize(51_200) == "50 KB" and filters.plural(0, "visit") == "visits" and filters.plural(1, "visit") == "visit"
    alerts = (Path(__file__).parent / "templates" / "settings_alerts.html").read_text()
    assert 'class="set-row under full"' in alerts and 'class="field chan-f wide"' in alerts


def test_a_driving_banner_names_where_it_is_going_first():
    """ "Comet driving to Work from Home": the destination is the news, the origin its context."""
    env = _env()
    html = env.get_template("_activity.html").render(acts=[{"vin": "V1", "name": "Comet", "kind": "driving", "origin": "Home", "dest": "Work"}], act_sig="x", act_link=False, root="", colors={})
    import re

    text = " ".join(re.sub(r"<[^>]+>", " ", html.split('class="act-t"')[1].split("</span></span>")[0]).split())
    assert "driving to Work from Home" in text, text


def test_a_banner_heading_home_drops_the_to():
    import re

    env = _env()
    html = env.get_template("_activity.html").render(
        acts=[{"vin": "V1", "name": "Comet", "kind": "driving", "origin": "Work", "dest": "Home", "dest_home": True}], act_sig="x", act_link=False, root="", colors={}
    )
    text = " ".join(re.sub(r"<[^>]+>", " ", html.split('class="act-t"')[1].split("</span></span>")[0]).split())
    assert "driving home from Work" in text and "driving to" not in text, text


def test_heading_says_home_for_the_home_place_and_to_for_the_rest():
    import activity

    homes = ["Cabin"]
    assert activity.heading("Home", homes) == "home" and activity.heading("cabin", homes) == "home" and activity.heading("Work", homes) == "to Work" and activity.heading(None, homes) == ""
    out = activity.banner({"vin": "V1", "name": "Comet", "status": "Driving"}, "driving", None, datetime(2026, 10, 6, tzinfo=timezone.utc), route={"name": "Cabin", "minutes": 4}, home_names=homes)
    assert out["dest_home"] is True
    assert _env().from_string("{{ heading('Work') }}").render() == "to Work", "the default, until the app knows which place is home"


def test_the_car_on_and_parked_shows_on_the_strip_the_chart_key_and_the_parked_page():
    from datetime import datetime, timedelta, timezone

    from parkedview import figures

    t0 = datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc)
    segs = [{"label": "idle", "width": 0.5, "start": t0, "end": t0 + timedelta(minutes=38)}, {"label": "caron", "width": 0.5, "start": t0 + timedelta(minutes=38), "end": t0 + timedelta(minutes=87)}]
    html = _env().from_string('{% import "_parts.html" as p %}{{ p.strip(segs, [], marks, "/cc", "V1", u) }}').render(segs=segs, marks=[{}, {}], u={"dist": "mi"})
    assert 'class="seg caron"' in html and "Car on, parked · " in html
    chart = {
        "t0": t0.timestamp(),
        "t1": (t0 + timedelta(hours=2)).timestamp(),
        "primary": {"key": "level", "label": "Battery", "unit": "%", "lo": 70, "hi": 80, "points": [[t0.timestamp(), 77.0]]},
        "extras": [],
        "lane": [[t0.timestamp(), t0.timestamp() + 600, "parked"], [t0.timestamp() + 600, t0.timestamp() + 3500, "caron"]],
        "events": [],
        "ticks": [],
    }
    key = _env().from_string('{% import "_parts.html" as p %}{{ p.chart("c1", chart) }}').render(chart=chart, root="/cc", u={"dist": "mi"})
    assert '<i class="k-caron"></i>Car on, parked' in key
    for page in ("_now_window", "garage", "sleep"):
        assert 'class="key caron"></span>Car on, parked' in (Path(__file__).parent / "templates" / f"{page}.html").read_text()
    assert "--caron:" in (Path(__file__).parent / "static" / "style.css").read_text() and "caron:" in (Path(__file__).parent / "static" / "chart.js").read_text()
    # the parked page: the car-on time in the summary line and the figures grid
    import filters
    import jinja2

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    car = {"name": "Alpha", "model": "Example EV", "updated_age": "just now", "capacity_kwh": 80.0, "vin": "V1", "live": True}
    base = {"root": "/cc", "now": t0, "car": car, "active": "", "vehicles": [{"vin": "V1", "name": "Alpha", "live": True}], "q": "?vin=V1", "colors": {"V1": 0}, "names": {"V1": "Alpha"}, "cur": "$"}
    end = t0 + timedelta(hours=2)
    on = [(t0 + timedelta(minutes=38), t0 + timedelta(minutes=87), "V1")]
    for car_on, shows in ((on, True), ((), False)):
        f = figures(t0, end, [(t0, 77.0)], [], [], [], [], [], None, None, car_on)
        page = env.get_template("parked.html").render(
            **base, start=t0, end=end, figs=f, where="Bravo Lake", where_ref=None, lat=None, lon=None, chart=chart, near=None, guard="Sentry", rates=None, samples=[], every=1, readings=0
        )
        assert ("car on 49 min" in page and "<dt>Car on</dt><dd>49 min" in page) is shows


def test_a_route_page_names_both_ends_and_the_drives_filter_says_them_in_words(monkeypatch):
    import jinja2
    from datetime import datetime, timedelta, timezone

    import daypage
    import filters

    app_module = _app(monkeypatch)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")), autoescape=True)
    filters.register(env)
    t0 = datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc)
    base = {
        "root": "/cc",
        "now": t0,
        "car": {"vin": "V1", "name": "Alpha", "live": True},
        "active": "places",
        "q": "?vin=V1",
        "colors": {"V1": 0},
        "names": {"V1": "Alpha"},
        "cur": "$",
        "vehicles": [],
    }
    home = {"id": 1, "name": "Home", "auto": False, "latitude": 40.71, "longitude": -74.0, "address": None, "street": None, "user_address": "12 Elm Street, Springfield", "city": "Springfield"}
    found = {"id": 7, "name": "Maple Street, Springfield", "auto": True, "latitude": 40.72, "longitude": -74.01, "address": None, "street": None, "user_address": None, "city": "Springfield"}
    cells = {}
    ends = [app_module.route_end(found, cells), app_module.route_end(home, cells)]
    assert ends[0]["unnamed"] and ends[0]["street"] == "Maple Street, Springfield" and not ends[1]["unnamed"]
    route = {
        "key": "1-7",
        "a": found,
        "b": home,
        "loop": False,
        "n": 14,
        "ids": [1],
        "tag": None,
        "time": timedelta(minutes=11),
        "kwh": 1.8,
        "wh_per_mi": 477.0,
        "first": t0,
        "last": t0,
        "ways": [("Home", "Maple Street, Springfield", 7), ("Maple Street, Springfield", "Home", 7)],
    }
    trip = {
        "id": 1,
        "vin": "V1",
        "start_date": t0,
        "destination_date": t0 + timedelta(minutes=11),
        "from": "Home",
        "to": "Maple Street, Springfield",
        "miles": 4.0,
        "duration": timedelta(minutes=11),
        "kwh_used": 1.8,
        "wh_per_mi": 450.0,
        "cost": None,
    }
    html = env.get_template("route.html").render(**base, r=route, span="all", ends=ends, drives=[trip], maps_on=True)
    h1 = re.sub(r"<[^>]+>", "", html.split('class="rte-h">')[1].split("</h1>")[0])
    assert " ⇄ " in h1, "the title copied into the phone bar keeps its spaces round the arrow"
    assert html.count('class="map rte-end-map"') == len([e for e in ends if e.get("lat") is not None]) >= 1, "each end has its own close-up to name it by"
    assert (
        "Unnamed place" in html and "Maple Street, Springfield" in html and ">Name it<" in html and 'data-lat="40.72"' in html and 'data-lon="-74.01"' in html
    ), "the unnamed end says so, where it is, and how to name it"
    assert 'href="/cc/places/1?vin=V1"' in html and "Springfield" in html
    assert 'id="rte-map"' in html and "/cc/places/routes/1-7/lines?span=all&vin=V1" in html and "CCMap.route" in html, "a map of the drives, drawn after the page"
    assert "7 → Maple Street, Springfield" in html and "11 min" in html and "1.8 kWh" in html and "477" in html and 'href="/cc/drives/1?vin=V1"' in html
    assert "/cc/drives?at=%237&dest=%231&both=on&from=2026-09-04&amp;vin=V1" in html, "See all in Drives goes by the places' ids"
    assert re.findall(r"<p[ >]", html.split('class="rte-ends"')[1].split("</section>")[0]) == ['<p '], "no paragraph of prose beyond the See all button's line"
    # the Drives filter: an unnamed end is an option of its own, selected, and said in words
    window = {"start": t0, "end": t0, "label": "September 2026", "ym": "2026-09", "prev": "2026-08", "prev_label": "Aug 2026", "next": "2026-10", "next_label": "Oct 2026", "is_current": True}
    every = [home, found]
    assert app_module.end_filter("#7", every) == (None, 7) and app_module.end_filter("#99", every) == (None, -1) and app_module.end_filter("Home", every) == ("Home", None)
    ends_ctx = app_module.filter_ends("#7", "Home", every, [home])
    page = dict(base, drives=[trip], days=daypage.by_day([trip], timezone.utc, "start_date"), window=window, sort="start", desc=True, sort_base="?", named_places=[home], ends=ends_ctx)
    html = env.get_template("drives.html").render(**page, flt={"raw": {"at": "#7", "dest": "Home", "both": "on"}})
    assert '<option value="#7" selected>Maple Street, Springfield</option>' in html and "Filter · Maple Street, Springfield ⇄ Home" in html and 'class="fsheet" open' in html
    html = env.get_template("drives.html").render(**page, flt={"raw": {"at": "#7", "dest": "Home", "min": "2"}})
    assert "Maple Street, Springfield → Home + 1" in html


def test_the_filter_panel_stacks_to_one_column_on_a_phone():
    css = (Path(__file__).parent / "static" / "style.css").read_text()
    assert ".fsheet form.range { grid-template-columns:minmax(0, 1fr); }" in css and "max-width: 600px" in css.split(".fsheet form.range { grid-template-columns")[0].rsplit("@media", 1)[1]
