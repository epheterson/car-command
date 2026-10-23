"""The analysis pages as rendered: the records board and efficiency compare-with on Stats,
the battery's lifetime report, the Software page's figures per version. Each renders from
plausible rows, and each says what it rests on in a label or an (i), never a paragraph."""

from datetime import date, datetime, timedelta, timezone

import effchart
import effcompare
import lifetime
import softwarepage
from test_stats_render import NOW, context, env, text  # noqa: F401 - fixtures
from units import Units

T0 = datetime(2025, 3, 1, 17, 0, tzinfo=timezone.utc)


def drive(i, **kw):
    d = {"id": i, "vin": "V1", "start_date": T0 + timedelta(days=i), "destination_date": T0 + timedelta(days=i, hours=1), "from": "Home", "to": "Lake", "miles": 38.2}
    d.update(duration=timedelta(minutes=67), level_start=86.0, level_end=61.0, wh_per_mi=322.0, kwh_used=12.3, max_mph=76.0, temp_f=31.0, tag="Sunday drive")
    d.update(kw)
    return d


def test_the_records_board_draws_each_record_as_its_row(env):  # noqa: F811 - the fixture
    board = [
        {"key": "distance", "label": "Longest drive", "value": 38.2, "fmt": "dist", "kind": "drive", "row": drive(1), "note": None, "thumb": '<svg class="route"></svg>'},
        {"key": "high", "label": "Highest point", "value": 689.0, "fmt": "elev", "kind": "drive", "row": drive(2), "note": None},
        {
            "key": "peak_kw",
            "label": "Fastest charge",
            "value": 150.0,
            "fmt": "kw",
            "kind": "charge",
            "row": {"id": 9, "vin": "V1", "session_start_date": T0, "where": "Lake", "kwh": 12.5, "charging_type": "DC"},
            "note": None,
        },
        {"key": "best_wh", "label": "Most efficient", "value": 249.0, "fmt": "econ", "kind": "drive", "row": drive(3, **{"from": "Grocery"}), "note": 10.0},
    ]
    import records

    groups = records.fold(board)
    groups[0]["thumb"] = '<svg class="route"></svg>'
    html = env.get_template("stats.html").render(**context(), records=groups)
    sec = html[html.index('id="records"') :]
    assert "Longest drive" in sec and "38.2<small>mi</small>" in sec and '<svg class="route">' in sec and 'href="/drives/1?vin=V1"' in sec
    lead = sec[: sec.index('class="rows reclist"')]
    assert lead.count('class="rec rec-') == 2 and "1 h 7 min" in lead, "two leads; the longest drive's row says its time, not its distance again"
    assert 'class="recl"' in sec and "Highest point" in sec[sec.index("reclist") :], "the rest are one line each"
    assert "689<small>ft</small>" in sec and "150<small>kW</small>" in sec and 'href="/charges/9?vin=V1"' in sec
    assert "over 10 mi" in sec and "249<small>Wh/mi</small>" in sec, "the efficiency record says how long a drive had to be"
    lines = {seg.split("</a>")[0].count('class="pl-slot"'): seg for seg in sec.split('<a class="recl"')[1:]}
    assert "Grocery" in lines[1] and "Highest point" in lines[0], "a route with no glyph first takes an empty slot, so every route starts at one x"
    assert 'id="records"' not in env.get_template("stats.html").render(**context(), records=[])


def test_compare_with_draws_the_second_set_beside_the_first(env):  # noqa: F811 - the fixture
    a = [{"lo": 40, "hi": 50, "drives": 12, "miles": 120.0, "wh_per_mi": 300.0, "share": 1.0}, {"lo": 50, "hi": 60, "drives": 8, "miles": 80.0, "wh_per_mi": 270.0, "share": 0.9}]
    b = [{"lo": 40, "hi": 50, "drives": 5, "miles": 50.0, "wh_per_mi": 330.0}]
    eff = {
        "key": "tires:Winter",
        "choices": effcompare.choices(["Winter"], ["Commute"], True),
        "names": ("Other drives", "Winter tires"),
        "temp": effcompare.pair(a, b),
        "speed": [],
        "matched": effcompare.matched(effcompare.pair(a, b)),
        "a": {"wh_per_mi": 290.0, "drives": 20, "miles": 200.0},
        "b": {"wh_per_mi": 330.0, "drives": 5, "miles": 50.0},
    }
    ctx = dict(context(), bins={"temp": a, "speed": []}, eff=eff, eff_keep=[("period", "all")])
    html = env.get_template("stats.html").render(**ctx)
    sec = html[html.index('id="efficiency"') :]
    assert '<option value="tires:Winter" selected>Winter tires</option>' in sec and 'name="period" value="all"' in sec and "data-autosubmit" in sec
    assert "+10<small>%</small>" in sec and "Winter tires against" in text(sec) and "at the same temperatures · 330 vs 300 Wh/mi" in text(sec)
    assert "Pooled across all temperatures, 330 vs 290 Wh/mi" in text(sec), "the pooled figure is one tap away, not the headline"
    assert 'class="bins pair"' in sec and "+30" in sec and 'class="pside pb"' in sec and 'class="few"' in sec, "five winter drives against twelve: quiet"
    plain = env.get_template("stats.html").render(**dict(ctx, eff=dict(eff, key="", names=None)))
    assert 'class="bins pair"' not in plain and 'class="bins"' in plain and "Compare with" in plain, "nothing chosen: the plain tables and the control"
    # the chart over the tables: miles as bars, Wh a mile as the line, the compare drawn beside it
    fb = effchart.bands(False)
    band = lambda t: next(x for x in fb if x[2] == t)[:2]  # noqa: E731
    side = lambda wh: [{"lo": band(t)[0], "hi": band(t)[1], "drives": 6, "miles": 40.0 + t, "wh_per_mi": wh - t} for t in (35, 40, 45)]  # noqa: E731
    html = env.get_template("stats.html").render(**dict(ctx, eff=dict(eff, chart=effchart.chart([side(330.0), side(350.0)], fb))))
    fig = html[html.index('<figure class="effc') : html.index("</figure>", html.index('<figure class="effc'))]
    assert 'class="effc cmp2"' in fig and fig.count('class="effc-bar s1"') == 3 and fig.count('class="effc-l s') == 2, "two sides, bars beside each other, a line each"
    assert "Winter tires: 85 mi · 305 Wh/mi · 6 drives" in fig and "Other drives" in fig and ">35<" in fig and "°F" in fig, "each band names both sides on hover"
    one = env.get_template("stats.html").render(**dict(ctx, eff=dict(eff, key="", names=None, chart=effchart.chart([side(330.0)], fb))))
    assert 'class="effc"' in one and "Wh/mi</span>" in one and "85 mi · 285 Wh/mi" in one


def test_the_lifetime_report_leads_with_the_loss_and_draws_the_chart(env):  # noqa: F811 - the fixture
    rs = [(T0 + timedelta(days=4 * i), 50 + 45.0 * i, 310 - 0.001 * 45 * i + (1.5 if i % 2 else -1.5), 80.0) for i in range(300)]
    rep = lifetime.report(rs, start=320.0)
    life = {**rep, "chart": lifetime.chart(rep, Units(), NOW.tzinfo, "odo", [("2025.2.1", T0 + timedelta(days=100))]), "by": "odo", "loss": "start", "start": 320.0}
    lv = {"t0": 0, "t1": 86400, "primary": {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": [[0, 80.0]]}, "extras": [], "lane": [], "events": [], "ticks": []}
    block = {"car": {"vin": "V1", "name": "Alpha", "soc": 80.0}, "chart": lv, "full": None, "life": life, "tires": {}, "firsts": {}, "points": 1}
    html = env.get_template("battery.html").render(**context(), blocks=[block], view={})
    sec = html[html.index('id="lifetime"') :]
    assert "Lost since your start" in sec and "Best and first" in sec and "−12.0 mi" not in sec and 'name="start"' in sec and 'value="320"' in sec, "the headline follows the row chosen"
    assert 'class="lw on"' not in sec.split("lw-start")[0] and 'class="lw lw-start on"' in sec
    assert 'id="life-V1"' in sec and '"xkind": "dist"' in sec and '"line": true' in sec and '"rug": [[' in sec and ">Date</a>" in sec and "Updates</a>" not in sec
    assert "How the lifetime range is worked out" in sec and "Range at 100%, 90 days" not in html


def test_the_software_rows_carry_range_temperature_and_a_year_of_bars(env):  # noqa: F811 - the fixture
    t = datetime(2026, 9, 1, 17, 0, tzinfo=timezone.utc)
    rows = [
        {"id": 2, "version": "2026.20.6", "seen_at": t, "until": None, "days": 42, "odometer": 14070.0, "by_hand": False},
        {"id": 1, "version": "2026.14.4", "seen_at": t - timedelta(days=30), "until": t, "days": 30, "odometer": 13700.0, "by_hand": False},
    ]
    rows = softwarepage.per_version(rows, [(t - timedelta(days=10), 1.0, 301.4), (t + timedelta(days=5), 2.0, 298.5)], [(t + timedelta(days=1), 64.0)])
    block = {
        "vin": "V1",
        "name": "Alpha",
        "rows": rows,
        "cadence": softwarepage.cadence(rows, t + timedelta(days=42)),
        "timeline": softwarepage.timeline(rows, datetime(2026, 10, 13, tzinfo=timezone.utc), NOW.tzinfo),
        "min_level": 60,
    }
    html = env.get_template("software.html").render(**context(), blocks=[block], add_cars=[], today=date(2026, 10, 13))
    assert (
        "298.5</b> mi" in html and "1 charge" in html and "−2.9 mi vs previous" in html and 'class="swr-c quiet"' in html and "64°F" in html
    ), "range, charges, change against the version before, outside"
    assert "every 30 days" in html and 'class="swtl"' in html and 'title="2026.20.6 · Sep 1, 2026"' in html and ">so far<" in html
    assert "rows-more" not in html, "two versions need no Show all"


def test_the_routes_tab_lists_routes_and_destinations(env):  # noqa: F811 - the fixture
    import toproutes

    home, work = {"id": 1, "name": "Home"}, {"id": 2, "name": "Work"}
    t = datetime(2026, 3, 2, 13, 0, tzinfo=timezone.utc)
    ds = [
        {"start_place": a, "end_place": b, "start_date": t + timedelta(days=i), "destination_date": t + timedelta(days=i, minutes=12), "kwh_used": 1.5, "miles": 5.0}
        for i, (a, b) in enumerate([(home, work)] * 3 + [(work, home)] * 2)
    ]
    rules = [{"from_name": "Home", "to_name": "Work", "tag": "Commute", "both_ways": True}]
    ctx = dict(context(), spans=(("all", "All time", None), ("90d", "90 days", 90)), span="all", routes=toproutes.routes(ds, rules, NOW.tzinfo), destinations=toproutes.destinations(ds), drives=5)
    html = env.get_template("routes.html").render(**ctx)
    assert "Top routes" in html and "Commute" in html and "3 → Work, 2 → Home" in html and "12 min" in html and "300 Wh/mi" in html
    assert 'href="/places/routes/1-2?span=all&amp;vin=V1"' in html, "a route opens its own page"
    assert 'href="/places/2?vin=V1"' in html and 'aria-current="page">Routes<' in html
    empty = env.get_template("routes.html").render(**dict(ctx, routes=[], destinations=[]))
    assert "No drives between places" in empty
