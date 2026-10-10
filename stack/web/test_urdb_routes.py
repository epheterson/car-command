"""Find my plan on Settings › Money: nothing is asked of OpenEI until a button is pressed,
only what was typed is sent, and the key is never shown back or logged."""

import logging

import pytest

import sources as sources_mod
import urdb
from test_empty import bare, client  # noqa: F401 - fixtures
from test_settings_routes import form
from test_urdb import ITEMS, KEY, L1, L2, L3, Rows, Wire


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


def test_the_page_explains_the_key_and_asks_nothing_on_load(money, client):  # noqa: F811
    r = client.get("/settings/money")
    assert r.status_code == 200
    assert "Plan search covers US utilities only." in r.text
    assert "https://openei.org/services/api/signup/" in r.text and 'name="key"' in r.text
    assert 'name="q"' not in r.text, "no search box without a key or a relay"
    assert 'name="from0"' in r.text, "manual entry still works"


def test_a_saved_key_is_sealed_and_shown_only_by_its_last_four(bare, money, client):  # noqa: F811
    store, _ = money
    r = client.post("/settings/money/plan-search/key", data=form(bare, {"key": KEY}))
    assert r.status_code == 303 and r.headers["location"].endswith("#find")
    assert KEY not in store.state[urdb.KEY_STATE] and bare.VAULT.open(__import__("json").loads(store.state[urdb.KEY_STATE])["sealed"].encode(), "state", urdb.KEY_STATE) == KEY
    page = client.get("/settings/money").text
    assert "saved, ends …wxyz" in page and KEY not in page and 'name="q"' in page
    bad = client.post("/settings/money/plan-search/key", data=form(bare, {"key": "not a key"}))
    assert "does not look like an OpenEI API key" in bad.text and "not a key" not in bad.text.split("does not look")[0][-200:]
    client.post("/settings/money/plan-search/key", data=form(bare, {"remove": "1"}))
    assert urdb.KEY_STATE not in store.state and 'name="key"' in client.get("/settings/money").text


def test_without_a_sealing_key_the_key_is_not_kept(bare, money, client, monkeypatch):  # noqa: F811
    store, _ = money
    monkeypatch.setattr(bare, "VAULT", sources_mod.Vault(None))
    r = client.post("/settings/money/plan-search/key", data=form(bare, {"key": KEY}))
    assert "cannot be kept" in r.text and urdb.KEY_STATE not in store.state and KEY not in r.text


def test_search_preview_and_use(bare, money, client, monkeypatch, caplog):  # noqa: F811
    store, saved = money
    urdb.save_key(store, bare.VAULT, KEY)
    wire = _wire(monkeypatch, (200, {"items": ITEMS}))
    caplog.set_level(logging.DEBUG)
    r = client.post("/settings/money/plan-search", data=form(bare, {"q": "Springfield Electric Co", "contains": ""}))
    assert r.status_code == 200 and "Residential EV-TOU (Sch)" in r.text and "3 versions since 2023-01-01" in r.text and "Time-of-use, tiered" in r.text
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
    assert KEY not in r.text and f'value="{L3}"' in r.text
    # Preview: the chosen page, then the utility's list from the day's cache.
    wire.answers = [(200, {"items": [ITEMS[2]]})]
    r = client.post("/settings/money/plan-search/preview", data=form(bare, {"label": L3, "q": "Springfield Electric Co"}))
    assert r.status_code == 200 and len(wire.asked) == 2 and wire.asked[1][1]["getpage"] == L3
    assert 'class="tou"' in r.text and "Use this plan" in r.text and "2025-01-01, 2024-01-01, 2023-01-01" in r.text and "Fixed charge $12.50/month" in r.text
    assert "Residential Flat" in r.text, "the list stays, from the cache"
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
    r = client.post("/settings/money/plan-search", data=form(bare, {"q": "62701"}))
    assert "OpenEI refused the API key" in r.text and KEY not in r.text and KEY not in caplog.text
    r = client.post("/settings/money/plan-search", data=form(bare, {"q": ""}))
    assert "Type your utility" in r.text
    r = client.post("/settings/money/plan-search/use", data=form(bare, {"label": "\"><b>bold"}))
    assert "not an OpenEI plan id" in r.text and "<b>bold" not in r.text


def test_a_relay_needs_no_key_and_gets_none(bare, money, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare, "URDB_RELAY", "https://relay.example/urdb")
    page = client.get("/settings/money").text
    assert 'name="q"' in page and 'name="key"' not in page and "plan relay" in page
    wire = _wire(monkeypatch, (200, {"items": ITEMS}))
    client.post("/settings/money/plan-search", data=form(bare, {"q": "62701"}))
    url, sent = wire.asked[0]
    assert url == "https://relay.example/urdb" and "api_key" not in sent and sent["address"] == "62701"


def test_typed_text_comes_back_escaped(bare, money, client, monkeypatch):  # noqa: F811
    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    _wire(monkeypatch, (200, {"items": []}))
    r = client.post("/settings/money/plan-search", data=form(bare, {"q": '<img src=x onerror=alert(1)>"'}))
    assert "<img src=x" not in r.text and "&lt;img src=x" in r.text


def test_a_grid_plan_can_be_given_up_for_typed_prices(bare, money, client, monkeypatch):  # noqa: F811
    store, saved = money
    plan = urdb.to_plan(urdb.versions_of(L1, [urdb.trim(x) for x in ITEMS]))
    monkeypatch.setattr(bare, "current_settings", lambda: {**bare.settings_mod.DEFAULTS, "rate_plan": plan})
    page = client.get("/settings/money").text
    assert "Stop using Residential EV-TOU (Sch)" in page and "its page" in page and 'name="pv2_3"' in page
    r = client.post("/settings/money/plan/clear", data=form(bare, {}))
    assert r.status_code == 303 and r.headers["location"] == "/settings/money?saved=rates" and saved["rate_plan"] is None


# --- review fixes ----------------------------------------------------------------------


def test_a_typed_plan_survives_trying_an_openei_one(bare, money, client, monkeypatch):  # noqa: F811
    """Typed in → Use an OpenEI plan → Stop using it: the typed plan is back, prices and all."""
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
    r = client.post("/settings/money/plan-search", data=form(bare, {"q": "62701"}))
    assert f"OpenEI did not answer within {urdb.DEADLINE_S} seconds" in r.text


def test_a_long_list_says_it_is_cut(bare, money, client, monkeypatch):  # noqa: F811
    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    many = [dict(ITEMS[3], label=f"{i:024x}", name=f"Plan {i:03}") for i in range(urdb.SHOWN + 10)]
    _wire(monkeypatch, (200, {"items": many}))
    r = client.post("/settings/money/plan-search", data=form(bare, {"q": "62701"}))
    assert f"showing the first {urdb.SHOWN}; narrow with the plan name filter" in r.text


def test_preview_fetches_versions_the_list_does_not_hold(bare, money, client, monkeypatch):  # noqa: F811
    """The utility's list is newest first and capped; older versions come by following the plan back."""
    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    wire = _wire(monkeypatch, (200, {"items": [ITEMS[2], ITEMS[3]]}), (200, {"items": [ITEMS[2]]}), (200, {"items": [ITEMS[1]]}), (200, {"items": [ITEMS[0]]}))
    client.post("/settings/money/plan-search", data=form(bare, {"q": "", "eia": "99999"}))
    assert wire.asked[0][1]["eia"] == 99999 and "ratesforutility" not in wire.asked[0][1] and "address" not in wire.asked[0][1]
    r = client.post("/settings/money/plan-search/preview", data=form(bare, {"label": L3, "eia": "99999"}))
    assert [p.get("getpage") for _, p in wire.asked[1:]] == [L3, L2, L1]
    assert "2025-01-01, 2024-01-01, 2023-01-01" in r.text


def test_the_utility_list_is_served_from_the_box_and_a_typed_name_is_picked(bare, money, client, monkeypatch):  # noqa: F811
    store, _ = money
    r = client.get("/settings/money/utilities.json")
    assert r.status_code == 200 and r.json()["utilities"][0][0] > 0 and "pg&e" in r.json()["aliases"]
    page = client.get("/settings/money").text
    urdb.save_key(store, bare.VAULT, KEY)
    page = client.get("/settings/money").text
    assert 'name="eia"' in page and "utilities.json" in page
    wire = _wire(monkeypatch, (200, {"items": []}), (200, {"items": []}))
    r = client.post("/settings/money/plan-search", data=form(bare, {"q": "PG&E"}))
    assert wire.asked[0][1]["eia"] == 14328, "an alias typed in full is the utility, by its EIA id"
    assert wire.asked[1][1]["ratesforutility"] == "Pacific Gas & Electric Co", "nothing filed under the id: by its name"


def test_an_id_search_keeps_only_that_utilitys_rates(bare, money, client, monkeypatch):  # noqa: F811
    """Should OpenEI ever ignore `eia`, its answer is every utility's: only the one picked is listed."""
    store, _ = money
    urdb.save_key(store, bare.VAULT, KEY)
    stranger = dict(ITEMS[3], label="f" * 24, name="Shelbyville Night Owl", utility="Shelbyville Power", eiaid=11111)
    _wire(monkeypatch, (200, {"items": [*ITEMS, stranger]}))
    r = client.post("/settings/money/plan-search", data=form(bare, {"q": "Springfield Electric Co", "eia": "99999"}))
    assert "Residential EV-TOU (Sch)" in r.text and "Shelbyville" not in r.text
