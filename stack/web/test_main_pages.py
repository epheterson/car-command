"""The browsing pages on the design system: each renders with
rich fictional rows, says nothing in a paragraph, and lays its records out as the components
do. Alpha and Bravo in Springfield; nothing here is real."""

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from test_settings_ui import _render, assert_plain

HERE = Path(__file__).parent
T0 = datetime(2026, 9, 21, 15, tzinfo=timezone.utc)
H = timedelta(hours=1)
WINDOW = {"start": T0, "end": T0, "label": "September 2026", "ym": "2026-09", "prev": "2026-08", "prev_label": "Aug 2026", "next": "2026-10", "next_label": "Oct 2026", "is_current": False}

# The templates this stage rebuilt: none may carry a paragraph of explanation in its source either.
TOUCHED = [
    "sleep.html",
    "places.html",
    "place.html",
    "stats.html",
    "timeline.html",
    "drive.html",
    "service.html",
    "software.html",
    "charging.html",
    "battery.html",
    "calendar.html",
    "calendar_year.html",
    "charge.html",
    "day.html",
    "drives.html",
    "charges.html",
    "now.html",
    "garage.html",
    "map.html",
]


def test_touched_templates_hold_no_long_paragraph_in_their_source():
    for name in TOUCHED:
        src = (HERE / "templates" / name).read_text()
        for m in re.finditer(r"<p\b[^>]*>(.*?)</p>", src, re.S):
            text = re.sub(r"\{% call .*?\{% endcall %\}", "", m.group(1), flags=re.S)  # an (i)'s note is its own, not the paragraph's
            text = re.sub(r"\{[{%#].*?[}%#]\}", "", text, flags=re.S)
            text = " ".join(re.sub(r"<[^>]+>", "", text).split())
            assert len(text) <= 100, f"{name}: {text[:80]}…"


def _sleep_block(n_days: int = 14, stops_a_day: int = 4) -> dict:
    days, idles = [], []
    for i in range(n_days):
        d = date(2026, 9, 21) - timedelta(days=i)
        seg = [{"label": "sleeping", "width": 0.9, "start": T0, "end": T0}, {"label": "driving", "width": 0.1, "start": T0, "end": T0}]
        day_idles = [
            {
                "start": T0 - i * 24 * H + k * 3 * H,
                "end": T0 - i * 24 * H + (k * 3 + 2) * H,
                "open": i == 0 and k == 0,
                "place": ["Home", "Work", None, "Lake"][k % 4],
                "hours": 2.0,
                "asleep_share": 0.9,
                "soc_start": 80.0,
                "soc_end": 79.0,
                "drop_pct": 1.0,
                "drop_per_day": 1.2,
                "sentry_share": 0.5 if k == 1 else None,
            }
            for k in range(stops_a_day)
        ]
        idles += day_idles
        days.append(
            {
                "date": d,
                "hours": {"sleeping": 21.6, "idle": 0.4, "driving": 2.0, "charging": 0.0, "unknown": 0.0},
                "span_h": 24.0,
                "segments": seg,
                "asleep_pct": 90.0,
                "wakeups": 3,
                "idles": day_idles,
            }
        )
    return {
        "vin": "V1",
        "name": "Alpha",
        "days": days,
        "summary": {"asleep_pct": 90.0, "wakeups_per_day": 3.0, "driving_h": 28.0, "charging_h": 9.0, "days": n_days},
        "known": True,
        "streak": ("sleeping", 7 * H),
        "idles": idles,
    }


def test_sleep_is_one_row_a_day_that_opens_onto_its_stops():
    """A month of stops was a 9-column table whose dates wrapped to four lines on a phone;
    now each day is one row (date, ribbon, asleep, wake-ups) and its stops sit beneath it."""
    rates = [{"name": "Alpha", "idle": 0.05, "idle_n": 350, "sentry": 0.4, "sentry_n": 12, "climate": None, "climate_n": 0}]
    html = _render("sleep.html", blocks=[_sleep_block(30)], window=WINDOW, parked_rates=rates, search={}, searching=False)
    assert_plain(html)
    main = html[html.index("<main") :]
    assert main.count('<details class="sday"') == 30 and main.count('class="sstop"') == 120
    assert "<table" not in main, "the stops and the days are rows, not a table"
    assert 'class="tiles t3"' in main and "too few yet" in main, "three tiles for what an hour parked costs"
    assert '<details class="fsheet">' in main and 'name="min_h"' in main, "the filter folds away until wanted"
    assert "→ now" in main, "a stop still going ends now"
    # every car: the costs compare in one table, cars as rows
    rates2 = rates + [dict(rates[0], name="Bravo")]
    html = _render("sleep.html", blocks=[_sleep_block(3)], window=None, parked_rates=rates2, search={"min_h": 2}, searching=True, car={"vin": "all", "name": "All cars", "garage": True}, q="?vin=all")
    main = html[html.index("<main") :]
    assert '<table class="cmp pair">' in main and main.count('<th scope="row">') == 2
    assert '<details class="fsheet" open>' in main and main.count('<details class="sday" open>') == 3, "a search opens the days it found stops on"


def test_a_place_has_one_count_and_it_is_called_visits():
    """A place page said "397 visits" over "194 stops" for the same place: the header read
    discovery's stored count (both ends of every drive, never updated), the list the stays.
    Now one function counts, every page and the API call it, and the word is visits."""
    import placespage

    stays = {5: {"n": 194}}
    assert placespage.visits({"id": 5, "visits": 397}, stays) == 194
    assert placespage.visits({"id": 6, "visits": 12}, stays) == 0, "no stays, no visits: the stored column is never shown"
    app = (HERE / "app.py").read_text()
    assert app.count("placespage.visits(") >= 5, "place page, card, merge preview and the admin API"
    for src in (app, (HERE / "placespage.py").read_text(), (HERE / "adminapi.py").read_text()):
        assert 'get("visits")' not in src, "nothing reads cc_places.visits for display"
    js = (HERE / "static" / "placesmap.js").read_text()
    assert '" stop"' not in js and '" stops"' not in js
    place = (HERE / "templates" / "place.html").read_text()
    assert "Stops here" not in place and "p.visits" in place and "st.n" not in place


# ---- the design review (2026-10-05): gold, the shell, the place page ---------------------


def test_gold_is_the_battery_and_the_mark_alone():
    """Gold was every switch, chip, chart fill and bar, so it meant nothing. Now only the
    battery (its spark, its gauge, a chart whose line is the battery, the ready state) and the
    money heatmap, a picture of price the review kept, may use it; the mark carries its own."""
    css = (HERE / "static" / "style.css").read_text()
    allowed = (".spark polyline", ".gauge i", ".chartbox.p-level", ".state.ready", ".status.ready", ".chip-v i", "table.tou td", ".tou-key")
    for line in css.splitlines():
        for rule in re.findall(r"([^{}]+)\{[^}]*var\(--gold\)", line):
            assert any(a in rule for a in allowed), rule.strip()
    assert "label.switch input:checked + span { background:var(--ink); }" in css
    js = (HERE / "static" / "chart.js").read_text()
    assert 'box.classList.toggle("p-level", P.key === "level")' in js


def test_the_shell_shows_the_car_everywhere_and_places_under_map():
    base = (HERE / "templates" / "base.html").read_text()
    head = base[base.index('<header class="phonebar">') : base.index("</header>")]
    assert "sec != 'settings'" not in head, "the switcher is on Settings pages too"
    assert "(key == 'map' and sec == 'places')" in base and "sec != 'places'" in base, "Places lights the Map tab, not More"
    css = (HERE / "static" / "style.css").read_text()
    assert "\ndetails.more > summary" not in css, "the tab bar's More has one style"


def test_the_places_list_is_the_named_places_then_the_ones_to_name():
    js = (HERE / "static" / "placesmap.js").read_text()
    assert "if (showAll || q || shown <= TOP) lists.need.appendChild(row(p));" in js and "TOP = 10" in js
    assert '"Show all " + fmt(shown)' in js and "plr-qb" not in js, "a list with Show all, not one row to name"


def test_a_place_opens_as_what_it_is_and_edit_opens_the_form():
    src = (HERE / "templates" / "place.html").read_text()
    assert '<form id="pd-form" class="pd-form picker-body"{% if not new %} hidden{% endif %}' in src
    assert "data-pd-read" in src and "data-pd-edit" in src and "function editing()" in src


def test_nothing_waits_for_the_logo():
    for page in (HERE / "templates").glob("*.html"):
        assert "ccAfterArrival" not in page.read_text(), page.name
    assert "ccAfterArrival" not in (HERE / "static" / "placesmap.js").read_text()


def test_service_and_software_are_rows_with_forms_folded_and_one_button_style():
    """Service was three always-open forms with three submit styles ("Save", "Add set",
    "Record" in 12 px); a car with no history saw only forms. Now every list is a group of
    rows, each form opens from its group's last row, and every button is a .btn."""
    for name in ("service.html", "software.html"):
        src = (HERE / "templates" / name).read_text()
        assert 'class="quiet' not in src and 'class="row"' not in src and "<table" not in src, name
        assert all("btn" in b for b in re.findall(r"<button[^>]*>", src)), name
        assert '<details class="set-x add">' in src and '<details class="set-x add" open' not in src, name
    html = _render(
        "service.html",
        items=[],
        tread=[],
        tire_sets=[],
        tread_today="2026-09-21",
        odos={},
        today=T0.date(),
    )
    assert_plain(html)
    main = html[html.index("<main") :]
    assert main.count('class="empty"') == 4 and "No service logged" in main, "an empty car is four lines, each with its action beneath"
    assert 'name="vin" value="V1"' in main and '<select id="svc-car"' not in main, "the page's car, no Car select"


def test_battery_axes_compare_like_with_like():
    """Range at 100% spanned 384–388 mi, turning 2 mi of noise into a seismograph; four tire
    charts each took their own scale, so no tire could be read against another."""
    from chartdata import steady

    lo, hi = steady([384.0, 388.0])
    assert hi - lo >= 0.1 * 386 and lo < 384 and hi > 388
    app = (HERE / "app.py").read_text()
    assert '"scale": "steady"' in app and "y_min=lo, y_max=hi" in app
    js = (HERE / "static" / "chart.js").read_text()
    assert "if (left < lastRight + 8) return;" in js, "a tick label that would touch the one before it is left out"
    src = (HERE / "templates" / "charging.html").read_text()
    assert 'class="segmented" aria-label="Period"' in src and "segs2" not in src and 'table class="cards"' not in src


def test_a_drive_opens_on_its_map_and_its_battery_axis_comes_from_the_readings():
    from chartdata import level_scale

    assert level_scale([86.0, 85.0]) == (80.0, 90.0), "a 1-point drive is a line across ten points, not a stroke on a 0–100 slab"
    assert level_scale([62.0, 18.0]) == (15.0, 65.0) and level_scale([99.5, 97.0]) == (90.0, 100.0) and level_scale([0.5, 3.0]) == (0.0, 10.0)
    src = (HERE / "templates" / "drive.html").read_text()
    assert src.index('id="map"') < src.index('<div class="tiles') < src.index("parts.chart("), "map, then the line and tiles, then the chart"
    tiles = src[src.index('<div class="tiles') : src.index("parts.chart(")]
    for footnote in ("average, moving and stopped", "from the car's positions", "odometer ", "at home rates", "of the drive"):
        assert footnote not in tiles, footnote


def test_sleep_names_each_cars_guard_mode_and_the_garage_both():
    one = [{"name": "Alpha", "guard": "Halo Secure", "idle": 0.05, "idle_n": 350, "sentry": 0.4, "sentry_n": 12, "climate": None, "climate_n": 0}]
    html = _render("sleep.html", blocks=[_sleep_block(3)], window=None, parked_rates=one, search={}, searching=False, guard={"V1": "Halo Secure"}, guard_all="Halo Secure")
    main = html[html.index("<main") :]
    assert '<span class="l">Halo Secure</span>' in main and "Halo Secure on</label>" in main and "Sentry" not in main
    both = one + [dict(one[0], name="Bravo", guard="Sentry")]
    html = _render(
        "sleep.html", blocks=[_sleep_block(3)], window=None, parked_rates=both, search={}, searching=False, guard_all="Sentry / Halo Secure", car={"vin": "all", "name": "All cars", "garage": True}
    )
    assert '<th class="r">Sentry / Halo Secure</th>' in html and "Sentry / Halo Secure on</label>" in html
