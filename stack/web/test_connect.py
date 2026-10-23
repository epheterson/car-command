"""Signing an app in: the rules (pure), the pages and the code exchange through the real
middleware, and the API stream. The database is a small fake that keeps what the routes write."""

import importlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import connect
import livestream

VERIFIER = "v" * 43
CHALLENGE = connect.challenge_of(VERIFIER)
GOOD = {"client": "Example", "redirect_uri": "example-app://done", "state": "st-1", "code_challenge": CHALLENGE, "code_challenge_method": "S256"}


# ---- the rules ---------------------------------------------------------------


def test_pkce_matches_the_rfc_example():
    assert connect.challenge_of("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_pkce_good_and_bad():
    assert connect.pkce_ok(VERIFIER, CHALLENGE)
    assert not connect.pkce_ok("w" * 43, CHALLENGE)
    assert not connect.pkce_ok("short", connect.challenge_of("short")), "a verifier outside RFC 7636's length is no verifier"
    assert not connect.pkce_ok(None, CHALLENGE) and not connect.pkce_ok(12, CHALLENGE)


def test_a_good_request_is_cleaned():
    params, problem = connect.check_request({**GOOD, "client": "  Example "})
    assert problem is None and params["client"] == "Example" and params["redirect_uri"] == "example-app://done"


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.test/cb",
        "http://example.test/cb",
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,x",
        "file:///etc/passwd",
        "ftp://x/y",
        "//example.test/cb",
        "/cb",
        "cb",
        "",
        "example-app:",
        "example-app://done#frag",
        "alpha app://done",
        "example-app://done\\x",
        "example-app://done\nX: 1",
        "1app://done",
        "a" * 600 + "://x",
    ],
)
def test_redirects_that_are_not_an_apps_are_refused(uri):
    params, problem = connect.check_request({**GOOD, "redirect_uri": uri})
    assert params is None and problem


@pytest.mark.parametrize("uri", ["example-app://done", "example-app:/done", "com.example.alpha:/oauth", "example-app://auth/done"])
def test_a_custom_scheme_is_accepted(uri):
    assert connect.check_request({**GOOD, "redirect_uri": uri})[1] is None


@pytest.mark.parametrize(
    "bad",
    [
        {"client": ""},
        {"client": "x" * 41},
        {"client": "a\nb"},
        {"state": ""},
        {"state": "s" * 300},
        {"code_challenge": "short"},
        {"code_challenge": CHALLENGE + "="},
        {"code_challenge_method": "plain"},
        {"code_challenge_method": None},
    ],
)
def test_every_parameter_is_checked(bad):
    assert connect.check_request({**GOOD, **bad})[0] is None


def test_a_redirect_keeps_the_query_it_had():
    url = connect.redirect_to("example-app://done?mode=x", code="c&d", state="s s")
    assert url.startswith("example-app://done?") and parse_qs(urlsplit(url).query) == {"mode": ["x"], "code": ["c&d"], "state": ["s s"]}
    assert connect.redirect_to("example-app:/cb", error="access_denied") == "example-app:/cb?error=access_denied"


def test_a_code_is_kept_only_as_its_hash():
    code = connect.new_code()
    assert len(code) >= 40 and connect.code_hash(code) != code and len(connect.code_hash(code)) == 64
    assert "code " not in connect.DDL.replace("cc_connect_codes", "")


def test_the_label_names_the_app_and_the_device():
    assert connect.token_label("Example", " Bravo phone ") == "Example · Bravo phone"
    assert connect.token_label("Example", None) == "Example" and connect.token_label("Example", "") == "Example"
    assert connect.token_label("Example", "x" * 90) == "Example · " + "x" * 40


def test_only_the_exchange_path_is_open_to_a_stranger():
    import auth

    def verdict(path):
        return auth.gate(path, "GET", enabled=True, password_set=True, signed_in=False, csrf_valid=True)

    assert verdict("/connect") == "login" and verdict("/api/stream") == "login" and verdict("/api/whoami") == "login"


def test_the_stream_sends_the_cars_that_changed():
    a, b = {"vin": "V1", "soc": 50}, {"vin": "V2", "soc": 60}
    sent, prev = livestream.changed_states({}, {"V1": a, "V2": b})
    assert sent == [a, b]
    sent, prev = livestream.changed_states(prev, {"V1": a, "V2": {"vin": "V2", "soc": 61}})
    assert sent == [{"vin": "V2", "soc": 61}]
    sent, prev = livestream.changed_states(prev, {"V2": {"vin": "V2", "soc": 61}})
    assert sent == [] and list(prev) == ["V2"], "a car that is gone is forgotten, so it is sent again if it returns"
    assert livestream.changed_states(prev, {"V1": a, "V2": {"vin": "V2", "soc": 61}})[0] == [a]


# ---- through the middleware ----------------------------------------------------


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeDb:
    """Just the statements the connect flow and the token list make, against dicts."""

    def __init__(self):
        self.codes, self.tokens, self.apps, self.sql = {}, [], {}, []  # apps: (redirect, client) -> the hash of the token it was allowed with
        self.app_share = {}  # (redirect, client) -> whether the Allow shared where each car is

    def execute(self, sql, params=()):
        if sql.startswith("DELETE FROM cc_connect_codes WHERE expires_at"):
            self.codes = {h: r for h, r in self.codes.items() if r["expires_at"] >= params[0]}
        elif sql.startswith("INSERT INTO cc_connect_codes"):
            h, challenge, client, uri, expires = params
            self.codes[h] = {"challenge": challenge, "client": client, "redirect_uri": uri, "expires_at": expires}
        elif sql.startswith("DELETE FROM cc_connect_codes WHERE hash"):
            row = self.codes.pop(params[0], None)
            return Result([row] if row else [])
        elif sql.startswith("INSERT INTO cc_api_tokens"):
            h, label, vin, kind, last4 = params
            self.tokens.append({"hash": h, "label": label, "vin": vin, "kind": kind, "last4": last4, "created_at": datetime.now(timezone.utc), "last_used_at": None})
        elif sql.startswith("INSERT INTO cc_connect_apps"):
            uri, client, share = params
            self.apps.setdefault((uri, client), None)
            self.app_share[(uri, client)] = bool(share)
        elif sql.startswith("SELECT a.share_place FROM cc_connect_apps"):
            self.sql.append(sql)
            key = tuple(params)
            return Result([{"share_place": self.app_share.get(key, False)}] if self.apps.get(key) in {t["hash"] for t in self.tokens} else [])
        elif sql.startswith("UPDATE cc_connect_apps SET share_place"):
            share, h = params[0], params[1]
            for key, token in self.apps.items():
                if token == h:
                    self.app_share[key] = bool(share)
        elif sql.startswith("UPDATE cc_connect_codes SET share_place"):
            if params[0] in self.codes:
                self.codes[params[0]]["share_place"] = True
        elif sql.startswith("UPDATE cc_api_tokens SET share_place"):
            if "= true" in sql:
                (h,) = params
                share = True
            else:
                share, h = params
            for t in self.tokens:
                if t["hash"] == h and t["kind"] == "status":
                    t["share_place"] = bool(share)
        elif sql.startswith("SELECT a.redirect_uri, a.client FROM cc_connect_apps"):
            self.sql.append(sql)
            live = {t["hash"] for t in self.tokens}
            return Result([{"redirect_uri": u, "client": c} for (u, c), h in sorted(self.apps.items()) if h in live])
        elif sql.startswith("UPDATE cc_connect_apps SET token_hash"):
            h, uri, client = params
            if (uri, client) in self.apps:
                self.apps[(uri, client)] = h
        elif sql.startswith("DELETE FROM cc_connect_apps"):
            self.apps.pop(tuple(params), None)
        elif sql.startswith("DELETE FROM cc_api_tokens"):
            self.tokens = [t for t in self.tokens if t["hash"] != params[0]]
            self.apps = {k: (h if h != params[0] else None) for k, h in self.apps.items()}  # ON DELETE CASCADE, in effect
        elif "FROM cc_api_tokens" in sql:
            return Result(list(self.tokens))
        return Result([])

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def world(monkeypatch):
    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    os.environ.setdefault("REST_USER", "u")
    os.environ.setdefault("REST_PASSWORD", "p")
    sys.path.insert(0, str(Path(__file__).parent))
    app = importlib.import_module("app")
    import auth
    from fastapi.testclient import TestClient

    fake = FakeDb()
    monkeypatch.setattr(app, "db", lambda: fake)
    monkeypatch.setattr(app, "TOKENS", {})
    monkeypatch.setattr(app, "CONNECT_FAILURES", auth.Failures(global_brake=False))
    monkeypatch.setattr(app, "_STATES", {})
    monkeypatch.setattr(app, "STREAMS_OPEN", {})
    monkeypatch.setattr(app, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}])
    monkeypatch.setitem(app.AUTH, "enabled", True)
    monkeypatch.setitem(app.AUTH, "secret", auth.new_secret())
    monkeypatch.setitem(app.AUTH, "password", auth.hash_password("a good long password", n=2**10))
    client = TestClient(app.app, follow_redirects=False)
    cookie = auth.new_session(app.AUTH["secret"])
    client.cookies.set(auth.SESSION_COOKIE, cookie)
    app.fake, app.csrf = fake, auth.csrf_token(app.AUTH["secret"], cookie)
    yield app, client
    del app.fake, app.csrf


def allow(app, client, decision="allow", **over):
    return client.post("/connect", data={**GOOD, **over, "decision": decision, "csrf_token": app.csrf})


def code_of(response) -> str:
    return parse_qs(urlsplit(response.headers["location"]).query)["code"][0]


def exchange(client, code, verifier=VERIFIER, **extra):
    stranger = client.__class__(client.app, follow_redirects=False)  # no cookie: an app has no session
    return stranger.post("/api/connect/token", json={"code": code, "code_verifier": verifier, **extra})


def test_the_consent_page_names_the_app_and_offers_two_buttons(world):
    app, client = world
    r = client.get("/connect", params=GOOD)
    assert r.status_code == 200 and "Example wants to read your cars" in r.text
    assert 'value="allow"' in r.text and 'value="cancel"' in r.text and "csrf_token" in r.text
    assert "form-action 'self' example-app:" in r.headers["content-security-policy"], "the answer may reach the app's scheme and nowhere else"


def test_the_app_name_cannot_inject_markup(world):
    app, client = world
    r = client.get("/connect", params={**GOOD, "client": "<script>x</script>"})
    assert "<script>x</script>" not in r.text and "&lt;script&gt;" in r.text


def test_a_stranger_is_sent_to_sign_in_and_comes_back(world):
    app, client = world
    r = client.__class__(client.app, follow_redirects=False).get("/connect", params=GOOD)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=%2Fconnect%3F")


@pytest.mark.parametrize("over", [{"redirect_uri": "https://example.test/cb"}, {"redirect_uri": "javascript:alert(1)"}, {"redirect_uri": ""}, {"state": ""}, {"code_challenge": "x"}])
def test_bad_parameters_show_a_page_and_never_redirect(world, over):
    app, client = world
    r = client.get("/connect", params={**GOOD, **over})
    assert r.status_code == 400 and "location" not in r.headers
    r = allow(app, client, **over)
    assert r.status_code == 400 and "location" not in r.headers and app.fake.codes == {}


def test_connect_is_refused_with_sign_in_off(world, monkeypatch):
    app, client = world
    monkeypatch.setitem(app.AUTH, "enabled", False)
    r = client.get("/connect", params=GOOD)
    assert r.status_code == 403 and "Sign-in is off" in r.text and "location" not in r.headers
    assert allow(app, client).status_code == 403 and app.fake.codes == {}


def test_a_good_flow_ends_in_a_status_token_for_every_car(world, monkeypatch):
    app, client = world
    monkeypatch.setattr(app, "APP_REDIRECTS", connect.allowlist("example-app://done?mode=x"))  # a query is for listed addresses
    r = allow(app, client, redirect_uri="example-app://done?mode=x")
    assert r.status_code == 303
    location = urlsplit(r.headers["location"])
    query = parse_qs(location.query)
    assert location.scheme == "example-app" and query["state"] == ["st-1"] and query["mode"] == ["x"]
    (stored,) = app.fake.codes.values()
    assert connect.code_hash(query["code"][0]) in app.fake.codes and query["code"][0] not in str(app.fake.codes), "only the hash is kept"
    assert stored["challenge"] == CHALLENGE and stored["client"] == "Example"
    assert timedelta(minutes=4) < stored["expires_at"] - datetime.now(timezone.utc) <= timedelta(minutes=5)

    got = exchange(client, query["code"][0], device="Bravo phone")
    assert got.status_code == 200 and got.json().keys() == {"token", "kind"} and got.json()["kind"] == "status"
    raw = got.json()["token"]
    entry = app.TOKENS[app.auth.token_hash(raw)]
    assert entry["kind"] == "status" and entry["vin"] is None and entry["label"] == "Example · Bravo phone"
    assert [t["label"] for t in app.token_list()] == ["Example · Bravo phone"], "listed in Settings like any other"
    assert app.token_list()[0]["car"] == "every car" and raw not in str(app.fake.tokens).replace(raw[-4:], "")
    assert app.fake.codes == {}, "the code is spent"


def test_cancel_goes_back_with_access_denied_and_stores_nothing(world):
    app, client = world
    r = allow(app, client, decision="cancel")
    assert r.status_code == 303 and parse_qs(urlsplit(r.headers["location"]).query) == {"error": ["access_denied"], "state": ["st-1"]}
    assert app.fake.codes == {}


def test_the_post_needs_the_forms_csrf_token(world):
    app, client = world
    r = client.post("/connect", data={**GOOD, "decision": "allow"})
    assert r.status_code == 400 and app.fake.codes == {}


def test_a_reused_code_is_refused(world):
    app, client = world
    code = code_of(allow(app, client))
    assert exchange(client, code).status_code == 200
    r = exchange(client, code)
    assert r.status_code == 400 and r.json() == {"error": "invalid_grant"}
    assert len(app.fake.tokens) == 1


def test_a_wrong_verifier_burns_the_code(world):
    app, client = world
    code = code_of(allow(app, client))
    assert exchange(client, code, "w" * 43).json() == {"error": "invalid_grant"}
    assert exchange(client, code).status_code == 400, "the right verifier is too late"
    assert app.fake.tokens == [] and app.fake.codes == {}


def test_an_expired_code_is_refused_and_deleted(world):
    app, client = world
    code = code_of(allow(app, client))
    for row in app.fake.codes.values():
        row["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert exchange(client, code).status_code == 400 and app.fake.tokens == [] and app.fake.codes == {}


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b'{"code": 5}', b'{"code": ""}', b'{"code_verifier": "x"}'])
def test_a_malformed_exchange_is_invalid_grant(world, body):
    app, client = world
    r = client.__class__(client.app, follow_redirects=False).post("/api/connect/token", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json() == {"error": "invalid_grant"}


def test_guessing_is_slowed_like_a_bad_password(world):
    app, client = world
    answers = [exchange(client, f"guess-{i}").status_code for i in range(8)]
    assert answers[:5] == [400] * 5 and 429 in answers
    code = code_of(allow(app, client))
    assert exchange(client, code).status_code == 429, "held, even for a good code, until the wait runs down"
    assert app.fake.tokens == []


def test_no_session_opens_the_exchange_and_nothing_else_in_api(world):
    app, client = world
    stranger = client.__class__(client.app, follow_redirects=False)
    assert stranger.post("/api/connect/token", json={}).status_code == 400
    assert stranger.get("/api/now").status_code == 401
    assert stranger.get("/api/stream").status_code == 401
    assert stranger.get("/api/connect/token").status_code == 401, "only the POST is open"
    assert stranger.post("/api/connect/token/x", json={}).status_code == 401, "exactly this path, not its neighbours"
    assert stranger.post("/api/whoami").status_code == 401


def test_the_minted_token_cannot_open_a_page(world):
    app, client = world
    raw = exchange(client, code_of(allow(app, client))).json()["token"]
    stranger = client.__class__(client.app, follow_redirects=False)
    assert stranger.get("/drives", headers={"Authorization": f"Bearer {raw}"}).status_code == 303
    r = stranger.get("/api/admin/places", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 403


def test_the_exchange_answers_exactly_token_and_kind(world):
    app, client = world
    got = exchange(client, code_of(allow(app, client)))
    assert got.json().keys() == {"token", "kind"} and got.json()["kind"] == "status" and got.headers["cache-control"] == "no-store"


def test_a_code_belongs_to_no_origin_cookie_or_mount(world):
    """Allowed on the WAN through a proxy under /carcommand, redeemed on the LAN at the root."""
    app, client = world
    wan = client.__class__(client.app, follow_redirects=False, base_url="https://garage.example.test")
    wan.cookies.set(*next(iter(client.cookies.items())))
    r = wan.post("/carcommand/connect", data={**GOOD, "decision": "allow", "csrf_token": app.csrf})
    assert r.status_code == 303
    lan = client.__class__(client.app, follow_redirects=False, base_url="http://192.0.2.7:3044")
    got = lan.post("/api/connect/token", json={"code": code_of(r), "code_verifier": VERIFIER})
    assert got.status_code == 200 and got.json()["kind"] == "status"
    again = client.__class__(client.app, follow_redirects=False, base_url="https://garage.example.test")
    assert again.post("/cc/api/connect/token", json={"code": code_of(r), "code_verifier": VERIFIER}).status_code == 400, "and once only, on any mount"
    assert set(app.fake.codes) == set() and not any("garage.example.test" in str(v) or "/carcommand" in str(v) for v in app.fake.tokens)


def test_the_allowlist_passes_only_exact_matches():
    allowed = connect.allowlist(" example-app://done , bravo:/cb ,")
    assert allowed == {"example-app://done", "bravo:/cb"}
    assert connect.check_request(GOOD, allowed)[1] is None
    for uri in ("example-app://done/", "example-app://done?x=1", "example-app://don", "example-app://done/more", "charlie://done", "EXAMPLE-APP://done"):
        assert connect.check_request({**GOOD, "redirect_uri": uri}, allowed)[0] is None, uri
    assert connect.check_request({**GOOD, "redirect_uri": "charlie://done"})[1] is None, "empty: any custom scheme"
    assert connect.allowlist("") == connect.allowlist(None) == frozenset()
    assert connect.check_request({**GOOD, "redirect_uri": "https://x.test/"}, connect.allowlist("https://x.test/"))[0] is None, "the list cannot let a web address in"


def allowed_by_hand(app, uri="example-app://done", client="Example"):
    """An app this install has allowed once, with the live token that Allow minted."""
    app.fake.tokens.append({"hash": "h-live", "label": client, "vin": None, "kind": "status", "last4": "live", "created_at": datetime.now(timezone.utc), "last_used_at": None})
    app.fake.apps[(uri, client)] = "h-live"


def test_the_allowlist_holds_on_both_pages(world, monkeypatch):
    app, client = world
    monkeypatch.setattr(app, "APP_REDIRECTS", connect.allowlist("example-app://done"))
    assert client.get("/connect", params=GOOD).status_code == 200, "listed, but never allowed by hand here: the Allow page"
    allowed_by_hand(app)
    assert client.get("/connect", params=GOOD).status_code == 303, "listed, allowed once, and signed in: straight back to the app with a code"
    app.fake.codes.clear()
    other = {**GOOD, "redirect_uri": "charlie://done"}
    assert client.get("/connect", params=other).status_code == 400
    r = allow(app, client, redirect_uri="charlie://done")
    assert r.status_code == 400 and "location" not in r.headers and app.fake.codes == {}
    assert allow(app, client).status_code == 303


def test_tokens_do_not_expire_and_a_revoked_one_gets_401(world):
    app, client = world
    raw = exchange(client, code_of(allow(app, client))).json()["token"]
    assert "expires" not in str(app.auth.API_DDL) and all("expires" not in t for t in app.TOKENS.values())
    stranger = client.__class__(client.app, follow_redirects=False)
    head = {"Authorization": f"Bearer {raw}"}
    assert stranger.get("/api/now", headers=head).status_code != 401
    app.TOKENS.clear()  # what Revoke does, once reload_tokens has run
    r = stranger.get("/api/now", headers=head)
    assert r.status_code == 401 and "reconnect" in r.json()["error"]
    assert stranger.get("/api/stream", headers=head).status_code == 401


# ---- the status kind

LOCATION = ("lat", "lon", "address", "place", "heading", "speed_mph", "at_home", "odometer_mi", "meters")


def full_car(vin="V1", **over):
    return {
        "vin": vin,
        "name": "Alpha",
        "soc": 58,
        "status": "Charging",
        "charge_minutes_left": 42,
        "lat": 1.5,
        "lon": 2.5,
        "heading": 90,
        "speed_mph": 0,
        "address": "1 Main St",
        "place": "Home",
        "at_home": True,
        "odometer_mi": 12345.6,
        "meters": {"trip": 3.0, "at": datetime(2026, 1, 2, tzinfo=timezone.utc)},
        **over,
    }


@pytest.fixture
def status_client(world, monkeypatch):
    app, client = world
    monkeypatch.setattr(app, "live_vehicle", lambda vin=None: full_car(vin or "V1"))
    monkeypatch.setattr(app, "garage", lambda: {})
    raw = exchange(client, code_of(allow(app, client))).json()["token"]
    app.status_raw = raw
    return app, client.__class__(client.app, follow_redirects=False, headers={"Authorization": f"Bearer {raw}"})


def test_a_status_token_reads_the_car_without_where_it_is(status_client):
    app, c = status_client
    body = c.get("/api/now?vin=V1").json()
    assert body["soc"] == 58 and body["charge_minutes_left"] == 42 and body["vin"] == "V1"
    assert "odometer_mi" not in body and "meters" not in body, "successive readings of either give the car's speed"
    assert not set(LOCATION) & set(body)
    assert all(not set(LOCATION) & set(car) for car in c.get("/api/now?vin=all").json())


def test_each_car_carries_its_chart_colour_for_light_and_dark(status_client, monkeypatch):
    app, c = status_client
    monkeypatch.setattr(app, "all_vehicles", lambda: [{"vin": "V1"}, {"vin": "V2"}])
    monkeypatch.setattr(app, "car_colors", lambda vehicles: {"V1": 3, "V2": 1})
    _, light, dark = app.identity.PALETTE[3]
    one = c.get("/api/now?vin=V1").json()
    assert (one["color"], one["color_dark"]) == (light, dark)
    cars = {car["vin"]: car for car in c.get("/api/now?vin=all").json()}
    assert cars["V2"]["color"] == app.identity.PALETTE[1][1]
    assert set(app.api_states(None, hide_location=True)["V1"]) >= {"color", "color_dark"}


def test_a_status_token_reads_nothing_else(status_client):
    app, c = status_client
    for path in ("/api/drives", "/api/charges", "/api/health", "/api/whoami", "/api/driveprint", "/api/month-routes?month=2026-01", "/api/admin/places", "/api/nowx"):
        assert c.get(path).status_code == 403, path


def test_a_status_stream_carries_no_location(status_client, monkeypatch):
    app, c = status_client
    monkeypatch.setattr(app, "STREAM_MAX_S", 0)
    monkeypatch.setattr(app, "STREAM_S", 0.01)
    sent = states(c.get("/api/stream?vin=V1").text)
    assert len(sent) == 1
    assert sent and all(not set(LOCATION) & set(s) and s["charge_minutes_left"] == 42 for s in sent)


def test_a_status_token_for_one_car_is_still_held_to_it(world, monkeypatch):
    app, client = world
    monkeypatch.setattr(app, "live_vehicle", lambda vin=None: full_car(vin))
    raw = "tok-springfield-status"
    app.TOKENS[app.auth.token_hash(raw)] = {"label": "dash", "vin": "V1", "kind": "status", "last4": raw[-4:]}
    c = client.__class__(client.app, follow_redirects=False, headers={"Authorization": f"Bearer {raw}"})
    assert c.get("/api/now").status_code == 403 and c.get("/api/now?vin=V2").status_code == 403 and c.get("/api/stream?vin=all").status_code == 403
    assert c.get("/api/now?vin=V1").status_code == 200


def test_a_stream_ends_when_its_token_is_revoked(status_client, monkeypatch):
    app, c = status_client
    monkeypatch.setattr(app, "STREAM_S", 0.01)
    monkeypatch.setattr(app, "STREAM_MAX_S", 5)
    looks = []

    def live(vin=None):
        looks.append(vin)
        app.TOKENS.pop(app.auth.token_hash(app.status_raw), None)  # revoked after the first look
        return full_car(vin or "V1")

    monkeypatch.setattr(app, "live_vehicle", live)
    started = time.monotonic()
    body = c.get("/api/stream?vin=V1").text
    assert len(states(body)) == 1 and len(looks) == 1 and time.monotonic() - started < 3, "the stream closed itself"


def test_the_mint_form_offers_status_and_the_list_badges_it(world, monkeypatch):
    app, client = world
    exchange(client, code_of(allow(app, client)))
    assert app.token_list()[0]["kind"] == "status"
    page = (Path(__file__).parent / "templates" / "settings_access.html").read_text()
    assert 'value="status"' in page and "'Status'" in page
    assert app.auth.kind_of("status") == "status" and app.auth.kind_of("admin") == "admin" and app.auth.kind_of("root") == "read"


# ---- the API stream --------------------------------------------------------------


def states(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.split("\n") if line.startswith("data: ")]


@pytest.fixture
def streaming(world, monkeypatch):
    app, client = world
    calls = {"n": 0}

    def live(vin=None):
        calls["n"] += 1
        # Alpha never moves; Bravo's charge climbs on every look.
        return {"vin": vin, "name": {"V1": "Alpha", "V2": "Bravo"}[vin], "soc": 50 if vin == "V1" else 60 + calls["n"], "at": datetime(2026, 1, 2, tzinfo=timezone.utc)}

    monkeypatch.setattr(app, "live_vehicle", live)
    monkeypatch.setattr(app, "STREAM_MAX_S", 0)
    monkeypatch.setattr(app, "STREAM_S", 0.01)
    return app, client


def test_the_stream_opens_with_every_car_as_now_events(streaming):
    app, client = streaming
    r = client.get("/api/stream")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    assert r.text.startswith(": keep\n\n") and r.text.count("event: now\n") == 2
    assert [s["vin"] for s in states(r.text)] == ["V1", "V2"] and states(r.text)[0]["at"] == "2026-01-02T00:00:00+00:00", "jsonable, like /api/now"


def test_the_stream_then_sends_only_what_changed(streaming, monkeypatch):
    app, client = streaming
    monkeypatch.setattr(app, "STREAM_MAX_S", 0.3)
    sent = states(client.get("/api/stream").text)
    assert [s["vin"] for s in sent].count("V1") == 1, "an unchanged car is sent once"
    assert [s["vin"] for s in sent].count("V2") >= 2


def test_one_car_by_vin(streaming):
    app, client = streaming
    assert [s["vin"] for s in states(client.get("/api/stream?vin=V2").text)] == ["V2"]
    assert client.get("/api/stream?vin=nope").status_code == 404
    assert len(states(client.get("/api/stream?vin=all").text)) == 2


def test_a_one_car_token_is_held_to_its_car(streaming):
    app, client = streaming
    raw = "tok-springfield-alpha"
    app.TOKENS[app.auth.token_hash(raw)] = {"label": "dash", "vin": "V1", "kind": "read", "last4": raw[-4:]}
    stranger = client.__class__(client.app, follow_redirects=False, headers={"Authorization": f"Bearer {raw}"})
    assert stranger.get("/api/stream").status_code == 403
    assert stranger.get("/api/stream?vin=all").status_code == 403
    assert stranger.get("/api/stream?vin=V2").status_code == 403
    mine = stranger.get("/api/stream?vin=V1")
    assert mine.status_code == 200 and [s["vin"] for s in states(mine.text)] == ["V1"]


def test_a_car_that_cannot_be_read_does_not_end_the_stream(streaming, monkeypatch):
    app, client = streaming

    def broken(vin=None):
        raise RuntimeError("connector down")

    monkeypatch.setattr(app, "live_vehicle", broken)
    r = client.get("/api/stream")
    assert r.status_code == 200 and states(r.text) == []


# ---- charge_minutes_left


NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)


def test_the_connectors_own_estimate_wins():
    from units import charge_minutes_left

    reached = (NOW + timedelta(minutes=37, seconds=20)).isoformat()
    assert charge_minutes_left("Charging", reached, 50, 20, 80, 100, NOW) == 37
    assert charge_minutes_left("Charging", (NOW - timedelta(minutes=5)).isoformat(), 50, 20, 80, 100, NOW) == 0


def test_without_one_it_is_the_energy_left_over_the_power():
    from units import charge_minutes_left

    assert charge_minutes_left("Charging", None, 60, 20, 80, 100, NOW) == 60, "60 kWh to add at 60 kW"
    assert charge_minutes_left("Charging", "not a time", 60, 20, 80, 100, NOW) == 60
    assert charge_minutes_left("Charging", None, 60, 90, 80, 100, NOW) == 0, "already past the target"
    assert isinstance(charge_minutes_left("Charging", None, 7.2, 41, 80, 90, NOW), int)


@pytest.mark.parametrize(
    "status,power,soc,target,cap",
    [("Idle", 60, 20, 80, 100), ("Charging", 0, 20, 80, 100), ("Charging", None, 20, 80, 100), ("Charging", 60, None, 80, 100), ("Charging", 60, 20, None, 100), ("Charging", 60, 20, 80, None)],
)
def test_it_is_none_when_not_charging_or_unknown(status, power, soc, target, cap):
    from units import charge_minutes_left

    assert charge_minutes_left(status, None, power, soc, target, cap, NOW) is None


def test_the_car_payload_carries_it_as_an_int_or_none(world, monkeypatch):
    app, client = world

    def car(state, power):
        leaf = lambda val: {"val": val}  # noqa: E731
        return {
            "name": leaf("Alpha"),
            "state": leaf("parked"),
            "connection_state": leaf("reachable"),
            "battery_capacity_kwh": leaf(100.0),
            "drives": {"primary": {"level": leaf(20.0)}},
            "charging": {"state": leaf(state), "power": leaf(power), "settings": {"target_level": leaf(80.0)}},
        }

    monkeypatch.setattr(app, "places", lambda: [])
    monkeypatch.setattr(app, "garage", lambda: {"V1": car("charging", 60.0)})
    now = app.live_vehicle("V1")
    assert now["charge_minutes_left"] == 60 and type(now["charge_minutes_left"]) is int
    monkeypatch.setattr(app, "garage", lambda: {"V1": car("off", 0.0)})
    assert app.live_vehicle("V1")["charge_minutes_left"] is None
    assert app.jsonable(now)["charge_minutes_left"] == 60


# ---- the review's fixes: browser hand-off redirects

HAND_OFF = [
    "ddgquicklink://attacker%2Eexample/cb",
    "ddgquicklink://attacker．example/cb",
    "ddgquicklink://attacker。example/cb",
    "ddgquicklink://3232235777/cb",
    "ddgquicklink://[2001:db8::1]/cb",
    "ddgquicklink://user@attacker%2eexample/cb",
    "ddgquicklink://attacker.example@x/cb",
    "ddgquicklink:attacker.example/cb",
    "x:attacker.example/cb",
    "x:///attacker.example/cb",
    "x://open?url=attacker.example",
    "samsunginternet://open?url=//attacker.example/cb",
    "android-app://com.android.chrome/https/a.example/",
    "example-app://u:p@host/cb",
    "example-app://host:8080/cb",
    "example-app://done?x=1",
    "example-app://done/../x",
    "example-app://done/a.b",
    "example-app://Done",
    "EXAMPLE-APP://done",
    "ＨＴＴＰＳ://a.example",
    "example-app://done?x=ｈｔｔｐｓ：//a.example",
    "example-app://done/%2e%2e",
    "x-safari-https%3A//a.example",
    "example-app://",
    "example-app:///",
    "example-app://done\n",
    "x-safari-https://attacker.example/cb",
    "googlechromes://attacker.example/cb",
    "googlechrome://attacker.example/cb",
    "microsoft-edge:https://attacker.example/cb",
    "firefox://open-url?url=https://attacker.example/cb",
    "opera-https://attacker.example/cb",
    "intent://attacker.example/cb",
    "brave://attacker.example/cb",
    "ms-browser://x",
    "example-app://attacker.example/cb",
    "example-app://connected?next=https://attacker.example",
    "example-app:/cb?next=HTTP://attacker.example",
    "example-app:/cb?next=https%3A%2F%2Fattacker.example",
    "example-app:/cb?next=%2568ttps%253a%252f%252fattacker.example",
    "example-app:https://attacker.example/cb",
    "blob:https://attacker.example/x",
]


@pytest.mark.parametrize("uri", HAND_OFF)
def test_a_redirect_that_can_hand_the_code_to_a_web_page_is_refused(uri):
    assert connect.check_request({**GOOD, "redirect_uri": uri})[0] is None, uri


@pytest.mark.parametrize("uri", ["example-app://connected", "example-app:/callback", "com.example.app:/callback", "example-app://auth/done", "example-app://done/deep/path", "example-app:/"])
def test_plain_app_callbacks_pass(uri):
    assert connect.check_request({**GOOD, "redirect_uri": uri})[1] is None, uri


def test_an_exact_allowlist_match_passes_unless_it_is_a_web_address():
    for uri in ("x-safari-https://app.example/cb", "example-app://host.example/cb", "example-app://done?mode=x"):
        assert connect.check_request({**GOOD, "redirect_uri": uri}, connect.allowlist(uri))[1] is None, uri
        assert connect.check_request({**GOOD, "redirect_uri": uri}, connect.allowlist("other-app://x"))[0] is None
    for uri in ("https://app.example/cb", "http://app.example/cb"):
        assert connect.check_request({**GOOD, "redirect_uri": uri}, connect.allowlist(uri))[0] is None


def test_the_consent_page_says_where_it_will_go(world, monkeypatch):
    app, client = world
    r = client.get("/connect", params={**GOOD, "redirect_uri": "example-app://done/here"})
    assert r.status_code == 200 and "opens example-app://done/here" in r.text
    assert "calls itself" in r.text.lower() and "Example" in r.text


# ---- the request body


def test_a_chunked_body_is_refused_over_the_cap(world):
    app, client = world

    def body():
        for _ in range(50):
            yield b" " * 4096

    r = client.__class__(client.app, follow_redirects=False).post("/api/connect/token", content=body())
    assert r.status_code == 413 and r.json() == {"error": "too_large"}


def test_the_body_is_read_only_as_far_as_the_cap(world):
    import asyncio

    app, client = world
    pulled = []

    class Req:
        async def stream(self):
            for i in range(500):
                pulled.append(i)
                yield b"x" * 1000

    assert asyncio.run(app.read_capped(Req(), 4096)) is None
    assert len(pulled) == 5, "stopped at the chunk that crossed the cap"

    class Small:
        async def stream(self):
            yield b'{"a":'
            yield b"1}"

    assert asyncio.run(app.read_capped(Small(), 4096)) == b'{"a":1}'


def test_a_deeply_nested_body_is_a_bad_grant_not_a_crash(world):
    app, client = world
    s = client.__class__(client.app, follow_redirects=False)
    r = s.post("/api/connect/token", content=b"[" * 4000, headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json() == {"error": "invalid_grant"}


# ---- shared streams and the cap


def test_streams_share_one_reading(streaming, monkeypatch):
    app, client = streaming
    calls = []
    real = app.live_vehicle
    monkeypatch.setattr(app, "live_vehicle", lambda vin=None: calls.append(vin) or real(vin))
    monkeypatch.setattr(app, "STREAM_S", 15)
    for _ in range(3):
        assert len(states(client.get("/api/stream").text)) == 2
    assert sorted(calls) == ["V1", "V2"], "three streams, one read of each car"


def test_one_reading_per_view_of_the_cars(streaming, monkeypatch):
    app, client = streaming
    monkeypatch.setattr(app, "STREAM_S", 15)
    full = app.shared_states(None, False)
    assert app.shared_states(None, True) is not full and app.shared_states("all", False) is full and app.shared_states(None, False) is full


def test_the_fifth_stream_on_a_token_is_refused(world, monkeypatch):
    app, client = world
    monkeypatch.setattr(app, "live_vehicle", lambda vin=None: full_car(vin))
    monkeypatch.setattr(app, "STREAM_MAX_S", 0)
    monkeypatch.setattr(app, "STREAM_S", 0.01)
    raw = "tok-springfield-cap"
    h = app.auth.token_hash(raw)
    app.TOKENS[h] = {"label": "dash", "vin": None, "kind": "read", "last4": raw[-4:]}
    c = client.__class__(client.app, follow_redirects=False, headers={"Authorization": f"Bearer {raw}"})
    app.STREAMS_OPEN[h] = 3
    assert c.get("/api/stream?vin=V1").status_code == 200
    assert app.STREAMS_OPEN.get(h, 0) == 3, "a stream gives its place back when it ends"
    app.STREAMS_OPEN[h] = 4
    r = c.get("/api/stream?vin=V1")
    assert r.status_code == 429 and r.json() == {"error": "too_many_streams"}
    assert app.STREAMS_OPEN[h] == 4
    other = "tok-springfield-other"
    app.TOKENS[app.auth.token_hash(other)] = {"label": "o", "vin": None, "kind": "read", "last4": "ther"}
    assert client.__class__(client.app, follow_redirects=False, headers={"Authorization": f"Bearer {other}"}).get("/api/stream?vin=V1").status_code == 200, "the cap is per token"


def test_sessions_and_open_sites_are_never_capped(world, monkeypatch):
    app, client = world
    monkeypatch.setattr(app, "live_vehicle", lambda vin=None: full_car(vin))
    monkeypatch.setattr(app, "STREAM_MAX_S", 0)
    monkeypatch.setattr(app, "STREAM_S", 0.01)
    for _ in range(6):
        assert client.get("/api/stream?vin=V1").status_code == 200, "cookie session"
    monkeypatch.setitem(app.AUTH, "enabled", False)
    for _ in range(6):
        assert client.__class__(client.app, follow_redirects=False).get("/api/stream?vin=V1").status_code == 200, "sign-in off"
    assert app.STREAMS_OPEN == {}


def test_the_pages_live_stream_is_never_capped(world, monkeypatch):
    app, client = world
    monkeypatch.setattr(app, "live_cards", lambda vin, root: {})
    monkeypatch.setattr(app, "STREAM_MAX_S", 0)
    monkeypatch.setattr(app, "STREAM_S", 0.01)
    app.STREAMS_OPEN.update({"anyone": 99})
    assert all(client.get("/stream").status_code == 200 for _ in range(6))


# ---- tokens, atomically


def test_reloading_tokens_never_empties_the_table(world, monkeypatch):
    app, client = world
    keep = app.auth.token_hash("keep")
    rows = [{"hash": keep, "label": "k", "vin": None, "kind": "read", "last4": "keep"}, {"hash": "new", "label": "n", "vin": None, "kind": "status", "last4": "new1"}]
    seen = []

    class Watched(dict):
        def clear(self):
            raise AssertionError("emptied")

        def update(self, *a, **kw):
            seen.append(keep in self)
            super().update(*a, **kw)

    class Rows(FakeDb):
        def execute(self, sql, params=()):
            return Result(rows)

    t = Watched({keep: {"label": "old", "vin": None, "kind": "read", "last4": "keep"}, "gone": {"label": "g", "vin": None, "kind": "read", "last4": "gone"}})
    monkeypatch.setattr(app, "TOKENS", t)
    monkeypatch.setattr(app, "db", lambda: Rows())
    app.reload_tokens()
    assert set(t) == {keep, "new"} and seen and all(seen), "a valid token never stopped resolving; a deleted one went"
    assert t["new"]["kind"] == "status"


# ---- the exchange, tightened


def test_a_redirect_uri_at_the_exchange_must_match_the_stored_one(world):
    app, client = world
    code = code_of(allow(app, client, redirect_uri="example-app://done"))
    bad = exchange(client, code, redirect_uri="example-app://other")
    assert bad.status_code == 400 and bad.json() == {"error": "invalid_grant"}
    assert exchange(client, code).status_code == 400, "and the mismatch spent the code"
    code = code_of(allow(app, client, redirect_uri="example-app://done"))
    assert exchange(client, code, redirect_uri="example-app://done").status_code == 200
    code = code_of(allow(app, client))
    assert exchange(client, code, redirect_uri=5).status_code == 400
    code = code_of(allow(app, client))
    assert exchange(client, code).status_code == 200, "optional, so older clients still connect"


def test_there_is_no_global_brake_on_the_exchange(world, monkeypatch):
    app, client = world
    counter = iter(range(10_000))
    monkeypatch.setattr(app, "connect_key", lambda request: f"client-{next(counter)}")
    answers = {exchange(client, f"guess-{i}").status_code for i in range(60)}
    assert answers == {400}, "sixty strangers do not slow a sixty-first"


def test_the_throttle_counts_an_ipv6_network_as_one_client():
    import auth

    a, b = auth.client_prefix("2001:db8:1:2:aaaa::1"), auth.client_prefix("2001:db8:1:2:bbbb::9")
    assert a == b and a != auth.client_prefix("2001:db8:1:3::1")
    assert auth.client_prefix("203.0.113.9") == "203.0.113.9" and auth.client_prefix("?") == "?"


def test_a_failing_client_is_held_without_a_global_brake():
    import auth

    f = auth.Failures(global_brake=False)
    for i in range(200):
        f.record(f"k{i}", 100.0)
    assert f.wait("fresh", 100.0) == 0.0
    for _ in range(6):
        f.record("one", 100.0)
    assert f.wait("one", 100.0) > 0


@pytest.mark.parametrize("auth_on", [True, False])
def test_an_unknown_bearer_is_401_with_sign_in_on_or_off(world, monkeypatch, auth_on):
    app, client = world
    monkeypatch.setitem(app.AUTH, "enabled", auth_on)
    c = client.__class__(client.app, follow_redirects=False, headers={"Authorization": "Bearer nonsense"})
    for path in ("/api/now", "/api/stream", "/api/whoami"):
        assert c.get(path).status_code == 401, path


def test_concurrent_reloads_never_lose_a_token(world, monkeypatch):
    import threading

    app, client = world
    keep = app.auth.token_hash("keep")
    rows = [{"hash": keep, "label": "k", "vin": None, "kind": "read", "last4": "keep"}]

    class Rows(FakeDb):
        def execute(self, sql, params=()):
            return Result(list(rows))

    monkeypatch.setattr(app, "db", lambda: Rows())
    app.TOKENS.clear()
    app.TOKENS[keep] = {"label": "k", "vin": None, "kind": "read", "last4": "keep"}
    missing, errors, stop = [], [], threading.Event()

    def reload_many():
        try:
            for _ in range(200):
                app.reload_tokens()
        except Exception as err:  # noqa: BLE001
            errors.append(err)

    def watch():
        while not stop.is_set():
            if keep not in app.TOKENS:
                missing.append(1)

    threads = [threading.Thread(target=reload_many) for _ in range(6)]
    watcher = threading.Thread(target=watch)
    watcher.start()
    [t.start() for t in threads]
    [t.join() for t in threads]
    stop.set()
    watcher.join()
    assert not errors and not missing and list(app.TOKENS) == [keep]


def test_the_throttle_keys_ipv4_mapped_addresses_by_their_ipv4():
    import auth

    assert auth.client_prefix("::ffff:203.0.113.9") == "203.0.113.9"
    assert auth.client_prefix("203.0.113.9") == "203.0.113.9"
    assert auth.client_prefix("2001:db8:1:2::1").endswith("/64") and auth.client_prefix("not an address") == "not an address"


# ---- the last three


@pytest.mark.parametrize("uri", ["x://0x7f000001/cb", "x://0x5db8d822/cb", "x://0x5db8d822", "x://3232235777/cb", "x://12345", "x://1a/cb" + "?x=1", "x-web-search://q", "web-search-app://q"])
def test_a_host_written_as_a_number_or_a_search_launcher_is_refused(uri):
    assert connect.check_request({**GOOD, "redirect_uri": uri})[0] is None, uri


@pytest.mark.parametrize("uri", ["x://a1/cb", "x://connected2", "x://auth-2/done"])
def test_an_authority_that_starts_with_a_letter_still_passes(uri):
    assert connect.check_request({**GOOD, "redirect_uri": uri})[1] is None, uri


def test_minting_and_revoking_do_their_database_work_off_the_event_loop(world, monkeypatch):
    import asyncio

    app, client = world
    on_loop = []
    for name in ("store_token", "reload_tokens"):
        real = getattr(app, name)
        monkeypatch.setattr(app, name, lambda *a, _real=real, _name=name, **kw: (on_loop.append(_name) if asyncio._get_running_loop() else None, _real(*a, **kw))[1])
    monkeypatch.setattr(app, "settings_render", lambda request, section, **kw: app.JSONResponse({"ok": True}))
    r = client.post("/settings/tokens", data={"label": "x", "kind": "read", "csrf_token": app.csrf})
    assert r.status_code == 200 and len(app.fake.tokens) == 1
    h = app.fake.tokens[0]["hash"]
    r = client.post(f"/settings/tokens/{h}/revoke", data={"csrf_token": app.csrf})
    assert r.status_code == 303
    real_set = app.auth.store_set
    monkeypatch.setattr(app.auth, "store_set", lambda *a, **kw: (on_loop.append("store_set") if asyncio._get_running_loop() else None, real_set(*a, **kw))[1])
    assert client.post("/settings/tokens/anywhere", data={"anywhere": "on", "csrf_token": app.csrf}).status_code == 303
    assert on_loop == [], f"ran on the loop: {on_loop}"


def test_the_app_stream_carries_the_garage_door_and_hides_whether_a_car_is_home_from_a_token_without_places(status_client, monkeypatch):
    app, c = status_client
    monkeypatch.setattr(app, "STREAM_MAX_S", 0)
    monkeypatch.setattr(app, "STREAM_S", 0.01)
    monkeypatch.setattr(app.garagedoor, "configured", lambda: True)
    opened = "2026-10-17T22:00:00+00:00"
    monkeypatch.setattr(app.garagedoor, "READER", app.garagedoor.Reader(get=lambda: {"state": "open", "last_changed": opened}))
    text = c.get("/api/stream?vin=V1").text
    door = [json.loads(b.split("data: ", 1)[1]) for b in text.split("\n\n") if b.startswith("event: garage")]
    assert door == [{"state": "open", "open": True, "since": opened}]
