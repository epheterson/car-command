"""Setting up the house inside the Tesla flow: the steps, where each action returns, and progress that updates in place."""

import json
from datetime import date, datetime, timedelta

import pytest

import energy
import energy_cloud
import energypage

TODAY = date(2026, 10, 8)


def cfg(**cloud):
    c = energy.default_config()
    c["cloud"].update(cloud)
    return c


IDLE = energy_cloud.progress({}.get)


def flow(c=None, scope=None, choices=None, prog=IDLE):
    return energypage.flow(c or cfg(), scope, choices, prog, TODAY)


def states(f):
    return {s["id"]: s["state"] for s in f["steps"]}


def test_without_an_account_the_flow_asks_for_the_sign_in_first_and_the_rest_wait():
    f = flow()
    assert states(f) == {"signin": "next", "powerwall": "waiting", "history": "waiting"} and f["current"] == "signin"


def test_a_signed_in_account_without_the_scope_is_offered_to_bring_the_powerwall_in():
    f = flow(scope={"granted": False})
    assert states(f)["signin"] == "done" and states(f)["powerwall"] == "next" and f["current"] == "powerwall"
    step = f["steps"][1]
    assert step["need"] == "scope" and step["title"] == "Your Powerwall"


def test_with_the_scope_one_site_is_used_without_a_choice_and_several_ask_which():
    one = flow(scope={"granted": True}, choices=[{"site_id": "1", "name": "Springfield", "kind": "battery"}])
    assert states(one)["powerwall"] == "next" and one["steps"][1]["need"] == "use", "the page adopts a single site; until it has, it is one button"
    many = flow(scope={"granted": True}, choices=[{"site_id": "1", "name": "House", "kind": "battery"}, {"site_id": "2", "name": "Cabin", "kind": "battery"}])
    assert many["steps"][1]["need"] == "pick" and len(many["steps"][1]["choices"]) == 2
    none = flow(scope={"granted": True}, choices=[])
    assert none["steps"][1]["need"] == "none" and states(none)["powerwall"] == "next"


def test_the_site_chosen_makes_the_powerwall_step_done_and_the_history_the_next_with_its_days_and_cost():
    f = flow(cfg(enabled=True, site_id="1", name="Springfield", installed="2024-03-01"), scope={"granted": True})
    assert states(f) == {"signin": "done", "powerwall": "done", "history": "next"} and f["current"] == "history"
    assert f["steps"][1]["name"] == "Springfield"
    h = f["steps"][2]
    assert h["days"] == 2048 or h["days"] == energy_cloud.days_between("2024-03-01", TODAY)
    assert h["label"] == f"{h['days']:,} days, about ${h['days'] * energy_cloud.PER_REQUEST:.2f}"


def test_an_unknown_install_date_says_so_rather_than_guessing():
    f = flow(cfg(enabled=True, site_id="1", name="x"), scope={"granted": True})
    assert f["steps"][2]["days"] == 0 and f["steps"][2]["label"] == "One request a day, newest first"


def test_history_running_shows_progress_and_done_finishes_the_flow():
    running = {**IDLE, "state": "running", "done": 84, "total": 1734, "next": "2026-07-16"}
    f = flow(cfg(enabled=True, site_id="1", name="x"), {"granted": True}, prog=running)
    h = f["steps"][2]
    assert states(f)["history"] == "next" and h["running"] and h["percent"] == 5 and h["progress"] == "84 of 1,734 days"
    done = {**IDLE, "state": "done", "done": 1734, "total": 1734}
    f = flow(cfg(enabled=True, site_id="1", name="x"), {"granted": True}, prog=done)
    assert states(f)["history"] == "done" and f["current"] is None and f["steps"][2]["label"] == "1,734 days read"


def test_the_progress_line_names_every_step_and_there_is_no_gateway_step():
    f = flow(cfg(enabled=True, site_id="1", name="x"), {"granted": True})
    assert [s["n"] for s in f["steps"]] == [1, 2, 3] and f["line"] == "1 Signed in · 2 Powerwall · 3 History"
    assert "gateway" not in str(f).lower()


def test_the_progress_json_is_what_the_page_redraws_from():
    prog = {**IDLE, "state": "running", "done": 84, "total": 1734, "next": "2026-07-16"}
    got = energypage.backfill_json(prog, 1734)
    assert got == {"state": "running", "done": 84, "total": 1734, "percent": 5, "text": "84 of 1,734 days", "error": None}
    assert energypage.backfill_json({**IDLE, "state": "done", "done": 9, "total": 9}, 9)["text"] == "9 days read"
    assert energypage.backfill_json({**IDLE, "state": "error", "error": "Sign in again", "done": 2}, 9)["error"] == "Sign in again"
    assert energypage.backfill_json(IDLE, 0)["percent"] == 0
    json.dumps(got)


# ---- the Tesla page

from urllib.parse import parse_qs, urlparse  # noqa: E402

import tesla_fleet  # noqa: E402
from test_energy_settings import Calls, form, house, tesla_account  # noqa: E402,F401
from test_empty import bare, client  # noqa: E402,F401

SITE = {"energy_site_id": 1234567890, "resource_type": "battery", "site_name": "Springfield"}
INSTALLED = (200, {"response": {"installation_date": "2024-03-01T00:00:00-08:00"}})


@pytest.fixture
def tesla(house, monkeypatch):  # noqa: F811
    """The Connect Tesla page with an app, no facts to wait for, and a Fleet that answers from a queue."""
    monkeypatch.setattr(house.app, "tesla_held", lambda: (None, False))
    house.app.TESLA_CACHE.clear()

    def fleet(*answers):
        calls = Calls(*answers)
        monkeypatch.setattr(house.app, "tesla_app", lambda: tesla_fleet.Fleet("c", "s", call=calls))
        return calls

    house.fleet = fleet
    fleet()
    return house


def pw_section(html):
    return html[html.index('id="pw"') :]


def test_the_flow_is_on_the_tesla_page_with_its_numbered_line_and_asks_for_the_sign_in_first(tesla, client):  # noqa: F811
    html = client.get("/settings/sources/tesla").text
    section = pw_section(html)
    assert '<ol class="pwline"' in section and [t for t in ("Signed in", "Powerwall", "History") if t in section.split("</ol>")[0]] == [
        "Signed in",
        "Powerwall",
        "History",
    ]
    assert 'href="/settings/sources/tesla/start?energy=1"' in section and "Sign in with Tesla" in section


def test_without_the_scope_the_step_offers_to_bring_the_powerwall_in(tesla, client, monkeypatch):  # noqa: F811
    tesla_account(tesla, False, monkeypatch)
    section = pw_section(client.get("/settings/sources/tesla").text)
    assert "Bring it in" in section and "start?energy=1" in section and 'class="btn primary" href' in section.split("Bring it in")[0][-120:] + "Bring it in"


def test_viewing_the_page_never_calls_tesla_or_writes_and_offers_one_button_to_use_the_powerwall(tesla, client, monkeypatch):  # noqa: F811
    tesla_account(tesla, True, monkeypatch)
    calls = tesla.fleet()
    writes = []
    monkeypatch.setattr(tesla.app, "state_set", lambda k, v: writes.append(k))
    section = pw_section(client.get("/settings/sources/tesla").text)
    assert calls.sent == [] and writes == [] and not energy.load_config(tesla.state.get)["cloud"]["enabled"]
    assert "Use it" in section and 'class="btn primary">Use it' in section and "Find and use" not in section


def test_the_button_takes_the_accounts_only_site_and_the_next_step_is_history_with_the_primary_button(tesla, client, monkeypatch):  # noqa: F811
    tesla_account(tesla, True, monkeypatch)
    calls = tesla.fleet((200, {"response": [SITE]}), INSTALLED)
    r = client.post("/settings/energy/cloud", data=form(tesla.app, {"back": "tesla"}))
    assert r.status_code == 303 and r.headers["location"].endswith("#pw")
    cfg = energy.load_config(tesla.state.get)
    assert cfg["cloud"] == {"enabled": True, "site_id": "1234567890", "name": "Springfield", "installed": "2024-03-01"}
    assert [u.split("/api/1/")[1].split("?")[0] for u in calls.sent] == ["products", "energy_sites/1234567890/site_info"]
    section = pw_section(client.get("/settings/sources/tesla").text)
    assert "Springfield" in section and "Use it" not in section and len(calls.sent) == 2, "the view asked nothing"
    days = energy_cloud.days_between("2024-03-01", datetime.now(tesla.app.TZ).date() - timedelta(days=1))
    assert f"{days:,} days, about $" in section
    start = section[section.index("Backfill from Tesla") :]
    assert 'class="btn sm primary"' in start and ">Start<" in start and 'name="back" value="tesla"' in start


def test_several_sites_are_listed_from_what_the_button_read_and_the_pick_comes_back_to_the_step(tesla, client, monkeypatch):  # noqa: F811
    tesla_account(tesla, True, monkeypatch)
    calls = tesla.fleet((200, {"response": [SITE, {"energy_site_id": 55, "resource_type": "battery", "site_name": "Cabin"}]}))
    r = client.post("/settings/energy/cloud", data=form(tesla.app, {"back": "tesla"}))
    assert r.status_code == 303 and not energy.load_config(tesla.state.get)["cloud"]["enabled"], "which one is not a guess"
    section = pw_section(client.get("/settings/sources/tesla").text)
    assert len(calls.sent) == 1, "the list is from what was just read"
    assert "Springfield" in section and "Cabin" in section and section.count("Use this one") == 2 and 'name="site" value="55"' in section
    tesla.fleet(INSTALLED)
    r = client.post("/settings/energy/cloud", data=form(tesla.app, {"site": "55", "back": "tesla"}))
    assert r.status_code == 303 and r.headers["location"].endswith("#pw")
    assert energy.load_config(tesla.state.get)["cloud"]["site_id"] == "55" and energy.load_config(tesla.state.get)["cloud"]["name"] == "Cabin"


def test_the_energy_sign_in_lands_back_on_the_powerwall_step_having_taken_the_site(tesla, client, monkeypatch):  # noqa: F811
    import base64
    import json as _json

    app = tesla.app
    payload = base64.urlsafe_b64encode(_json.dumps({"scp": ["openid", "energy_device_data"]}).encode()).decode().rstrip("=")
    token = f"h.{payload}.s"
    monkeypatch.setattr(app, "TESLA_REDIRECT", "https://example.test/cb")
    monkeypatch.setattr(app.sources, "add", lambda *a, **k: 1)
    calls = tesla.fleet((200, {"access_token": token, "refresh_token": "r", "expires_in": 3600}), (200, {"response": {"fleet_api_base_url": tesla_fleet.NA}}), (200, {"response": [SITE]}), INSTALLED)
    start = client.get("/settings/sources/tesla/start?energy=1")
    assert tesla.state[app.TESLA_STATE_KEY].endswith(" e")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    back = client.get(f"/settings/sources/tesla/callback?code=abc&state={state}")
    assert back.status_code == 303 and back.headers["location"].endswith("/settings/sources/tesla?signed=1#pw")
    assert energy.load_config(tesla.state.get)["cloud"]["site_id"] == "1234567890" and len(calls.sent) == 4
    plain = client.get("/settings/sources/tesla/start")
    assert not tesla.state[app.TESLA_STATE_KEY].endswith(" e"), "a car sign-in lands where it always did"
    assert plain.status_code == 303


@pytest.mark.parametrize(
    "path,data,anchor",
    [("/settings/energy/backfill", {}, "#pw-history"), ("/settings/energy/cloud/off", {}, "#pw")],
)
def test_actions_from_the_flow_come_back_to_their_own_step_and_never_to_another_page(tesla, client, bare, path, data, anchor):  # noqa: F811
    tesla.app.ENERGY_CLOUD["site"] = "1234567890"
    cfg = energy.load_config(tesla.state.get)
    cfg["cloud"] = {"enabled": True, "site_id": "1234567890", "name": "x", "installed": "2026-01-01"}
    energy.save_config(tesla.state.__setitem__, cfg)
    tesla.fleet(INSTALLED)
    r = client.post(path, data=form(bare, {**data, "back": "tesla"}))
    assert r.status_code == 303 and urlparse(r.headers["location"]).path.endswith("/settings/sources/tesla") and r.headers["location"].endswith(anchor), r.headers["location"]


# ---- progress in place


def test_the_progress_answer_is_small_json(tesla, client, bare):  # noqa: F811
    cfg = energy.load_config(tesla.state.get)
    cfg["cloud"] = {"enabled": True, "site_id": "1", "name": "x", "installed": "2026-01-01"}
    energy.save_config(tesla.state.__setitem__, cfg)
    tesla.state[energy_cloud.BACKFILL_KEY] = json.dumps({"state": "running", "next": "2026-07-16", "stop": "2026-01-01", "total": 200, "done": 50, "error": None})
    r = client.get("/settings/energy/progress")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {"state": "running", "done": 50, "total": 200, "percent": 25, "text": "50 of 200 days", "error": None}


@pytest.mark.parametrize("page", ["/settings/energy", "/settings/sources/tesla"])
def test_a_running_backfill_is_marked_for_in_place_updates_and_the_pages_never_reload_themselves(tesla, client, page, monkeypatch):  # noqa: F811
    cfg = energy.load_config(tesla.state.get)
    cfg["cloud"] = {"enabled": True, "site_id": "1", "name": "x", "installed": "2026-01-01"}
    energy.save_config(tesla.state.__setitem__, cfg)
    tesla.app.ENERGY_CLOUD["site"] = "1"
    tesla_account(tesla, True, monkeypatch)
    tesla.state[energy_cloud.BACKFILL_KEY] = json.dumps({"state": "running", "next": "2026-07-16", "stop": "2026-01-01", "total": 200, "done": 50, "error": None})
    html = client.get(page).text
    assert 'data-bf="' in html and 'data-state="running"' in html and "<progress" in html and "25%" in html
    assert "location.reload" not in html and "http-equiv" not in html.lower() and "setTimeout" not in html.split('data-bf')[1][:3000]


def test_the_script_polls_every_few_seconds_stops_when_done_or_hidden_and_never_reloads():
    from pathlib import Path

    js = (Path(__file__).parent / "static" / "shell.js").read_text()
    part = js[js.index("The Powerwall history backfill's bar") :]
    assert "[data-bf]" in part and "setInterval(tick, 4000)" in part and "document.hidden" in part and "visibilitychange" in part
    assert 'd.state !== "running") halt()' in part and "location.reload" not in part


# ---- Cars and sources


def test_the_cars_card_line_says_where_the_powerwall_stands_and_the_one_action_to_go_on():
    c = cfg()
    assert energypage.cars_card(c, None) is None
    assert energypage.cars_card(c, {"granted": False}) == {"text": "Not brought in yet", "action": "Bring it in", "todo": True}
    assert energypage.cars_card(c, {"granted": True}) == {"text": "On this account", "action": "Set up", "todo": True}
    assert energypage.cars_card(cfg(enabled=True, site_id="1", name="Springfield"), {"granted": True}) == {"text": "Springfield", "action": "Manage", "todo": False}


def test_the_tesla_account_card_in_cars_and_sources_carries_the_powerwall_line_into_the_same_steps(tesla, client, monkeypatch):  # noqa: F811
    row = {"id": 7, "vin": None, "kind": "tesla", "label": "Tesla", "config": {}, "plain": "{}", "enabled": True, "last_ok_at": None, "last_error": None, "readable": True}
    tesla_account(tesla, False, monkeypatch)
    monkeypatch.setattr(tesla.app.sources, "load", lambda conn, vault: [row])
    html = client.get("/settings/cars").text
    assert "Powerwall" in html and "Bring it in" in html and 'href="/settings/sources/tesla#pw"' in html
