"""Settings that give the site more power over the cars and the house: commands on, a tier raised, a token minted,
the garage set to open. An owner, confirmed in the last two minutes, or nothing is saved and the page asks
(security review F2, F8); lowering power never asks. An Access identity is an owner only when CC_ACCESS_EMAILS names
it (F1); tier 3 for a car needs an alert channel first (F9)."""

import time

import pytest

import auth
from test_empty import bare, client  # noqa: F401 - fixtures

VIN = "5YJ3E1EA0SF000001"


@pytest.fixture
def site(bare, monkeypatch):  # noqa: F811
    """A signed-in owner with a password, so a confirmation exists to be asked for."""
    saved, minted = {}, []
    settings = {**bare.settings_mod.DEFAULTS, "commands_on": False, "command_tiers": {}}
    bare.AUTH.update(enabled=True, password=auth.hash_password("correct horse", n=2**4))
    cookie = auth.new_session(bare.AUTH["secret"])
    monkeypatch.setattr(bare.settings_mod, "save", lambda conn, key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(bare, "current_settings", lambda: settings)
    monkeypatch.setattr(bare, "store_token", lambda *a: minted.append(a) or "raw-token-" + "x" * 30)
    monkeypatch.setattr(bare.recorder, "channels", lambda: ["discord"])
    bare.STEP_UPS.clear()
    bare.SETTING_STEPS.clear()
    yield {"app": bare, "saved": saved, "minted": minted, "settings": settings, "cookie": cookie}
    bare.STEP_UPS.clear()
    bare.AUTH.update(enabled=False, password=None)


def post(site, client, path, data):  # noqa: F811
    client.cookies.set(auth.SESSION_COOKIE, site["cookie"])
    return client.post(path, data={**data, "csrf_token": auth.csrf_token(site["app"].AUTH["secret"], site["cookie"])})


def confirm(site):
    site["app"].STEP_UPS[site["app"].csrf_subject(site["cookie"], None)] = time.monotonic()


def test_turning_commands_on_asks_first_and_saves_nothing_until_confirmed(site, client):  # noqa: F811
    r = post(site, client, "/settings/commands", {"commands_shown": "1", "commands_on": "on"})
    assert r.status_code == 409 and "Confirm it&#39;s you, then save again." in r.text and "data-cf-row" in r.text
    assert site["saved"] == {}
    confirm(site)
    assert post(site, client, "/settings/commands", {"commands_shown": "1", "commands_on": "on"}).status_code == 303
    assert site["saved"]["commands_on"] is True


def test_raising_a_tier_asks_and_lowering_one_or_turning_off_does_not(site, client):  # noqa: F811
    assert post(site, client, "/settings/commands", {"tiers_shown": "1", f"tier_{VIN}": "2"}).status_code == 409
    site["settings"].update(commands_on=True, command_tiers={VIN: 3})
    r = post(site, client, "/settings/commands", {"commands_shown": "1", "tiers_shown": "1", f"tier_{VIN}": "2"})
    assert r.status_code == 303 and site["saved"]["command_tiers"] == {VIN: 2} and site["saved"]["commands_on"] is False


def test_access_for_a_car_needs_somewhere_to_tell_the_owner(site, client, monkeypatch):  # noqa: F811
    confirm(site)
    monkeypatch.setattr(site["app"].recorder, "channels", lambda: [])
    r = post(site, client, "/settings/commands", {"tiers_shown": "1", f"tier_{VIN}": "3"})
    assert r.status_code == 409 and "Add an alert channel" in r.text and "command_tiers" not in site["saved"]


def test_a_token_is_minted_only_once_confirmed_and_a_control_token_keeps_its_cap_and_days(site, client):  # noqa: F811
    assert post(site, client, "/settings/tokens", {"label": "HA", "kind": "control"}).status_code == 409 and site["minted"] == []
    confirm(site)
    r = post(site, client, "/settings/tokens", {"label": "HA", "kind": "control", "cap": "2", "days": "30"})
    assert r.status_code == 200 and site["minted"] == [("HA", "control", None, False, False, 2, 30)]
    post(site, client, "/settings/tokens", {"label": "HA", "kind": "control", "cap": "3", "days": "9999"})
    assert site["minted"][-1][-2:] == (1, auth.EXPIRY_DEFAULT), "never tier 3, and only the offered lifetimes"
    assert auth.EXPIRY_DEFAULT == 0, "a token works until revoked unless the owner picks a lifetime"
    post(site, client, "/settings/tokens", {"label": "Grafana", "kind": "read", "days": "30"})
    assert site["minted"][-1][1] == "read" and site["minted"][-1][-1] == 30, "any kind can be given a lifetime"


def test_the_garage_set_to_open_asks_and_off_or_watch_does_not(site, client, monkeypatch):  # noqa: F811
    held = {}
    monkeypatch.setattr(site["app"], "state_set", lambda k, v: held.__setitem__(k, v))
    monkeypatch.setattr(site["app"], "garage_open_config", lambda: {"mode": "watch", "vin": VIN, "place": None, "aim": 13})
    monkeypatch.setattr(site["app"].garagedoor, "configured", lambda: True)
    monkeypatch.setattr(site["app"], "all_vehicles", lambda: [{"vin": VIN, "name": "Alpha"}])
    monkeypatch.setattr(site["app"], "learn_garage_model", lambda: None)
    site["app"].APPROACH.model = object()
    assert post(site, client, "/settings/alerts/ha/arrival", {"mode": "open", "vin": VIN}).status_code == 409 and held == {}
    assert post(site, client, "/settings/alerts/ha/arrival", {"mode": "off", "vin": VIN}).status_code == 303 and held
    confirm(site)
    assert post(site, client, "/settings/alerts/ha/arrival", {"mode": "open", "vin": VIN}).status_code == 303


def test_an_access_identity_owns_only_when_the_list_names_it(bare, monkeypatch):  # noqa: F811
    class Req:
        class state:
            cf = {"email": "Owner@Example.test"}

    monkeypatch.setattr(bare, "EMAILS", set())
    assert not bare.owner(Req()), "with no list, Cloudflare's policy may admit a whole domain"
    monkeypatch.setattr(bare, "EMAILS", {"owner@example.test"})
    assert bare.owner(Req())
    Req.state.cf = {"common_name": "a-service-token"}
    assert not bare.owner(Req()), "a service token names no person"
    assert not bare.may_change_confirming(Req())


def test_the_page_asks_before_a_save_only_where_power_rises(site, client):  # noqa: F811
    client.cookies.set(auth.SESSION_COOKIE, site["cookie"])
    page = client.get("/settings/access").text
    assert 'data-power="always"' in page and 'data-power="switch"' in page and "data-cf-blank" in page and "/static/confirm.js" in page
    r = client.post("/settings/confirmed", headers={"X-CSRF-Token": auth.csrf_token(site["app"].AUTH["secret"], site["cookie"])})
    assert r.json()["ok"] is False and r.json()["owner"] is True and "password" in r.json()["said"]


def test_the_commands_page_shows_the_saved_switches(site, client):  # noqa: F811
    """A switch shown off while saved on posts off with the next save of anything on the page."""
    import re

    client.cookies.set(auth.SESSION_COOKIE, site["cookie"])
    for on in (True, False):
        site["settings"].update(commands_on=on, fleet_budget_on=on)
        page = client.get("/settings/commands").text
        for name in ("commands_on", "fleet_budget_on"):
            tag = re.search(rf'<input[^>]*name="{name}"[^>]*>', page)
            assert tag and (" checked" in tag.group(0)) is on, f"{name} saved {on}"
