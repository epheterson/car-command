"""Signing in through Cloudflare Access, through the real middleware (the server uvicorn runs, so the socket's
own address is known): pinning the application, the gate, who may present the header, forms bound to the
person, and the connect flow with and without the consent page."""

import logging

import pytest
from fastapi.testclient import TestClient

import connect
import proxyauth
from test_connect import GOOD, VERIFIER, code_of  # noqa: F401
from test_connect import world as connect_world  # noqa: F401 - fixture
from test_proxyauth import AUD, NOW, OTHER, TEAM, Certs, Clock, jwks, jwt
from test_signin_switch import CONNECT, PASSWORD, csrf, render_access, sign_in, world  # noqa: F401 - fixtures

HDR = "Cf-Access-Jwt-Assertion"
LAN, TUNNEL, PUBLIC = "192.168.1.40", "172.18.0.5", "93.184.216.34"


@pytest.fixture
def cf(world, monkeypatch):  # noqa: F811
    """The signin-switch world with a verifier that trusts the test key, and a counted certs fetch."""
    app, client = world
    certs = Certs()
    monkeypatch.setattr(app, "PROXY", proxyauth.Verifier(fetch=certs, clock=Clock()))
    monkeypatch.setattr(app, "PROXIES", [])
    monkeypatch.setattr(app, "EMAILS", set())
    monkeypatch.setattr(app, "_CF_REJECTED", {})
    for name in ("CF_ACCESS_TEAM", "CF_ACCESS_AUD", "CC_ACCESS_PROXY", "CC_ACCESS_EMAILS"):
        monkeypatch.delenv(name, raising=False)
    app.CF.update(team=None, aud=None, env_team=False, env_aud=False)
    app.stored["secret"] = app.AUTH["secret"]  # the signing key the install already has
    app.seed = dict(app.stored)
    app.certs, app.mp = certs, monkeypatch
    yield app, client
    app.CF.update(team=None, aud=None, env_team=False, env_aud=False)
    del app.certs, app.mp, app.seed


def at(app, peer=LAN, **kw):
    return TestClient(app.server, follow_redirects=False, client=(peer, 50000), **kw)


def team_only(app):
    """The team configured, as CF_ACCESS_TEAM does it, and nothing pinned; the owner listed, as the one tap needs."""
    app.mp.setenv("CF_ACCESS_TEAM", TEAM)
    app.mp.setattr(app, "EMAILS", {"owner@example.test"})
    app.CF.update(team=TEAM, aud=None, env_team=True)


def pinned(app):
    """Sign-in on and the application pinned, as after the one tap."""
    app.CF.update(team=TEAM, aud=AUD, env_team=False, env_aud=False)
    app.AUTH["enabled"] = True


def pin(app, client, token=None, **kw):
    headers = {HDR: token} if token else {}
    return client.post("/settings/signin/cloudflare", data={"csrf_token": csrf(app, client), **kw}, headers=headers)


def form_token(app, email="owner@example.test"):
    return app.auth.csrf_token(app.AUTH["secret"], f"cf:email:{email}")


# ---- pinning: the team is configured, the tap only learns the application


def test_the_tap_pins_the_audience_of_the_configured_team(cf, caplog):
    app, _ = cf
    team_only(app)
    c = at(app)
    old = app.AUTH["secret"]
    token = jwt(aud=[AUD, "second"])
    with caplog.at_level(logging.INFO):
        r = pin(app, c, token, next=CONNECT)
    assert r.status_code == 303 and r.headers["location"] == CONNECT
    assert app.stored["cf_aud"] == AUD and app.stored["enabled"] == "1" and "cf_team" not in app.stored, "the team was never taken from the request"
    assert app.stored["secret"] != old and app.AUTH["secret"] == app.stored["secret"], "older sessions do not come back"
    assert app.AUTH["enabled"] is True and app.CF["team"] == TEAM and app.CF["aud"] == AUD
    ((kind, text),) = app.told
    assert kind == "login" and "owner@example.test" in text and AUD[-6:] in text and AUD not in text
    assert token not in text and token not in caplog.text and AUD[-6:] in caplog.text, "the event is logged, the token never"


def test_without_a_configured_team_there_is_nothing_to_tap(cf):
    app, _ = cf
    c = at(app)
    r = pin(app, c, jwt())
    assert r.status_code == 303 and app.stored == app.seed and app.AUTH["enabled"] is False and app.CF["team"] is None
    assert app.cf_candidate(type("R", (), {"headers": {HDR: jwt()}, "scope": {app.TruePeer.KEY: LAN}})()) is False
    html = render_access(auth_enabled=False, cf_team=None, cf_offer=False)
    assert "Set CF_ACCESS_TEAM in .env" in html and 'form="cfon"' not in html


def test_a_token_from_another_team_pins_nothing_even_with_its_own_valid_certs(cf, monkeypatch):
    app, _ = cf
    team_only(app)
    theirs = "attacker-team"
    monkeypatch.setattr(app, "PROXY", proxyauth.Verifier(fetch=lambda team: jwks(k1=OTHER) if team == theirs else jwks(k1=app.PROXY.fetch.doc and OTHER), clock=Clock()))
    forged = jwt(key=OTHER, iss=proxyauth.issuer(theirs))
    r = pin(app, at(app), forged)
    assert r.status_code == 400 and app.stored == app.seed and app.CF["aud"] is None and app.AUTH["enabled"] is False and app.told == []


@pytest.mark.parametrize("token", [None, "junk", "forged", "expired", "none-alg", "no-aud"])
def test_a_request_cloudflare_did_not_vouch_for_pins_nothing(cf, token):
    app, _ = cf
    team_only(app)
    token = {None: None, "junk": "x.y.z", "forged": jwt(key=OTHER), "expired": jwt(exp=NOW - 3600), "none-alg": jwt(alg="none", sign=False), "no-aud": jwt(aud=[])}[token]
    r = pin(app, at(app), token)
    assert r.status_code == 400 and app.stored == app.seed and app.CF["aud"] is None and app.told == []


def test_a_peer_that_may_not_present_the_header_cannot_pin(cf):
    app, _ = cf
    team_only(app)
    r = pin(app, at(app, PUBLIC), jwt())
    assert r.status_code == 400 and app.stored == app.seed


def test_a_second_pin_never_overwrites(cf):
    app, _ = cf
    pinned(app)
    c = at(app)
    r = c.post("/settings/signin/cloudflare", data={"csrf_token": form_token(app)}, headers={HDR: jwt()})
    assert r.status_code == 303 and app.stored == app.seed and app.CF["aud"] == AUD
    other = jwt(aud=["a-different-application"])
    r = c.post("/settings/signin/cloudflare", data={"csrf_token": form_token(app)}, headers={HDR: other})
    assert app.stored == app.seed and app.CF["aud"] == AUD, "and a token for another application changes nothing either"


def test_pinning_needs_the_forms_csrf_token(cf):
    app, _ = cf
    team_only(app)
    r = at(app).post("/settings/signin/cloudflare", data={}, headers={HDR: jwt()})
    assert r.status_code == 400 and app.stored == app.seed


# ---- the team, by hand or from the environment


def test_someone_signed_in_with_the_password_can_name_the_team(cf, monkeypatch):
    app, client = cf
    pinned(app)
    app.CF.update(team=None, aud=None)
    app.stored["password"] = app.auth.hash_password(PASSWORD, n=2**10)
    monkeypatch.setitem(app.AUTH, "password", app.stored["password"])
    sign_in(app, client)
    r = client.post("/settings/signin/cloudflare/set", data={"team": "https://Example-Team.cloudflareaccess.com/", "csrf_token": csrf(app, client)})
    assert r.status_code == 303 and app.stored["cf_team"] == TEAM and app.stored["cf_aud"] == "" and app.CF["team"] == TEAM
    r = client.post("/settings/signin/cloudflare/set", data={"team": "evil.test/x", "csrf_token": csrf(app, client)})
    assert r.status_code == 400 and app.CF["team"] == TEAM


def test_only_a_password_session_can_name_the_team(cf, monkeypatch):
    app, _ = cf
    pinned(app)
    c = at(app)
    r = c.post("/settings/signin/cloudflare/set", data={"team": "attacker-team", "csrf_token": form_token(app)}, headers={HDR: jwt()})
    assert r.status_code == 403 and app.stored == app.seed and app.CF["team"] == TEAM, "a Cloudflare sign-in cannot point the site at another team"


def test_the_environment_names_the_team_and_application_for_a_scripted_install(cf, monkeypatch):
    app, _ = cf
    monkeypatch.setenv("CF_ACCESS_TEAM", "https://Example-Team.cloudflareaccess.com/")
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    app.load_auth()
    assert (app.CF["team"], app.CF["aud"], app.CF["env_team"], app.CF["env_aud"]) == (TEAM, AUD, True, True) and app.AUTH["enabled"] is True
    r = at(app).post("/settings/signin/cloudflare/off", headers={HDR: jwt()}, data={"csrf_token": form_token(app)})
    assert r.status_code == 409 and app.CF["team"] == TEAM, "set in .env, so not switched off from the page"


def test_a_team_in_the_environment_that_is_not_a_team_is_ignored(cf, monkeypatch):
    app, _ = cf
    for bad in ("evil.test/x", "http://example-team.cloudflareaccess.com", "a.b"):
        monkeypatch.setenv("CF_ACCESS_TEAM", bad)
        monkeypatch.setenv("CF_ACCESS_AUD", AUD)
        app.load_auth()
        assert app.CF["team"] is None and app.AUTH["enabled"] is False, bad


def test_the_team_alone_in_the_environment_does_not_turn_sign_in_on(cf, monkeypatch):
    app, _ = cf
    monkeypatch.setenv("CF_ACCESS_TEAM", TEAM)
    app.load_auth()
    assert app.CF["team"] == TEAM and app.CF["aud"] is None and app.AUTH["enabled"] is False


def test_a_restart_remembers_the_pin(cf):
    app, _ = cf
    team_only(app)
    app.stored["cf_team"] = TEAM
    pin(app, at(app), jwt())
    app.AUTH["enabled"] = False
    app.CF.update(team=None, aud=None)
    app.load_auth()
    assert app.AUTH["enabled"] is True and (app.CF["team"], app.CF["aud"]) == (TEAM, AUD)


# ---- the gate


def test_with_the_header_through_the_proxy_a_gated_page_opens(cf):
    app, _ = cf
    pinned(app)
    assert at(app, TUNNEL).get("/settings/access", headers={HDR: jwt()}).status_code == 200


def test_a_forged_header_is_refused_and_never_falls_through(cf, monkeypatch):
    app, client = cf
    pinned(app)
    monkeypatch.setitem(app.AUTH, "password", app.auth.hash_password(PASSWORD, n=2**10))
    sign_in(app, client)
    for token in ("x.y.z", jwt(key=OTHER), jwt(exp=NOW - 3600), jwt(aud=["other"]), jwt(alg="none", sign=False)):
        r = client.get("/settings/access", headers={HDR: token})
        assert r.status_code == 401, "not even a good password session gets past a bad header"
        assert at(app).get("/api/whoami", headers={HDR: token}).status_code == 401
        assert at(app).get("/api/whoami", headers={HDR: token}).json()["error"].startswith("Cloudflare Access")
    app.TOKENS[app.auth.token_hash("tok-springfield")] = {"label": "dash", "vin": None, "kind": "read", "last4": "ield"}
    assert at(app).get("/api/whoami", headers={HDR: "x.y.z", "Authorization": "Bearer tok-springfield"}).status_code == 401, "nor a bearer token"


def test_a_header_from_an_untrusted_peer_is_ignored_as_if_not_sent(cf):
    app, _ = cf
    pinned(app)
    r = at(app, PUBLIC).get("/settings/access", headers={HDR: jwt()})
    assert r.status_code == 401 and "Sign in through Cloudflare Access" in r.text, "no better than no header"
    assert at(app, PUBLIC).get("/settings/access", headers={HDR: "junk"}).status_code == 401
    assert app.certs.calls == [], "and never even looked at"


def test_a_list_of_proxies_narrows_who_may_present_it(cf, monkeypatch):
    app, _ = cf
    pinned(app)
    monkeypatch.setattr(app, "PROXIES", proxyauth.parse_proxies("172.18.0.0/16"))
    assert at(app, TUNNEL).get("/settings/access", headers={HDR: jwt()}).status_code == 200
    r = at(app, LAN).get("/settings/access", headers={HDR: jwt()})
    assert r.status_code == 401 and "Sign in through" in r.text, "the LAN is not the proxy: ignored, not believed"


def test_a_list_of_people_narrows_who_counts(cf, monkeypatch):
    app, _ = cf
    pinned(app)
    monkeypatch.setattr(app, "EMAILS", proxyauth.parse_emails("owner@example.test"))
    assert at(app).get("/settings/access", headers={HDR: jwt()}).status_code == 200
    assert at(app).get("/settings/access", headers={HDR: jwt(email="stranger@example.test")}).status_code == 401


def test_a_direct_lan_request_with_no_header_falls_back_to_the_password(cf, monkeypatch):
    app, client = cf
    pinned(app)
    r = at(app).get("/settings/access")
    assert r.status_code == 401 and "Cloudflare Access address" in r.text, "no password: the page says where to sign in"
    monkeypatch.setitem(app.AUTH, "password", app.auth.hash_password(PASSWORD, n=2**10))
    r = at(app).get("/settings/access")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    sign_in(app, client)
    assert client.get("/settings/access").status_code == 200
    app.TOKENS[app.auth.token_hash("tok-springfield")] = {"label": "dash", "vin": None, "kind": "read", "last4": "ield"}
    monkeypatch.setattr(app, "garage", lambda: {})
    assert at(app).get("/api/whoami", headers={"Authorization": "Bearer tok-springfield"}).status_code == 200


def test_an_unsigned_email_header_means_nothing(cf):
    app, _ = cf
    pinned(app)
    assert at(app).get("/settings/access", headers={"Cf-Access-Authenticated-User-Email": "owner@example.test"}).status_code == 401


@pytest.mark.parametrize("path", ["/setup", "/login"])
def test_setup_and_login_are_not_a_way_round_it(cf, path):
    app, _ = cf
    pinned(app)
    assert at(app).get(path).status_code == 401
    assert at(app).post(path, data={"password": PASSWORD, "confirm": PASSWORD}).status_code == 401 and app.stored == app.seed


def test_a_verified_header_costs_one_fetch_not_one_per_request(cf):
    app, _ = cf
    pinned(app)
    token = jwt()
    for _ in range(5):
        assert at(app).get("/settings/access", headers={HDR: token}).status_code == 200
    assert app.certs.calls == [TEAM]


def test_cloudflares_keys_being_unreachable_fails_closed(cf):
    app, _ = cf
    pinned(app)
    app.certs.fail = True
    assert at(app).get("/settings/access", headers={HDR: jwt()}).status_code == 401


def test_a_form_token_is_bound_to_the_identity_in_the_header(cf):
    app, _ = cf
    pinned(app)
    c = at(app)
    for bad in ({}, {"csrf_token": form_token(app, "other@example.test")}, {"csrf_token": app.auth.csrf_token(app.AUTH["secret"], None)}):
        assert c.post("/settings/signin/cloudflare/off", headers={HDR: jwt()}, data=bad).status_code == 400, bad
    assert app.CF["team"] == TEAM
    r = c.post("/settings/signin/cloudflare/off", headers={HDR: jwt()}, data={"csrf_token": form_token(app)})
    assert r.status_code == 303 and r.headers["location"].endswith("saved=cloudflare-off")


def test_stopping_clears_the_pin_and_tells_the_owner(cf):
    app, _ = cf
    pinned(app)
    app.stored.update(cf_team=TEAM, cf_aud=AUD, enabled="1")
    r = at(app).post("/settings/signin/cloudflare/off", headers={HDR: jwt()}, data={"csrf_token": form_token(app)})
    assert r.status_code == 303 and app.stored["cf_team"] == "" and app.stored["cf_aud"] == "" and app.CF["team"] is None
    assert app.AUTH["enabled"] is True, "sign-in stays on: with no password the site asks for one, as a new install does"
    assert [k for k, _ in app.told] == ["login"]


def test_a_stranger_cannot_stop_it(cf):
    app, _ = cf
    pinned(app)
    assert at(app).post("/settings/signin/cloudflare/off", data={"csrf_token": "x"}).status_code == 401 and app.CF["team"] == TEAM


# ---- the connect flow


def cf_connect(connect_world, monkeypatch, password=False):  # noqa: F811
    app, client = connect_world
    monkeypatch.setattr(app, "PROXY", proxyauth.Verifier(fetch=Certs(), clock=Clock()))
    monkeypatch.setattr(app, "PROXIES", [])
    monkeypatch.setattr(app, "EMAILS", set())
    monkeypatch.setattr(app, "_CF_REJECTED", {})
    if not password:
        monkeypatch.setitem(app.AUTH, "password", None)
    app.CF.update(team=TEAM, aud=AUD, env_team=False, env_aud=False)
    return app, client


def test_the_consent_page_runs_on_a_cloudflare_sign_in_with_no_password_at_all(connect_world, monkeypatch):  # noqa: F811
    app, _ = cf_connect(connect_world, monkeypatch)
    try:
        me = at(app, TUNNEL, headers={HDR: jwt()})
        page = me.get("/connect", params=GOOD)
        assert page.status_code == 200 and "wants to read your cars" in page.text
        r = me.post("/connect", data={**GOOD, "decision": "allow", "csrf_token": form_token(app)})
        assert r.status_code == 303
        got = at(app, TUNNEL).post("/api/connect/token", json={"code": code_of(r), "code_verifier": VERIFIER})
        assert got.status_code == 200 and got.json()["kind"] == "status"
    finally:
        app.CF.update(team=None, aud=None)


def test_an_app_the_install_lists_skips_the_allow_page(connect_world, monkeypatch):  # noqa: F811
    app, client = cf_connect(connect_world, monkeypatch, password=True)
    monkeypatch.setattr(app, "APP_REDIRECTS", connect.allowlist("example-app://done"))
    from test_connect import allowed_by_hand

    allowed_by_hand(app)  # allowed once by hand, with a live token, and listed
    try:
        r = at(app, TUNNEL, headers={HDR: jwt()}).get("/connect", params=GOOD)
        assert r.status_code == 303 and r.headers["location"].startswith("example-app://done?") and code_of(r) and "wants to read" not in r.text
        r = client.get("/connect", params=GOOD)
        assert r.status_code == 303 and code_of(r), "a password session does as well"
        got = at(app, TUNNEL).post("/api/connect/token", json={"code": code_of(r), "code_verifier": VERIFIER})
        assert got.json()["kind"] == "status" and "Example" in [t["label"] for t in app.token_list()], "still a status token, still listed"
    finally:
        app.CF.update(team=None, aud=None)


def test_an_unlisted_app_still_gets_the_allow_page(connect_world, monkeypatch):  # noqa: F811
    app, client = cf_connect(connect_world, monkeypatch, password=True)
    monkeypatch.setattr(app, "APP_REDIRECTS", connect.allowlist("other-app://done"))
    try:
        for me in (client, at(app, TUNNEL, headers={HDR: jwt()})):
            r = me.get("/connect", params={**GOOD, "redirect_uri": "example-app://done"})
            assert r.status_code == 400, "with a list set, only the listed pass"
        monkeypatch.setattr(app, "APP_REDIRECTS", frozenset())
        for me in (client, at(app, TUNNEL, headers={HDR: jwt()})):
            r = me.get("/connect", params=GOOD)
            assert r.status_code == 200 and "wants to read your cars" in r.text and app.fake.codes == {}
    finally:
        app.CF.update(team=None, aud=None)


def test_a_listed_app_with_nobody_signed_in_never_gets_a_code(connect_world, monkeypatch):  # noqa: F811
    app, _ = cf_connect(connect_world, monkeypatch, password=True)
    monkeypatch.setattr(app, "APP_REDIRECTS", connect.allowlist("example-app://done"))
    try:
        r = at(app, TUNNEL).get("/connect", params=GOOD)
        assert r.status_code == 303 and r.headers["location"].startswith("/login") and app.fake.codes == {}
        monkeypatch.setitem(app.AUTH, "password", None)
        r = at(app, TUNNEL).get("/connect", params=GOOD)
        assert r.status_code == 401 and app.fake.codes == {}, "Cloudflare only, and not through it"
    finally:
        app.CF.update(team=None, aud=None)


def test_a_forged_header_from_the_lan_gets_no_code(connect_world, monkeypatch):  # noqa: F811
    app, client = cf_connect(connect_world, monkeypatch, password=True)
    monkeypatch.setattr(app, "APP_REDIRECTS", connect.allowlist("example-app://done"))
    try:
        for token in ("x.y.z", jwt(key=OTHER), jwt(aud=["other"])):
            r = at(app, LAN, cookies=dict(client.cookies)).get("/connect", params=GOOD, headers={HDR: token})
            assert r.status_code == 401 and "location" not in r.headers and app.fake.codes == {}
    finally:
        app.CF.update(team=None, aud=None)


def test_the_connect_refusal_offers_the_tap_when_there_is_something_to_pin(cf):
    app, _ = cf
    team_only(app)
    r = at(app).get(CONNECT, headers={HDR: jwt()})
    assert r.status_code == 403 and "Use Cloudflare Access" in r.text
    assert 'action="/settings/signin/cloudflare"' in r.text and 'name="next" value="/connect?client=Example' in r.text and 'name="csrf_token"' in r.text
    assert "Turn on sign-in" in at(app).get(CONNECT).text and "Use Cloudflare Access" not in at(app).get(CONNECT).text
    app.CF.update(team=None)
    assert "Use Cloudflare Access" not in at(app).get(CONNECT, headers={HDR: jwt()}).text, "no configured team, no button"


def test_the_tap_carries_the_person_back_to_the_consent_page(cf):
    app, _ = cf
    team_only(app)
    r = pin(app, at(app), jwt(), next=CONNECT)
    assert r.headers["location"] == CONNECT
    page = at(app).get("/connect", params=GOOD, headers={HDR: jwt()})
    assert page.status_code == 200 and "wants to read your cars" in page.text


# ---- the page


def test_the_page_offers_the_tap_when_a_team_is_configured_and_the_request_came_through_it():
    html = render_access(auth_enabled=False, cf_team=TEAM, cf_offer=True, signin_next=CONNECT)
    assert 'form="cfon"' in html and "Use Cloudflare Access" in html and 'action="/carcommand/settings/signin/cloudflare"' in html and "example-team.cloudflareaccess.com" in html


def test_the_page_says_to_open_it_through_cloudflare_when_the_request_did_not():
    html = render_access(auth_enabled=False, cf_team=TEAM, cf_offer=False)
    assert 'form="cfon"' not in html and "Cloudflare address" in html


def test_the_page_shows_who_is_signed_in_and_offers_stopping():
    html = render_access(auth_enabled=True, cf_team=TEAM, cf_pinned=True, cf_user="owner@example.test")
    assert "signed in as owner@example.test" in html and "Stop using Cloudflare Access" in html


def test_a_team_set_in_the_environment_cannot_be_stopped_from_the_page():
    html = render_access(auth_enabled=True, cf_team=TEAM, cf_pinned=True, cf_env=True)
    assert "CF_ACCESS_TEAM" in html and "Stop using Cloudflare Access" not in html


def test_only_a_password_session_is_offered_the_team_form():
    assert 'form="cfset"' in render_access(auth_enabled=True, cf_can_set=True) and 'form="cfset"' not in render_access(auth_enabled=True, cf_can_set=False)
