"""Shape checks on the template source that a render test cannot see.

Two bugs the browser found that every render test had passed: the tread
form on Service was written inside `{% block title %}`, so the whole section rendered
into <title> and never appeared on the page; and the token forms on Settings sat inside
the page's own <form>, which HTML does not allow, so the browser dropped the inner
<form> tags and every button submitted the outer one. Both are visible in the source.
"""

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

TEMPLATES = sorted((Path(__file__).parent / "templates").glob("*.html"))
TITLE = re.compile(r"{%\s*block title\s*%}(.*?){%\s*endblock\s*%}", re.S)


def test_title_blocks_hold_only_text():
    for path in TEMPLATES:
        for body in TITLE.findall(path.read_text()):
            assert "<" not in body, f"{path.name}: markup inside the title block renders into <title>"


class FormDepth(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.nested = False

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self.depth += 1
            self.nested = self.nested or self.depth > 1

    def handle_endtag(self, tag):
        if tag == "form":
            self.depth = max(0, self.depth - 1)


def test_no_form_opens_inside_another():
    """Jinja tags are plain text to the HTML parser, so nesting in the source is nesting on the page."""
    for path in TEMPLATES:
        p = FormDepth()
        p.feed(path.read_text())
        assert not p.nested, f"{path.name}: a <form> opens inside another; the browser drops the inner tag"


def test_sub_navigation_lives_in_the_shell_only():
    for path in TEMPLATES:
        if path.name == "base.html":
            continue
        assert "seg-links subnav" not in path.read_text(), f"{path.name} draws its own sub-navigation; the shell does"


def test_the_content_column_never_grows_past_the_screen():
    """The page column sits in a flex column, whose children default to their widest content:
    one long unwrapped line on Stats widened the whole page to 761 px on a 390 px phone
    The column and its grid must be allowed to shrink."""
    import re

    css = (Path(__file__).parent / "static" / "style.css").read_text()
    rule = re.search(r"\.stage main \{([^}]*)\}", css)
    assert rule and "min-width:0" in rule.group(1).replace(" ", "") and "minmax(0,1fr)" in rule.group(1).replace(" ", "")


def test_nothing_sets_a_minimum_wider_than_a_phone():
    """A 22em note field (352 px) and two 1fr tables pushed the drive page and Stats past a
    390 px screen. A minimum width goes through min(…, 100%), and grid columns that hold
    tables go through minmax(min(…, 100%), 1fr)."""
    import re

    css = (Path(__file__).parent / "static" / "style.css").read_text()
    for m in re.finditer(r"(?<!\()min-width:\s*(\d+(?:\.\d+)?)(em|px)", css):  # a media query's (min-width: …) is not a rule
        px = float(m.group(1)) * (16 if m.group(2) == "em" else 1)
        assert px <= 200, f"min-width:{m.group(1)}{m.group(2)} can be wider than a phone; write min({m.group(1)}{m.group(2)}, 100%)"
    assert not re.search(r"\.two \{[^}]*grid-template-columns:\s*1fr 1fr", css), ".two must be able to drop to one column"
    assert re.search(r"\.two \{[^}]*minmax\(min\(", css)


def test_only_the_shells_own_sheets_hide_their_disclosure_triangle():
    """Hiding the triangle on every <details> made "Add a drive by hand" and the readings
    folds read as plain text with nothing saying they open."""
    import re

    css = (Path(__file__).parent / "static" / "style.css").read_text()
    assert not re.search(r"(^|[}\s])details > summary \{[^}]*list-style:none", css), "a bare details > summary rule reaches every fold on the site"
    assert re.search(r"\.carsheet > summary, \.tabbar \.more > summary \{[^}]*list-style:none", css)


def test_files_and_new_tabs_are_never_prefetched():
    """Hovering a receipt on the Service page downloaded the photo or PDF.
    A link that opens a file or a new tab is not a page the next tap is waiting for."""
    base = (Path(__file__).parent / "templates" / "base.html").read_text()
    js = (Path(__file__).parent / "static" / "prefetch.js").read_text()
    service = (Path(__file__).parent / "templates" / "service.html").read_text()
    assert '[target=_blank]' in base and '_blank' in js
    assert 'href="{{ root }}/service/files/{{ f.id }}" data-no-prefetch' in service


def _css():
    return (Path(__file__).parent / "static" / "style.css").read_text()


def test_the_wordmark_style_stays_in_the_shell():
    """The sidebar's wordmark rule was bare .brand and moved the sign-in card's by 8 px."""
    import re

    assert not re.search(r"(^|\n)\.brand \{", _css()) and ".side .brand, .phonebar .brand {" in _css()


def test_sign_out_lines_up_with_settings():
    import re

    css = _css()
    button = re.search(r"\.side-foot button\.side-sec \{[^}]*(padding:[^;]+);", css)
    row = re.search(r"\.side-car, \.side-sec \{[^}]*(padding:[^;]+);", css)
    assert button and row and button.group(1) == row.group(1), "Sign out takes the same padding as the rows above it"


def test_the_old_header_left_no_styles_behind():
    import re

    css = _css()
    for dead in (r"\.signout \{", r"\.veh \{", r"(^|\n)\.car \{", r"\.car \{ margin-left: 0"):
        assert not re.search(dead, css), dead


def test_the_shell_says_where_you_are_and_its_sheets_close():
    base = (Path(__file__).parent / "templates" / "base.html").read_text()
    assert '<nav class="side"' in base, "the sidebar is navigation"
    assert base.count('{% if pk == active %} aria-current="page"{% endif %}') == 3, "the current page is announced in the sidebar (sections' pages and Settings' pages) and the phone pills"
    assert 'aria-label="Settings">' not in base, "on a phone Settings is its landing list and each page's way back, not a row of pills"
    assert base.count('{% if v.vin == car.vin %} aria-current="true"{% endif %}') == 2, "the current car is announced in the sidebar and the sheet"
    js = (Path(__file__).parent / "static" / "shell.js").read_text()
    assert "Escape" in js and "toggle" in js and "pointerdown" in js
    assert 'src="{{ root }}/static/shell.js' in base


def test_opening_one_sheet_closes_the_other_on_the_tap_itself():
    """The toggle event arrives a task later, so More and the car sheet were both open for a
    moment (and in a fast test, stayed so). The tap that opens one closes the other."""
    js = (Path(__file__).parent / "static" / "shell.js").read_text()
    assert "closeAll(e.target.closest" in js


def test_no_new_class_rule_reaches_a_class_the_templates_already_use_for_tables():
    """`.cards { display:grid }` for the Now page's car cards also caught the eighteen
    `<table class="cards">` lists, which broke every table on a laptop."""
    import re

    css = _css()
    assert not re.search(r"(^|\n)\.cards \{", css), "a bare .cards rule reaches every table.cards"
    assert ".car-cards {" in css
    garage = (Path(__file__).parent / "templates" / "garage.html").read_text()
    assert 'class="car-cards"' in garage


def test_phase_two_small_style_things():
    import re

    css = _css()
    nm = re.search(r"\.cc-nm \{([^}]*)\}", css)
    assert nm and "min-width:0" in nm.group(1) and "text-overflow:ellipsis" in nm.group(1), "a long car name truncates instead of pushing the state pill out"
    for dead in (r"\.carcard", r"\.rcs \{", r"\.lvbar", r"table\.cards\.brief"):
        assert not re.search(dead, css), dead
    assert ".cc-hd .dot, .today .car .dot { margin-right:0; }" in css, "the dot's old margin doubles the flex gap"
    assert re.search(r"h1 \.more \{[^}]*margin-left:16px", css), "two links floated in a heading need space between them"


def test_the_chart_script_draws_with_text_nodes_only():
    """The card shows place names; built with innerHTML a place called <img onerror=…> would run."""
    js = (Path(__file__).parent / "static" / "chart.js").read_text()
    assert "innerHTML" not in js and "textContent" in js and "chart-data" in js
    base = (Path(__file__).parent / "templates" / "base.html").read_text()
    assert 'src="{{ root }}/static/chart.js' in base


def test_chart_styles_stay_inside_the_chart():
    """The server-drawn charts use .grid, .tk and .day too; the new chart's rules must not reach them."""
    import re

    css = _css()
    start = css.index("/* The chart:")
    block = re.sub(r"/\*.*?\*/", "", css[start : css.index("/* end of the chart */", start)], flags=re.S)
    for sel in re.findall(r"(?m)^([^{}@\n][^{]*)\{", block):
        for part in sel.split(","):
            part = part.strip()
            assert part.startswith((".chartbox", ".chart-", ":root", "@")), part


def test_a_row_time_keeps_its_am_pm_and_a_date_wraps_above_it():
    """nowrap on the whole label kept "12:16 AM" whole on a phone but pushed "Yesterday
    11:32 AM" on the Now page into the thumbnail beside it."""
    import re

    rule = re.search(r"\n\.row-t \{([^}]*)\}", _css())
    assert rule and "nowrap" not in rule.group(1), "a date has to be able to wrap"
    from test_parts import render

    html = render("{{ parts.row('/d/1', 'Yesterday 11:32 AM', 'Home → Work', [], '4.7 mi', '') }}")
    assert '<span class="row-t">Yesterday 11:32\u00a0AM</span>' in html


def test_the_segmented_switch_is_one_line_on_a_phone():
    import re

    phone = " ".join(re.findall(r"@media \(max-width: 600px\) \{(.*)", _css()))
    assert ".segs2 { flex-wrap:nowrap; overflow-x:auto;" in phone, "Software 30 wrapped onto a line of its own at 390 px"


def test_the_chart_draws_the_servers_time_labels():
    js = (Path(__file__).parent / "static" / "chart.js").read_text()
    assert "data.ticks" in js, "labels come from the page, in the server's zone"
    assert "setHours(24" not in js, "no day boundaries worked out in the browser's zone"


def test_a_long_also_show_switch_scrolls_inside_the_chart():
    """A drive offers six second lines; at 390 px the switch pushed the whole chart 66 px
    past the screen."""
    import re

    css = _css()
    segs = re.search(r"\n\.chart-segs \{([^}]*)\}", css).group(1).replace(" ", "")
    assert "overflow-x:auto" in segs and "max-width:100%" in segs
    assert re.search(r"\.chartbox > \* \{[^}]*min-width:0", css), "a grid child grows to its content unless told otherwise"


def test_a_chart_without_a_lane_draws_none_and_names_no_state():
    js = (Path(__file__).parent / "static" / "chart.js").read_text()
    assert "hasLane" in js and "if (hasLane)" in js, "Battery's month of readings has no lane of states to draw"
    parts = (Path(__file__).parent / "templates" / "_parts.html").read_text()
    assert "{% if data.lane %}" in parts, "no key for a lane that is not there"


def test_the_charts_left_margin_fits_its_widest_label():
    js = (Path(__file__).parent / "static" / "chart.js").read_text()
    assert "widest" in js, '"388mi" was cut off by a margin sized for "100%"'


def test_every_map_uses_the_shared_base_and_no_page_inverts_the_tiles():
    """Free, good-looking maps. OpenFreeMap's
    vector styles (Positron, Dark): free, no key, no limits; OSM raster greyed as the fallback."""
    tpl = Path(__file__).parent / "templates"
    for name in ("calendar.html", "map.html", "day.html", "now.html", "drive.html", "tags.html", "place.html"):
        s = (tpl / name).read_text()
        assert "tile.openstreetmap.org" not in s and "CCMap.base(" in s and '{% include "_maphead.html" %}' in s and "var palette = [" not in s, name
    head = (tpl / "_maphead.html").read_text()
    assert "maplibre-gl@5.24.0/dist/maplibre-gl.js" in head and "maplibre-gl-leaflet@0.1.4/leaflet-maplibre-gl.js" in head and head.count('integrity="sha384-') >= 4 and "maps.js" in head
    js = (Path(__file__).parent / "static" / "maps.js").read_text()
    assert "tiles.openfreemap.org/styles/positron" in js and "tiles.openfreemap.org/styles/dark" in js and "L.maplibreGL" in js
    assert "openstreetmap.org/copyright" in js and "OpenFreeMap" in js and "OpenMapTiles" in js, "attribution, linked"
    assert "tile.openstreetmap.org" in js and "cc-quiet" in js, "the raster fallback when a browser cannot draw vector maps"
    assert "prefers-color-scheme: dark" in js and "data-theme" in js and "toDataURL" in js, "dark by setting and by system; small maps keep a picture, not a live canvas"
    css = (Path(__file__).parent / "static" / "style.css").read_text()
    assert "hue-rotate(180deg)" not in css and ".cc-quiet .leaflet-tile-pane" in css


def test_the_places_page_reads_cached_stays_that_a_place_change_clears():
    """Matching 13,900 arrivals to places ran on every load (0.32 s warm)."""
    import re

    src = (Path(__file__).parent / "app.py").read_text()
    index = re.search(r"def places_index\(.*?(?=\n@site\.|\ndef )", src, re.S).group(0)
    assert 'cached("place_stays"' in src and "place_stays()" in index
    forget = re.search(r"def forget_places\(.*?(?=\ndef )", src, re.S).group(0)
    assert '"place_stays"' in forget and "_cache.pop(" in forget
    for fn in ("add_place", "delete_place", "rename_place", "merge_places"):
        body = re.search(rf"def {fn}\(.*?(?=\ndef )", src, re.S).group(0)
        assert '_cache.pop("place_stays"' in body or "forget_places()" in body, fn


def test_places_add_and_rename_both_validate_the_posted_icon():
    """Every POST route that stores an icon runs it through `_valid_icon` before anything is
    saved — an unrecognised value is a 400, not a silently-ignored string that would sit
    in the column forever, drawing nothing."""
    import re

    src = (Path(__file__).parent / "app.py").read_text()
    add_body = re.search(r"def places_add\(.*?(?=\n@site\.|\ndef )", src, re.S).group(0)
    rename_body = re.search(r"def places_rename\(.*?(?=\n@site\.|\ndef )", src, re.S).group(0)
    pin_body = re.search(r"def places_pin\(.*?(?=\n@site\.|\ndef )", src, re.S).group(0)
    assert "_valid_icon(icon)" in add_body, "places_add"
    assert "_valid_icon(icon)" in rename_body, "places_rename"
    assert "_valid_icon(icon)" in pin_body, "places_pin"
    # validation happens before any of the three routes touches the database
    assert add_body.index("_valid_icon(icon)") < add_body.index("add_place(")
    assert rename_body.index("_valid_icon(icon)") < rename_body.index("rename_place(")
    assert pin_body.index("_valid_icon(icon)") < pin_body.index("pin_place(")


def test_a_map_never_draws_over_the_phone_bars():
    css = (Path(__file__).parent / "static" / "style.css").read_text()
    assert (
        ".leaflet-container { isolation:isolate; }" in css and "html .leaflet-container { background:var(--panel); }" in css
    ), "Leaflet's panes sit at z-index 400 and covered the phone bar and tab bar"


def test_a_place_row_can_never_overflow_its_column():
    """A long name or address used to run a card past its right edge: a flex/grid item's
    default min-width is its content's width, not 0. Every link from the row down to its
    text gets min-width:0 and the text ellipsizes; the long looked-up address is cut to
    "Road, City" before it reaches the page."""
    css = (Path(__file__).parent / "static" / "style.css").read_text()
    for rule in (".plr", "button.plr-b", ".ptext", ".pd-form", ".pd-mapcol", ".pcard-hd", ".icorow", ".icofull"):
        block = re.search(re.escape(rule) + r"\s*\{([^}]*)\}", css)
        assert block and "min-width:0" in block.group(1).replace(" ", ""), rule
    for rule in (".ptext b", ".ptext .small"):
        assert "text-overflow:ellipsis" in re.search(re.escape(rule) + r"\s*\{([^}]*)\}", css).group(1).replace(" ", ""), rule
    place_html = (Path(__file__).parent / "templates" / "place.html").read_text()
    assert "p.address|short_address" in place_html, "the long Nominatim string is cut down before it ever reaches the page"


def test_the_icon_glyph_is_recentered_inside_its_square():
    """.pl-ic's base rule (for icons sitting inline in running text) carries a baseline
    vertical-align and a right margin meant for the word that follows it. Neither belongs
    in a centered square: place-items:center centers the margin box, so a stray
    margin-right alone reads as the glyph sitting low/left, which is exactly what
    showed on the live page. Both the closed face button and the open grid's options zero
    them out."""
    css = (Path(__file__).parent / "static" / "style.css").read_text()
    for selector in (".icopt .pl-ic", ".pico .pl-ic"):
        block = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        assert block, selector
        rule = block.group(1).replace(" ", "")
        assert "margin:0" in rule and "display:block" in rule, selector
    # the squares themselves still center by box, not by text baseline
    for selector in (".icopt", ".pico"):
        block = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        assert block and "place-items:center" in block.group(1).replace(" ", ""), selector


def test_the_places_page_draws_one_map_and_a_places_page_one():
    """The list's small maps once came and went while scrolling. The Places page now has one
    map (placesmap.js), started at once from scripts kept unrun in the page, with the
    vector library left out when backgrounds are off; a place's own page has its one real,
    fixed-size map."""
    here = Path(__file__).parent / "templates"
    lst = (here / "places.html").read_text()
    assert '<template id="pl-maphead">{% include "_maphead.html" %}</template>' in lst, "Leaflet is in the page only as a template"
    assert "<script src=\"https://" not in lst and "CCMap" not in lst and "IntersectionObserver" not in lst
    js = (Path(__file__).parent / "static" / "placesmap.js").read_text()
    assert js.count("CCMap.base(") == 1 and "ccAfterArrival" not in js and "el.async = false" in js
    assert '!tiles && /maplibre/.test(' in js, "with backgrounds off the vector library is never fetched"
    one = (here / "place.html").read_text()
    assert one.count("CCMap.base(") == 1 and "draggable: true" in one and '{% include "_maphead.html" %}' in one
    css = (Path(__file__).parent / "static" / "style.css").read_text()
    assert re.search(r"\.pd-map\s*\{[^}]*height:440px", css), "the map's box has its height before it draws"
    assert re.search(r"\.plmap\s*\{[^}]*height:calc\(100dvh - 200px\)", css), "so has the Places map's"
    assert ".pmap" not in css, "the old card maps' styles are gone"


def test_dark_maps_take_the_sites_own_tones_and_the_controls_follow_the_theme():
    """The dark vector style is neutral black; its ground, water and roads are recoloured to the page's
    dark tones once the style loads. Leaflet's white zoom buttons and credit strip use the theme tokens,
    with a selector strong enough to beat Leaflet's sheet, which loads after the site's."""
    js = (Path(__file__).parent / "static" / "maps.js").read_text()
    assert "style.load" in js and "setPaintProperty" in js
    for layer in ("background", "water", "highway_motorway_inner", "highway_major_inner", "landuse_residential"):
        assert '"' + layer + '"' in js or layer + ":" in js, layer
    css = (Path(__file__).parent / "static" / "style.css").read_text()
    assert ".leaflet-container .leaflet-control-container .leaflet-bar a {" in css
    assert ".leaflet-container .leaflet-control-container .leaflet-control-attribution {" in css
    rule = css[css.index(".leaflet-container .leaflet-control-container .leaflet-bar a {") :]
    assert "var(--panel)" in rule[: rule.index("}")] and "var(--ink)" in rule[: rule.index("}")]


def test_the_map_credit_stays_on_one_line_on_a_phone():
    css = (Path(__file__).parent / "static" / "style.css").read_text()
    assert "@media (max-width: 600px) { .leaflet-container .leaflet-control-container .leaflet-control-attribution { font-size:10px;" in css


def test_map_popups_and_tooltips_are_text_never_html():
    """Place names come from street addresses and from what people type; a popup builds text nodes from
    them, never HTML."""
    tpl = Path(__file__).parent / "templates"
    for f in sorted(tpl.glob("*.html")):
        s = f.read_text()
        for m in re.finditer(r"\.bind(Popup|Tooltip)\(", s):
            arg = s[m.end() : m.end() + 40]
            assert arg.startswith("CCMap.lines(") or re.match(r'"[A-Za-z ]*"\)', arg), (f.name, arg)
    js = (Path(__file__).parent / "static" / "maps.js").read_text()
    body = js[js.index("function lines(") :]
    body = body[: body.index("\n  }")]
    assert "createTextNode" in body and "innerHTML" not in js


def test_map_car_chips_hide_that_cars_charges_too():
    s = (Path(__file__).parent / "templates" / "map.html").read_text()
    assert "charges[p.v" in s or "charges[v] = charges[v] ||" in s
    assert "on.charges && carOn(v)" in s


def test_map_cells_load_once_however_fast_the_chip_is_pressed():
    """On, off and on again before the first load returns started a second load, and the first one's
    cells stayed on the map with no chip to hide them."""
    s = (Path(__file__).parent / "templates" / "map.html").read_text()
    assert s.count("loadCells(") == 2 and "if (!loading)" in s and "loading = false" in s


def test_place_stays_take_the_next_departure_over_every_drive():
    """A drive with no recorded end still ends the stay before it: the next-departure window runs over
    every drive, and only then are the drives without an end dropped."""
    src = (Path(__file__).parent / "app.py").read_text()
    fn = src[src.index("def place_arrivals") :]
    sql = fn[fn.index('"""SELECT') : fn.index('"""', fn.index('"""SELECT') + 3)]
    inner, outer = sql[: sql.rindex(")")], sql[sql.rindex(")") :]
    assert "LEAD(start_date)" in inner and "FROM trips" in inner and "WHERE" not in inner
    assert "arrived IS NOT NULL" in outer and "lat IS NOT NULL" in outer


def test_every_map_has_its_renderer_before_anything_is_drawn():
    """Routes and pins added before a map has a view wait for it in order; when a pin made the shared
    renderer after a route was queued, the route drew first against a renderer with no bounds and the
    page threw (Calendar, and a drive with a route). The base adds the renderer as the map is made."""
    js = (Path(__file__).parent / "static" / "maps.js").read_text()
    body = js[js.index("function base(") :]
    body = body[: body.index("\n  }")]
    made = body.index("L.map(el")
    assert made < body.index("map.getRenderer(") < body.index("vector()")


def test_cars_and_sources_is_cards_not_tables():
    """A card per car and a row per account; a table here was a spreadsheet of settings."""
    cars = (Path(__file__).parent / "templates" / "settings_cars.html").read_text()
    assert "<table" not in cars and '<section class="group car"' in cars and '<div class="group-b accts">' in cars and "<style>" not in cars
    assert "the connector" not in cars, "sources are named, never 'the connector'"
    assert '<details class="set-x" id="add-teslafi">' in cars, "picking a kind opens its form beneath it, without JS"


def test_every_install_gets_a_key_and_nothing_overwrites_env():
    root = Path(__file__).parent.parent.parent
    example = (root / "stack" / ".env.example").read_text()
    assert "CC_SECRET_KEY=GENERATE_KEY" in example
    setup = (root / "scripts" / "setup.sh").read_text()
    assert "GENERATE_KEY" in setup and "openssl rand -base64 32 | tr '+/' '-_'" in setup
    add = (root / "scripts" / "secret-key.sh").read_text()
    assert "grep -Eq '^CC_SECRET_KEY=.+'" in add and 'cp -p "$env_file" "$env_file.bak-' in add and ">>" in add and "sed -i" not in add, "appends after a dated copy, never rewrites"
    compose = (root / "stack" / "docker-compose.yml").read_text()
    assert "CC_SECRET_KEY=${CC_SECRET_KEY:-}" in compose
    assert "secret-key:" in (root / "Makefile").read_text()
    assert "CC_SECRET_KEY" in (root / "scripts" / "backup.sh").read_text()


def test_the_brand_is_the_star_garage_everywhere():
    """The approved identity: the star-garage mark beside the name in the sidebar, the phone bar
    and the sign-in pages, drawn inline so it takes the theme's colour; the favicon and the
    home-screen icons are the same mark."""
    here = Path(__file__).parent
    icons = (here / "templates" / "_icons.html").read_text()
    assert "macro brand_mark" in icons and 'stroke="currentColor"' in icons[icons.index("macro brand_mark") :]
    base = (here / "templates" / "base.html").read_text()
    assert base.count('ic.brand_mark("tow")') == 3 and "ic.brand_mark()" not in base and 'class="mark"' not in base
    # phones show the mark too: beside the car picker when there are several cars, beside the name when one
    bar = base[base.index('<header class="phonebar">') : base.index("</header>")]
    # the phone's mark arrives on a fresh load too (the sidebar is hidden on phones)
    assert '<a class="phonemark" href="{{ root }}/{{ q }}" aria-label="Car Command home">{{ ic.brand_mark("tow") }}</a>' in bar
    assert '<span class="brand">{{ ic.brand_mark("tow") }}<span class="nm">{{ car.name }}</span></span>' in bar
    css = (here / "static" / "style.css").read_text()
    assert "html.arrive .phonebar .tow" in css and "--tow-fill:var(--bar)" in css
    # once it has arrived the still mark takes over, so no fill can show against a see-through bar
    assert "html.arrive.arrived .mark.towing { display:none; }" in css and "html.arrive.arrived .mark.towed { display:block; }" in css
    assert "classList.add('arrived')" in base[: base.index("</head>")]
    assert 'rel="icon" type="image/svg+xml" href="{{ root }}/static/favicon.svg?v=3"' in base
    assert "brand_mark(" in (here / "templates" / "gate.html").read_text()
    css = (here / "static" / "style.css").read_text()
    assert "--mark:" in css and "border-radius:50%; background:var(--gold)" not in css
    for name in ("favicon.svg", "favicon-32.png", "favicon-16.png"):
        assert (here / "static" / name).stat().st_size > 200, name
    root = here.parent.parent
    assert not (root / "scripts" / "make-icons.py").exists(), "the monogram placeholders are retired"
    readme = (root / "README.md").read_text()
    # the README opens on the arriving mark, gold on a dark theme; the lockups stay for everything else
    assert "brand/mark-arrive.svg" in readme and "brand/mark-arrive-night.svg" in readme and (root / "brand" / "lockup-horizontal.svg").exists()
    assert sorted(f.name for f in (root / "brand").iterdir()) == sorted(
        ["README.md", "favicon.svg", "icon-light.svg", "icon.svg", "lockup-horizontal-night.svg", "lockup-horizontal.svg", "mark-arrive.svg", "mark-outlined.svg", "mark.svg"]
        # and what brand/make-readme-art.sh makes for the repository page, with its sources
        + ["make-readme-art.sh", "mark-arrive-night.svg", "social-preview.html", "social-preview.png"]
        # and the demo loop, stills and card that brand/make-demo-media.sh records from the demo car
        + ["make-demo-media.sh", "record-demo.py", "demo-routes.py", "media"]
    ), "the identity only; the explorations are out of the tree"


def test_the_sidebar_brand_tows_the_name_in_on_a_fresh_load():
    """The name rides the road behind the car: the road runs on under it, and on a fresh load (a
    new visit or a reload, not a click inside the site) the mark arrives and the car pulls the
    name in along the road. Car and name share one keyframe so the name can't drift; reduced
    motion and in-site clicks get the still lockup."""
    from jinja2 import Environment, FileSystemLoader

    here = Path(__file__).parent
    base = (here / "templates" / "base.html").read_text()
    assert '<a class="brand" href="{{ root }}/{{ q }}">{{ ic.brand_mark("tow") }}<span class="nm">Car Command</span></a>' in base
    head = base[: base.index("</head>")]
    script = head[head.index("<script>") : head.index("</script>")]
    for part in ("prefers-reduced-motion", "reload", "document.referrer", "location.origin", "classList.add('arrive')"):
        assert part in script, part
    env = Environment(loader=FileSystemLoader(here / "templates"))
    tow = env.from_string('{% import "_icons.html" as ic %}{{ ic.brand_mark("tow") }}').render()
    assert tow.count("<svg") == 2 and 'class="mark towing"' in tow and 'class="mark towed"' in tow
    assert tow.count('class="tow"') == 2, "the car and the fill that hides the road under it move together"
    assert "<animate" not in tow and "<mask" not in tow, "one clock: the sidebar moves on CSS alone"
    assert "fill:var(--tow-fill, var(--panel))" in tow, "the car hides the road under it, in the colour behind it"
    for part in ("tw-vl", "tw-vr", "tw-leg", "tw-arm", "tw-flap", "tw-roof", "tw-drive"):
        assert f'class="{part}"' in tow, part
    # the lower edge hinges inside the upper edge's group, so the two turn as one garage door
    assert tow.index('class="tw-arm"') < tow.index('class="tw-flap"') < tow.index("</g>", tow.index('class="tw-arm"'))
    css = (here / "static" / "style.css").read_text()
    for rule in (
        ".mark.towing { display:none; }",
        "html.arrive .mark.towing { display:block; }",
        "html.arrive .mark.towed { display:none; }",
        "@keyframes cc-tow",
        "@keyframes cc-fold",
        "@keyframes cc-leg",
        "@keyframes cc-run",
        ".side .brand::after",
    ):
        assert rule in css, rule
    tows = [line for line in css.splitlines() if "animation:cc-tow" in line]
    assert len(tows) >= 4 and len({line[line.index("animation:") :].split(";")[0] for line in tows}) == 1, "one timing for the car, the name and the phone's car"


def test_the_favicon_keeps_clear_of_the_tile_edge():
    """The glyph sits 2 px inside its 16 px tile, so the road no longer runs off the rounded corner;
    the links carry a version so browsers drop the cached one."""
    import re

    here = Path(__file__).parent
    svg = (here / "static" / "favicon.svg").read_text()
    path = svg[svg.index("<path") :]
    width = float(re.search(r'stroke-width="([\d.]+)"', path).group(1))
    d = re.search(r' d="([^"]+)"', path).group(1)
    nums = [float(n) for n in re.findall(r"-?\d+\.?\d*", re.sub(r"A [\d.]+ [\d.]+ 0 0 [01] ", "A ", d))]
    xs, ys = nums[0::2], nums[1::2]
    assert min(xs) - width / 2 >= 2 and max(xs) + width / 2 <= 14, (min(xs), max(xs))
    assert min(ys) - width / 2 >= 2 and max(ys) + width / 2 <= 14, (min(ys), max(ys))
    assert svg == (here.parent.parent / "brand" / "favicon.svg").read_text()
    for page in ("base.html", "gate.html"):
        text = (here / "templates" / page).read_text()
        assert "favicon.svg?v=3" in text and "favicon-32.png?v=3" in text, page


def test_the_mark_arrives_where_a_visit_begins_and_pulls_in_while_you_wait():
    """Motion only where it means something: on the sign-in and first-run pages the mark arrives
    once (the star folds into the garage, the car pulls in), and a looping
    'pulling in' shows during the waits that take seconds (checking a source, gathering the map's
    cells). Anyone who asks for less motion gets the still mark."""
    from jinja2 import Environment, FileSystemLoader

    here = Path(__file__).parent
    icons = (here / "templates" / "_icons.html").read_text()
    macro = icons[icons.index("macro brand_mark") :]
    assert "macro brand_mark(motion" in icons and 'pathLength="1"' in macro
    env = Environment(loader=FileSystemLoader(here / "templates"))
    arrive = env.from_string('{% import "_icons.html" as ic %}{{ ic.brand_mark("arrive") }}').render()
    css = (here / "static" / "style.css").read_text()
    assert arrive.count("<svg") == 2 and 'class="mark arrive"' in arrive and 'class="mark arrive-still"' in arrive
    assert "<animate" not in arrive and "tw-flap" in arrive, "the same CSS fold as the sidebar, on one set of timings"
    assert 'clip-path="url(#cc-arrive-frame)"' in arrive, "the car is clipped to the frame as it rolls in; the mark itself overflows"
    assert ".mark.arrive .tow {" in css and ":is(html.arrive, .mark.arrive) .tw-flap" in css
    still = env.from_string('{% import "_icons.html" as ic %}{{ ic.brand_mark() }}').render()
    assert still.count("<svg") == 1 and "<animate" not in still
    css = (here / "static" / "style.css").read_text()
    assert "@keyframes cc-loop" in css and "cc-draw" not in css and ".mark.draw" not in css
    assert ".mark.arrive-still { display:none; }" in css
    calm = css[css.index("@media (prefers-reduced-motion: reduce)", css.index(".mark.arrive-still")) :][:300]
    assert ".mark.arrive { display:none; }" in calm and ".mark.arrive-still { display:block; }" in calm
    for page in ("gate.html", "welcome.html"):
        text = (here / "templates" / page).read_text()
        assert 'brand_mark("arrive")' in text and '"draw"' not in text, page
    base = (here / "templates" / "base.html").read_text()
    assert '<template id="cc-wait">' in base and 'brand_mark("loop")' in base
    assert "data-wait" in (here / "static" / "shell.js").read_text()
    cars = (here / "templates" / "settings_cars.html").read_text()
    assert cars.count("data-wait=") >= 4, "Test and add (three kinds), Move, and Test wait on TeslaFi or a database"
    assert "cc-wait" in (here / "templates" / "map.html").read_text()


def test_the_mark_is_the_garage_with_its_roof():
    """The star folded into a garage: one stroke as before up to the star's upper-right notch, then a
    level roof out to the road's right end. The right point and the stub under it are gone everywhere
    the mark ships, and the arrival ends on exactly that picture."""
    import re

    here = Path(__file__).parent
    root = here.parent.parent
    master = (root / "brand" / "mark.svg").read_text()
    d = re.search(r' d="([^"]+)"', master).group(1)
    assert d.startswith("M 68.27 44.72 L 61.27 44.72") and d.endswith("A 0.6 0.6 0 0 0 31.1 16.21 L 68.27 16.21")
    assert 'viewBox="0 0 69.57 46.02"' in master and 'stroke-width="2.6"' in master
    # the roof's and the road's right ends line up, and their round caps stay inside the box
    assert 68.27 + 2.6 / 2 <= 69.57 and 16.21 - 2.6 / 2 >= 0
    icons = (here / "templates" / "_icons.html").read_text()
    assert f'd="{d}"' in icons, "the site's still mark is the master's path"
    from jinja2 import Environment, FileSystemLoader

    still = Environment(loader=FileSystemLoader(here / "templates")).from_string('{% import "_icons.html" as ic %}{{ ic.brand_mark() }}').render()
    for name, text in (("mark.svg", master), ("the site's still mark", still), ("favicon.svg", (root / "brand" / "favicon.svg").read_text())):
        assert "34.86 28.79" not in text and "45.62 17.75" not in text, f"{name}: no right point and no stub, the roof replaces them"
    # the arrival starts from the star (its point and leg are only the first frame) and ends on the roof
    arrive = (root / "brand" / "mark-arrive.svg").read_text()
    assert 'values="0 31.1 16.21;-7.968 31.1 16.21"' in arrive and 'values="0 45.96 18.29;-128.623 45.96 18.29"' in arrive
    # the parts fold onto the roof and the road: the point's two edges add up to level about its root,
    # the leg turns from up to along the road, and the run-outs pick up where they end
    css = (here / "static" / "style.css").read_text()
    arm = float(re.search(r"\.tw-arm \{[^}]*rotate\((-?[\d.]+)deg", css).group(1))
    flap = float(re.search(r"\.tw-flap \{[^}]*rotate\((-?[\d.]+)deg", css).group(1))
    leg = float(re.search(r"\.tw-leg \{[^}]*rotate\((-?[\d.]+)deg", css).group(1))
    import math

    top = math.degrees(math.atan2(18.29 - 16.21, 45.96 - 31.1))
    low = math.degrees(math.atan2(28.79 - 18.29, 34.86 - 45.96))
    assert abs(top + arm) < 0.01 and abs(low + arm + flap) < 0.01
    assert abs(math.degrees(math.atan2(28.79 - 44.72, 34.86 - 37.5)) + leg) < 0.01
    tip = 31.1 + math.hypot(45.96 - 31.1, 18.29 - 16.21) + math.hypot(34.86 - 45.96, 28.79 - 18.29)
    foot = 37.5 + math.hypot(34.86 - 37.5, 28.79 - 44.72)
    assert f"M {tip:.2f} 16.21 L 68.27 16.21" in icons and f"M {foot:.2f} 44.72 L 68.27 44.72" in icons
    # every phase reads one line of timings, so the leg and the point can open together or in turn
    assert re.search(r":root \{ --t-base:[^}]*--t-car:[\d.]+s; \}", css)
    fold = css[css.index("@keyframes cc-fold") : css.index(".mark.loop path")]
    assert re.findall(r"animation:[^;]*?(\d?\.\d+s) both", fold) == [], "phase delays come from --t-*, not literals"
    # the favicon: road and roof on whole pixels, both ending at the same column
    fav = (root / "brand" / "favicon.svg").read_text()
    fd = re.search(r' d="([^"]+)"', fav).group(1)
    assert fd.startswith("M 13 13 ") and fd.endswith(" L 13 7")


# The table kinds (style.css): cards become stacked rows on a phone; data and bins scroll
# inside div.ovx; cmp, tou and pairs are built to fit; readings scroll in their own box.
TABLE_KINDS = {"cards", "data", "bins", "cmp", "tou", "pairs", "readings"}


def test_every_table_is_one_of_the_table_kinds_and_fills_its_card():
    """Tables used to be display:block so they could scroll, which left one narrower than its
    card hugging its content with the row rules stopping short of the edge. Now every table
    spans its container, and one that can outgrow a phone scrolls inside div.ovx or stacks."""
    here = Path(__file__).parent
    css = (here / "static" / "style.css").read_text()
    assert "table { display:table; width:100%; }" in css and "table { display:block" not in css and "table.fill" not in css
    for page in sorted((here / "templates").glob("*.html")):
        src = page.read_text()
        for m in re.finditer(r"<table(?:\s+class=\"([^\"]*)\")?", src):
            kinds = set((m.group(1) or "").split()) & TABLE_KINDS
            assert kinds, f"{page.name}: <table class=\"{m.group(1) or ''}\"> is none of {sorted(TABLE_KINDS)}"
            if kinds & {"data", "bins"}:
                assert re.search(r'<div class="ovx(?: [a-z0-9-]+)*">$', src[max(0, m.start() - 60) : m.start()].rstrip()), f"{page.name}: a {kinds} table scrolls inside div.ovx"
    # places.html lists places as rows of links, not a table.


def test_a_places_page_has_the_icon_row_and_the_icon_set_has_one_definition():
    """The icon choice lives once in _icons.html (ic.icon_row), shared by a place's page and the
    naming card, reading a set defined exactly once (placeicons.py) that the `place_icon`
    macro and the `places` filter both draw from: no second copy of the SVG paths."""
    import filters
    import placeicons

    here = Path(__file__).parent
    place_html = (here / "templates" / "place.html").read_text()
    card_html = (here / "templates" / "_placecard.html").read_text()
    icons_html = (here / "templates" / "_icons.html").read_text()
    assert "ic.icon_row(" in place_html and "ic.icon_row(" in card_html, "one markup for both"
    assert "icon_picker" not in icons_html and "icondd" not in icons_html, "the dropdown that covered the address field is gone"
    assert 'class="icofull" hidden' in icons_html and 'name="icon"' in icons_html and 'value="none"' in icons_html
    assert "PLACE_ICON_QUICK" in icons_html, "the row iterates the shared key list, not a hand-written one"
    assert "macro place_icon(key)" in icons_html and "PLACE_ICONS" in icons_html
    assert filters.PLACE_ICONS is placeicons.PLACE_ICONS
    for app_file in ("filters.py", "placeicons.py"):
        assert (here / app_file).read_text().count("PLACE_ICONS: dict") <= 1


def test_the_icon_row_is_radios_in_the_forms_flow_and_checks_only_what_is_stored():
    """Every choice is a real radio (Tab, Space and the arrow keys work), each with its own
    "Icon: X" label. Nothing is checked unless the place stores it, so a Save that touches no
    icon posts none and the stored one stays; the icon a name guesses is marked, not checked.
    More… opens below the row, in the flow: nothing floats over the fields under it."""
    import jinja2

    import filters
    import placeicons

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    ctx = dict(
        root="",
        car={"name": "Alpha", "lat": None, "lon": None, "address": None},
        active="places",
        vehicles=[{"vin": "V1", "name": "Alpha", "live": True}],
        q="",
        colors={"V1": 0},
        names={"V1": "Alpha"},
        theme="system",
        week_start=0,
        alert_kinds=[],
        now=datetime.now(timezone.utc),
        levels={},
        here="/places",
        css_v="x",
        js_v="y",
        csrf="t",
        auth_on=False,
        connector=False,
        findings=[],
        cur="$",
        st=None,
        choices=[],
        maps_on=False,
        new=False,
    )
    base = {"latitude": 40.7, "longitude": -74.0, "radius_m": 150, "auto": False, "visits": 3, "address": None, "user_address": None, "price": None, "parts": [], "city": ""}
    home = env.get_template("place.html").render(**ctx, p={**base, "id": 1, "name": "SF Home", "icon": None, "home": True})
    row = home[home.index('class="icorow"') : home.index('class="icofull"')]
    assert 'class="icopt eff" title="Home"' in row and " checked" not in row, "SF Home shows the house, marked, with nothing stored and nothing checked"
    kids = env.get_template("place.html").render(**ctx, p={**base, "id": 2, "name": "Alpha Daycare", "icon": "kids", "home": False})
    assert 'aria-label="Icon: Daycare, kids" checked' in kids
    bakery = env.get_template("place.html").render(**ctx, p={**base, "id": 3, "name": "Corner", "icon": "croissant", "home": False})
    quick = bakery[bakery.index('class="icoquick"') :]
    assert quick.index('value="croissant"') < quick.index('value="home"'), "a stored icon from the larger set leads the row"
    assert home.count('type="radio" name="icon"') == len(placeicons.QUICK_KEYS) + 1, "the row carries its few and None; More… fetches the rest"
    assert home.index('name="name"') < home.index('class="icorow"') < home.index('name="address"'), "name, then icons, then the address: the row pushes, never covers"
    css = (Path(__file__).parent / "static" / "style.css").read_text()
    full = re.search(r"\.icofull\s*\{([^}]*)\}", css).group(1).replace(" ", "")
    assert "position:absolute" not in full and "display:grid" in full
    opts = re.search(r"\.icofull \.icoopts\s*\{([^}]*)\}", css).group(1).replace(" ", "")
    assert "overflow-y:auto" in opts and "max-height:" in opts


def test_software_sections_keep_the_page_rhythm():
    """Each car's block on Software is spaced like Day's: the car's name clear of its tiles, and
    the Versions heading clear of the tiles above it."""
    text = (Path(__file__).parent / "templates" / "software.html").read_text()
    assert '<section class="daycar">' in text and '<div class="tiles t3">' in text and '<div class="group-b">' in text


def test_every_place_name_renders_through_the_home_and_work_glyphs(monkeypatch):
    """Drive and charge rows, the parked and asleep lines, the day's place chips, the car cards,
    the drive and charge headings and the place columns all mark Home and Work the same way."""
    import importlib
    import sys

    for k, v in (("DATABASE_URL", "postgresql://u:p@localhost:1/x"), ("REST_USER", "u"), ("REST_PASSWORD", "p")):
        monkeypatch.setenv(k, v)
    sys.path.insert(0, str(Path(__file__).parent))
    env = importlib.import_module("app").templates.env
    row = env.from_string('{% import "_parts.html" as p %}{{ p.row("/d/1", "8:49 AM", "Home → Maple Street", [], "", "") }}').render()
    gap = env.from_string('{% import "_parts.html" as p %}{{ p.gap_row("Parked 18 min at Work · lost 0.2 kWh") }}').render()
    assert row.count("pl-home") == 1 and gap.count("pl-work") == 1
    here = Path(__file__).parent / "templates"
    for page, needle in (
        ("day.html", "{{ x.place|places }}"),
        ("drive.html", "{{ place_refs(trip.from, [trip.get('from_ref')]) }}"),
        ("charge.html", "<h1>{{ place_refs(session.where, [session.get('where_ref')]) }}</h1>"),
        ("_now_hero.html", "<b>{{ place_refs(car.place, "),
        ("_parts.html", "{{ c.last_drive.from|short_place|places }} → {{ c.last_drive.to|short_place|places }}"),
        ("_parts.html", "{{ (c.get('place') or c.get('address') or '—')|places }}"),
        ("tags.html", "{{ d.from|places }}"),
        ("charging.html", "{{ p.where|places }}"),
    ):
        assert needle in (here / page).read_text(), (page, needle)
    css = (Path(__file__).parent / "static" / "style.css").read_text()
    assert ".pl-ic {" in css and ".pl { white-space:nowrap; }" in css


def test_a_stacked_month_bar_is_one_shape(monkeypatch):
    """The cars' segments are square and flush; one clip per stack rounds only its top, and runs
    past the axis so the bottom stays flat. Clip ids differ between the wide and phone drawings."""
    import importlib
    import sys
    from datetime import date

    for k, v in (("DATABASE_URL", "postgresql://u:p@localhost:1/x"), ("REST_USER", "u"), ("REST_PASSWORD", "p")):
        monkeypatch.setenv(k, v)
    sys.path.insert(0, str(Path(__file__).parent))
    app_module = importlib.import_module("app")
    from statspage import bars, by_month
    from units import Units

    rows = lambda m: by_month([{"period": date(2026, 9, 1), "miles": m, "drives": 1, "added_kwh": 0, "cost": 0, "used_kwh": 0, "drive_h": 0, "charges": 0}])
    env = app_module.templates.env
    out = []
    for short in (False, True):
        g = bars({"V1": rows(300), "V2": rows(500)}, ["V1", "V2"], (2026, 9), (2026, 9), Units(), n=2, short=short)
        out.append(env.from_string('{% import "_parts.html" as p %}{{ p.bars(g, "mi", {}, {}) }}').render(g=g))
    wide, phone = out
    assert 'class="mb' in wide and not re.search(r'<rect class="mb[^>]*rx=', wide), "segments are square"
    clips = re.findall(r'<clipPath id="([^"]+)"><rect [^>]*rx="3"', wide)
    assert len(clips) == 1 and f'clip-path="url(#{clips[-1]})"' in wide, "one clip per month with miles; an empty August draws nothing"
    assert not set(clips) & set(re.findall(r'<clipPath id="([^"]+)"', phone)), "ids are unique per drawing"
    last = re.findall(r'<clipPath id="[^"]+"><rect x="[^"]+" y="([^"]+)" width="[^"]+" height="([^"]+)"', wide)[-1]
    g = bars({"V1": rows(300), "V2": rows(500)}, ["V1", "V2"], (2026, 9), (2026, 9), Units(), n=2)
    assert float(last[0]) == g["bars"][-1]["top"] and float(last[0]) + float(last[1]) > g["base"], "the clip runs past the axis"


def test_the_chart_readout_is_a_hover_not_a_fixture():
    """The crosshair, its dots and the floating card show while a pointer or finger is on the
    chart and go away when it leaves (a touch lingers a moment). At rest only the phone's static
    panel under the chart keeps its summary; on a wide screen nothing floats over the data."""
    here = Path(__file__).parent
    js = (here / "static" / "chart.js").read_text()
    assert 'box.classList.add("resting")' in js and 'box.classList.remove("resting")' in js
    leave = js[js.index('addEventListener("mouseleave"') :][:120]
    assert "rest()" in leave and "show(resting)" not in leave
    assert 'addEventListener("touchend"' in js and "setTimeout(rest" in js
    css = (here / "static" / "style.css").read_text()
    assert ".chartbox.resting .chart-svg .cross, .chartbox.resting .chart-svg .dotl, .chartbox.resting .chart-svg .dot2 { visibility:hidden; }" in css
    assert ".chartbox.resting .chart-area:not(.narrow) .chart-hov { visibility:hidden; }" in css


def test_settings_has_a_maps_section_that_says_what_leaves_the_box():
    from settingsform import DEFAULTS, LABELS

    here = Path(__file__).parent
    assert "maps" in LABELS
    assert DEFAULTS["public_tiles"] is False and DEFAULTS["public_geocoder"] is False
    t = (here / "templates" / "settings_maps.html").read_text()
    for phrase in ("public_tiles", "public_geocoder", "Download regions automatically", "Turn on local maps"):
        assert phrase in t, phrase


def test_the_card_asks_for_json_and_never_moves_a_place():
    """The card saves through the routes a place's page uses, asking for JSON so the page stays
    where it is (no redirect, so no back path that could double the mount, /cc/cc/day). It never
    sends "moved" or "resized": a Save from the card renames, re-icons or prices, never moves."""
    js = (Path(__file__).parent / "static" / "placecard.js").read_text()
    assert 'headers: { Accept: "application/json" }' in js and '"/places/pin"' in js and '"/edit"' in js
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    assert "moved" not in code and "resized" not in code and "back" not in re.findall(r"fd\.set\(\"(\w+)\"", js)


def _wide_tables_wrapped(name: str, opens: list[str]) -> None:
    src = (Path(__file__).parent / "templates" / name).read_text()
    for table in opens:
        at = src.index(table)
        assert re.search(r'<div class="ovx(?: [a-z0-9-]+)*">$', src[max(0, at - 60) : at].rstrip()), f"{name}: {table} must sit in an overflow box, or a phone page scrolls sideways"
    assert len(re.findall(r'<div class="ovx(?: [a-z0-9-]+)*">', src)) == src.count("</table></div>") >= len(opens), f"{name}: every overflow box closes after its table"


def test_wide_tables_scroll_in_their_own_box_on_a_phone():
    """/stats rendered 716 px wide at 390: a display:table can't scroll itself, so the
    per-week, time-of-use and by-day tables scroll inside div.ovx instead."""
    _wide_tables_wrapped("stats.html", ['<table class="data">', '<table class="bins">'])
    _wide_tables_wrapped("charging.html", ['<table class="bins"><thead><tr><th>Level</th>'])


def test_the_month_comparison_has_phone_headings():
    """table.cmp fits a phone because every heading is short to begin with: "Oct 1–5",
    "Sep 1–5", "12-mo avg, 1–5", never "October so far" or "12-month average"."""
    from statspage import heads

    src = (Path(__file__).parent / "templates" / "stats.html").read_text()
    head = src[src.index('<table class="cmp">') : src.index("</thead>", src.index('<table class="cmp">'))]
    assert "{{ h.this }}" in head and "{{ h.last }}" in head and "{{ h.avg }}" in head
    for fraction, day in ((1.0, None), (0.5, 15), (0.99, 31)):
        assert all(len(v) <= 15 for k, v in heads((2026, 10), fraction, day).items() if k != "partial"), (fraction, day)


def test_every_map_credits_openstreetmap():
    """Place names (local or public) and the map data come from OpenStreetMap, whose licence asks
    for visible credit wherever they're shown: in each map's own corner, backgrounds on or off
    (not only in a page footer), and in the README."""
    here = Path(__file__).parent
    js = (here / "static" / "maps.js").read_text()
    assert js.count('link("https://www.openstreetmap.org/copyright", "OpenStreetMap")') >= 3, "vector, raster and no-tiles credits"
    base = (here / "templates" / "base.html").read_text()
    assert "Live from the car" not in base and "from logged history" not in base, "no footer sentence"
    readme = (here.parent.parent / "README.md").read_text()
    assert "## Credits" in readme and "OpenStreetMap contributors" in readme and "Valhalla" in readme


def test_a_phones_header_carries_the_pages_title_and_the_h1_gives_its_row_up():
    """Phones: the page's title sits bold in the sticky bar's free right side, the car's name is
    cut with an ellipsis before it is, and the in-page h1 (and a title row with nothing else on
    it) goes. Desktop shows no bar, so is unchanged."""
    here = Path(__file__).parent
    base = (here / "templates" / "base.html").read_text()
    bar = base[base.index('<header class="phonebar">') : base.index("</header>")]
    assert '<span class="ptitle" id="ptitle">' in bar and '<span class="cn">' in bar
    assert "document.querySelector(\"main h1\")" in base and "classList.add(\"pt\")" in base
    css = _css()
    phone = css[css.index("@media (max-width: 860px) {\n  .shell") :]
    assert "position:sticky" in phone[: phone.index(".ptitle")] and "html.pt main h1 { display:none; }" in phone
    assert ".pbar:not(:has(> :not(h1, .pbar-ctx, .sp))) { display:none; }" in phone
    assert ".carsheet { position:relative; flex:0 1 auto; min-width:0; }" in phone and "text-overflow:ellipsis" in phone
    assert ".shell { grid-template-columns:minmax(0, 1fr); }" in phone, "a long name must not widen the page"
    assert "html.pt main h1" not in css[: css.index("@media (max-width: 860px) {\n  .shell")], "desktop keeps its h1"


def test_the_title_never_gives_way_to_the_car_name_and_the_version_line_is_muted():
    css = _css()
    assert ".ptitle { display:block; margin-left:auto; flex:none;" in css and "text-overflow:ellipsis" in css[css.index(".carsheet .cn") :]
    assert ".ver a { color:inherit; text-decoration:none; }" in css
    assert 'class="muted small ver"' in (Path(__file__).parent / "templates" / "settings_index.html").read_text()
