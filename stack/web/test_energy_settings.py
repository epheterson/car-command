"""Settings › Energy: the Tesla account's Powerwall and its backfill; no local gateway any more."""

import json
from datetime import datetime, timezone

import pytest

import auth
import energy
import tesla_fleet
from test_empty import bare, client  # noqa: F401 - fixtures


def form(bare, data):  # noqa: F811
    return dict(data, csrf_token=auth.csrf_token(bare.AUTH["secret"], None))


@pytest.fixture
def house(bare, monkeypatch):  # noqa: F811
    """A site with a state store that remembers."""
    state = {}
    monkeypatch.setattr(bare, "state_get", state.get)
    monkeypatch.setattr(bare, "state_set", lambda k, v: state.__setitem__(k, v))
    monkeypatch.setattr(bare, "state_delete", lambda k: state.pop(k, None))
    monkeypatch.setattr(bare.cloud_worker, "get", state.get)
    monkeypatch.setattr(bare.cloud_worker, "put", lambda k, v: state.__setitem__(k, v))
    bare.ENERGY_CLOUD["site"] = ""
    bare.ENERGY_LIVE.reading = None

    class H:
        pass

    h = H()
    h.state, h.app = state, bare
    yield h
    bare.ENERGY_CLOUD["site"] = ""
    bare.ENERGY_LIVE.reading = None


def test_the_page_opens_with_nothing_set_up(house, client):  # noqa: F811
    r = client.get("/settings/energy")
    assert r.status_code == 200 and "<h1>Energy</h1>" in r.text
    assert "Tesla account" in r.text and "Connect Tesla" in r.text, "no account yet"
    assert "gateway" not in r.text.lower() and "Find it" not in r.text and "CC_POWERWALL" not in r.text


def test_the_local_gateway_routes_are_gone(house, client, bare):  # noqa: F811
    for path in (
        "/settings/energy/gateway",
        "/settings/energy/gateway/test",
        "/settings/energy/gateway/pin",
        "/settings/energy/gateway/remove",
        "/settings/energy/gateway/trust",
        "/settings/energy/find",
    ):
        assert client.post(path, data=form(bare, {"url": "192.0.2.10", "password": "x"})).status_code in (404, 405), path


def test_what_a_gateway_left_in_the_state_goes_at_startup_and_the_page_opens_after(house, client, bare):  # noqa: F811
    house.state[energy.LEGACY_PASSWORD_KEY] = "sealed"
    house.state[energy.CONFIG_KEY] = json.dumps({"local": {"url": "https://192.0.2.10", "fingerprint": "ab:cd"}, "cloud": {"enabled": False, "site_id": "", "name": ""}})
    energy.drop_gateway(bare.state_get, bare.state_set, bare.state_delete)
    assert energy.LEGACY_PASSWORD_KEY not in house.state and "192.0.2.10" not in house.state[energy.CONFIG_KEY]
    assert client.get("/settings/energy").status_code == 200


def test_the_page_shows_the_last_reading_from_memory(house, client, bare):  # noqa: F811
    bare.ENERGY_LIVE.set({"solar_w": 4200.0, "home_w": 1800.0, "grid_w": -300.0, "battery_w": -2100.0, "soe_pct": 61.0}, "cloud", datetime.now(timezone.utc))
    page = client.get("/settings/energy").text
    assert "4.2 kW" in page and "61%" in page and "from Tesla" in page


def test_the_settings_list_names_energy_and_what_it_reads_from(house, client, bare):  # noqa: F811
    assert 'href="/settings/energy"' in client.get("/settings").text
    assert "Gateway" not in client.get("/settings").text
    cfg = energy.load_config(house.state.get)
    cfg["cloud"].update(enabled=True, site_id="1", name="x")
    energy.save_config(house.state.__setitem__, cfg)
    assert "Tesla account" in client.get("/settings").text


# ---- the Tesla account


def tesla_account(house, granted, monkeypatch):
    """A signed-in account whose token carries (or lacks) the energy scope."""
    import base64

    scopes = ["openid", "offline_access", "vehicle_device_data"] + (["energy_device_data"] if granted else [])
    payload = base64.urlsafe_b64encode(json.dumps({"scp": scopes}).encode()).decode().rstrip("=")
    token = f"h.{payload}.s"
    row = {
        "id": 7,
        "vin": None,
        "kind": "tesla",
        "label": "Tesla",
        "config": {},
        "plain": json.dumps({"access_token": token, "refresh_token": "r", "expires_at": 9e12}),
        "enabled": True,
        "last_ok_at": None,
        "last_error": None,
    }
    monkeypatch.setattr(house.app, "tesla_account", lambda conn: row)
    monkeypatch.setattr(house.app, "tesla_access", lambda refresh=True: (token, tesla_fleet.NA))
    return token


class Calls:
    def __init__(self, *answers):
        self.answers, self.sent = list(answers), []

    def __call__(self, method, url, *, headers, data=None, json=None):
        self.sent.append(url)
        return self.answers.pop(0)


def test_a_token_without_the_energy_scope_is_told_to_sign_in_again(house, client, monkeypatch):  # noqa: F811
    tesla_account(house, False, monkeypatch)
    page = client.get("/settings/energy").text
    assert "Not granted" in page and "Sign in again" in page and "disabled" in page


def test_the_account_is_searched_for_a_site_and_it_is_switched_on(house, client, bare, monkeypatch):  # noqa: F811
    tesla_account(house, True, monkeypatch)
    calls = Calls(
        (200, {"response": [{"energy_site_id": 1234567890, "resource_type": "battery", "site_name": "Springfield"}]}), (200, {"response": {"installation_date": "2024-03-01T00:00:00-08:00"}})
    )
    monkeypatch.setattr(bare, "tesla_app", lambda: tesla_fleet.Fleet("c", "s", call=calls))
    r = client.post("/settings/energy/cloud", data=form(bare, {}))
    assert r.status_code == 303
    cfg = energy.load_config(house.state.get)
    assert cfg["cloud"] == {"enabled": True, "site_id": "1234567890", "name": "Springfield", "installed": "2024-03-01"} and bare.ENERGY_CLOUD["site"] == "1234567890"
    page = client.get("/settings/energy").text
    assert "Granted" in page and "Backfill from Tesla" in page and "Tesla bills Fleet API requests" in page
    client.post("/settings/energy/cloud/off", data=form(bare, {}))
    assert bare.ENERGY_CLOUD["site"] == "" and not energy.load_config(house.state.get)["cloud"]["enabled"]


def test_an_account_with_no_powerwall_or_without_access_says_so(house, client, bare, monkeypatch):  # noqa: F811
    tesla_account(house, True, monkeypatch)
    monkeypatch.setattr(bare, "tesla_app", lambda: tesla_fleet.Fleet("c", "s", call=Calls((200, {"response": [{"vin": "5YJ3E1EA0SF000001"}]}))))
    r = client.post("/settings/energy/cloud", data=form(bare, {}))
    assert r.status_code == 400 and "no Powerwall" in r.text and house.state == {}
    monkeypatch.setattr(bare, "tesla_app", lambda: tesla_fleet.Fleet("c", "s", call=Calls((403, {"error": "forbidden"}))))
    r = client.post("/settings/energy/cloud", data=form(bare, {}))
    assert r.status_code == 400 and "Sign in with Tesla again" in r.text
    monkeypatch.setattr(bare, "tesla_app", lambda: None)
    assert client.post("/settings/energy/cloud", data=form(bare, {})).status_code == 400


def test_the_backfill_starts_shows_its_progress_and_stops(house, client, bare, monkeypatch):  # noqa: F811
    tesla_account(house, True, monkeypatch)
    calls = Calls((200, {"response": {"installation_date": "2026-01-01T00:00:00-08:00"}}))
    monkeypatch.setattr(bare, "tesla_app", lambda: tesla_fleet.Fleet("c", "s", call=calls))
    bare.ENERGY_CLOUD["site"] = "1234567890"
    cfg = energy.load_config(house.state.get)
    cfg["cloud"] = {"enabled": True, "site_id": "1234567890", "name": "Springfield"}
    energy.save_config(house.state.__setitem__, cfg)
    assert client.post("/settings/energy/backfill", data=form(bare, {})).status_code == 303
    page = client.get("/settings/energy").text
    assert "0 of" in page and "Stop" in page and "estimate" in page.lower() and "<progress" in page
    client.post("/settings/energy/backfill", data=form(bare, {"do": "stop"}))
    page = client.get("/settings/energy").text
    assert "Stopped at" in page and "Resume" in page


def test_a_backfill_needs_the_cloud_source_on(house, client, bare):  # noqa: F811
    r = client.post("/settings/energy/backfill", data=form(bare, {}))
    assert r.status_code == 400 and "Turn on the Tesla account" in r.text


def test_a_state_write_without_the_csrf_token_is_refused(house, client):  # noqa: F811
    assert client.post("/settings/energy/gateway", data={"url": "192.0.2.10", "password": "x"}).status_code in (400, 403)
    assert house.state == {}


def test_connect_tesla_notes_the_energy_scope_and_the_sign_in_again_it_needs(house, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(house.app, "tesla_app", lambda: tesla_fleet.Fleet("c", "s", call=Calls()))
    page = client.get("/settings/sources/tesla").text
    assert "energy_device_data" in page and "Asked for at sign-in" in page
    tesla_account(house, False, monkeypatch)
    page = client.get("/settings/sources/tesla").text
    assert "energy_device_data" in page and "Not granted" in page and "Sign in again to grant it" in page
    tesla_account(house, True, monkeypatch)
    assert "Sign in again to grant it" not in client.get("/settings/sources/tesla").text.split('id="powerwall"')[1]


# ---- the connect slice: test, the brake, and Find


def test_only_the_energy_pages_sign_in_asks_for_the_energy_scope(house, client, monkeypatch):  # noqa: F811
    from urllib.parse import parse_qs, urlparse

    monkeypatch.setattr(house.app, "tesla_app", lambda: tesla_fleet.Fleet("c", "s", call=Calls()))
    monkeypatch.setattr(house.app, "TESLA_REDIRECT", "https://example.test/cb")
    scopes = lambda r: parse_qs(urlparse(r.headers["location"]).query)["scope"][0].split()  # noqa: E731
    assert "energy_device_data" not in scopes(client.get("/settings/sources/tesla/start"))
    assert "energy_device_data" in scopes(client.get("/settings/sources/tesla/start?energy=1"))
    tesla_account(house, False, monkeypatch)
    assert "/settings/sources/tesla/start?energy=1" in client.get("/settings/energy").text
    assert "/settings/sources/tesla/start?energy=1" in client.get("/settings/sources/tesla").text.split('id="powerwall"')[1]
    assert "/settings/sources/tesla/start\"" not in client.get("/settings/sources/tesla").text.split('id="powerwall"')[1]


def test_a_car_sign_in_keeps_the_powerwall_scope_when_it_is_already_held_or_the_cloud_is_on(house, client, bare, monkeypatch):  # noqa: F811
    from urllib.parse import parse_qs, urlparse

    monkeypatch.setattr(bare, "tesla_app", lambda: tesla_fleet.Fleet("c", "s", call=Calls()))
    monkeypatch.setattr(bare, "TESLA_REDIRECT", "https://example.test/cb")
    has = lambda: "energy_device_data" in parse_qs(urlparse(client.get("/settings/sources/tesla/start").headers["location"]).query)["scope"][0].split()  # noqa: E731
    tesla_account(house, False, monkeypatch)
    assert not has()
    tesla_account(house, True, monkeypatch)
    assert has(), "the token already holds it: a car re-sign-in must not drop it"
    tesla_account(house, False, monkeypatch)
    cfg = energy.load_config(house.state.get)
    cfg["cloud"] = {"enabled": True, "site_id": "1", "name": "x"}
    energy.save_config(house.state.__setitem__, cfg)
    assert has(), "the cloud source is on"
