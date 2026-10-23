"""Sign-in edges: who may pin Cloudflare Access, verdicts that outlive their token, turning it off,
apps that skip the Allow page, and the smaller things."""

import asyncio

import httpx
import pytest

import connect
import proxyauth
from test_cloudflare_access import AUD, HDR, LAN, NOW, TEAM, TUNNEL, Certs, Clock, at, cf, form_token, jwt, pin, pinned, team_only  # noqa: F401 - fixtures
from test_connect import GOOD, VERIFIER, code_of, exchange  # noqa: F401
from test_connect import world as connect_world  # noqa: F401 - fixture
from test_first_run import app_module, client_from, fresh  # noqa: F401 - fixtures
from test_signin_switch import PASSWORD, csrf, render_access, sign_in, world  # noqa: F401 - fixtures

OWNER = "owner@example.test"


def listed(app, *emails):
    app.EMAILS = set(emails)


@pytest.fixture
def team(cf):  # noqa: F811
    """A team configured, nothing pinned, and the owner's e-mail not listed (the default)."""
    app, client = cf
    app.mp.setenv("CF_ACCESS_TEAM", TEAM)
    app.CF.update(team=TEAM, aud=None, env_team=True)
    app.mp.setattr(app, "EMAILS", set())
    return app, client


def tap(app, who, token, **form):
    return pin(app, who, token, **form)


# ---- 1. who may pin


def test_a_neighbour_with_another_apps_token_cannot_pin_it(team):
    app, _ = team
    other = jwt(aud=["some-other-app-in-the-team"], email="guest@else.test")
    r = tap(app, at(app, LAN), other)
    assert r.status_code == 403 and app.CF["aud"] is None and app.AUTH["enabled"] is False and "cf_aud" not in app.stored and app.told == []


def test_a_listed_owner_can_pin_with_one_tap(team):
    app, _ = team
    listed(app, OWNER)
    r = tap(app, at(app, LAN), jwt())
    assert r.status_code == 303 and app.CF["aud"] == AUD and app.stored["cf_aud"] == AUD


def test_the_list_always_applies_to_the_pinning_token(team):
    app, _ = team
    listed(app, OWNER)
    code = app.auth.setup_code(app.AUTH["secret"])
    r = tap(app, at(app, LAN), jwt(email="guest@else.test"), code=code)
    assert r.status_code == 403 and app.CF["aud"] is None, "not even the right setup code lets an unlisted person pin"


def test_a_service_token_can_never_pin(team):
    app, _ = team
    code = app.auth.setup_code(app.AUTH["secret"])
    token = jwt(email=None, common_name="svc.access")
    for who in (at(app, "127.0.0.1"), at(app, LAN)):
        assert tap(app, who, token, code=code).status_code == 403
    listed(app, OWNER)
    assert tap(app, at(app, LAN), token).status_code == 403 and app.CF["aud"] is None


def test_the_setup_code_lets_a_neighbour_pin_when_no_one_is_listed(team):
    app, _ = team
    code = app.auth.setup_code(app.AUTH["secret"])
    assert tap(app, at(app, LAN), jwt(), code="AAAA-BBBB-CCCC").status_code == 403 and app.CF["aud"] is None
    assert tap(app, at(app, LAN), jwt(), code=code).status_code == 303 and app.CF["aud"] == AUD


def test_even_the_machine_itself_needs_the_code_to_pin(team):
    """A header-less proxy on this machine (nginx, socat, ssh -R, Tailscale Funnel) is loopback. The owner has a terminal."""
    app, _ = team
    assert tap(app, at(app, "127.0.0.1"), jwt()).status_code == 403 and app.CF["aud"] is None
    code = app.auth.setup_code(app.AUTH["secret"])
    assert tap(app, at(app, "127.0.0.1"), jwt(), code=code).status_code == 303 and app.CF["aud"] == AUD


def test_a_password_session_may_pin(team, monkeypatch):
    app, client = team
    app.stored["password"] = app.auth.hash_password(PASSWORD, n=2**10)
    monkeypatch.setitem(app.AUTH, "password", app.stored["password"])
    app.AUTH["enabled"] = True
    neighbour = at(app, LAN)
    import auth

    neighbour.cookies.set(auth.SESSION_COOKIE, auth.new_session(app.AUTH["secret"]))
    r = neighbour.post("/settings/signin/cloudflare", data={"csrf_token": csrf(app, neighbour)}, headers={HDR: jwt(email="guest@else.test")})
    assert r.status_code == 303 and app.CF["aud"] == AUD


def test_the_host_header_is_never_kept(team):
    app, _ = team
    listed(app, OWNER)
    tap(app, at(app, LAN, headers={"host": "evil.example"}), jwt())
    assert "cf_host" not in app.stored and "host" not in app.CF
    page = at(app, LAN, headers={"host": "evil.example"}).get("/settings/access")
    assert page.status_code == 401 and "evil.example" not in page.text and "Sign in through your Cloudflare Access address" in page.text and "https://" not in page.text.split("<main")[1]


def test_two_pins_at_once_keep_only_the_first(team):
    app, _ = team
    listed(app, OWNER)

    async def both():
        transport = httpx.ASGITransport(app=app.server, client=(LAN, 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://site") as c:
            token = app.auth.csrf_token(app.AUTH["secret"], None)
            first = c.post("/settings/signin/cloudflare", data={"csrf_token": token}, headers={HDR: jwt(aud=["first-app"])})
            second = c.post("/settings/signin/cloudflare", data={"csrf_token": token}, headers={HDR: jwt(aud=["second-app"])})
            return await asyncio.gather(first, second)

    asyncio.run(both())
    assert app.stored["cf_aud"] in ("first-app", "second-app") and app.CF["aud"] == app.stored["cf_aud"]
    assert [t for _, t in app.told].count(next(t for _, t in app.told)) == 1 and len(app.told) == 1, "one pin, one alert"


# ---- 2/3. verdicts


def test_a_remembered_verdict_does_not_outlive_its_token():
    clock = Clock()
    v = proxyauth.Verifier(fetch=Certs(), clock=clock)
    token = jwt(exp=NOW - 50)  # inside the leeway now, out of it in ten seconds
    assert v.verify(token, TEAM, AUD)
    clock.t += 55
    assert v.cached(token, TEAM, AUD) is None and v.verify(token, TEAM, AUD) is None


def test_a_missing_key_is_not_remembered():
    v = proxyauth.Verifier(fetch=Certs(fail=True), clock=Clock())
    assert v.check(jwt(), TEAM, AUD) == (None, "no-key")
    assert v.cached(jwt(), TEAM, AUD) is None and not v.verdicts


def test_a_deeply_nested_header_is_just_a_bad_token(cf):  # noqa: F811
    import base64

    app, _ = cf
    pinned(app)
    head = base64.urlsafe_b64encode(b"[" * 5000).rstrip(b"=").decode()
    assert proxyauth.unverified_claims(head + ".e30.AA") is None
    r = at(app, LAN, raise_server_exceptions=False).get("/", headers={HDR: head + ".e30.AA"})
    assert r.status_code == 401


# ---- 5. turning it off


def test_turning_cloudflare_off_needs_the_password_when_there_is_one(cf, monkeypatch):  # noqa: F811
    app, client = cf
    pinned(app)
    app.stored["password"] = app.auth.hash_password(PASSWORD, n=2**10)
    monkeypatch.setitem(app.AUTH, "password", app.stored["password"])
    r = at(app).post("/settings/signin/cloudflare/off", headers={HDR: jwt()}, data={"csrf_token": form_token(app)})
    assert r.status_code == 403 and app.CF["team"] == "example-team", "a Cloudflare sign-in alone is not enough"
    sign_in(app, client)
    r = client.post("/settings/signin/cloudflare/off", data={"csrf_token": csrf(app, client)})
    assert r.status_code == 303 and app.CF["team"] is None


def test_without_a_password_a_service_token_cannot_turn_it_off(cf):  # noqa: F811
    app, _ = cf
    pinned(app)
    svc = jwt(email=None, common_name="svc.access")
    r = at(app).post("/settings/signin/cloudflare/off", headers={HDR: svc}, data={"csrf_token": app.auth.csrf_token(app.AUTH["secret"], "cf:cn:svc.access")})
    assert r.status_code == 403 and app.CF["team"] == "example-team"


# ---- 7. form tokens come from the identity


def test_a_form_token_follows_the_identity_not_the_session_id(cf):  # noqa: F811
    app, _ = cf
    assert app.csrf_subject(None, {"email": "a@x.test", "common_name": "c", "sub": "s"}) == "cf:email:a@x.test"
    assert app.csrf_subject(None, {"common_name": "svc.access", "sub": ""}) == "cf:cn:svc.access"
    assert app.csrf_subject(None, {"sub": "u-1"}) == "cf:sub:u-1"
    assert app.csrf_subject(None, {"email": "x"}) != app.csrf_subject(None, {"common_name": "x"}), "the kind is part of it"
    assert app.csrf_subject("cookie", {"email": "a@x.test"}) == "cookie" and app.csrf_subject(None, None) is None
    assert app.csrf_subject(None, {}) is None or app.csrf_subject(None, {}) == "cf:sub:"


# ---- 8/9. what the page says


def test_the_page_suggests_narrowing_the_proxy_while_it_is_unset():
    assert "CC_ACCESS_PROXY" in render_access(auth_enabled=True, cf_team=TEAM, cf_pinned=True, cf_proxy_unset=True)
    assert "CC_ACCESS_PROXY" not in render_access(auth_enabled=True, cf_team=TEAM, cf_pinned=True, cf_proxy_unset=False)


def test_the_setup_page_names_only_the_command_for_this_install(app_module, fresh, monkeypatch):  # noqa: F811
    monkeypatch.delenv("CC_INSTALL", raising=False)
    page = client_from(app_module, "203.0.113.9").get("/setup").text
    assert "docker compose -f stack/docker-compose.yml exec web python3 setup_code.py" in page
    assert "carcommand web python3" not in page and "make setup-code" not in page
    monkeypatch.setenv("CC_INSTALL", "one-container")
    page = client_from(app_module, "203.0.113.9").get("/setup").text
    assert "docker exec carcommand carcommand web python3 setup_code.py" in page
    assert "docker compose" not in page and "make setup-code" not in page


# ---- 10. /setup counts the attempt first


class Spy:
    def __init__(self, real, log):
        self.real, self.log = real, log

    def wait(self, *a):
        self.log.append("wait")
        return self.real.wait(*a)

    def record(self, *a):
        self.log.append("record")
        return self.real.record(*a)

    def clear(self, *a):
        self.log.append("clear")
        return self.real.clear(*a)


def test_the_first_password_counts_the_attempt_before_checking_the_code(app_module, fresh, monkeypatch):  # noqa: F811
    import auth

    log = []
    monkeypatch.setattr(app_module, "FAILURES", Spy(auth.Failures(), log))
    c = client_from(app_module, "203.0.113.9")
    from test_first_run import _token

    token = _token(c.get("/setup").text)
    data = {"password": "a good long password", "confirm": "a good long password", "csrf_token": token}
    c.post("/setup", data={**data, "code": "AAAA-BBBB-CCCC"})
    assert log == ["wait", "record"]
    log.clear()
    c.post("/setup", data={**data, "code": auth.setup_code(app_module.AUTH["secret"])})
    assert log == ["wait", "record", "clear"]


# ---- 6. apps that skip the Allow page


def cf_connect_world(connect_world, monkeypatch):  # noqa: F811
    app, client = connect_world
    monkeypatch.setattr(app, "APP_REDIRECTS", connect.allowlist("example-app://done"))
    return app, client


def test_the_first_connect_of_a_listed_app_shows_the_allow_page_and_later_ones_skip_it(connect_world, monkeypatch):  # noqa: F811
    app, client = cf_connect_world(connect_world, monkeypatch)
    first = client.get("/connect", params=GOOD)
    assert first.status_code == 200 and "wants to read your cars" in first.text and app.fake.codes == {}
    allowed = allow_post(app, client)
    assert allowed.status_code == 303 and ("example-app://done", "Example") in app.fake.apps, "remembered after a manual Allow"
    assert exchange(client, code_of(allowed)).status_code == 200, "and the app trades its code for its token"
    app.fake.codes.clear()
    second = client.get("/connect", params=GOOD)
    assert second.status_code == 303 and code_of(second) and len(app.fake.codes) == 1


def allow_post(app, client):
    return client.post("/connect", data={**GOOD, "decision": "allow", "csrf_token": app.csrf})


def test_a_remembered_app_that_is_not_listed_still_gets_the_allow_page(connect_world, monkeypatch):  # noqa: F811
    app, client = connect_world
    monkeypatch.setattr(app, "APP_REDIRECTS", frozenset())
    allow_post(app, client)
    assert ("example-app://done", "Example") in app.fake.apps
    assert client.get("/connect", params=GOOD).status_code == 200, "listing is half of the trust"


def test_a_listed_app_under_another_name_is_a_new_app(connect_world, monkeypatch):  # noqa: F811
    app, client = cf_connect_world(connect_world, monkeypatch)
    allow_post(app, client)
    assert client.get("/connect", params={**GOOD, "client": "Another"}).status_code == 200


def test_cancelling_remembers_nothing(connect_world, monkeypatch):  # noqa: F811
    app, client = cf_connect_world(connect_world, monkeypatch)
    client.post("/connect", data={**GOOD, "decision": "cancel", "csrf_token": app.csrf})
    assert app.fake.apps == {}


def test_forgetting_an_app_brings_the_allow_page_back(connect_world, monkeypatch):  # noqa: F811
    app, client = cf_connect_world(connect_world, monkeypatch)
    exchange(client, code_of(allow_post(app, client)))
    assert app.remembered_apps() == [{"redirect_uri": "example-app://done", "client": "Example"}]
    r = client.post("/settings/connect/forget", data={"redirect_uri": "example-app://done", "client": "Example", "csrf_token": app.csrf})
    assert r.status_code == 303 and app.fake.apps == {}
    assert client.get("/connect", params=GOOD).status_code == 200


def test_the_page_lists_remembered_apps_with_a_forget_button():
    html = render_access(auth_enabled=True, remembered_apps=[{"redirect_uri": "example-app://done", "client": "Example"}])
    assert "Example" in html and "example-app://done" in html and "Forget" in html and "/settings/connect/forget" in html
    assert "Forget" not in render_access(auth_enabled=True, remembered_apps=[])


# ---- the list compares e-mails without regard to case or stray spaces


def test_the_list_of_people_ignores_case_and_spaces(team):  # noqa: F811
    app, _ = team
    assert proxyauth.parse_emails("  Owner@Example.TEST ,\tGuest@Else.test ") == {OWNER, "guest@else.test"}
    emails = proxyauth.parse_emails("Owner@Example.test")
    for claim in ("OWNER@EXAMPLE.TEST", " owner@example.test ", "Owner@example.test"):
        assert proxyauth.allowed_identity({"email": claim}, emails), claim
    assert not proxyauth.allowed_identity({"email": "owner@example.test.evil"}, emails)
    app.EMAILS = proxyauth.parse_emails(" OWNER@example.test ")
    r = tap(app, at(app, LAN), jwt(email=" Owner@Example.TEST "))
    assert r.status_code == 303 and app.CF["aud"] == AUD, "the tap compares the same way"


# ---- a remembered app lives and dies with its token


def link(app):
    return app.fake.apps.get(("example-app://done", "Example"))


def test_the_app_is_tied_to_the_token_its_allow_produced(connect_world, monkeypatch):  # noqa: F811
    app, client = cf_connect_world(connect_world, monkeypatch)
    allowed = allow_post(app, client)
    assert link(app) is None, "allowed, but no token yet"
    assert client.get("/connect", params=GOOD).status_code == 200, "so it still asks"
    raw = exchange(client, code_of(allowed)).json()["token"]
    assert link(app) == app.auth.token_hash(raw)
    skipped = client.get("/connect", params=GOOD)
    assert skipped.status_code == 303
    second = exchange(client, code_of(skipped)).json()["token"]
    assert link(app) == app.auth.token_hash(second) != app.auth.token_hash(raw), "a skipped connect moves the row to its new token"


def test_revoking_the_token_brings_the_allow_page_back(connect_world, monkeypatch):  # noqa: F811
    app, client = cf_connect_world(connect_world, monkeypatch)
    raw = exchange(client, code_of(allow_post(app, client))).json()["token"]
    assert client.get("/connect", params=GOOD).status_code == 303
    h = app.auth.token_hash(raw)
    r = client.post(f"/settings/tokens/{h}/revoke", data={"csrf_token": app.csrf})
    assert r.status_code == 303
    assert client.get("/connect", params=GOOD).status_code == 200 and app.remembered_apps() == []


def test_a_token_gone_by_any_other_path_does_too(connect_world, monkeypatch):  # noqa: F811
    app, client = cf_connect_world(connect_world, monkeypatch)
    exchange(client, code_of(allow_post(app, client)))
    app.fake.tokens.clear()  # deleted some other way
    assert client.get("/connect", params=GOOD).status_code == 200


def test_the_question_asked_of_the_database_joins_the_live_token(connect_world, monkeypatch):  # noqa: F811
    app, client = cf_connect_world(connect_world, monkeypatch)
    exchange(client, code_of(allow_post(app, client)))
    client.get("/connect", params=GOOD)
    app.remembered_apps()
    assert app.fake.sql and all("JOIN cc_api_tokens" in q for q in app.fake.sql)
    assert "REFERENCES cc_api_tokens(hash) ON DELETE CASCADE" in connect.DDL


# ---- the database work stays off the event loop


def test_connect_does_its_database_work_off_the_loop(connect_world, monkeypatch):  # noqa: F811
    import asyncio

    app, client = cf_connect_world(connect_world, monkeypatch)
    on_loop = []
    for name in ("remember_app", "issue_code", "app_known"):
        real = getattr(app, name)
        monkeypatch.setattr(app, name, lambda *a, _real=real, _name=name, **kw: (on_loop.append(_name) if asyncio._get_running_loop() else None, _real(*a, **kw))[1])
    exchange(client, code_of(allow_post(app, client)))
    assert client.get("/connect", params=GOOD).status_code == 303
    assert on_loop == [], f"ran on the loop: {on_loop}"


def test_cloudflared_on_this_machine_is_not_the_machine_for_pinning(team):  # noqa: F811
    """Under host networking cloudflared connects from 127.0.0.1 and adds proxy headers."""
    app, _ = team
    for headers in ({"Via": "1.1 cloudflared"}, {"X-Forwarded-For": "203.0.113.9"}, {"CF-Ray": "abc"}, {"X-Forwarded-Whatever": "1"}, {"Tailscale-Funnel-Request": "?1"}):
        r = tap(app, at(app, "127.0.0.1", headers=headers), jwt())
        assert r.status_code == 403 and app.CF["aud"] is None, headers
