"""Sign in with Tesla: the state that guards the way back, and tokens kept in the right order."""

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import sources
import tesla_fleet
from test_empty import EmptyConnection, bare, client  # noqa: F401 - fixtures


class Answers:
    """A fake Fleet API: each call answers from a queue and is recorded."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent = []

    def __call__(self, method, url, *, headers, data=None, json=None):
        self.sent.append((method, url, data, json))
        return self.answers.pop(0)


class Recording(EmptyConnection):
    def __init__(self, log):
        self.log = log

    def commit(self):
        self.log.append("commit")


@pytest.fixture
def tesla(bare, monkeypatch):  # noqa: F811
    """A site with app keys, a sealing key, a state store and a source table that records."""
    state, log, rows = {}, [], []
    vault = sources.Vault(Fernet.generate_key().decode())
    monkeypatch.setattr(bare, "VAULT", vault)
    monkeypatch.setattr(bare, "state_get", state.get)
    monkeypatch.setattr(bare, "state_set", lambda k, v: state.__setitem__(k, v))
    monkeypatch.setattr(bare, "TESLA_REDIRECT", "https://example.test/carcommand/settings/sources/tesla/callback")
    monkeypatch.setattr(bare, "db", lambda: Recording(log))

    def add(conn, vault_, kind, vin, label, config, secret):
        rows[:] = [r for r in rows if r["kind"] != kind]
        rows.append({"id": 7, "vin": vin, "kind": kind, "label": label, "config": config, "plain": secret, "enabled": True, "last_ok_at": None, "last_error": None})
        log.append(("add", kind))
        return 7

    def reseal(conn, vault_, source_id, secret, kind="tesla"):
        rows[0]["plain"] = secret
        log.append(("reseal", json.loads(secret)["refresh_token"]))

    def noted(conn, source_id, ok, error):
        rows[0]["last_error"] = error
        log.append(("noted", error))

    monkeypatch.setattr(bare.sources, "add", add)
    monkeypatch.setattr(bare.sources, "reseal", reseal)
    monkeypatch.setattr(bare.sources, "noted", noted)
    monkeypatch.setattr(bare.sources, "load", lambda conn, vault_: list(rows))

    class T:
        pass

    t = T()
    t.state, t.log, t.rows, t.app = state, log, rows, bare

    def answer(*a):
        fake = Answers(*a)
        monkeypatch.setattr(bare, "tesla_app", lambda: tesla_fleet.Fleet("client-alpha", "ta-secret.springfield", call=fake))
        return fake

    t.answer = answer
    answer()
    return t


def test_start_sends_you_to_tesla_with_a_fresh_state_each_time(tesla, client):  # noqa: F811
    r1 = client.get("/settings/sources/tesla/start")
    s1 = tesla.state["tesla_oauth_state"].split()[0]
    r2 = client.get("/settings/sources/tesla/start")
    s2 = tesla.state["tesla_oauth_state"].split()[0]
    assert r1.status_code == 303 and r1.headers["location"].startswith(tesla_fleet.AUTHORIZE)
    assert f"state={s2}" in r2.headers["location"] and s1 != s2


@pytest.mark.parametrize("query", ["code=c1", "code=c1&state=wrong"])
def test_a_way_back_without_the_right_state_saves_nothing(tesla, client, query):  # noqa: F811
    client.get("/settings/sources/tesla/start")
    r = client.get(f"/settings/sources/tesla/callback?{query}")
    assert r.status_code == 400 and "start again" in r.text.lower()
    assert tesla.rows == []


def test_a_stale_state_saves_nothing(tesla, client):  # noqa: F811
    tesla.state["tesla_oauth_state"] = "old 1000"
    r = client.get("/settings/sources/tesla/callback?code=c1&state=old")
    assert r.status_code == 400 and tesla.rows == []


def test_a_cancelled_sign_in_says_so(tesla, client):  # noqa: F811
    client.get("/settings/sources/tesla/start")
    s = tesla.state["tesla_oauth_state"].split()[0]
    r = client.get(f"/settings/sources/tesla/callback?error=access_denied&state={s}")
    assert r.status_code == 400 and "cancelled" in r.text.lower() and tesla.rows == []


def test_a_good_sign_in_keeps_the_account_sealed_with_its_region(tesla, client):  # noqa: F811
    tesla.answer((200, {"access_token": "acc", "refresh_token": "ref", "expires_in": 28800}), (200, {"response": {"fleet_api_base_url": "https://fleet-api.prd.na.vn.cloud.tesla.com"}}))
    client.get("/settings/sources/tesla/start")
    s = tesla.state["tesla_oauth_state"].split()[0]
    r = client.get(f"/settings/sources/tesla/callback?code=c1&state={s}")
    assert r.status_code == 303 and r.headers["location"].endswith("/settings/sources/tesla?signed=1")
    assert len(tesla.rows) == 1 and tesla.rows[0]["kind"] == "tesla"
    assert json.loads(tesla.rows[0]["plain"])["refresh_token"] == "ref"
    assert tesla.rows[0]["config"]["base"].startswith("https://fleet-api")
    assert "commit" in tesla.log
    assert client.get(f"/settings/sources/tesla/callback?code=c1&state={s}").status_code == 400, "a state works once"


def test_tesla_refusing_the_code_shows_why_with_the_secret_masked(tesla, client):  # noqa: F811
    tesla.answer((400, {"error": "invalid_grant", "error_description": "code expired for ta-secret.springfield"}))
    client.get("/settings/sources/tesla/start")
    s = tesla.state["tesla_oauth_state"].split()[0]
    r = client.get(f"/settings/sources/tesla/callback?code=c1&state={s}")
    assert r.status_code == 400 and "invalid_grant" in r.text and "ta-secret.springfield" not in r.text and tesla.rows == []


def account(tesla, expires_at):
    tesla.rows.append(
        {
            "id": 7,
            "vin": None,
            "kind": "tesla",
            "label": "Tesla",
            "config": {"base": tesla_fleet.NA},
            "plain": json.dumps({"access_token": "old", "refresh_token": "r1", "expires_at": expires_at}),
            "enabled": True,
            "last_ok_at": None,
            "last_error": None,
        }
    )


def test_a_fresh_token_is_used_as_it_is(tesla):
    import time

    account(tesla, time.time() + 3600)
    assert tesla.app.tesla_access() == ("old", tesla_fleet.NA)
    assert not any(isinstance(x, tuple) and x[0] == "reseal" for x in tesla.log)


def test_an_expiring_token_is_refreshed_and_the_new_one_kept_before_it_is_used(tesla):
    account(tesla, 0)
    tesla.answer((200, {"access_token": "new", "refresh_token": "r2", "expires_in": 28800}))
    assert tesla.app.tesla_access() == ("new", tesla_fleet.NA)
    assert tesla.log[-2:] == [("reseal", "r2"), "commit"], "the single-use refresh token is saved and committed before the caller has the access token"


def test_a_refused_refresh_keeps_the_account_and_asks_for_a_new_sign_in(tesla):
    account(tesla, 0)
    tesla.answer((401, {"error": "login_required"}))
    assert tesla.app.tesla_access() is None
    assert len(tesla.rows) == 1 and "sign in" in tesla.rows[0]["last_error"].lower()


def test_reseal_replaces_only_the_secret():
    class Conn:
        def __init__(self):
            self.sql = []

        def execute(self, sql, args):
            self.sql.append((sql, args))

    vault, conn = sources.Vault(Fernet.generate_key().decode()), Conn()
    sources.reseal(conn, vault, 7, "s3cret")
    sql, args = conn.sql[0]
    assert "SET secret" in sql and "config" not in sql and args[1] == 7 and vault.open(args[0], "tesla", 7) == "s3cret"


def form(bare, data):  # noqa: F811
    import auth

    return dict(data, csrf_token=auth.csrf_token(bare.AUTH["secret"], None))


def test_the_generic_add_form_sends_a_tesla_account_to_its_own_page(tesla, client):  # noqa: F811
    r = client.post("/settings/sources", data=form(tesla.app, {"kind": "tesla", "url": "postgresql://x"}))
    assert r.status_code == 303 and r.headers["location"].endswith("/settings/sources/tesla")
    assert tesla.rows == []


def test_testing_the_tesla_account_asks_tesla_for_its_cars(tesla, client, monkeypatch):  # noqa: F811
    import time

    account(tesla, time.time() + 3600)
    tesla.answer((200, {"response": [{"vin": "5YJ3E1EA0JF000001", "display_name": "Alpha"}]}))
    r = client.post("/settings/sources/7/test", data=form(tesla.app, {}))
    assert r.status_code == 200 and "1 car" in r.text
    assert ("noted", None) in tesla.log


ALPHA, BRAVO = "5YJ3E1EA0JF000001", "5YJ3E1EA0JF000002"


def done_facts(**kw):
    f = {"app_ok": True, "app_error": None, "key": True, "public_ok": True, "public_error": None, "registered": True, "register_error": None, "account": True, "account_error": None}
    f["cars"] = [
        {"vin": ALPHA, "name": "Alpha", "paired": True, "firmware": "2026.32.5", "keys": 6, "known_as": "Alpha"},
        {"vin": BRAVO, "name": "Bravo", "paired": False, "firmware": "2026.20.1", "keys": 4, "known_as": None},
    ]
    return {**f, **kw}


def test_the_page_walks_each_car_through_its_key(tesla, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tesla.app, "TESLA_DOMAIN", "example.test")
    monkeypatch.setattr(tesla.app, "tesla_facts", lambda **_: done_facts())
    r = client.get("/settings/sources/tesla")
    assert r.status_code == 200
    html = r.text
    assert html.count('class="step done"') == 3 and 'class="step next" id="step-keys"' in html
    assert f"https://tesla.com/_ak/example.test?vin={BRAVO}" in html and f"_ak/example.test?vin={ALPHA}" not in html
    assert html.count('class="segno"') == 1, "a QR for the car still waiting, none for the car that has the key"
    assert "history continues as Alpha" in html and "new to Car Command" in html and "6 of 20 keys used" in html


def test_the_first_visit_shows_what_to_paste_into_tesla(tesla, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tesla.app, "TESLA_DOMAIN", "example.test")
    monkeypatch.setattr(
        tesla.app,
        "tesla_facts",
        lambda **_: {
            "app_ok": False,
            "app_error": None,
            "key": False,
            "public_ok": False,
            "public_error": None,
            "registered": False,
            "register_error": None,
            "account": False,
            "account_error": None,
            "cars": [],
        },
    )
    html = client.get("/settings/sources/tesla").text
    assert "https://example.test/carcommand/settings/sources/tesla/callback" in html
    assert "vehicle_location" in html and "TESLA_CLIENT_SECRET" in html and 'class="step next" id="step-app"' in html


def test_registering_checks_the_domain_serves_the_key_before_telling_tesla(tesla, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tesla.app, "TESLA_DOMAIN", "example.test")
    monkeypatch.setattr(tesla.app, "tesla_public_check", lambda pem: "example.test answered 302 for the key.")
    monkeypatch.setattr(tesla.app, "tesla_facts", lambda **_: done_facts(registered=False, account=False, cars=[]))
    fake = tesla.answer()
    r = client.post("/settings/sources/tesla/register", data=form(tesla.app, {}))
    assert r.status_code == 400 and "302" in r.text
    assert fake.sent == [], "Tesla is not asked while the domain cannot serve the key"
    assert tesla.state.get("tesla_private_key"), "the key was made and kept"


def test_registering_tells_tesla_once_the_key_is_reachable(tesla, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tesla.app, "TESLA_DOMAIN", "example.test")
    monkeypatch.setattr(tesla.app, "tesla_public_check", lambda pem: None)
    fake = tesla.answer((200, {"access_token": "partner", "expires_in": 1}), (200, {"response": {}}))
    r = client.post("/settings/sources/tesla/register", data=form(tesla.app, {}))
    assert r.status_code == 303 and r.headers["location"].endswith("/settings/sources/tesla?registered=1")
    assert fake.sent[1][3] == {"domain": "example.test"}


def test_with_a_stream_address_the_app_registers_and_pairs_under_it(tesla, client, monkeypatch):  # noqa: F811
    """Tesla keeps one domain per app; registering the stream address replaces the site's, so
    once one is set, step 2 registers it and the pairing links point at it."""
    monkeypatch.setattr(tesla.app, "TESLA_DOMAIN", "example.test")
    monkeypatch.setattr(tesla.app, "TESLA_STREAM_HOST", "carcommand-stream.tail0000.ts.net")
    monkeypatch.setattr(tesla.app, "tesla_public_check", lambda pem: None)
    fake = tesla.answer((200, {"access_token": "partner", "expires_in": 1}), (200, {"response": {}}))
    r = client.post("/settings/sources/tesla/register", data=form(tesla.app, {}))
    assert r.status_code == 303
    assert fake.sent[1][3] == {"domain": "carcommand-stream.tail0000.ts.net"}
    assert tesla.app.tesla_key_domain() == "carcommand-stream.tail0000.ts.net"
    monkeypatch.setattr(tesla.app, "TESLA_STREAM_HOST", "")
    assert tesla.app.tesla_key_domain() == "example.test"


def test_registering_needs_the_form_token(tesla, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tesla.app, "TESLA_DOMAIN", "example.test")
    tesla.app.AUTH.update(enabled=True)
    try:
        assert client.post("/settings/sources/tesla/register", data={}).status_code in (303, 403)
        assert tesla.state.get("tesla_private_key") is None
    finally:
        tesla.app.AUTH.update(enabled=False)


def test_the_public_check_names_a_sign_in_page_in_front_of_the_key(tesla, monkeypatch):
    import httpx

    class R:
        status_code, text = 302, ""

    monkeypatch.setattr(tesla.app, "TESLA_DOMAIN", "example.test")
    monkeypatch.setattr(tesla.app.httpx, "get", lambda *a, **k: R())
    assert "sign-in page" in tesla.app.tesla_public_check("pem")

    def down(*a, **k):
        raise httpx.ConnectError("no")

    monkeypatch.setattr(tesla.app.httpx, "get", down)
    assert "did not answer" in tesla.app.tesla_public_check("pem")


def test_a_new_sign_in_forgets_the_old_accounts_cars(tesla, client):  # noqa: F811
    import time

    tesla.app.TESLA_CACHE["vehicles"] = (time.time(), [{"vin": ALPHA, "name": "Someone else's"}])
    tesla.answer((200, {"access_token": "acc", "refresh_token": "ref", "expires_in": 28800}), (200, {"response": {"fleet_api_base_url": tesla_fleet.NA}}))
    client.get("/settings/sources/tesla/start")
    s = tesla.state["tesla_oauth_state"].split()[0]
    assert client.get(f"/settings/sources/tesla/callback?code=c1&state={s}").status_code == 303
    assert "vehicles" not in tesla.app.TESLA_CACHE


def test_removing_the_account_forgets_its_cars(tesla, client, monkeypatch):  # noqa: F811
    import time

    account(tesla, time.time() + 3600)
    monkeypatch.setattr(tesla.app.sources, "remove", lambda conn, sid: tesla.rows.clear())
    monkeypatch.setattr(tesla.app.sources, "bump", lambda conn: None)
    monkeypatch.setattr(tesla.app, "apply_sources", lambda: {})
    tesla.app.TESLA_CACHE["vehicles"] = (time.time(), [{"vin": ALPHA, "name": "Alpha"}])
    client.post("/settings/sources/7/remove", data=form(tesla.app, {}))
    assert "vehicles" not in tesla.app.TESLA_CACHE


def test_the_way_back_saves_under_the_refresh_lock(tesla, client, monkeypatch):  # noqa: F811
    class Lock:
        def __enter__(self):
            tesla.log.append("lock")

        def __exit__(self, *a):
            tesla.log.append("unlock")

    monkeypatch.setattr(tesla.app, "TESLA_LOCK", Lock())
    tesla.answer((200, {"access_token": "acc", "refresh_token": "ref", "expires_in": 28800}), (200, {"response": {"fleet_api_base_url": tesla_fleet.NA}}))
    client.get("/settings/sources/tesla/start")
    s = tesla.state["tesla_oauth_state"].split()[0]
    client.get(f"/settings/sources/tesla/callback?code=c1&state={s}")
    i = tesla.log.index(("add", "tesla"))
    assert "lock" in tesla.log[:i] and "unlock" in tesla.log[i:], "a refresh in flight cannot write over the sign-in just made"


def test_a_garbled_kept_state_is_a_spent_link_not_a_crash(tesla, client):  # noqa: F811
    tesla.state["tesla_oauth_state"] = "abc not-a-time"
    r = client.get("/settings/sources/tesla/callback?code=c1&state=abc")
    assert r.status_code == 400 and tesla.rows == []


def test_the_account_is_kept_when_only_the_region_lookup_fails(tesla, client):  # noqa: F811
    tesla.answer((200, {"access_token": "acc", "refresh_token": "ref", "expires_in": 28800}), (503, {"error": "busy"}))
    client.get("/settings/sources/tesla/start")
    s = tesla.state["tesla_oauth_state"].split()[0]
    r = client.get(f"/settings/sources/tesla/callback?code=c1&state={s}")
    assert r.status_code == 303 and len(tesla.rows) == 1 and tesla.rows[0]["config"]["base"] == tesla_fleet.NA, "the code is spent; the tokens must not be"


def test_key_status_says_when_the_account_needs_a_sign_in(tesla, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tesla.app, "tesla_access", lambda: None)
    r = client.get("/settings/sources/tesla/keys.json")
    assert r.status_code == 409 and "sign-in" in r.json()["error"].lower()
    html = (Path(__file__).parent / "templates" / "settings_tesla.html").read_text()
    assert "r.status === 409" in html, "the page stops looking when the account needs a sign-in"


# ---- step 5: streaming
HOST = "carcommand-stream.tail0000.ts.net"


@pytest.fixture
def streaming(tesla, monkeypatch):
    """An account with a fresh token, the app's key, a stream address, and a config store that records."""
    import time

    import tesla_keys

    account(tesla, time.time() + 3600)
    pem = tesla_keys.new_private_pem()
    tesla.state["tesla_private_key"] = tesla.app.VAULT.seal(pem, "state", "tesla_private_key").decode()
    monkeypatch.setattr(tesla.app, "TESLA_STREAM_HOST", HOST)
    tesla.app.TESLA_CACHE.clear()
    tesla.app.TESLA_CACHE["vehicles"] = (time.time(), [{"vin": ALPHA, "name": "Alpha"}, {"vin": BRAVO, "name": "Bravo"}])
    used = []
    monkeypatch.setattr(tesla.app.tesla_stream_worker, "use", lambda vins, url=None: used.append(sorted(vins)))

    def set_config(conn, source_id, config):
        tesla.rows[0]["config"] = config
        tesla.log.append(("config", sorted(config.get("stream") or [])))

    monkeypatch.setattr(tesla.app.sources, "set_config", set_config)
    tesla.pem, tesla.used = pem, used
    return tesla


def test_starting_a_car_sends_tesla_a_config_signed_with_the_apps_key(streaming, client):  # noqa: F811
    import base64

    import tesla_jws

    fake = streaming.answer((200, {"response": {"updated_vehicles": 1, "skipped_vehicles": {}}}))
    r = client.post("/settings/sources/tesla/stream/start", data=form(streaming.app, {"vin": ALPHA}))
    assert r.status_code == 303 and "streaming=1" in r.headers["location"]
    method, url, _data, body = fake.sent[0]
    assert method == "POST" and url.endswith("/api/1/vehicles/fleet_telemetry_config_jws") and body["vins"] == [ALPHA]
    head, claims, sig = body["token"].split(".")
    claims = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
    assert claims["hostname"] == HOST and claims["port"] == 8443 and "Location" in claims["fields"] and claims["aud"] == tesla_jws.TELEMETRY_AUDIENCE
    assert streaming.rows[0]["config"]["stream"] == [ALPHA] and streaming.used[-1] == [ALPHA], "kept with the account, and the listener told"


def test_a_car_tesla_skips_says_why_and_is_not_started(streaming, client):  # noqa: F811
    streaming.answer((200, {"response": {"updated_vehicles": 0, "skipped_vehicles": {"missing_key": [BRAVO]}}}))
    r = client.post("/settings/sources/tesla/stream/start", data=form(streaming.app, {"vin": BRAVO}))
    assert r.status_code == 400 and "Bravo does not hold the app&#39;s key yet" in r.text
    assert "stream" not in streaming.rows[0]["config"] and not streaming.used


def test_a_car_not_on_the_account_is_never_started(streaming, client):  # noqa: F811
    fake = streaming.answer()
    r = client.post("/settings/sources/tesla/stream/start", data=form(streaming.app, {"vin": "5YJ3E1EA0JF000009"}))
    assert r.status_code == 400 and "not on the signed-in Tesla account" in r.text and fake.sent == []


def test_starting_needs_the_form_token(streaming, client):  # noqa: F811
    fake = streaming.answer()
    streaming.app.AUTH.update(enabled=True)
    try:
        assert client.post("/settings/sources/tesla/stream/start", data={"vin": ALPHA}).status_code in (303, 403)
    finally:
        streaming.app.AUTH.update(enabled=False)
    assert fake.sent == []


def test_stopping_forgets_the_car_and_asks_tesla_to_take_the_config_out(streaming, client):  # noqa: F811
    streaming.rows[0]["config"] = {"base": tesla_fleet.NA, "stream": [ALPHA, BRAVO]}
    fake = streaming.answer((200, {"response": {}}))
    r = client.post("/settings/sources/tesla/stream/stop", data=form(streaming.app, {"vin": ALPHA}))
    assert r.status_code == 303 and streaming.rows[0]["config"]["stream"] == [BRAVO] and streaming.used[-1] == [BRAVO]
    assert fake.sent[0][0] == "DELETE" and fake.sent[0][1].endswith(f"/vehicles/{ALPHA}/fleet_telemetry_config")


def test_registering_the_stream_address_checks_it_serves_the_key_first(streaming, client, monkeypatch):  # noqa: F811
    checked = []
    monkeypatch.setattr(streaming.app, "tesla_public_check", lambda pem, host=None: checked.append(host) or None)
    fake = streaming.answer((200, {"access_token": "partner"}), (200, {"response": {}}))
    r = client.post("/settings/sources/tesla/stream/register", data=form(streaming.app, {}))
    assert r.status_code == 303 and checked == [HOST]
    assert fake.sent[1][1].endswith("/api/1/partner_accounts") and fake.sent[1][3] == {"domain": HOST}


def test_a_new_sign_in_keeps_the_cars_that_stream(streaming, client):  # noqa: F811
    streaming.rows[0]["config"] = {"base": tesla_fleet.NA, "stream": [ALPHA]}
    streaming.answer((200, {"access_token": "acc", "refresh_token": "ref", "expires_in": 28800}), (200, {"response": {"fleet_api_base_url": tesla_fleet.NA}}))
    client.get("/settings/sources/tesla/start")
    s = streaming.state["tesla_oauth_state"].split()[0]
    assert client.get(f"/settings/sources/tesla/callback?code=c1&state={s}").status_code == 303
    assert streaming.rows[0]["config"]["stream"] == [ALPHA]


def test_the_listener_is_given_the_started_cars():
    import app as app_module

    rows = [{"kind": "teslafi", "config": {}}, {"kind": "tesla", "config": {"base": "x", "stream": [ALPHA]}}]
    assert app_module.tesla_streaming(rows) == [ALPHA] and app_module.tesla_streaming([]) == []


def test_step_five_asks_tesla_and_the_receiver_and_reads_the_listener(streaming, monkeypatch):
    import time

    from datetime import datetime, timezone

    import tesla_keys

    streaming.rows[0]["config"] = {"base": tesla_fleet.NA, "stream": [ALPHA]}
    w = streaming.app.tesla_stream_worker
    monkeypatch.setattr(w, "state", {ALPHA: {"at": datetime.now(timezone.utc), "connected": True}})
    monkeypatch.setattr(w, "car_errors", {})
    monkeypatch.setattr(streaming.app, "tesla_public_check", lambda pem, host=None: None)
    monkeypatch.setattr(streaming.app.tesla_net, "reach", lambda host: {"ok": True, "problem": None, "error": None, "expires": time.time() + 60 * 86400})
    streaming.answer(
        (200, {"access_token": "partner"}),
        (200, {"response": {"public_key": tesla_keys.public_hex(streaming.pem)}}),
        (200, {"response": {"synced": True, "limit_reached": False, "key_paired": True, "config": {"hostname": HOST, "port": 8443}}}),
        (200, {"access_token": "partner"}),
        (200, {"response": {"fleet_telemetry_errors": [{"vin": ALPHA, "name": "evotlb", "error": "dial tcp: timeout", "created_at": "2026-10-04T10:00:00Z"}]}}),
    )
    f = {"cars": [{"vin": ALPHA, "name": "Alpha", "paired": True}, {"vin": BRAVO, "name": "Bravo", "paired": True}]}
    streaming.app.tesla_stream_facts(f, streaming.app.tesla_app(), ("old", tesla_fleet.NA), streaming.pem, [ALPHA])
    assert f["stream_host_ok"] and f["stream_registered"] and f["receiver"]["ok"] and f["receiver"]["expires_at"]
    alpha, bravo = f["stream_cars"][ALPHA], f["stream_cars"][BRAVO]
    assert alpha["on"] and alpha["live"] and alpha["config"]["synced"] and alpha["errors"][0]["name"] == "evotlb"
    assert not bravo["on"] and not bravo["config_known"], "a car not started costs no call"


def test_after_a_restart_the_last_message_comes_from_the_stored_records(streaming, monkeypatch):
    """The listener's memory starts empty with the site; a car that streamed before (and is
    asleep now) is not "nothing heard yet": its newest cc_tesla_raw record says when."""
    from datetime import datetime, timedelta, timezone

    app = streaming.app
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(app.tesla_stream_worker, "state", {})
    monkeypatch.setattr(app.tesla_stream_worker, "car_errors", {})
    app.TESLA_CACHE.clear()
    stored = {ALPHA: {"at": now - timedelta(hours=3), "connected": False}, BRAVO: {"at": now - timedelta(seconds=30), "connected": True}}
    asked = []
    monkeypatch.setattr(app, "stream_heard", lambda vins: asked.append(vins) or stored)
    f = {"cars": [{"vin": ALPHA, "name": "Alpha", "paired": True}, {"vin": BRAVO, "name": "Bravo", "paired": True}]}
    app.tesla_stream_facts(f, None, ("old", tesla_fleet.NA), None, [ALPHA, BRAVO])
    alpha, bravo = f["stream_cars"][ALPHA], f["stream_cars"][BRAVO]
    assert alpha["last_at"] == stored[ALPHA]["at"] and not alpha["live"], "asleep three hours: heard, not live"
    assert bravo["live"], "a record half a minute old: live"
    app.tesla_stream_facts(f, None, ("old", tesla_fleet.NA), None, [ALPHA, BRAVO])
    assert asked == [[ALPHA, BRAVO]], "kept 30 s: re-opening the page asks the database once"
    app.TESLA_CACHE.clear()


def test_the_stored_last_message_is_one_index_lookup_a_car_and_a_disconnect_is_not_live():
    import app as app_module
    from datetime import datetime, timezone

    at = datetime(2026, 10, 5, 17, 38, tzinfo=timezone.utc)
    seen = []

    class Conn:
        def execute(self, sql, params):
            seen.append((sql, params))
            row = {"at": at, "kind": "connectivity", "status": "DISCONNECTED"} if params[0] == ALPHA else None

            class R:
                def fetchone(self):
                    return row

            return R()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    orig = app_module.db
    app_module.db = lambda: Conn()
    try:
        got = app_module.stream_heard([ALPHA, BRAVO])
    finally:
        app_module.db = orig
    assert got == {ALPHA: {"at": at, "connected": False}}
    assert all("ORDER BY at DESC LIMIT 1" in s and "WHERE vin = %s" in s for s, _ in seen) and len(seen) == 2


def test_the_page_shows_each_car_with_start_or_stop_and_what_tesla_says(tesla, client, monkeypatch):  # noqa: F811
    from datetime import datetime, timezone

    monkeypatch.setattr(tesla.app, "TESLA_DOMAIN", "example.test")
    monkeypatch.setattr(tesla.app, "TESLA_STREAM_HOST", HOST)
    cars = [{"vin": ALPHA, "name": "Alpha", "paired": True, "known_as": "Alpha"}, {"vin": BRAVO, "name": "Bravo", "paired": True, "known_as": None}]
    stream = {
        ALPHA: {
            "on": True,
            "config_known": True,
            "config": {"synced": True, "limit_reached": False},
            "live": True,
            "last_at": datetime(2026, 10, 4, 17, 0, tzinfo=timezone.utc),
            "errors": [{"name": "evotlb", "body": "dial tcp: timeout", "at": None}],
            "sent": tesla.app.tesla_net.fields_version(),
        },
        BRAVO: {"on": False, "config_known": False, "config": None, "live": False, "last_at": None, "errors": []},
    }
    f = done_facts(
        cars=cars,
        stream_host=HOST,
        stream_host_ok=True,
        stream_registered=True,
        receiver={"ok": True, "problem": None, "error": None, "expires": None, "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc)},
        stream_cars=stream,
        stream_dropped=12,
    )
    monkeypatch.setattr(tesla.app, "tesla_facts", lambda **_: f)
    html = client.get("/settings/sources/tesla").text
    assert 'class="step done" id="step-stream"' in html and f"{HOST}:8443" in html and "certificate the cars trust" in html
    assert html.count("/settings/sources/tesla/stream/stop") == 1 and html.count("/settings/sources/tesla/stream/start") == 1
    assert 'class="dot live"' in html and "Streaming now · config taken" in html
    assert "Tesla couldn&#39;t reach the receiver" in html and "dial tcp: timeout" in html and "evotlb" not in html, "Tesla's error in words, the raw text behind the (i), never the app id"
    assert "12 records from cars not started here were dropped" in html and "comes next" not in html
    assert "config is outdated" not in html and ">Update<" not in html, "a car sent today's field list is up to date"
    stream[ALPHA]["sent"] = "older"
    html = client.get("/settings/sources/tesla").text
    assert "config is outdated" in html and ">Update<" in html and html.count("/settings/sources/tesla/stream/start") == 2, "a car sent an older list is offered Update"
    assert html.count('class="notice warn bar streamold"') == 1, "only the started car: Bravo, not started, is never outdated"


def test_the_page_says_how_to_set_up_the_stream_before_there_is_one(tesla, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tesla.app, "TESLA_DOMAIN", "example.test")
    cars = [{"vin": ALPHA, "name": "Alpha", "paired": True, "known_as": "Alpha"}]
    monkeypatch.setattr(tesla.app, "tesla_facts", lambda **_: done_facts(cars=cars, stream_host=None, stream_host_ok=False))
    html = client.get("/settings/sources/tesla").text
    assert 'class="step next" id="step-stream"' in html and "TESLA_STREAM_HOST" in html and "Register the stream address" not in html


def test_started_cars_with_an_older_field_list_are_sent_the_new_one_once(tesla, monkeypatch):
    app = tesla.app
    monkeypatch.setattr(app, "TESLA_STREAM_HOST", HOST)
    monkeypatch.setattr(app, "tesla_app", lambda: object())
    monkeypatch.setattr(app, "tesla_access", lambda: ("acc", "https://fleet.example.test"))
    monkeypatch.setattr(app.tesla_stream_worker, "vins", {ALPHA, BRAVO})
    started = [ALPHA, BRAVO]
    monkeypatch.setattr(app, "tesla_started_now", lambda: list(started))
    added = []
    monkeypatch.setattr(app, "_tesla_streaming_set", lambda change: added.append(change))
    now = app.tesla_net.fields_version()
    tesla.state[f"stream_fields:{BRAVO}"] = now
    sent = []

    def send(vin):
        sent.append(vin)
        tesla.state[f"stream_fields:{vin}"] = now
        return None

    monkeypatch.setattr(app, "tesla_stream_send", send)
    assert app.tesla_refresh_configs() == [ALPHA] and sent == [ALPHA], "Bravo has today's list already"
    assert added == [], "a re-send never adds a car to the stream list"
    assert app.tesla_refresh_configs() == [] and sent == [ALPHA], "once"
    tesla.state[f"stream_fields:{ALPHA}"] = "older"
    monkeypatch.setattr(app, "tesla_stream_send", lambda vin: sent.append(vin) or "Tesla said no")
    assert app.tesla_refresh_configs() == [], "tried with this list already: the owner's Update is the retry"
    monkeypatch.setattr(app.tesla_stream_worker, "vins", set())
    tesla.state.clear()
    assert app.tesla_refresh_configs() == [], "never a car not started"


@pytest.mark.parametrize("status,retries", [(None, True), (503, True), (400, False), (412, False)])
def test_tesla_out_of_reach_leaves_one_more_try_and_a_refusal_none(tesla, monkeypatch, status, retries):
    app = tesla.app
    monkeypatch.setattr(app, "TESLA_STREAM_HOST", HOST)
    monkeypatch.setattr(app, "tesla_app", lambda: object())
    monkeypatch.setattr(app, "tesla_access", lambda: ("acc", "https://fleet.example.test"))
    monkeypatch.setattr(app.tesla_stream_worker, "vins", {ALPHA})
    monkeypatch.setattr(app, "tesla_started_now", lambda: [ALPHA])
    sent = []

    def fail(vin):
        sent.append(vin)
        raise tesla_fleet.TeslaError("Tesla did not answer" if status is None else f"Tesla refused ({status})", status)

    monkeypatch.setattr(app, "tesla_stream_send", fail)
    app.tesla_refresh_configs()  # the first start
    app.tesla_refresh_configs()  # the next
    app.tesla_refresh_configs()  # and the one after
    assert len(sent) == (2 if retries else 1), "a network failure or a 5xx: one more try, then no more; a 4xx: none"


def test_stop_pressed_meanwhile_is_never_undone(tesla, monkeypatch):
    app = tesla.app
    monkeypatch.setattr(app, "TESLA_STREAM_HOST", HOST)
    monkeypatch.setattr(app, "tesla_app", lambda: object())
    monkeypatch.setattr(app, "tesla_access", lambda: ("acc", "https://fleet.example.test"))
    monkeypatch.setattr(app.tesla_stream_worker, "vins", {ALPHA, BRAVO})  # as the check began
    monkeypatch.setattr(app, "tesla_started_now", lambda: [BRAVO])  # Alpha was stopped since
    sent = []
    monkeypatch.setattr(app, "tesla_stream_send", lambda vin: sent.append(vin))
    assert app.tesla_refresh_configs() == [BRAVO] and sent == [BRAVO]
    assert app.STREAM_LOCK.acquire(blocking=False), "the lock is let go"
    app.STREAM_LOCK.release()
