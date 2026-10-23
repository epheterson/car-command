"""Find my plan on Settings › Money: nothing is asked of OpenEI until a button is pressed,
only what was typed is sent, and the key is never shown back or logged."""

import html
import logging

import pytest

import billread as billread_mod
import sources as sources_mod
import urdb
from test_empty import bare, client  # noqa: F401 - fixtures
from test_settings_routes import form
from test_urdb import EV_CLOSED, ITEMS, KEY, L1, L2, L3, LARGE, Rows, Wire, _lab


class Store(Rows):
    """Rows, plus the statements the money page and a plan change run."""

    def __init__(self):
        super().__init__()
        self.ran = []

    def execute(self, sql, args=()):
        self.ran.append(" ".join(sql.split()))
        return super().execute(sql, args)


@pytest.fixture
def money(bare, monkeypatch):  # noqa: F811
    store, saved = Store(), {}
    monkeypatch.setattr(bare, "db", lambda: store)
    monkeypatch.setattr(bare, "VAULT", sources_mod.Vault(sources_mod.new_key()))
    monkeypatch.setattr(bare, "URDB_RELAY", None)
    monkeypatch.setattr(bare.settings_mod, "save", lambda conn, key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(bare, "apply_settings", lambda st: None)
    monkeypatch.setattr(urdb, "_httpx", lambda url, params: pytest.fail(f"OpenEI asked unprompted: {url}"))
    return store, saved


def _wire(monkeypatch, *answers):
    wire = Wire(*answers)
    monkeypatch.setattr(urdb, "_httpx", wire)
    return wire


FIND = "/settings/money/plan"


def _typed(app, saved, monkeypatch, name="Springfield EV2A", low=True):
    """A typed-in plan in use, its prices well under a tariff's when `low` (a community-choice
    supplier's generation, say)."""
    import cost
    from conftest import SPRINGFIELD_SCHEDULE

    p = 0.1 if low else 0.4
    saved["rate_plan"] = cost.compile_rows(name, [{"from": "2024-01-01", "off_peak": p, "part_peak": p + 0.05, "peak": p + 0.1}], SPRINGFIELD_SCHEDULE)
    monkeypatch.setattr(app, "current_settings", lambda: {**app.settings_mod.DEFAULTS, **saved})
    return saved["rate_plan"]


def _home(app, monkeypatch, kwh=2400.0):
    import cost

    profile = [0.0] * 576
    for m in range(1, 13):
        profile[cost.profile_index(False, m, 1)] = kwh / 12  # weekday nights
    monkeypatch.setattr(app, "home_profile", lambda: {"profile": profile, "kwh": kwh, "charges": 150, "first": None})


def test_the_money_page_links_to_the_finder_and_asks_nothing(money, client):  # noqa: F811
    page = client.get("/settings/money").text
    assert 'href="/settings/money/plan"' in page and "Find my plan" in page and 'id="find"' not in page, "a step of its own, not a row that opens"
    assert 'name="from0"' in page, "manual entry still works"
    r = client.get(FIND)
    assert r.status_code == 200 and "Plan search covers US utilities only." in r.text
    assert "https://apps.openei.org/services/api/signup/" in r.text and 'name="key"' in r.text
    assert 'name="q"' not in r.text, "no search box without a key or a relay"
    assert '<li class="now" aria-current="step"><span>Utility</span>' in r.text and 'href="/settings/money">‹ Money' in r.text


def test_a_saved_key_is_sealed_and_the_next_step_is_the_utility(bare, money, client):  # noqa: F811
    store, _ = money
    r = client.post("/settings/money/plan-search/key", data=form(bare, {"key": KEY}))
    assert r.status_code == 303 and r.headers["location"] == "/settings/money/plan?saved=key"
    assert KEY not in store.state[urdb.KEY_STATE] and bare.VAULT.open(__import__("json").loads(store.state[urdb.KEY_STATE])["sealed"].encode(), "state", urdb.KEY_STATE) == KEY
    page = client.get(r.headers["location"]).text
    assert "OpenEI key saved. Pick your utility." in page and "saved, ends …wxyz" in page and KEY not in page
    box = page.split('id="urdb-q"')[1].split(">")[0]
    assert 'name="q"' in page and "autofocus" in box, "the utility box, ready to type in"
    bad = client.post("/settings/money/plan-search/key", data=form(bare, {"key": "not a key"}))
    assert "does not look like an OpenEI API key" in bad.text and "not a key" not in bad.text.split("does not look")[0][-200:]
    r = client.post("/settings/money/plan-search/key", data=form(bare, {"remove": "1"}))
    assert r.headers["location"] == FIND and urdb.KEY_STATE not in store.state and 'name="key"' in client.get(FIND).text


def test_without_a_sealing_key_the_key_is_not_kept(bare, money, client, monkeypatch):  # noqa: F811
    store, _ = money
    monkeypatch.setattr(bare, "VAULT", sources_mod.Vault(None))
    r = client.post("/settings/money/plan-search/key", data=form(bare, {"key": KEY}))
    assert "cannot be kept" in r.text and urdb.KEY_STATE not in store.state and KEY not in r.text


def test_plans_compare_and_use(bare, money, client, monkeypatch, caplog):  # noqa: F811
    store, saved = money
    urdb.save_key(store, bare.VAULT, KEY)
    wire = _wire(monkeypatch, (200, {"items": ITEMS}))
    caplog.set_level(logging.DEBUG)
    r = client.get(FIND, params={"q": "Springfield Electric Co", "all": "1"})
    assert r.status_code == 200 and "Residential EV-TOU (Sch)" in r.text and "3 price changes since 2023" in r.text and "Time-of-use, tiered" in r.text
    assert wire.asked[0][1]["ratesforutility"] == "Springfield Electric Co" and wire.asked[0][1]["api_key"] == KEY
    assert set(wire.asked[0][1]) == {
        "version",
        "format",
        "sector",
        "approved",
        "detail",
        "limit",
        "orderby",
        "direction",
        "offset",
        "ratesforutility",
        "api_key",
    }, "nothing but what was typed, and the key"
    assert KEY not in r.text and f"label={L3}" in r.text and "data-no-prefetch" in r.text
    # One plan: the list from the day's cache, then the chosen page.
    wire.answers = [(200, {"items": [ITEMS[2]]})]
    r = client.get(FIND, params={"label": L3, "q": "Springfield Electric Co"})
    assert r.status_code == 200 and len(wire.asked) == 2 and wire.asked[1][1]["getpage"] == L3
    assert 'class="tou"' in r.text and "Use this plan" in r.text and "2025-01-01, 2024-01-01, 2023-01-01" in r.text and "Fixed charge $12.50/month" in r.text
    assert '<li class="now" aria-current="step"><span>Compare</span>' in r.text and "&label=" not in r.text.split('class="find-steps"')[1].split("</ol>")[0]
    assert 'class="pbar-back always" href="/settings/money/plan?' in r.text and "‹ Plans" in r.text, "a way back to the plans from the plan beside yours"
    # Use: from the cache, no further request; the plan is saved and home charges forgotten for re-pricing.
    r = client.post("/settings/money/plan-search/use", data=form(bare, {"label": L3, "q": "Springfield Electric Co"}))
    assert r.status_code == 303 and r.headers["location"] == "/settings/money?saved=rates" and len(wire.asked) == 2
    plan = saved["rate_plan"]
    assert plan["source"] == f"urdb:{L3}" and [v["from"] for v in plan["versions"]] == ["2023-01-01", "2024-01-01", "2025-01-01"]
    assert any(s.startswith("DELETE FROM cc_charge_facts WHERE at_home") for s in store.ran)
    assert KEY not in caplog.text


def test_a_failed_search_says_why_without_the_key(bare, money, client, monkeypatch, caplog):  # noqa: F811
    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    _wire(monkeypatch, (403, {"error": {"code": "API_KEY_INVALID", "message": f"bad {KEY}"}}))
    caplog.set_level(logging.DEBUG)
    r = client.get(FIND, params={"q": "62701"})
    assert "OpenEI refused the API key" in r.text and KEY not in r.text and KEY not in caplog.text
    r = client.get(FIND, params={"q": ""})
    assert 'name="q"' in r.text and 'class="notice err"' not in r.text, "no utility: the first step, asking nothing"
    r = client.post("/settings/money/plan-search/use", data=form(bare, {"label": "\"><b>bold"}))
    assert "not an OpenEI plan id" in r.text and "<b>bold" not in r.text


def test_a_relay_needs_no_key_and_gets_none(bare, money, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare, "URDB_RELAY", "https://relay.example/urdb")
    page = client.get(FIND).text
    assert 'name="q"' in page and 'name="key"' not in page and "plan relay" in page
    wire = _wire(monkeypatch, (200, {"items": ITEMS}))
    client.get(FIND, params={"q": "62701"})
    url, sent = wire.asked[0]
    assert url == "https://relay.example/urdb" and "api_key" not in sent and sent["address"] == "62701"


def test_typed_text_comes_back_escaped(bare, money, client, monkeypatch):  # noqa: F811
    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    _wire(monkeypatch, (200, {"items": []}))
    r = client.get(FIND, params={"q": '<img src=x onerror=alert(1)>"'})
    assert "<img src=x" not in r.text and "&lt;img src=x" in r.text


def test_a_grid_plan_can_be_given_up_for_typed_prices(bare, money, client, monkeypatch):  # noqa: F811
    store, saved = money
    plan = urdb.to_plan(urdb.versions_of(L1, [urdb.trim(x) for x in ITEMS]))
    monkeypatch.setattr(bare, "current_settings", lambda: {**bare.settings_mod.DEFAULTS, "rate_plan": plan})
    page = client.get("/settings/money").text
    assert "Stop using Residential EV-TOU (Sch)" in page and "its page" in page and 'name="pv2_3"' in page and "Change plan" in page
    r = client.post("/settings/money/plan/clear", data=form(bare, {}))
    assert r.status_code == 303 and r.headers["location"] == "/settings/money?saved=rates" and saved["rate_plan"] is None


def test_a_typed_plan_survives_trying_an_openei_one(bare, money, client, monkeypatch):  # noqa: F811
    """Typed in → Use an OpenEI plan → Go back: the typed plan is back, prices and all."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    import cost
    from conftest import SPRINGFIELD_RATES, SPRINGFIELD_SCHEDULE

    store, saved = money
    manual = cost.compile_rows("Springfield EV", SPRINGFIELD_RATES, SPRINGFIELD_SCHEDULE)
    saved["rate_plan"] = manual
    monkeypatch.setattr(bare, "current_settings", lambda: {**bare.settings_mod.DEFAULTS, **saved})
    urdb.save_key(store, bare.VAULT, KEY)
    _wire(monkeypatch, (200, {"items": [ITEMS[2]]}), (200, {"items": ITEMS}))
    r = client.post("/settings/money/plan-search/use", data=form(bare, {"label": L3, "q": "Springfield Electric Co"}))
    assert r.status_code == 303 and saved["rate_plan"]["source"] == f"urdb:{L3}"
    assert saved["manual_plan"] == manual, "kept beside it, not thrown away"
    page = client.get("/settings/money").text
    assert "Go back to Springfield EV" in page
    deletes = sum(s.startswith("DELETE FROM cc_charge_facts WHERE at_home") for s in store.ran)
    r = client.post("/settings/money/plan/clear", data=form(bare, {}))
    assert r.status_code == 303 and r.headers["location"] == "/settings/money?saved=rates"
    assert saved["rate_plan"] == manual
    assert sum(s.startswith("DELETE FROM cc_charge_facts WHERE at_home") for s in store.ran) == deletes + 1, "re-priced by the usual path"
    tz = ZoneInfo("America/Chicago")
    start = datetime(2026, 7, 6, 17, tzinfo=tz)
    hour = [(start, start + timedelta(hours=1), 7.0)]
    assert cost.session_cost(hour, start, start + timedelta(hours=1), tz, saved["rate_plan"]) == cost.session_cost(hour, start, start + timedelta(hours=1), tz, manual)
    # With no typed plan to go back to, Stop using leaves none.
    saved.pop("manual_plan")
    saved["rate_plan"] = urdb.to_plan([urdb.trim(ITEMS[2])])
    client.post("/settings/money/plan/clear", data=form(bare, {}))
    assert saved["rate_plan"] is None


def test_a_slow_openei_is_given_up_on_at_the_deadline(bare, money, client, monkeypatch):  # noqa: F811
    import httpx

    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    _wire(monkeypatch, httpx.ReadTimeout("slow"))
    r = client.get(FIND, params={"q": "62701"})
    assert f"OpenEI did not answer within {urdb.DEADLINE_S} seconds" in r.text


def test_a_long_list_says_it_is_cut(bare, money, client, monkeypatch):  # noqa: F811
    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    many = [dict(ITEMS[3], label=f"{i:024x}", name=f"Plan {i:03}") for i in range(urdb.SHOWN + 10)]
    _wire(monkeypatch, (200, {"items": many}))
    r = client.get(FIND, params={"q": "62701", "all": "1"})
    assert f"Showing the first {urdb.SHOWN} of {urdb.SHOWN + 10}" in r.text


def test_a_plan_fetches_versions_the_list_does_not_hold(bare, money, client, monkeypatch):  # noqa: F811
    """The utility's list is newest first and capped; older versions come by following the plan back."""
    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    wire = _wire(monkeypatch, (200, {"items": [ITEMS[2], ITEMS[3]]}), (200, {"items": [ITEMS[2]]}), (200, {"items": [ITEMS[1]]}), (200, {"items": [ITEMS[0]]}))
    client.get(FIND, params={"q": "", "eia": "99999"})
    assert wire.asked[0][1]["eia"] == 99999 and "ratesforutility" not in wire.asked[0][1] and "address" not in wire.asked[0][1]
    r = client.get(FIND, params={"label": L3, "eia": "99999"})
    assert [p.get("getpage") for _, p in wire.asked[1:]] == [L3, L2, L1]
    assert "2025-01-01, 2024-01-01, 2023-01-01" in r.text


def test_the_utility_list_is_served_from_the_box_and_a_typed_name_is_picked(bare, money, client, monkeypatch):  # noqa: F811
    store, _ = money
    r = client.get("/settings/money/utilities.json")
    assert r.status_code == 200 and r.json()["utilities"][0][0] > 0 and "pg&e" in r.json()["aliases"]
    urdb.save_key(store, bare.VAULT, KEY)
    page = client.get(FIND).text
    assert 'name="eia"' in page and "utilities.json" in page
    wire = _wire(monkeypatch, (200, {"items": []}), (200, {"items": []}))
    client.get(FIND, params={"q": "PG&E"})
    assert wire.asked[0][1]["eia"] == 14328, "an alias typed in full is the utility, by its EIA id"
    assert wire.asked[1][1]["ratesforutility"] == "Pacific Gas & Electric Co", "nothing filed under the id: by its name"


def test_an_id_search_keeps_only_that_utilitys_rates(bare, money, client, monkeypatch):  # noqa: F811
    """Should OpenEI ever ignore `eia`, its answer is every utility's: only the one picked is listed."""
    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    stranger = dict(ITEMS[3], label="f" * 24, name="Shelbyville Night Owl", utility="Shelbyville Power", eiaid=11111)
    _wire(monkeypatch, (200, {"items": [*ITEMS, stranger]}))
    r = client.get(FIND, params={"q": "Springfield Electric Co", "eia": "99999", "all": "1"})
    assert "Residential EV-TOU (Sch)" in r.text and "Shelbyville" not in r.text


# --- what makes the choice obvious ------------------------------------------------------


def test_the_home_zip_suggests_its_utility_without_asking_anyone(bare, money, client, monkeypatch):  # noqa: F811
    """The home's ZIP code, from its stored address, names its utility from the bundled list;
    the state alone offers the state's largest. Nothing goes out: no request at all."""
    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    monkeypatch.setattr(bare, "home_where", lambda: ("CA", "93721"))
    page = client.get(FIND).text
    assert "Your utility, from your home's ZIP code" in html.unescape(page) and "CC BY 4.0" in page
    assert 'value="Pacific Gas &amp; Electric Co"' in page and 'value="14328"' in page and "Serving 93721" in page
    monkeypatch.setattr(bare, "home_where", lambda: ("CA", None))
    page = client.get(FIND).text
    assert "Largest in CA" in page and "eia=14328" in page and 'id="urdb-eia" name="eia" value=""' in page, "a state alone fills nothing in"


def test_the_home_place_gives_its_state_and_zip_from_what_is_stored(bare, monkeypatch):  # noqa: F811
    home = {"id": 1, "name": "Home", "icon": "home", "auto": False, "address": None, "user_address": "1 Alpha Way, Springfield, IL 62701", "latitude": 39.8, "longitude": -89.65, "merged_into": None}
    monkeypatch.setattr(bare, "place_rows", lambda: [home])
    monkeypatch.setattr(bare, "db", lambda: Store())
    assert bare.home_where() == ("IL", "62701")
    monkeypatch.setattr(bare, "place_rows", lambda: [dict(home, user_address="1 Alpha Way")])
    assert bare.home_where() == ("IL", None)
    monkeypatch.setattr(bare, "place_rows", lambda: [])
    assert bare.home_where() == (None, None)


def test_zip_codes_name_their_utilities_from_the_bundled_list():
    assert urdb.zip_of("12345 Alpha Way, Springfield, IL 62701-1234") == "62701" and urdb.zip_of("12345 Alpha Way") is None
    assert urdb.zip_of(None, "Bravo St, Springfield, Sangamon County, Illinois, 62701, United States") == "62701"
    near = urdb.in_state("IL", zip_code="62701")
    assert near["zip"] == "62701" and {r["eia"] for r in near["rows"] if r["zip"]} == {17828, 56697} and not near["sure"]
    assert urdb.in_state("CA", zip_code="93721")["sure"] and urdb.in_state("CA", zip_code="93721")["rows"][0]["eia"] == 14328
    assert not urdb.in_state("IL", zip_code="93721")["zip"], "a ZIP code from another state is no answer"
    assert urdb.in_state(None) == {"state": None, "zip": None, "rows": [], "sure": False}


def test_the_plans_step_shows_the_few_to_choose_between_first(bare, money, client, monkeypatch):  # noqa: F811
    """The plan like theirs first and marked, open EV plans next; the rest behind Show all,
    where closed and ended are folded below, each saying so. Nothing but the name is sent."""
    store, saved = money
    urdb.save_key(store, bare.VAULT, KEY)
    _typed(bare, saved, monkeypatch)
    wire = _wire(monkeypatch, (200, {"items": LARGE}))
    r = client.get(FIND, params={"q": "Springfield Electric Co"})
    assert wire.asked[0][1]["ratesforutility"] == "Springfield Electric Co" and not {"name", "rate_plan", "plan"} & set(wire.asked[0][1])
    main = r.text.split("<main")[1]
    assert main.index("EV2 (Sch)") < main.index("EV (Sch)<") and main.count("Matches EV2A") == 1
    assert "E-1 Residential" not in main and "Closed rate" not in main and "Show all 5 plans" in main
    full = client.get(FIND, params={"q": "Springfield Electric Co", "all": "1"}).text.split("<main")[1]
    head, old = full.split('<details class="find-more old"')
    assert "E-1 Residential" in head and "13 price changes since 2014" in head
    assert "Closed to new customers since Dec 1, 2025" in old and "Ended Dec 31, 2021" in old
    assert "2026-03-27 to 2025-12-01" not in full and len(wire.asked) == 1, "the day's cache answers the second look"


def test_one_plan_beside_yours_on_your_charging(bare, money, client, monkeypatch):  # noqa: F811
    store, saved = money
    urdb.save_key(store, bare.VAULT, KEY)
    _typed(bare, saved, monkeypatch)
    _home(bare, monkeypatch)
    newest = _lab("e", 12)
    wire = _wire(monkeypatch, (200, {"items": LARGE}), (200, {"items": [LARGE[12]]}))
    r = client.get(FIND, params={"label": newest, "q": "Springfield Electric Co"})
    assert [p.get("getpage") for _, p in wire.asked] == [None, newest], "the comparison asks OpenEI nothing"
    page = html.unescape(r.text)
    assert "At Springfield Electric's published prices" in page and "$461" in page, "2,400 kWh on weekday nights at 0.192"
    assert "Before (typed in)" in page and "$240" in page and "2,400 kWh" in page and "last 12 months" in page
    assert "Springfield EV2A kept as Go back" in page and "Matches EV2A" in page and "tariff" not in page.lower()
    assert "Yours is lower than Springfield Electric's published prices" in page and "Read your bill" in page
    assert f'name="label" value="{newest}"' in r.text and 'action="/settings/money/plan-search/use"' in r.text


def test_a_tariff_near_your_prices_is_not_called_lower(bare, money, client, monkeypatch):  # noqa: F811
    store, saved = money
    urdb.save_key(store, bare.VAULT, KEY)
    _typed(bare, saved, monkeypatch, low=False)
    _home(bare, monkeypatch)
    _wire(monkeypatch, (200, {"items": LARGE}), (200, {"items": [EV_CLOSED]}))
    r = client.get(FIND, params={"label": EV_CLOSED["label"], "q": "Springfield Electric Co"})
    assert "published prices" in r.text and "Yours is lower" not in r.text and "btn primary" in r.text
    assert "Closed to new customers since Dec 1, 2025" in r.text, "the plan's own page says it is closed"


def test_regional_variants_are_one_choice_with_a_variant_picker(bare, money, client, monkeypatch):  # noqa: F811
    from test_urdb import EV_OPEN

    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    regions = [dict(EV_OPEN, label=_lab("a", i), name=f"E-TOU-C Residential Time of Use Baseline Region {c}") for i, c in enumerate("PQRS")]
    _wire(monkeypatch, (200, {"items": regions}), (200, {"items": [regions[1]]}))
    r = client.get(FIND, params={"q": "Springfield Electric Co"})
    main = r.text.split("<main")[1]
    assert main.count('class="find-row') == 1 and "4 variants" in main and ">E-TOU-C Residential Time of Use<" in main
    r = client.get(FIND, params={"q": "Springfield Electric Co", "label": _lab("a", 1)})
    assert r.text.count("<option") == 4 and f'value="{_lab("a", 1)}" selected' in r.text and 'name="label"' in r.text


def test_after_use_the_page_says_so_at_the_top_with_the_way_back(bare, money, client, monkeypatch):  # noqa: F811
    store, saved = money
    urdb.save_key(store, bare.VAULT, KEY)
    _typed(bare, saved, monkeypatch)
    _wire(monkeypatch, (200, {"items": [LARGE[12]]}), (200, {"items": LARGE}))
    r = client.post("/settings/money/plan-search/use", data=form(bare, {"label": _lab("e", 12), "q": "Springfield Electric Co"}))
    assert r.status_code == 303 and saved["rate_plan"]["name"] == "Electric Vehicle EV2 (Sch)" and saved["manual_plan"]["name"] == "Springfield EV2A"
    page = client.get(r.headers["location"]).text
    top = page[page.index('class="plan-sum"') : page.index('id="plan-name"')]
    assert "Electric Vehicle EV2 (Sch)" in top and "Go back to Springfield EV2A" in top and 'form="plan-clear"' in top
    assert page.count("Go back to Springfield EV2A") == 1 and "Re-pricing home charges" in page


def test_the_home_profile_reads_each_charge_at_home_once(bare, monkeypatch):  # noqa: F811
    """A year of charges, home by their own position; each one's power runs fold into the
    profile, nothing from away or from between charges."""
    from datetime import datetime, timedelta, timezone

    import cost

    # 19:00 UTC, midday across the US, on a Saturday: the charge never straddles a local midnight, and the
    # one two days on is a weekday, so the two never share a profile bucket whatever day the test runs
    t0 = datetime.now(timezone.utc).replace(hour=19, minute=0, second=0, microsecond=0) - timedelta(days=30)
    t0 -= timedelta(days=(t0.weekday() - 5) % 7)
    hour = timedelta(hours=1)
    sessions = [
        {"vin": "A", "a": t0, "b": t0 + 2 * hour, "lat": 1.0, "lon": 1.0},
        {"vin": "A", "a": t0 + 24 * hour, "b": t0 + 25 * hour, "lat": 9.0, "lon": 9.0},  # away
        {"vin": "A", "a": t0 + 48 * hour, "b": t0 + 49 * hour, "lat": 1.0, "lon": 1.0},
    ]
    runs = [
        {"first_date": t0, "last_date": t0 + 2 * hour, "power": 7.0},
        {"first_date": t0 + 2 * hour, "last_date": t0 + 24 * hour, "power": 0.0},
        {"first_date": t0 + 24 * hour, "last_date": t0 + 25 * hour, "power": 50.0},
        {"first_date": t0 + 30 * hour, "last_date": t0 + 31 * hour, "power": 3.0},  # between charges: not one
        {"first_date": t0 + 48 * hour, "last_date": t0 + 49 * hour, "power": 11.0},
    ]

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, sql, args=()):
            s = " ".join(sql.split())
            out = sessions if "FROM charging_sessions" in s else runs if "FROM charging_powers" in s else [{"t": None}]

            class R:
                def fetchall(self):
                    return out

                def fetchone(self):
                    return out[0]

            return R()

    monkeypatch.setattr(bare, "db", Conn)
    monkeypatch.setattr(bare, "places", lambda: [])
    monkeypatch.setattr(bare, "is_home", lambda lat, lon, known=None: lat == 1.0)
    bare._cache.pop("home_profile", None)
    hp = bare.home_profile()
    bare._cache.pop("home_profile", None)
    assert hp["charges"] == 2 and hp["kwh"] == pytest.approx(14.0 + 11.0) and len(hp["profile"]) == 576
    local = t0.astimezone(bare.TZ)
    assert hp["first"] == local.date() and hp["profile"][cost.profile_index(local.weekday() >= 5, local.month, local.hour)] == pytest.approx(7.0)


# --- a bill read here -------------------------------------------------------------------


def test_a_bill_goes_straight_to_its_plan_with_its_prices(bare, money, client, monkeypatch):  # noqa: F811
    """Upload → the bill's utility and schedule pick the plan (nothing of the file sent, only
    the utility's id as on a pick) → the plan at the bill's prices beside the tariff's →
    Use with your bill's prices. The PDF itself is kept nowhere."""
    from urllib.parse import parse_qs, urlparse

    from test_billread import BILL, pdf

    store, saved = money
    urdb.save_key(store, bare.VAULT, KEY)
    _typed(bare, saved, monkeypatch)
    _home(bare, monkeypatch)
    pge = [dict(x, utility="Pacific Gas & Electric Co", eiaid=14328) for x in LARGE]
    wire = _wire(monkeypatch, (200, {"items": pge}), (200, {"items": [pge[12]]}), (200, {"items": [pge[13]]}))
    r = client.post("/settings/money/plan/bill?csrf_token=" + form(bare, {})["csrf_token"], files={"bill": ("bill.pdf", pdf(BILL), "application/pdf")})
    assert r.status_code == 303 and r.headers["location"].startswith("/settings/money/plan?")
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    assert q["eia"] == "14328" and q["bill"] == "EV2A" and q["cca"] == "MCE" and q["bm"] == "7" and q["bp"].count(":") == 3
    r = client.get(r.headers["location"], follow_redirects=False)
    assert r.status_code == 303 and f"label={_lab('e', 12)}" in r.headers["location"], "the plan the bill names, picked"
    assert wire.asked[0][1] == {**wire.asked[0][1], "eia": 14328} and not {"bill", "bp", "cca"} & set(wire.asked[0][1]), "only the utility goes out"
    page = html.unescape(client.get(r.headers["location"]).text)
    assert "Read from your bill · <b>EV2A</b> · MCE" in page and "What you'll be charged" in page and "PG&E's published prices" in page
    assert "Use your bill's prices" in page and "Use PG&E's published prices" in page and "tariff" not in page.lower()
    assert not any(isinstance(v, (bytes, bytearray)) for v in store.state.values()) and not store.cache.get("bill")
    # the Plan step's link keeps the reading but no longer jumps: the short list, reachable
    steps = page.split('class="find-steps"')[1].split("</ol>")[0]
    plan_link = html.unescape(steps.split('href="')[2].split('"')[0])
    assert "bill=EV2A" in plan_link and "go=" not in plan_link and client.get(plan_link, follow_redirects=False).status_code == 200
    # another plan of the same utility is shown at its published prices, never the bill's
    other = html.unescape(client.get(f"/settings/money/plan?eia=14328&label={_lab('f', 1)}&" + "&".join(f"{k}={q[k]}" for k in ("bill", "bp", "cca", "bm", "bs"))).text)
    assert "What you'll be charged" not in other and "Use your bill's prices" not in other
    refused = client.post("/settings/money/plan-search/use", data=form(bare, {"label": _lab("f", 1), "eia": "14328", "as": "bill", **{k: q[k] for k in ("bill", "bp", "cca", "bm", "bs")}}))
    assert "its prices are not this plan's" in html.unescape(refused.text) and saved["rate_plan"]["name"] == "Springfield EV2A"
    r = client.post("/settings/money/plan-search/use", data=form(bare, {"label": _lab("e", 12), "eia": "14328", "as": "bill", **{k: q[k] for k in ("bill", "bp", "cca", "bm", "bs")}}))
    plan = saved["rate_plan"]
    assert r.status_code == 303 and plan["source"] == f"bill:{_lab('e', 12)}" and plan["name"] == "EV2A · MCE (your bill)"
    assert [v["from"] for v in plan["versions"]] == ["2024-01-01", "2026-07-01"], "charges before the bill keep the typed plan"
    assert saved["manual_plan"]["name"] == "Springfield EV2A", "the typed plan is kept to go back to"


def test_an_oversize_upload_is_refused_as_it_arrives(bare, money, client):  # noqa: F811
    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    big = b"%PDF-1.4\n" + b"0" * (11 * 1024 * 1024)
    r = client.post("/settings/money/plan/bill?csrf_token=" + form(bare, {})["csrf_token"], files={"bill": ("big.pdf", big, "application/pdf")})
    assert r.status_code == 413 and "over 10 MB" in r.text


def test_a_broken_pdf_is_refused_plainly(bare, money, client):  # noqa: F811
    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    r = client.post("/settings/money/plan/bill?csrf_token=" + form(bare, {})["csrf_token"], files={"bill": ("x.pdf", b"%PDF-1.7\n1 0 obj << /Kids [1 0 R] >>", "application/pdf")})
    assert r.status_code == 200 and "Couldn&#39;t read this bill" in r.text and 'name="q"' in r.text


def test_a_utility_with_community_providers_offers_them_as_generation(bare, money, client, monkeypatch):  # noqa: F811
    """PG&E's area: a Generation row, PG&E picked, its providers leading to the bill."""
    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    pge = [dict(x, utility="Pacific Gas & Electric Co", eiaid=14328) for x in LARGE]
    _wire(monkeypatch, (200, {"items": pge}))
    page = html.unescape(client.get(FIND, params={"q": "Pacific Gas & Electric Co", "eia": "14328"}).text)
    gen = page.split('class="set-row stack find-gen"')[1].split("</div></div>")[0]
    assert '<span class="btn sm on" aria-current="true">PG&E</span>' in gen and ">Community provider</a>" in gen and gen.count('class="btn sm') == 2
    assert "MCE, " in gen and "SVCE" in gen and "Sonoma Clean Power" in gen and "#bill" in gen, "the providers named in the tooltip and the title"
    step = html.unescape(client.get(FIND, params={"cca": "MCE"}).text)
    assert "MCE · read its prices from your bill" in step and "Choose bill PDF" in step


def test_a_second_bill_at_once_is_told_to_wait(bare, money, client, monkeypatch):  # noqa: F811
    from test_billread import BILL, pdf

    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    taken = [billread_mod._SLOTS.acquire(blocking=False) for _ in range(2)]
    try:
        r = client.post("/settings/money/plan/bill?csrf_token=" + form(bare, {})["csrf_token"], files={"bill": ("bill.pdf", pdf(BILL), "application/pdf")})
    finally:
        for got in taken:
            if got:
                billread_mod._SLOTS.release()
    assert r.status_code == 429 and "Another bill is being read" in r.text


def test_the_capped_reader_stops_reading_past_the_limit(bare):  # noqa: F811
    """A body streamed in chunks: read only until it passes the limit, then None, the rest
    left unread."""
    import asyncio

    read = []

    class Stream:
        async def stream(self):
            for i in range(1000):
                read.append(i)
                yield b"x" * 1024

    assert asyncio.run(bare.read_capped(Stream(), 10 * 1024)) is None and len(read) == 11, "stops at the chunk that passed the limit"
    read.clear()
    assert asyncio.run(bare.read_capped(Stream(), 2_000_000)) == b"x" * 1024 * 1000 and len(read) == 1000


def test_what_is_not_a_bill_says_so_on_the_first_step(bare, money, client):  # noqa: F811
    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    r = client.post("/settings/money/plan/bill?csrf_token=" + form(bare, {})["csrf_token"], files={"bill": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 200 and "That is not a PDF" in r.text and 'name="q"' in r.text


def test_a_typed_plans_hours_are_checked_against_openeis_once_a_day(bare, money, client, monkeypatch):  # noqa: F811
    """The tariff of the same code, today's version: an hour in another role is said on Money
    and alerted once; only the utility's id and the tariff's id are asked for."""
    import cost
    from datetime import date

    store, saved = money
    urdb.save_key(store, bare.VAULT, KEY)
    kept, sent = {}, []
    monkeypatch.setattr(bare, "state_get", lambda k: kept.get(k))
    monkeypatch.setattr(bare, "state_set", lambda k, v: kept.__setitem__(k, v))
    monkeypatch.setattr(bare.recorder, "notify", lambda text, kind="other", vin=None: sent.append((kind, text)))
    monkeypatch.setattr(urdb, "utility_in", lambda text: {"eia": 99999, "name": "Springfield Electric Co", "states": "IL"})
    rows = [{"period": "part_peak", "days": "all", "start": 6, "end": 16, "months": None}, {"period": "peak", "days": "all", "start": 16, "end": 21, "months": None}]

    def typed(evening_end):
        saved["rate_plan"] = cost.compile_rows(
            "Springfield EV-TOU",
            [{"from": "2024-01-01", "off_peak": 0.1, "part_peak": 0.2, "peak": 0.3}],
            rows + [{"period": "part_peak", "days": "all", "start": 21, "end": evening_end, "months": None}],
        )
        monkeypatch.setattr(bare, "current_settings", lambda: {**bare.settings_mod.DEFAULTS, **saved})

    typed(23)
    wire = _wire(monkeypatch, (200, {"items": ITEMS}), (200, {"items": [ITEMS[2]]}))
    got = bare.check_plan_hours(today=date(2026, 10, 19))
    assert got["differ"] == ["off-peak here, part-peak there: 11pm–12am every month"] and got["label"] == L3
    assert "99999" in [str(v) for v in wire.asked[0][1].values()] and wire.asked[1][1]["getpage"] == L3
    assert len(sent) == 1 and sent[0][0] == "data" and "11pm–12am" in sent[0][1]
    assert "Hours differ from OpenEI" in client.get("/settings/money").text and "11pm–12am every month" in client.get("/settings/money").text
    bare.check_plan_hours(today=date(2026, 10, 20))
    assert len(sent) == 1, "the same difference is said once"
    typed(24)
    assert bare.check_plan_hours(today=date(2026, 10, 21))["differ"] == [] and "Hours differ" not in client.get("/settings/money").text
