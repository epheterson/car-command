"""The Settings design system and Settings › Money built on it: the
shared form rules a phone needs, the Money page in each shape a plan comes in, and the save
contract the redesign must not move (an unchanged save re-prices nothing)."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

import cost
from settingsform import parse, reprices
from test_settingsform import WAS, _money_page, _post_of

HERE = Path(__file__).parent
CSS = (HERE / "static" / "style.css").read_text()


def _rule(selector: str) -> str:
    m = re.search(r"(?:^|\n)" + re.escape(selector) + r" \{([^}]*)\}", CSS)
    assert m, selector
    return m.group(1).replace(" ", "")


def test_a_label_is_plain_and_every_field_is_16px():
    """The old global rule made every label 12px caps and every input inside one 12px: iOS
    zoomed on each focus, and a checkbox label without .check stranded its box."""
    label = _rule("label")
    assert "uppercase" not in label and "letter-spacing" not in label and "font-size:var(--t-meta)" in label
    assert "--t-body:16px" in CSS.replace(" ", "")
    for sel in ("input", "textarea"):
        assert "font-size:var(--t-body)" in _rule(sel), sel
    assert "select{font:inherit;font-size:var(--t-body)" in CSS.replace(" ", ""), "select too"
    assert "label:where(:not([class]):has(>input[type=checkbox],>input[type=radio])){display:flex" in CSS.replace(" ", ""), "a bare checkbox label puts the box beside its words"
    assert "accent-color:var(--ink)" in _rule("input[type=checkbox], input[type=radio]"), "one colour for every box, and never the battery's gold"
    # no smaller text in a date field either
    assert "font-size:14px" not in _rule(".pbar .stepper input[type=date]") and "font-size:14px" not in _rule("form.dates input[type=date]")


def test_the_components_are_defined_once_with_tokens():
    for sel in (".btn", ".btn.primary", ".btn.danger", ".btn.sm", ".set-row", ".group-b", "label.switch", ".segmented", ".num", ".num input", ".savebar", ".notice", ".empty", ".mpick", ".chip-v"):
        assert CSS.count("\n" + sel + " {") == 1, sel
    assert "min-height:var(--h-ctl)" in _rule(".btn") and "font-size:var(--t-body)" in _rule(".num input")
    for token in ("--danger:", "--warn:", "--faint:", "--s4:16px", "--r-card:12px", "--h-ctl:40px"):
        assert token in CSS.replace(" ", ""), token
    macros = (HERE / "templates" / "_settings.html").read_text()
    for name in ("num", "segmented", "switch", "remove", "savebar"):
        assert f"macro {name}(" in macros, name
    assert 'data-savebar' in (HERE / "static" / "shell.js").read_text()


class _Text(HTMLParser):
    """Each <p>'s text and every inline style, from rendered HTML."""

    def __init__(self):
        super().__init__()
        self.ps, self.styles, self.depth = [], [], 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("style"):
            self.styles.append(a["style"])
        if tag == "p":
            self.ps.append("")
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == "p" and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and self.ps:
            self.ps[-1] += data


def _manual(n_rates: int = 2):
    rates = [{"from": f"{2024 + i}-01-01", "off_peak": 0.3 + i / 100, "part_peak": 0.4 + i / 100, "peak": 0.5 + i / 100} for i in range(n_rates)]
    schedule = [{"period": "peak", "days": "all", "start": 16, "end": 21, "months": None}, {"period": "part_peak", "days": "all", "start": 21, "end": 22, "months": [1, 2, 3, 11, 12]}]
    return cost.compile_rows("Springfield EV", rates, schedule)


def _imported(periods: int):
    labels = ("Super off-peak", "Off-peak", "Mid-peak", "Peak", "Critical")[:periods]
    grid = [[(h // 5) % periods for h in range(24)] for _ in range(12)]
    return {
        "name": "Springfield Solar TOU",
        "utility": "Springfield Electric Co",
        "source": "urdb:abc",
        "link": "https://openei.org/apps/USURDB/rate/view/abc",
        "versions": [
            {"from": f"{2024 + v}-01-01", "periods": [{"key": f"p{i}", "label": n, "price": 0.1 + v / 100 + i / 10} for i, n in enumerate(labels)], "weekday": grid, "weekend": grid} for v in range(2)
        ],
    }


@pytest.mark.parametrize("shape", ["none", "manual", "imported3", "imported5"])
def test_money_renders_every_plan_shape_without_paragraphs(shape):
    plan = {"none": None, "manual": _manual(), "imported3": _imported(3), "imported5": _imported(5)}[shape]
    html = _money_page(plan)
    t = _Text()
    t.feed(html[html.index("<main") : html.index("</main>")])  # the page, not the shell around it
    long = [p.strip() for p in t.ps if len(" ".join(p.split())) > 100]
    assert not long, long
    assert all(s.startswith("--heat:") for s in t.styles), "no inline style but the grid's shade"
    assert html.count('class="savebar"') == 1 and "data-savebar" in html and 'name="currency"' not in html and 'name="sc_price"' in html
    main = html[html.index('<form id="money"') : html.index("</form>", html.index('<form id="money"'))]
    assert "<form" not in main[1:], "no form inside the Money form"
    if plan is None:
        assert "No rate plan yet" in html and 'class="tou"' not in html and 'name="from0"' in html
        return
    assert html.count('<table class="tou"') == 1 and 'class="chip-v"' in html, "every plan reads as its grid, with today's prices as chips"
    if shape == "manual":
        assert 'name="from2"' in html and 'name="del0"' in html and 'name="sdel1"' in html and 'name="sp2"' in html, "each row and rule removable; one blank of each adds"
        assert 'name="pv0_0"' not in html and "Stop using" not in html
    else:
        n = int(shape[-1])
        assert f'name="pv1_{n - 1}"' in html and 'name="from0"' not in html and "Stop using Springfield Solar TOU" in html
        assert html.count('class="chip-v"') == n
    if shape == "imported5":
        assert "0.11–0.51" in html, "five prices summarise as a range"


@pytest.mark.parametrize("plan", [_manual(14), _imported(3), _imported(5)], ids=["manual", "imported3", "imported5"])
def test_saving_money_unchanged_reprices_nothing(plan):
    was = dict(WAS, rate_plan=plan)
    form = _post_of(_money_page(plan))
    got = parse("money", form, was)
    assert "rate_plan" not in got and not reprices(got, was), "nothing changed, nothing saved"
    assert not {"q", "key", "label", "contains"} & set(form), "Find my plan's fields belong to its own forms"


def test_remove_drops_a_dated_row_or_a_rule_and_reprices():
    plan = _manual(3)
    was = dict(WAS, rate_plan=plan)
    form = _post_of(_money_page(plan))
    got = parse("money", dict(form, del1="on"), was)
    assert [v["from"] for v in got["rate_plan"]["versions"]] == ["2024-01-01", "2026-01-01"] and reprices(got, was)
    got = parse("money", dict(form, sdel0="on"), was)
    assert cost.plan_rows(got["rate_plan"])[1] == [{"period": "part_peak", "days": "all", "start": 21, "end": 22, "months": [1, 2, 3, 11, 12]}]
    got = parse("money", dict(form, sdel0="on", sdel1="on"), was)
    assert cost.plan_rows(got["rate_plan"])[1] == [] and reprices(got, was), "every rule removed is off-peak all day, not the old schedule"
    added = parse("money", dict(form, sp2="peak", sd2="weekends", ss2="7", se2="9"), was)
    assert added["rate_plan"]["versions"][0]["weekend"][0][8] == 2 and reprices(added, was), "the blank rule adds one once its price is picked"


def test_the_settings_pages_lead_back_and_the_landing_renders():
    import jinja2

    import filters
    from settingsform import index

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(HERE / "templates")))
    filters.register(env)
    car = {"name": "Alpha", "vin": "V1", "live": True}
    base = {"root": "", "now": __import__("datetime").datetime(2026, 9, 4, 16), "car": car, "active": "settings", "vehicles": [car], "q": "", "colors": {"V1": 0}, "names": {"V1": "Alpha"}, "cur": "$"}
    html = env.get_template("settings_index.html").render(**base, groups=index({}, 1, [], False))
    assert html.count('class="set-row set-nav"') == 8 and html.count('<h2 class="sec">') == 3 and "<p" not in html.split("<main")[1].split("</main>")[0]


# ---- stage 2: every other Settings page on the same components ----------------------------


def _env():
    import jinja2

    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(HERE / "templates")))
    filters.register(env)
    return env


def _render(page: str, **ctx) -> str:
    from datetime import datetime, timezone

    car = {"name": "Alpha", "vin": "V1", "live": True}
    base = {
        "root": "",
        "now": datetime(2026, 9, 4, 16, tzinfo=timezone.utc),
        "car": car,
        "active": "",
        "vehicles": [car],
        "q": "",
        "colors": {"V1": 0},
        "names": {"V1": "Alpha"},
        "cur": "$",
        "csrf": "t",
        "conv": {},
        "saved": False,
    }
    return _env().get_template(page).render(**{**base, **ctx})


def _main(html: str) -> str:
    return html[html.index("<main") : html.index("</main>")]


def assert_plain(html: str):
    """No explanatory paragraph: every <p> in the page reads in one glance."""
    t = _Text()
    t.feed(_main(html))
    long = [p.strip() for p in t.ps if len(" ".join(p.split())) > 100]
    assert not long, long


ALERT_ST = {"plug_reminder_at": "21:00", "plug_reminder_below": 50.0, "summary_at": "07:00", "tire_low_psi": 30.0, "tire_high_psi": None}


@pytest.mark.parametrize("chans", [[], ["discord"], ["discord", "pushover"]])
def test_alerts_are_grouped_switch_rows_with_one_save_bar(chans):
    import notify

    routes = {"parked": {chans[-1]}, "login": set()} if chans else {}
    html = _render("settings_alerts.html", st=ALERT_ST, section="alerts", label="Alerts", channels=chans, routes=routes, alert_kinds=notify.KINDS, alert_groups=notify.GROUPS)
    assert_plain(html)
    assert html.count('class="savebar"') == 1 and "data-savebar" in html and "<table" not in _main(html)
    assert 'value="50"' in html and 'name="plug_reminder_below"' in html, "a whole percentage reads as one"
    assert 'type="time" name="plug_reminder_at"' in html and 'type="time" name="summary_at"' in html
    switches = html.count('class="switch"')
    if not chans:
        assert "No channel set up" in html and switches == 0 and 'name="routes_shown"' not in html
        return
    titles = re.findall(r'<h2 class="sec">([^<]+)', html)
    assert [t.strip() for t in titles] == ["Channels"] + [g for g, _ in notify.GROUPS]
    assert switches == len(chans) * (len(notify.KINDS) + 1), "a switch per kind per channel, and one per channel"
    assert ("al-cols" in html) is (len(chans) > 1), "channel names above the switches only when there are several"
    assert 'name="tire_low_psi" value="30"' in html
    # an unchanged save stores what was there
    form = _post_of(html)
    got = parse("alerts", form, dict(WAS, **ALERT_ST), channels=chans, kinds=notify.KINDS)
    want = {k: [c for c in chans if routes.get(k) is None or c in routes[k]] for k in notify.KINDS}
    assert got["alert_routes"] == want and got["plug_reminder_below"] == 50.0 and got["tire_low_psi"] == 30.0 and got["tire_high_psi"] is None
    assert got["plug_reminder_at"] == "21:00" and got["summary_at"] == "07:00"


def test_every_alert_kind_is_in_exactly_one_group():
    import notify

    listed = [k for _, keys in notify.GROUPS for k in keys]
    assert sorted(listed) == sorted(notify.KINDS) and len(listed) == len(set(listed))


def test_a_source_has_one_plain_name_everywhere():
    """Data's checks said "the connector" and "the TeslaFi feed" while Cars and sources said
    Lucid and TeslaFi: one name per source, from carspage, in the checks and their alerts too."""
    from datetime import datetime, timezone

    import carspage
    import health

    for kind in ("feed", "teslamate", "tesla"):
        assert health.SOURCES[kind] == carspage.WRITERS[kind]
    assert carspage.writer_name("live", "Lucid") == "Lucid (CarConnectivity)" and carspage.writer_name("feed") == "TeslaFi (API)"
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    said = health.check({"vin": "V1", "name": "Alpha", "source": "live", "source_name": "Lucid (CarConnectivity)", "raw_newest": None}, now)[0]["text"]
    assert said == "Nothing has ever arrived from Lucid (CarConnectivity) for Alpha."
    assert "Tesla (direct)" in health.check({"vin": "V1", "name": "Alpha", "source": "tesla", "raw_newest": None}, now)[0]["text"], "the stream has a name too"
    code = "\n".join((HERE / p).read_text() for p in ("app.py", "health.py", "recorder.py", "sources.py", "carspage.py", "history_writer.py"))
    pages = "\n".join(p.read_text() for p in (HERE / "templates").glob("*.html"))
    for old in ('"the connector', '("The connector is', '"The connector did', '"The connector reports', "from the connector for", '"the TeslaFi feed', "TeslaFi (live feed)"):
        assert old not in code.replace('never "the connector"', ""), old
    for old in ("the connector", "the TeslaFi feed", "live feed"):
        assert old not in pages.lower(), old


def _maps(status, **kw):
    from datetime import datetime, timezone

    t = datetime(2026, 10, 3, 19, tzinfo=timezone.utc)
    regions = [{"id": "us/oregon", "label": "Oregon", "why": "Your drives", "mb": 254, "added_dt": t, "refreshed_dt": t}] if status else []
    ctx = dict(st={"public_tiles": True, "public_geocoder": False}, section="maps", label="Maps", regions=regions, wait_until_dt=None, maps_on=bool(status), status=status)
    return _render("settings_maps.html", **{**ctx, **kw})


@pytest.mark.parametrize("status", [None, {"building": "idle", "version": 3, "disk_bytes": 2.5e9, "auto": False, "refresh": "quarterly", "error": None}])
def test_maps_is_one_form_of_switch_rows(status):
    from settingsform import maps_service

    html = _maps(status)
    assert_plain(html)
    assert html.count('class="savebar"') == 1 and "<table" not in _main(html) and "Save</button>" in html
    assert html.count('type="submit"') == (3 if status else 1), "Save, plus Add and a Remove for the one region when the add-on runs"
    form = _post_of(html)
    got = parse("maps", form, dict(WAS, public_tiles=True, public_geocoder=False))
    assert got == {"public_tiles": True, "public_geocoder": False}
    assert maps_service(form) is None, "an unchanged save makes no call to the add-on"
    if status:
        assert html.count('class="switch"') == 3 and "Oregon" in html and 'form="region-rm" name="id" value="us/oregon"' in html
        assert maps_service(dict(form, auto="on")) == {"auto": True, "refresh": "quarterly"} and maps_service(dict(form, refresh="off")) == {"auto": False, "refresh": "off"}
    else:
        assert html.count('class="switch"') == 2 and 'name="auto"' not in html and "region-add" not in html, "no add-on: nothing to set on it"


@pytest.mark.parametrize("anywhere", [False, True])
def test_access_is_rows_with_kind_badges_and_a_danger_switch(anywhere):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    made = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    tokens = [
        {"hash": "ab" * 32, "label": "assistant", "kind": "admin", "last4": "zz99", "car": "every car", "created_at": made, "last_used_at": None},
        {"hash": "cd" * 32, "label": "Home Assistant", "kind": "read", "last4": "q1w2", "car": "Alpha", "created_at": made, "last_used_at": made},
    ]
    log = [{"at": made, "label": "assistant", "last4": "zz99", "method": "POST", "path": "/api/admin/tesla/register", "status": 200, "client_ip": "192.168.1.40"}]
    html = _render(
        "settings_access.html",
        st={},
        section="access",
        label="Sign-in and API",
        tokens=tokens,
        api_log=log,
        admin_anywhere=anywhere,
        auth_enabled=True,
        minted="raw-token-shown-once",
        minted_label="assistant",
        minted_kind="admin",
        request=SimpleNamespace(url=SimpleNamespace(scheme="https", netloc="example.test")),
    )
    assert_plain(html)
    main = _main(html)
    assert "<table" not in main and html.count('class="savebar"') == 1 and 'class="switch danger"' in html and "style=" not in main
    assert html.count('class="badge kind admin"') == 1 and html.count('class="badge kind"') == 1 and html.count(">Revoke</button>") == 2
    assert 'form="pw"' in html and 'form="mint"' in html and 'id="pw"' in html and "POST /api/admin/tesla/register" in html
    form = _post_of(html)
    assert set(form) == ({"csrf_token", "anywhere"} if anywhere else {"csrf_token"}), "the page's own form carries only the switch"


def _cars_and_packs():
    from datetime import datetime, timezone

    import carspage

    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = [{"id": 4, "kind": "teslafi", "vin": "V1", "label": "Alpha", "config": {}, "readable": True, "status": (t0, None), "last_ok_at": None, "last_error": None}]
    accts = carspage.accounts(rows, [], True, {"V1": "Alpha", "V3": "Charlie"}, ["V3"], [], (t0, None), [])
    packs = {"V1": carspage.pack(81.2, None, None, 75.0), "V3": carspage.pack(117.1, 117.1, "Lucid", None)}
    cars = carspage.cards(
        [{"vin": "V1", "name": "Alpha"}, {"vin": "V3", "name": "Charlie"}],
        {"V1": ("Tesla", "Model 3"), "V3": ("Lucid", "Gravity")},
        {"V1": "feed", "V3": "live"},
        {"V1": (t0, None), "V3": (t0, None)},
        {"V1": t0},
        packs,
        accts,
    )
    return cars, accts, rows, packs


def test_cars_and_sources_is_grouped_rows():
    cars, accts, rows, _ = _cars_and_packs()
    html = _render("settings_cars.html", st={}, section="cars", label="Cars and sources", cars=cars, accounts=accts, sources=rows, env_sources=[], vault_ready=True, cc_catalogue=[])
    assert_plain(html)
    main = _main(html)
    assert "<table" not in main and "<style" not in main and html.count('class="set-row car-h"') == 2 and html.count('class="set-row acct') == 2
    assert "TeslaFi (API)" in html and "Lucid (CarConnectivity)" in html and 'class="btn sm danger">Remove' in html and 'class="btn sm">Test' in html
    assert html.count('<details class="set-x" id="add-') == 5, "each kind of source opens beneath its row"


def test_the_pack_says_where_it_came_from_in_the_same_words_everywhere():
    """Battery said "assumed from the model" for a pack Cars and sources said Lucid reported:
    both now read carspage.pack, which the battery page gets from the same capacities()."""
    cars, accts, rows, packs = _cars_and_packs()
    settings = _render("settings_cars.html", st={}, section="cars", label="Cars and sources", cars=cars, accounts=accts, sources=rows, env_sources=[], vault_ready=True, cc_catalogue=[])
    lv = {"t0": 0, "t1": 1, "primary": {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": []}, "extras": [], "lane": [], "events": [], "ticks": []}
    car = {"vin": "V3", "name": "Charlie", "garage": False}
    block = {"car": {"vin": "V3", "name": "Charlie", "capacity_kwh": 117.1}, "chart": lv, "full": None, "tires": {}, "firsts": {}, "points": 0}
    battery = _render("battery.html", car=car, blocks=[block], packs=packs, u={"dist": "mi", "temp": "°F"})
    assert "reported by Lucid" in settings and "usable, reported by Lucid" in battery and "assumed" not in battery
    unset = _render("battery.html", car=car, blocks=[block], packs={}, u={"dist": "mi", "temp": "°F"})
    assert "117.1<small>kWh</small>" not in unset, "no pack the settings page doesn't know"
    src = (HERE / "app.py").read_text()
    page = src[src.index("def battery_page(") : src.index("def level_at(")]
    assert 'packs = {c["vin"]: c["pack"] for c in capacities()}' in page and "packs=packs" in page


def test_connect_tesla_folds_finished_steps_and_leads_with_the_stream():
    from datetime import datetime, timedelta, timezone

    import teslapage

    now = datetime.now(timezone.utc)
    err = "webconnection error: dial tcp 203.0.113.9:8443: i/o timeout"
    facts = {
        "app_ok": True,
        "registered": True,
        "account": True,
        "cars": [{"vin": "V1", "name": "Alpha", "paired": True}, {"vin": "V2", "name": "Bravo", "paired": False}],
        "stream_host_ok": True,
        "stream_registered": True,
        "stream_cars": {
            "V1": {"on": True, "live": False, "last_at": now, "config": {"synced": True}, "errors": [{"at": (now - timedelta(hours=h)).isoformat(), "body": err, "name": "x"} for h in (1, 2, 3, 4)]}
        },
    }
    html = _render(
        "settings_tesla.html",
        st={},
        section="cars",
        label="Connect Tesla",
        back=("/settings/cars", "Cars and sources"),
        steps=teslapage.steps(facts),
        facts=facts,
        links={"V2": {"url": "https://tesla.com/_ak/x?vin=V2", "qr": "<svg></svg>"}},
        stream=teslapage.stream_cars(facts),
        tesla_scopes=[],
        billing={"granted": True, "count": 3, "read_at": now},
        history_step=[],
    )
    assert_plain(html)
    main = _main(html)
    assert "‹ Cars and sources" in main
    assert main.count('<li class="step done"') == 3 and main.count("<details class=\"set-x\"><summary class=\"set-row\"><span class=\"step-mark\"") == 3, "each finished step is one row"
    assert 'class="step next" id="step-keys"' in main and "Add the key to Bravo" in main, "the step to do now is open"
    assert main.index('id="streaming"') < main.index('id="setup"'), "the stream per car comes first"
    assert main.count('class="notice err trouble"') == 1 and "×4" in main, "one error, counted, its raw text behind an (i)"


def test_data_checks_are_rows_that_fit_a_phone():
    from datetime import datetime, timezone

    checks = [
        {
            "name": "Alpha",
            "source": "TeslaFi (API)",
            "facts": {"raw_newest": datetime(2026, 9, 28, tzinfo=timezone.utc), "odo_7d": 114.6, "trips_7d": 0.0},
            "findings": [{"level": "warn", "text": "115 of the 115 mi Alpha drove this week are not in any drive."}],
        },
        {"name": "Charlie", "source": "Lucid (CarConnectivity)", "facts": {}, "findings": []},
    ]
    html = _render("settings_data.html", st={"min_drive_mi": 0.1, "min_charge_kwh": 0.0, "weather": True}, section="data", label="Data", data_checks=checks)
    assert_plain(html)
    main = _main(html)
    assert "<table" not in main and html.count('class="savebar"') == 1 and 'class="notice warn"' in html and "1 finding" in html and "not checked yet" in html
    assert "TeslaFi (API)" in html and "Lucid (CarConnectivity)" in html and "/drives.csv" in html and "/charges.csv" in html
    got = parse("data", _post_of(html), dict(WAS, min_drive_mi=0.1, min_charge_kwh=0.0, weather=True))
    assert got == {"min_drive_mi": 0.1, "min_charge_kwh": 0.0, "weather": True}


@pytest.mark.parametrize("st", [{"currency": "€", "units": "metric", "week_start": "sunday", "clock": "24", "date_order": "dmy", "theme": "dark", "timezone": "Europe/Paris"}, {}])
def test_units_and_look_are_segmented_rows_and_save_unchanged(st):
    html = _render("settings_look.html", st=st, section="look", label="Units and look", zones=("Europe/Paris",), tz_key="Europe/Paris", tz_default="America/Chicago")
    assert_plain(html)
    assert html.count('class="segmented"') == 5 and "<select" not in _main(html) and html.count('class="savebar"') == 1
    got = parse("look", _post_of(html), dict(WAS, **st))
    want = {"currency": "$", "units": "imperial", "week_start": "monday", "clock": "12", "date_order": "mdy", "theme": "system", "timezone": None}
    assert got == {**want, **st}, "an unchanged save stores what was there, the currency symbol included"
