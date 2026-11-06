"""Car commands end to end against fakes: the site's POST, the control token's API, the
guards in order, the log, the alerts, the step-up, and both ways to a car (Tesla's signing
proxy and the connector's REST plugin). Nothing reaches a car, Tesla or a connector."""

import base64
import json

import pytest

import auth
import commands as cm
import tesla_cmd
from test_empty import EmptyConnection, bare  # noqa: F401 - fixtures

TESLA, LUCID = "5YJ3E1EA0SF000001", "LUCIDBRAVO0000002"
CONTROL, READ, STATUS, ONE_CAR = "ctl-" + "a" * 40, "read-" + "b" * 40, "stat-" + "c" * 40, "one-" + "d" * 40


def jwt(scopes):
    body = base64.urlsafe_b64encode(json.dumps({"scp": scopes}).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


class Logged(EmptyConnection):
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql="", params=None, *a, **kw):
        if "INSERT INTO cc_command_log" in str(sql):
            self.rows.append(dict(zip(("vin", "command", "arg", "tier", "source", "ip", "ok", "detail", "ms"), params)))
        return super().execute(sql, params, *a, **kw)


class FakeProxy:
    def __init__(self, *answers):
        self.answers, self.sent = list(answers), []

    def __call__(self, method, url, *, headers, json=None):
        self.sent.append((method, url, json))
        return self.answers.pop(0) if self.answers else (200, {"response": {"result": True, "reason": ""}})


@pytest.fixture
def client(bare):  # noqa: F811
    """A browser on the home network: over the test client's plain http, tier 3 goes only from there."""
    from fastapi.testclient import TestClient

    return TestClient(bare.app, follow_redirects=False, client=("192.168.1.20", 50000))


@pytest.fixture
def site(bare, monkeypatch):  # noqa: F811
    """Two cars (a Tesla through the proxy, a Lucid on the connector), commands on, sign-in on."""
    log, alerts, settings = [], [], {**bare.settings_mod.DEFAULTS, "commands_on": True, "command_tiers": {TESLA: 3}}
    cars = {
        TESLA: {"vin": TESLA, "name": "Alpha", "make": "Tesla", "source": "stream", "live": False, "status": "Idle", "plugged": True, "locked": True, "charge_target": 80},
        LUCID: {"vin": LUCID, "name": "Bravo", "make": "Lucid", "source": "connector", "live": True, "status": "Charging", "plugged": True, "locked": None, "climate": "off"},
    }
    proxy = FakeProxy()
    monkeypatch.setattr(bare, "db", lambda: Logged(log))
    monkeypatch.setattr(bare, "all_vehicles", lambda: [{"vin": v, "name": c["name"], "live": c["live"]} for v, c in cars.items()])
    monkeypatch.setattr(bare, "live_vehicle", lambda vin=None: dict(cars[vin or TESLA]))
    monkeypatch.setattr(bare, "current_settings", lambda: settings)
    monkeypatch.setattr(bare, "command_signed", lambda: True)
    monkeypatch.setattr(bare, "TESLA_PROXY_URL", "https://localhost:4443")
    monkeypatch.setattr(bare, "tesla_signer", lambda: tesla_cmd.Proxy("https://localhost:4443", call=proxy, sleep=lambda s: None))
    monkeypatch.setattr(bare, "tesla_access", lambda refresh=True: (jwt(["vehicle_device_data", "vehicle_charging_cmds", "vehicle_cmds"]), "https://fleet.example.test"))
    monkeypatch.setattr(bare.recorder, "notify", lambda text, kind="other": alerts.append((kind, text)))
    monkeypatch.setattr(bare, "COMMAND_LIMIT", cm.Limiter())
    monkeypatch.setattr(bare, "background", lambda fn, *a: fn(*a))
    monkeypatch.setattr(bare, "owner", lambda request: True)  # the open test site stands in for a signed-in owner
    bare.STEP_UPS.clear()
    tree = {LUCID: {"charging": {"commands": {"start-stop": {"val": None}}, "settings": {"target_level": {"val": 80}}}, "doors": {"commands": {"lock-unlock": {"val": None}}}}}
    bare._cache["garage_tree"] = (bare.datetime.now(bare.timezone.utc), tree)
    for raw, kind, vin in ((CONTROL, "control", None), (READ, "read", None), (STATUS, "status", None), (ONE_CAR, "control", LUCID)):
        bare.TOKENS[auth.token_hash(raw)] = {"label": kind, "vin": vin, "kind": kind, "last4": raw[-4:]}
    yield {"app": bare, "log": log, "alerts": alerts, "settings": settings, "proxy": proxy, "cars": cars, "tree": tree}
    for raw in (CONTROL, READ, STATUS, ONE_CAR):
        bare.TOKENS.pop(auth.token_hash(raw), None)


def csrf(bare):  # noqa: F811
    return {"X-CSRF-Token": auth.csrf_token(bare.AUTH["secret"], None)}


def test_a_tap_on_the_site_sends_through_the_proxy_and_is_logged(site, client):  # noqa: F811
    r = client.post(f"/cars/{TESLA}/command", json={"command": "charge_limit", "value": 85}, headers=csrf(site["app"]))
    assert r.status_code == 200 and r.json()["ok"] and r.json()["said"].startswith("Charge limit set to 85%")
    assert site["proxy"].sent == [("POST", f"https://localhost:4443/api/1/vehicles/{TESLA}/command/set_charge_limit", {"percent": 85})]
    assert site["log"] == [{"vin": TESLA, "command": "charge_limit", "arg": 85, "tier": 1, "source": "site", "ip": "192.168.1.20", "ok": True, "detail": None, "ms": site["log"][0]["ms"]}]
    assert site["alerts"] == [], "a tier 1 command is logged, not announced"


def test_no_command_without_the_forms_token_and_none_by_get(site, client):  # noqa: F811
    assert client.post(f"/cars/{TESLA}/command", json={"command": "lock"}).status_code == 400
    assert client.get(f"/cars/{TESLA}/command").status_code in (404, 405)
    assert site["proxy"].sent == []


def test_the_switch_off_refuses_everything_and_still_logs_it(site, client):  # noqa: F811
    site["settings"]["commands_on"] = False
    r = client.post(f"/cars/{TESLA}/command", json={"command": "lock"}, headers=csrf(site["app"]))
    assert r.status_code == 403 and "Commands are off" in r.json()["said"]
    assert site["proxy"].sent == [] and site["log"][0]["ok"] is False
    assert r.json()["said"].endswith("Turn them on in Settings › Commands."), "the row is in Settings here, so that is where"


def test_with_the_commands_row_hidden_the_refusal_says_what_comes_first(site, client, monkeypatch):  # noqa: F811
    """No Tesla account and no connector car: Settings does not list Commands, so a pointer to it would lead nowhere."""
    site["settings"]["commands_on"] = False
    monkeypatch.setattr(site["app"], "tesla_usable", lambda vehicles, makes: False)
    monkeypatch.setattr(site["app"], "car_makes", lambda vehicles, idents: ([], False))
    said = client.post(f"/cars/{TESLA}/command", json={"command": "lock"}, headers=csrf(site["app"])).json()["said"]
    assert "Settings › Cars and sources" in said and said.startswith("Commands are off.")
    said = client.post(f"/cars/{TESLA}/command", json={"command": "flash_lights"}, headers=csrf(site["app"])).json()["said"]
    assert "Settings › Cars and sources" in said and "Commands are also off." in said


def test_tier_two_needs_the_car_enabled_and_is_announced(site, client):  # noqa: F811
    site["settings"]["command_tiers"] = {}
    r = client.post(f"/cars/{TESLA}/command", json={"command": "sentry_on"}, headers=csrf(site["app"]))
    assert r.status_code == 403 and "off for this car" in r.json()["said"]
    site["settings"]["command_tiers"] = {TESLA: 2}
    r = client.post(f"/cars/{TESLA}/command", json={"command": "sentry_on"}, headers=csrf(site["app"]))
    assert r.status_code == 200 and site["alerts"] == [("command", "Alpha: Sentry on sent from site")]


def test_tier_three_asks_for_the_password_again_then_goes(site, client):  # noqa: F811
    app = site["app"]
    app.AUTH.update(enabled=True, password=auth.hash_password("correct horse", n=2**4))
    cookie = auth.new_session(app.AUTH["secret"])
    client.cookies.set(auth.SESSION_COOKIE, cookie)
    token = {"X-CSRF-Token": auth.csrf_token(app.AUTH["secret"], cookie)}
    try:
        r = client.post(f"/cars/{TESLA}/command", json={"command": "unlock"}, headers=token)
        assert r.status_code == 409 and r.json()["step_up"] and site["proxy"].sent == []
        assert client.post("/commands/step-up", data={"password": "wrong"}, headers=token).status_code == 401
        assert client.post("/commands/step-up", data={"password": "correct horse"}, headers=token).json()["ok"]
        r = client.post(f"/cars/{TESLA}/command", json={"command": "unlock"}, headers=token)
        assert r.status_code == 200 and site["proxy"].sent[-1][1].endswith("/command/door_unlock")
        assert ("access", "Alpha: Unlock sent from site · password") in site["alerts"]
        assert ("access", "Alpha: Unlock refused from site. Confirm it first.") in site["alerts"], "the try without a confirmation is told too"
        assert site["log"][-1]["source"] == "site · password"
        client.cookies.clear()
        r = client.post(f"/cars/{TESLA}/command", json={"command": "unlock"}, headers=csrf(app))
        assert r.status_code in (401, 400), "another browser has not entered it"
    finally:
        app.AUTH.update(enabled=False, password=None)
        app.FAILURES.clear(app.sign_in_key.__globals__["auth"].throttle_key({}, "192.168.1.20"))


def test_a_cloudflare_sign_in_is_not_a_step_up_and_with_nothing_set_up_it_says_what_to_add(site, client):  # noqa: F811
    class Req:
        class state:
            cf = {"email": "owner@example.test", "iat": __import__("time").time() - 5}

        cookies = {}

    assert not site["app"].stepped_up(Req()), "the identity provider waves a signed-in phone through: it proves nothing new"
    r = client.post(f"/cars/{TESLA}/command", json={"command": "unlock"}, headers=csrf(site["app"]))
    assert r.status_code == 409 and r.json()["said"] == "Add a passkey or a PIN in Settings › Access to do this from here."
    assert site["proxy"].sent == []


def test_ten_a_car_a_minute(site, client):  # noqa: F811
    for _ in range(10):
        assert client.post(f"/cars/{TESLA}/command", json={"command": "flash"}, headers=csrf(site["app"])).status_code == 200
    r = client.post(f"/cars/{TESLA}/command", json={"command": "flash"}, headers=csrf(site["app"]))
    assert r.status_code == 429 and "10 commands" in r.json()["said"]


def test_a_control_token_commands_without_a_form_token_and_read_and_status_tokens_never_do(site, client):  # noqa: F811
    r = client.post(f"/api/cars/{LUCID}/command", json={"command": "lock"}, headers={"Authorization": f"Bearer {READ}"})
    assert r.status_code == 403 and "control token" in r.json()["error"]
    r = client.post(f"/api/cars/{LUCID}/command", json={"command": "lock"}, headers={"Authorization": f"Bearer {STATUS}"})
    assert r.status_code == 403
    sent = []

    class Answer:
        status_code = 200

    def request(method, url, headers, json, timeout):
        sent.append((method, url, json))
        return Answer()

    site["app"].httpx.request, real = request, site["app"].httpx.request
    try:
        r = client.post(f"/api/cars/{LUCID}/command", json={"command": "charge_stop"}, headers={"Authorization": f"Bearer {CONTROL}"})
    finally:
        site["app"].httpx.request = real
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert sent == [("POST", f"{site['app'].REST_URL}/garage/{LUCID}/charging/commands/start-stop", {"value": "stop"})]
    assert site["log"][-1]["source"] == f"api control…{CONTROL[-4:]}"


def test_a_control_token_for_one_car_commands_that_car_only(site, client):  # noqa: F811
    r = client.post(f"/api/cars/{TESLA}/command", json={"command": "lock"}, headers={"Authorization": f"Bearer {ONE_CAR}"})
    assert r.status_code == 403 and site["proxy"].sent == []


def test_tier_three_is_never_sent_by_a_token(site, client):  # noqa: F811
    r = client.post(f"/api/cars/{TESLA}/command", json={"command": "unlock"}, headers={"Authorization": f"Bearer {CONTROL}"})
    assert r.status_code == 403 and "everyday commands only" in r.json()["said"] and site["proxy"].sent == []
    site["app"].TOKENS[auth.token_hash(CONTROL)]["tier_cap"] = 2
    r = client.post(f"/api/cars/{TESLA}/command", json={"command": "unlock"}, headers={"Authorization": f"Bearer {CONTROL}"})
    assert r.status_code == 403 and "everyday and car settings commands only" in r.json()["said"], "a cap of 2 is the most there is"
    assert [k for k, _ in site["alerts"]] == ["access", "access"], "each try is told"


def test_a_control_token_sends_tier_two_only_when_it_was_made_to(site, client):  # noqa: F811
    r = client.post(f"/api/cars/{TESLA}/command", json={"command": "sentry_on"}, headers={"Authorization": f"Bearer {CONTROL}"})
    assert r.status_code == 403 and "everyday commands only" in r.json()["said"], "a token made before caps sends everyday commands"
    site["app"].TOKENS[auth.token_hash(CONTROL)]["tier_cap"] = 2
    r = client.post(f"/api/cars/{TESLA}/command", json={"command": "sentry_on"}, headers={"Authorization": f"Bearer {CONTROL}"})
    assert r.status_code == 200 and r.json()["ok"]


def test_an_expired_control_token_is_no_token(site, client):  # noqa: F811
    site["app"].TOKENS[auth.token_hash(CONTROL)]["expires_at"] = site["app"].datetime.now(site["app"].timezone.utc) - site["app"].timedelta(seconds=1)
    r = client.post(f"/api/cars/{TESLA}/command", json={"command": "flash"}, headers={"Authorization": f"Bearer {CONTROL}"})
    assert r.status_code == 401 and "reconnect" in r.json()["error"] and site["proxy"].sent == []


def test_an_mcp_token_never_reaches_a_command(site, client):  # noqa: F811
    raw = "mcp-" + "e" * 40
    site["app"].TOKENS[auth.token_hash(raw)] = {"label": "mcp", "vin": None, "kind": "mcp", "last4": raw[-4:]}
    try:
        for path in (f"/api/cars/{TESLA}/command", f"/cars/{TESLA}/command"):
            r = client.post(path, json={"command": "flash"}, headers={"Authorization": f"Bearer {raw}"})
            assert r.status_code in (400, 401, 403), path  # the site's path wants its form token, which no token carries
        assert site["proxy"].sent == [] and site["log"] == []
    finally:
        site["app"].TOKENS.pop(auth.token_hash(raw), None)


def test_only_an_owner_sends_tier_three_and_the_try_is_told(site, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(site["app"], "owner", lambda request: False)
    monkeypatch.setattr(site["app"], "stepped_up", lambda request: True)
    r = client.post(f"/cars/{TESLA}/command", json={"command": "unlock"}, headers=csrf(site["app"]))
    assert r.status_code == 403 and r.json()["said"] == cm.OWNERS_ONLY and site["proxy"].sent == []
    assert site["alerts"] == [("access", f"Alpha: Unlock refused from site. {cm.OWNERS_ONLY}")]
    assert client.post(f"/cars/{TESLA}/command", json={"command": "flash"}, headers=csrf(site["app"])).status_code == 200, "everyday commands still go"


def test_a_command_the_connector_does_not_offer_is_unavailable_and_not_sent(site, client):  # noqa: F811
    r = client.post(f"/cars/{LUCID}/command", json={"command": "climate_on"}, headers=csrf(site["app"]))
    assert r.status_code == 403 and "doesn't offer climate on" in r.json()["said"]


def test_a_connector_that_answers_404_is_unavailable(site, client, monkeypatch):  # noqa: F811
    class Answer:
        status_code = 404

        def json(self):
            return {"error": "Path not found: /garage/x/doors/commands/lock-unlock"}

    monkeypatch.setattr(site["app"].httpx, "request", lambda *a, **k: Answer())
    r = client.post(f"/cars/{LUCID}/command", json={"command": "lock"}, headers=csrf(site["app"]))
    assert r.status_code == 502 and "doesn't offer lock" in r.json()["said"]


def test_a_command_the_connector_refuses_says_the_connectors_reason(site, client, monkeypatch):  # noqa: F811
    """The Lucid connector (0.4.0) refuses what the car's config says it lacks, before sending."""

    class Answer:
        status_code = 400

        def json(self):
            return {"error": "Not sent: this car's frunk has a gas strut and closes by hand"}

    monkeypatch.setattr(site["app"].httpx, "request", lambda *a, **k: Answer())
    r = client.post(f"/cars/{LUCID}/command", json={"command": "lock"}, headers=csrf(site["app"]))
    assert r.status_code == 502 and r.json()["said"] == "Not sent: this car's frunk has a gas strut and closes by hand."
    assert site["log"][-1]["detail"] == r.json()["said"], "the log keeps the reason too"


def test_an_error_page_from_the_connector_is_not_shown(site, client, monkeypatch):  # noqa: F811
    class Answer:
        status_code = 500

        def json(self):
            raise ValueError("not JSON")

    monkeypatch.setattr(site["app"].httpx, "request", lambda *a, **k: Answer())
    r = client.post(f"/cars/{LUCID}/command", json={"command": "lock"}, headers=csrf(site["app"]))
    assert r.json()["said"] == "The car didn't take the command. Try again shortly."


def test_without_a_proxy_a_tesla_has_no_way_and_says_why(site, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(site["app"], "TESLA_PROXY_URL", "")
    r = client.post(f"/cars/{TESLA}/command", json={"command": "lock"}, headers=csrf(site["app"]))
    assert r.status_code == 403 and "signing proxy" in r.json()["said"]


def test_a_token_without_the_scope_is_asked_to_sign_in_again(site, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(site["app"], "tesla_access", lambda refresh=True: (jwt(["vehicle_device_data"]), "x"))
    r = client.post(f"/cars/{TESLA}/command", json={"command": "lock"}, headers=csrf(site["app"]))
    assert r.status_code == 502 and r.json()["said"] == "Sign in again to allow commands."
    assert site["proxy"].sent == []


def test_nothing_unknown_and_nothing_that_moves_the_car(site, client):  # noqa: F811
    for key in ("summon", "software_update", "../../x"):
        r = client.post(f"/cars/{TESLA}/command", json={"command": key}, headers=csrf(site["app"]))
        assert r.status_code == 400 and f"no command called {key!r}" in r.json()["said"] and "charge_start" in r.json()["said"]
    assert "also off" not in r.json()["said"], "commands are on here"
    site["settings"]["commands_on"] = False
    r = client.post(f"/cars/{TESLA}/command", json={"command": "flash_lights"}, headers=csrf(site["app"]))
    assert r.status_code == 400 and "no command called 'flash_lights'" in r.json()["said"] and "Commands are also off" in r.json()["said"], "off and misspelt: both said"
    r = client.post(f"/cars/{TESLA}/command", json={"command": "flash"}, headers=csrf(site["app"]))
    assert r.status_code == 403 and r.json()["said"].startswith("Commands are off"), "a right name with commands off says they are off"
    assert site["proxy"].sent == []


def test_the_now_row_shows_what_the_car_may_be_sent_from_memory(site):
    app = site["app"]
    view = app.command_view(site["cars"][LUCID])
    assert view["allowed"] == {"charge_start", "charge_stop", "charge_limit", "lock"} and view["charging"] is True
    site["settings"]["commands_on"] = False
    assert app.command_view(site["cars"][LUCID]) is None


def test_the_log_opens_on_demand(site, client):  # noqa: F811
    r = client.get(f"/cars/{TESLA}/commands")
    assert r.status_code == 200 and "No commands yet" in r.text


def test_settings_commands_renders_each_car_and_its_way(site, client):  # noqa: F811
    r = client.get("/settings/commands")
    assert r.status_code == 200
    assert "Through Tesla's signing proxy" in r.text and "Through CarConnectivity" in r.text and f'name="hold_{LUCID}"' in r.text


def test_homelink_goes_with_where_the_car_is_and_not_without_it(site, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(site["app"], "stepped_up", lambda request: True)
    r = client.post(f"/cars/{TESLA}/command", json={"command": "homelink"}, headers=csrf(site["app"]))
    assert r.status_code == 403 and "where the car is" in r.json()["said"] and site["proxy"].sent == []
    site["cars"][TESLA].update(lat=37.5, lon=-122.25)
    r = client.post(f"/cars/{TESLA}/command", json={"command": "homelink"}, headers=csrf(site["app"]))
    assert r.status_code == 200 and r.json()["ok"]
    assert site["proxy"].sent == [("POST", f"https://localhost:4443/api/1/vehicles/{TESLA}/command/trigger_homelink", {"lat": 37.5, "lon": -122.25})]
    assert site["log"][-1]["arg"] is None, "the position is not kept in the log"


def test_a_lucid_seat_goes_to_the_connector_as_seat_mode_and_level(site, client, monkeypatch):  # noqa: F811
    site["tree"][LUCID]["climatization"] = {"commands": {"seat-climate": {"val": None}}}
    sent = []

    class R:
        status_code = 200

        def json(self):
            return {}

    monkeypatch.setattr(site["app"].httpx, "request", lambda method, url, **kw: sent.append((method, url, kw["json"])) or R())
    r = client.post(f"/cars/{LUCID}/command", json={"command": "seat_heat_passenger", "value": 1}, headers=csrf(site["app"]))
    assert r.status_code == 200 and r.json()["ok"], r.json()
    assert sent[0][1].endswith(f"garage/{LUCID}/climatization/commands/seat-climate") and sent[0][2] == {"value": "front-passenger heat low"}


def test_a_lucid_schedule_goes_to_the_connector_with_no_place_and_the_other_end_kept(site, client, monkeypatch):  # noqa: F811
    site["tree"][LUCID]["charging"]["commands"]["charging-schedule"] = {"val": None}
    site["settings"]["command_tiers"] = {LUCID: 2}
    sent = []

    class R:
        status_code = 200

        def json(self):
            return {}

    monkeypatch.setattr(site["app"].httpx, "request", lambda method, url, **kw: sent.append((method, url, kw["json"])) or R())
    url = f"{site['app'].REST_URL}/garage/{LUCID}/charging/commands/charging-schedule"
    for command, value, body in (("sched_charge", 23 * 60, "23:00-"), ("sched_charge_end", 6 * 60 + 30, "-06:30"), ("sched_charge_off", None, "off")):
        r = client.post(f"/cars/{LUCID}/command", json={"command": command, "value": value}, headers=csrf(site["app"]))
        assert r.status_code == 200 and r.json()["ok"], (command, r.json())
        assert sent[-1] == ("POST", url, {"value": body})
    assert site["proxy"].sent == [], "a Lucid never goes through the Tesla proxy"
    assert sent and all("lat" not in repr(x) for x in sent), "a Lucid schedule has no place"


def test_a_lucid_connector_that_does_not_offer_the_schedule_is_refused_before_sending(site, client, monkeypatch):  # noqa: F811
    site["settings"]["command_tiers"] = {LUCID: 2}
    monkeypatch.setattr(site["app"].httpx, "request", lambda *a, **k: pytest.fail("sent to a connector that does not offer it"))
    r = client.post(f"/cars/{LUCID}/command", json={"command": "sched_charge", "value": 600}, headers=csrf(site["app"]))
    assert r.status_code == 403 and not r.json()["ok"]


def test_the_command_view_offers_a_lucid_the_two_times_and_no_place(site, monkeypatch):  # noqa: F811
    app = site["app"]
    site["tree"][LUCID]["charging"]["commands"]["charging-schedule"] = {"val": None}
    site["settings"]["command_tiers"] = {LUCID: 2}
    monkeypatch.setattr(app, "car_identities", lambda cars: {})
    view = app.command_view(site["cars"][LUCID])
    assert {"sched_charge", "sched_charge_end", "sched_charge_off"} <= view["allowed"] and "where" not in view
    assert "depart" not in view["allowed"], "Lucid's API has no departure timer"


ROWS = [
    {"id": 7, "name": "Alpha House", "latitude": 37.7, "longitude": -122.1, "icon": "home", "auto": False, "radius_m": 150},
    {"id": 8, "name": "Bravo Office", "latitude": 37.8, "longitude": -122.2, "icon": "work", "auto": False, "radius_m": 150},
]
HOME_ANSWER = {
    "response": {
        "vehicle_state": {"car_version": "2025.2.6 x"},
        "charge_state": {"home_location": {"latitude": 37.5, "longitude": -122.25}},
        "preconditioning_schedule_data": {"precondition_schedules": [{"enabled": True, "precondition_time": 450, "latitude": 37.7, "longitude": -122.1}]},
    }
}


class SchedFleet:
    def __init__(self, answer):
        self.answer = answer

    def schedules(self, token, base, vin):
        return self.answer


@pytest.fixture
def sched(site, monkeypatch):  # noqa: F811
    app = site["app"]
    monkeypatch.setattr(app, "sched_rows", lambda: ROWS)
    monkeypatch.setattr(app, "tesla_app", lambda: SchedFleet(HOME_ANSWER))
    app.SCHEDULES.clear()
    yield site
    app.SCHEDULES.clear()


def depart(client, site, **body):  # noqa: F811
    return client.post(f"/cars/{TESLA}/command", json={"command": "depart", "value": 450, **body}, headers=csrf(site["app"]))


def test_a_schedule_with_no_place_known_is_refused_and_sends_nothing(sched, client):  # noqa: F811
    r = depart(client, sched)
    assert r.status_code == 403 and "where the car is" in r.json()["said"] and sched["proxy"].sent == []


def test_a_schedule_goes_to_the_place_picked_and_is_read_back_by_name(sched, client):  # noqa: F811
    r = depart(client, sched, where="place:8")
    assert r.status_code == 200 and r.json()["ok"]
    ((_, url, body),) = sched["proxy"].sent
    assert url.endswith("/command/add_precondition_schedule") and (body["lat"], body["lon"]) == (37.8, -122.2)
    assert sched["log"][-1]["arg"] == 450 and "37.8" not in repr(sched["log"][-1]) and "Bravo" not in repr(sched["log"][-1]), "no place in the log"
    kept = sched["app"].SCHEDULES[TESLA]
    assert kept["precondition"] == [{"time": 450, "at": "Alpha House"}] and "37.7" not in repr(kept["precondition"])
    assert sched["app"].schedules.lines(kept)[0] == "warm by 7:30 AM (Alpha House)"


def test_without_a_pick_it_is_teslas_home_else_where_the_car_is(sched, client):  # noqa: F811
    sched["cars"][TESLA].update(lat=37.9, lon=-122.9)
    depart(client, sched)
    assert (sched["proxy"].sent[-1][2]["lat"], sched["proxy"].sent[-1][2]["lon"]) == (37.9, -122.9), "no Tesla Home read yet: here"
    sched["app"].SCHEDULES[TESLA] = sched["app"].schedules.parse(HOME_ANSWER)
    depart(client, sched)
    assert (sched["proxy"].sent[-1][2]["lat"], sched["proxy"].sent[-1][2]["lon"]) == (37.5, -122.25), "Tesla's own Home"
    depart(client, sched, where="car")
    assert sched["proxy"].sent[-1][2]["lat"] == 37.9


def test_a_pick_that_is_not_an_offered_place_is_refused_whatever_it_says(sched, client):  # noqa: F811
    for where in ("place:999", "37.8,-122.2", "tesla:work"):
        r = depart(client, sched, where=where)
        assert r.status_code == 403 and r.json()["said"] == "Choose where it applies.", where
    assert sched["proxy"].sent == []


def test_off_needs_no_place_and_old_firmware_gets_the_legacy_command(sched, client):  # noqa: F811
    r = client.post(f"/cars/{TESLA}/command", json={"command": "depart_off"}, headers=csrf(sched["app"]))
    assert r.status_code == 200 and [u.rsplit("/", 1)[1] for _, u, _ in sched["proxy"].sent] == ["remove_precondition_schedule", "remove_charge_schedule"]
    sched["proxy"].sent.clear()
    sched["cars"][TESLA].update(software="2024.20.9")
    r = depart(client, sched)
    assert r.status_code == 200 and sched["proxy"].sent[0][1].endswith("/command/set_scheduled_departure") and "lat" not in sched["proxy"].sent[0][2]


def test_the_command_view_offers_the_places_by_name_with_tesla_s_home_the_default(sched, monkeypatch):  # noqa: F811
    app = sched["app"]
    monkeypatch.setattr(app, "command_way", lambda car: "tesla")
    monkeypatch.setattr(app, "tesla_command_scope", lambda: {"charging": True, "all": True})
    monkeypatch.setattr(app, "car_identities", lambda cars: {})
    app.SCHEDULES[TESLA] = app.schedules.parse(HOME_ANSWER)
    view = app.command_view(dict(sched["cars"][TESLA], lat=37.9, lon=-122.9))
    names = [o["name"] for o in view["where"]["options"]]
    assert names[:3] == ["Home", "Alpha House", "Bravo Office"] and "Where the car is now" in names and view["where"]["default"] == "tesla:home"
    assert "37.9" not in repr(view["where"]) and "122" not in repr(view["where"])


def test_commands_offer_a_passkey_or_pin_until_one_locks_them(site, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(site["app"], "PASSKEYS", [])
    monkeypatch.setitem(site["app"].AUTH, "pin", None)
    r = client.get("/settings/commands")
    assert "lock-offer" in r.text and "#confirming\">Add a passkey or PIN" in r.text
    monkeypatch.setitem(site["app"].AUTH, "pin", "set")
    assert "lock-offer" not in client.get("/settings/commands").text
    monkeypatch.setitem(site["app"].AUTH, "pin", None)
    monkeypatch.setattr(site["app"], "PASSKEYS", [{"id": "k"}])
    assert "lock-offer" not in client.get("/settings/commands").text
