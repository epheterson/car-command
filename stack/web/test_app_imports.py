"""The app must import with only environment configuration; every page route must be mounted at / and /cc."""

import importlib
from datetime import datetime, timezone
from types import SimpleNamespace

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
    missing = modules - copied
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
    assert "Elm Street" in html and ">38<" in html and "401" in html and 'id="map"' in html and ">71<" in html
    assert 'class="pn pn-new" type="button" aria-haspopup="dialog" data-lat="40.76" data-lon="-73.99"' in html, "the unnamed end opens the naming card, marked as wanting a name"
    assert 'class="pn" type="button" aria-haspopup="dialog" data-lat="40.71"' in html, "the named start opens it too, to rename it"
    assert "68→66<small>°F</small>" in html, "outside temperature at both ends of the drive"
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
    assert 'Navigated to <b>Bravo Market</b>' in html and html.index('id="map"') < html.index("Navigated to"), "under the route"
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
    assert "160<small>mi</small>" in html and "of 49.1 kWh" in html and "96<small>%</small>" in html and "240<small>V</small>" in html
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
    window = {"start": t0, "end": t0, "label": "September 2026", "ym": "2026-09", "prev": "2026-08", "prev_label": "Aug 2026", "next": "2026-10", "next_label": "Oct 2026", "is_current": True}
    import daypage

    html = env.get_template("drives.html").render(
        **base, drives=[trip], days=daypage.by_day([trip], timezone.utc, "start_date"), window=window, sort="start", desc=True, sort_base="?", flt={"raw": {}}
    )
    assert "September 2026" in html and "month=2026-08" in html and "month=2026-10" not in html and "Elm Street" in html
    assert html.count('class="row-b') == 1 and 'class="day-h"' in html and 'class="fsheet"' in html and "<table" not in html
    assert "sort=miles" in html and "drives.csv" in html and "show=hidden" in html
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
    html = env.get_template("calendar.html").render(**base, weeks=weeks, window=window, totals={"drives": 3, "miles": 37.8, "charges": 1, "kwh": 30.0}, max_miles=12.6, center=[40.71, -74.0])
    assert 'href="/cc/calendar?year=2026&vin=V1">Year</a>' in html and html.index('class="cal"') < html.index('<div class="tiles'), "Month and Year in one control; the grid first"
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
        **base, year=2026, months=months, totals={"drives": 3, "miles": 37.8, "charges": 1, "kwh": 30.0, "days_driven": 2}, best=None, max_miles=12.6, this_year=2026
    )
    assert 'href="/cc/calendar?month=2026-09&vin=V1">September' in yhtml and ">January<" not in yhtml and ">October<" not in yhtml, "the months from the first logged one up to this one"
    assert 'href="/cc/calendar?year=2025&vin=V1" aria-label="2025">‹' in yhtml and 'aria-label="2027"' not in yhtml
    assert yhtml.count("ym-day busy") == 3 and yhtml.count("charged") >= 1, "three days drove, one charged"
    assert 'class="pbar"' in yhtml and 'class="tiles pair"' in yhtml and "seg-links" not in yhtml and ">Month</a>" in yhtml
    assert "30 kWh" in html and "13 mi" in html and html.count('class="cal-day') == 7 and "date=2026-09-04" in html
    assert 'class="pbar"' in html and 'class="tiles"' in html and "seg-links" not in html
    mwin = {"start": t0, "end": t0, "label": "September 2026", "ym": "2026-09", "prev": "2026-08", "prev_label": "Aug 2026", "next": "2026-10", "next_label": "Oct 2026", "is_current": True}
    html = env.get_template("map.html").render(**base, positions=336598, since=t0, center=[40.71, -74.0], window=mwin, pins=[])
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
        "Parked 1 h 21 min at <span class=\"pl\">" in html and "Home</span> · lost 15.0%" in html and 'class="row-gap"><span class="dot c' in html
    ), "parked time is the thin line, with the car's dot in the garage"
    assert (
        'href="/cc/software?vin=all"' in html and 'class="route"' in html and 'class="dot c1"' in html
    ), "the update links to Software; the route is the drive's thumbnail; the car's dot leads in the garage"
    assert "All cars</a>" not in html.split('class="stage"')[1], "the shell switches car, not the page"
    assert 'class="day-h"' in html and 'class="segs2"' not in html and 'aria-label="Aug 2026"' in html and 'class="tday"' in html
    html = env.get_template("timeline.html").render(
        **garage, days=[dict(days[0], miles=0, kwh=0.1)], window=window, kind="all", kinds=("all", "drives", "charges", "software"), counts={"drives": 1, "charges": 1, "software": 1}
    )
    assert "0.1 kWh</span></h2>" in html, "a small charge is not 0 kWh in the day header"
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
    assert html.count('class="row-b') == 2 and 'class="row-gap"' in html, "drives and charges are rows; parked time a thin line"
    assert 'type="date"' in html and "data-autosubmit" in html, "a date picker in the bar"
    assert 'class="week"' not in html and 'class="tiles pair"' in html and "Since the last charge" in html
    assert "Phantom drain" in html and "Battery used" in html and "show=parked" in html and "29% of rated" in html
    assert "Home</span> → the office" in html and "Asleep 8 h · lost 1.0%" in html and "257 Wh/mi" in html and "60→82%" in html and "$8.00" in html
    assert "68°F out" in html and "until 9:42 AM" in html and "avg 38 mph" in html, "the Day row carries the second tier too"
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
        "places": [{"where": "Home", "charges": 3, "kwh": 90.0, "kwh_per_charge": 30.0, "mi_per_kwh": 3.4, "cost": 27.0, "rate": 0.3, "dc": 0}],
        "ends": [{"level": 80, "charges": 3, "share": 1.0}],
        "superchargers": [{"where": "Shelbyville Supercharger", "charges": 2, "kwh": 60.0, "cost": 24.0, "minutes": 50.0}],
        "total_kwh": 150.0,
        "total_charges": 5,
        "total_cost": 51.0,
        "tou": {"periods": []},
    }
    html = env.get_template("charging.html").render(**garage, s=summary, days=90)
    assert 'class="pbar"' in html and 'aria-label="Period"' in html and 'class="tiles pair"' in html and "150<small>kWh</small>" in html and "Home" in html and "seg-links" not in html
    assert 'href="/cc/charging?days=90&vin=all" aria-current="page"' in html and "Shelbyville Supercharger" in html
    assert "Most energy" in html and "Most used" not in html, "the tile ranks places by kWh"
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
    assert 'class="chartbox"' in html and "Alpha" in html and 'class="tiles pair"' in html and "72<small>%</small>" in html and 'class="chart-key"' not in html
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
    assert 'class="pbar"' in html and ">1 place<" in html and 'href="/cc/places/new"' in html and "Find a place" in html
    assert html.index('id="pl-named"') < html.index('id="pl-need"'), "the named places first, then the found ones to name"
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
        {"id": 1, "version": "2026.26.6", "seen_at": t0, "until": None, "days": 12, "odometer": 1234.0, "by_hand": False},
        {"id": 2, "version": "2026.20.1", "seen_at": t0 - timedelta(days=30), "until": t0, "days": 30, "odometer": 1000.0, "by_hand": True},
    ]
    html = env.get_template("software.html").render(**base, blocks=[{"vin": "V1", "name": "Alpha", "rows": sw}], add_cars=[{"vin": "V1", "name": "Alpha"}], today=t0.date())
    assert 'class="pbar"' in html and 'class="tiles t3"' in html and "2026.26.6" in html and "/software/add" in html and "/software/2/delete" in html and "30 days" in html
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
    html = env.get_template("settings_look.html").render(**sec, section="look", label="Units and look", tz="Europe/London")
    assert "Europe/London" in html and 'name="units"' in html and 'name="currency" value="€"' in html
    html = env.get_template("settings_data.html").render(**sec, section="data", label="Data", data_checks=checks)
    assert "115 of the 115 mi" in html and "not checked yet" in html and "115 mi on the odometer, 0 in drives" in html
    assert 'name="weather_shown"' in html and 'name="min_drive_mi"' in html and "/drives.csv" in html
    html = env.get_template("settings_alerts.html").render(
        **sec,
        section="alerts",
        label="Alerts",
        channels=["discord", "pushover"],
        routes={"parked": {"pushover"}, "login": set()},
        alert_kinds=__import__("notify").KINDS,
        alert_groups=__import__("notify").GROUPS,
    )
    assert 'name="route_parked_pushover" id="route_parked_pushover" aria-label="Parked on Pushover" checked>' in html
    assert 'name="route_parked_discord" id="route_parked_discord" aria-label="Parked on Discord">' in html
    assert 'name="route_login_discord" id="route_login_discord" aria-label="Sign-in on Discord">' in html and 'aria-label="Set off on Discord" checked>' in html
    assert 'name="routes_shown"' in html
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
    assert "from a sign-in file" in html and "secrets/lucid-token.json" in html and "kept in the app" in html and "still read from an older token setting" in html
    for k in ("tesla", "teslafi", "teslamate", "lucid"):
        assert f'id="add-{k}"' in html and f'id="pane-{k}"' in html
    assert html.index('id="pane-teslafi"') < html.index('name="token"') < html.index('id="pane-teslamate"') < html.index('name="url"') < html.index('id="pane-lucid"') < html.index('name="password"')
    html = env.get_template("settings_access.html").render(**dict(sec, saved=False), section="access", label="Sign-in and API", tokens=[], auth_enabled=True)
    assert "Mint a token" in html and "Sign-in is on" in html
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
    """/api/now?vin=<the Tesla> answered 500 for weeks.

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
    assert app_module.templates.env.filters["local"](datetime(2026, 1, 1, 12, tzinfo=timezone.utc)).endswith("12:00 PM")
    app_module.use_timezone(None)
    assert app_module.TZ.key == "America/Los_Angeles"
    assert app_module.templates.env.filters["local"](datetime(2026, 1, 1, 12, tzinfo=timezone.utc)).endswith("4:00 AM")


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
    app_module._cache["connector_down"] = (app_module.datetime.now(app_module.timezone.utc), "CarConnectivity is not answering (ConnectError).")
    assert app_module.connector_down() == app_module.NO_CONNECTOR
    app_module._cache["connector_seen"] = True
    assert "not answering" in app_module.connector_down()
    app_module._cache.pop("connector_down", None)
    app_module._cache.pop("connector_seen", None)


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
    est = env.get_template("now.html").render(**dict(ctx, drives=[dict(drive, cost_estimate=True)], charges=[dict(charge, cost_source="average")]), today=None)
    assert "≈$0.46" in est and "≈$5.20" in est and "≈" not in html, "an estimated cost is marked on Now's rows, a real one is not"
    assert html.count('class="row-b') == 2, "recent drives and charges are one-line rows"
    assert 'class="gauge"' in html and "limit 90%" in html and "</i>Charging · 7.2 kW</span>" in html
    assert "1 drive today · 3 mi" in html and "charged 17 kWh today" in html and "route" in html and "$0.46" in html and "17.4 kWh" in html
    quiet = env.get_template("now.html").render(**dict(ctx, drives=[], charges=[]), today=None)
    assert "no drives today" in quiet and "No drives yet" in quiet


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
    """The route referenced a module name app.py never imported and answered 500 in
    production (2026-09-30); the template tests could not see it. Run the route itself."""
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
    ctx = dict(base, st={"public_tiles": True, "public_geocoder": False}, conv={}, saved=False, section="maps", label="Maps", regions=[], wait_until_dt=None, maps_on=False, status=None)
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
    assert "Not running" in off


def test_settings_maps_public_switches_stay_editable_with_local_maps_on():
    """Local map backgrounds do not exist yet and the public geocoder still names stops
    outside the regions, so both switches are editable whether or not local maps run."""
    for maps_on in (False, True):
        html = _maps_page(status=_maps_status("idle") if maps_on else None, maps_on=maps_on)
        assert "disabled" not in html and "Not needed" not in html
        assert 'name="public_tiles_shown"' in html and 'name="public_geocoder_shown"' in html
        assert "Map backgrounds from OpenFreeMap" in html and "OpenFreeMap sees which area you view" in html
        assert "Off: maps draw routes and places on a plain background. Local backgrounds aren't built yet." in html
        assert ("Turn on local maps" in html) is (not maps_on) and "Just this region" not in html
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
    assert html.count('class="guard"') == 1 and 'title="Halo Secure on"' in html, "the guard dot is on Bravo's card, named for its make"
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
    assert one.count('class="guard"') == 1
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
    """Eric 2026-10-09: "Always start the site on all cars / now". No ?vin, two cars: the garage.
    One car, or a car asked for by name, keeps its own page."""
    app_module = _app(monkeypatch)
    picked = []
    monkeypatch.setattr(app_module, "pick", lambda request: picked.append(1) or {"vin": "V1", "name": "Alpha", "empty": True})
    seen = {}
    monkeypatch.setattr(app_module, "page", lambda request, name, **ctx: seen.update(name=name, car=ctx.get("car")) or "ok")
    monkeypatch.setattr(app_module, "garage", lambda: {})
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
