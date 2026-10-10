"""Turning sign-in on and off from Settings: the flow through the middleware, the flag that
outranks the environment across a restart, the connect page's way there, and the page itself."""

import importlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PASSWORD = "a good long password"
CONNECT = "/connect?client=Example&redirect_uri=example-app%3A%2F%2Fdone&state=s"


class Conn:
    def execute(self, *_a, **_kw):
        return type("R", (), {"fetchall": lambda self: [], "fetchone": lambda self: None})()

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

    for name in ("CC_PASSWORD", "CC_SECRET", "CC_AUTH"):
        monkeypatch.delenv(name, raising=False)
    stored, told = {}, []
    monkeypatch.setattr(app, "db", lambda: Conn())
    monkeypatch.setattr(auth, "store_get", lambda conn, k: stored.get(k))
    monkeypatch.setattr(auth, "store_set", lambda conn, k, v: stored.__setitem__(k, v))
    monkeypatch.setattr(app.recorder, "notify", lambda text, kind="other": told.append((kind, text)))
    monkeypatch.setattr(app, "FAILURES", auth.Failures())
    monkeypatch.setattr(app, "TOKENS", {})
    monkeypatch.setattr(app, "settings_render", lambda request, section, **kw: app.HTMLResponse(kw.get("signin_error") or kw.get("pw_error") or "page"))
    monkeypatch.setitem(app.AUTH, "enabled", False)
    monkeypatch.setitem(app.AUTH, "secret", auth.new_secret())
    monkeypatch.setitem(app.AUTH, "password", None)
    client = TestClient(app.server, follow_redirects=False, client=("127.0.0.1", 50000))  # the machine itself, as the socket shows it
    app.stored, app.told = stored, told
    yield app, client
    del app.stored, app.told


def csrf(app, client):
    import auth

    return auth.csrf_token(app.AUTH["secret"], client.cookies.get(auth.SESSION_COOKIE))


def turn_on(app, client, password=PASSWORD, confirm=None, **extra):
    """Turning sign-in on always asks for the setup code, from loopback too: the owner has a terminal."""
    data = {"password": password, "confirm": password if confirm is None else confirm, "code": app.auth.setup_code(app.AUTH["secret"]), "csrf_token": csrf(app, client)}
    return client.post("/settings/signin/on", data={**data, **extra})


def sign_in(app, client):
    import auth

    client.cookies.clear()
    client.cookies.set(auth.SESSION_COOKIE, auth.new_session(app.AUTH["secret"]))


# ---- turning on


def test_turning_on_stores_the_password_and_the_flag_and_signs_this_browser_in(world):
    import auth

    app, client = world
    r = turn_on(app, client)
    assert r.status_code == 303 and r.headers["location"].endswith("/settings/access?saved=signin")
    assert auth.verify_password(app.stored["password"], PASSWORD) and app.stored["enabled"] == "1"
    assert app.AUTH["enabled"] is True, "live, no restart"
    assert auth.valid_session(app.AUTH["secret"], r.cookies.get(auth.SESSION_COOKIE)), "this browser is signed in at once"
    assert [k for k, _ in app.told] == ["login"] and "sign-in" in app.told[0][1].lower()


def test_a_stranger_is_now_asked_to_sign_in(world):
    app, client = world
    turn_on(app, client)
    from fastapi.testclient import TestClient

    assert TestClient(app.app, follow_redirects=False).get("/drives").status_code == 303


def test_turning_on_needs_the_forms_csrf_token(world):
    app, client = world
    r = client.post("/settings/signin/on", data={"password": PASSWORD, "confirm": PASSWORD})
    assert r.status_code == 400 and app.stored == {} and app.AUTH["enabled"] is False


@pytest.mark.parametrize("password,confirm", [("a good long password", "another one entirely"), ("short", "short"), ("", "")])
def test_a_bad_password_turns_nothing_on(world, password, confirm):
    app, client = world
    r = turn_on(app, client, password, confirm)
    assert r.status_code == 400 and app.stored == {} and app.AUTH["enabled"] is False and app.told == []


def test_a_password_set_in_the_environment_is_not_asked_for(world, monkeypatch):
    app, client = world
    monkeypatch.setenv("CC_PASSWORD", PASSWORD)
    r = client.post("/settings/signin/on", data={"code": app.auth.setup_code(app.AUTH["secret"]), "csrf_token": csrf(app, client)})
    assert r.status_code == 303 and app.AUTH["enabled"] is True and set(app.stored) <= {"enabled", "secret"} and "password" not in app.stored, "the switch only, the password stays in .env"


def test_turning_on_when_it_is_on_changes_nothing(world, monkeypatch):
    app, client = world
    monkeypatch.setitem(app.AUTH, "enabled", True)
    monkeypatch.setitem(app.AUTH, "password", __import__("auth").hash_password(PASSWORD, n=2**10))
    sign_in(app, client)
    r = turn_on(app, client, "a different long password")
    assert r.status_code == 303 and app.stored == {} and app.told == []


# ---- where to go afterwards


@pytest.mark.parametrize(
    "target,expected",
    [
        (CONNECT, CONNECT),
        ("/connect", "/connect"),
        ("https://evil.example/connect", None),
        ("//evil.example/connect", None),
        ("/\\evil.example", None),
        ("/drives", None),
        ("connect?x=1", None),
        ("", None),
        (None, None),
        ("/connect\nX: 1", None),
        ("/connectx", None),
    ],
)
def test_only_a_connect_path_on_this_site_is_a_way_back(world, target, expected):
    app, _ = world
    assert app.signin_return(target) == expected


def test_the_way_back_is_followed_under_the_mount(world):
    app, client = world
    r = client.post(
        "/carcommand/settings/signin/on", data={"password": PASSWORD, "confirm": PASSWORD, "code": app.auth.setup_code(app.AUTH["secret"]), "csrf_token": csrf(app, client), "next": CONNECT}
    )
    assert r.status_code == 303 and r.headers["location"] == "/carcommand" + CONNECT


@pytest.mark.parametrize("bad", ["https://evil.example/", "//evil.example/", "/drives"])
def test_a_way_back_that_leaves_the_connect_flow_is_ignored(world, bad):
    app, client = world
    r = turn_on(app, client, next=bad)
    assert r.status_code == 303 and r.headers["location"].endswith("/settings/access?saved=signin")


def test_the_connect_refusal_offers_to_turn_sign_in_on(world):
    app, client = world
    r = client.get(CONNECT)
    assert r.status_code == 403 and "Turn on sign-in" in r.text
    assert 'href="/settings/access?next=%2Fconnect%3Fclient%3DExample' in r.text and r.text.count("#signin") >= 1 and "btn primary" in r.text


# ---- surviving a restart


def test_the_flag_outranks_the_environment_at_start(world, monkeypatch):
    app, client = world
    monkeypatch.setitem(app.AUTH, "enabled", False)
    monkeypatch.setenv("CC_AUTH", "off")
    app.stored["enabled"] = "1"
    app.load_auth()
    assert app.AUTH["enabled"] is True, "turned on in the app, it stays on after a restart whatever .env says"
    app.stored["enabled"] = "0"
    app.load_auth()
    assert app.AUTH["enabled"] is False
    del app.stored["enabled"]
    monkeypatch.setitem(app.AUTH, "enabled", True)
    app.load_auth()
    assert app.AUTH["enabled"] is True, "no flag: whatever the environment decided stands"


def test_turned_on_here_then_a_restart_keeps_the_password_and_the_switch(world):
    app, client = world
    turn_on(app, client)
    app.AUTH.update(enabled=False, password=None)  # a fresh process before load_auth
    app.load_auth()
    assert app.AUTH["enabled"] is True and app.auth.verify_password(app.AUTH["password"], PASSWORD)


# ---- turning off


def test_turning_off_needs_the_current_password(world, monkeypatch):
    app, client = world
    turn_on(app, client)
    sign_in(app, client)
    app.told.clear()
    bad = client.post("/settings/signin/off", data={"password": "not it at all", "csrf_token": csrf(app, client)})
    assert bad.status_code == 401 and app.AUTH["enabled"] is True and app.stored["enabled"] == "1" and app.told == []
    good = client.post("/settings/signin/off", data={"password": PASSWORD, "csrf_token": csrf(app, client)})
    assert good.status_code == 303 and good.headers["location"].endswith("/settings/access?saved=signin-off")
    assert app.AUTH["enabled"] is False and app.stored["enabled"] == "0"
    assert [k for k, _ in app.told] == ["login"] and "off" in app.told[0][1].lower()


def test_wrong_passwords_to_turn_off_are_throttled(world):
    app, client = world
    turn_on(app, client)
    sign_in(app, client)
    answers = [client.post("/settings/signin/off", data={"password": f"wrong {i}", "csrf_token": csrf(app, client)}).status_code for i in range(8)]
    assert answers[0] == 401 and 429 in answers
    assert client.post("/settings/signin/off", data={"password": PASSWORD, "csrf_token": csrf(app, client)}).status_code == 429 and app.AUTH["enabled"] is True


def test_turning_off_works_when_sign_in_came_from_the_environment(world, monkeypatch):
    app, client = world
    monkeypatch.setitem(app.AUTH, "enabled", True)
    monkeypatch.setitem(app.AUTH, "password", app.auth.hash_password(PASSWORD, n=2**10))
    sign_in(app, client)
    assert "enabled" not in app.stored
    r = client.post("/settings/signin/off", data={"password": PASSWORD, "csrf_token": csrf(app, client)})
    assert r.status_code == 303 and app.AUTH["enabled"] is False and app.stored["enabled"] == "0", "by the flag, which a restart honours"


def test_turning_off_is_for_a_signed_in_browser_only(world):
    app, client = world
    turn_on(app, client)
    from fastapi.testclient import TestClient

    stranger = TestClient(app.app, follow_redirects=False)
    r = stranger.post("/settings/signin/off", data={"password": PASSWORD})
    assert r.status_code in (400, 401) and app.AUTH["enabled"] is True


# ---- the page


def render_access(**ctx):
    import jinja2

    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    base = dict(
        now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        root="/carcommand",
        q="",
        car={"vin": "V1", "name": "Alpha"},
        vehicles=[],
        colors={},
        active="settings-access",
        label="Sign-in and API",
        st={},
        conv={},
        tokens=[],
        section="access",
        csrf="tok",
    )
    return env.get_template("settings_access.html").render(**{**base, **ctx})


def test_with_sign_in_off_the_page_has_the_turn_on_form():
    html = render_access(auth_enabled=False, signin_next=CONNECT)
    assert 'action="/carcommand/settings/signin/on"' in html and 'id="signin"' in html
    assert 'autocomplete="username"' in html and html.count('autocomplete="new-password"') == 2 and 'minlength="8"' in html
    assert 'name="csrf_token" value="tok"' in html and f'name="next" value="{CONNECT}"' in html
    assert "Turn on sign-in" in html and "Remove CC_AUTH=off" not in html


def test_with_the_password_in_the_environment_the_form_is_only_the_switch():
    html = render_access(auth_enabled=False, password_in_env=True)
    assert "Turn on sign-in" in html and 'autocomplete="new-password"' not in html


def test_with_sign_in_on_the_page_offers_turning_it_off_behind_the_password():
    html = render_access(auth_enabled=True)
    assert 'action="/carcommand/settings/signin/off"' in html and "Turn off sign-in" in html and 'autocomplete="current-password"' in html
    assert "/settings/signin/on" not in html


# ---- review fixes


def test_turning_on_rotates_the_signing_key_so_old_cookies_stay_dead(world):
    import auth

    app, client = world
    old_secret = app.AUTH["secret"]
    old_cookie = auth.new_session(old_secret)  # a session from before an earlier turn-off
    r = turn_on(app, client)
    assert r.status_code == 303 and app.AUTH["secret"] != old_secret and app.stored["secret"] == app.AUTH["secret"]
    assert not auth.valid_session(app.AUTH["secret"], old_cookie)
    assert auth.valid_session(app.AUTH["secret"], r.cookies.get(auth.SESSION_COOKIE)), "while this browser is signed in under the new one"


def test_a_signing_key_set_in_the_environment_is_not_rotated_and_the_page_says_so(world, monkeypatch):
    app, client = world
    monkeypatch.setenv("CC_SECRET", "pinned-by-the-owner")
    monkeypatch.setitem(app.AUTH, "secret", "pinned-by-the-owner")
    turn_on(app, client)
    assert "secret" not in app.stored
    html = render_access(auth_enabled=True, saved=True, said="signin", secret_in_env=True)
    assert "older sessions stay valid" in html and "CC_SECRET" in html
    assert "older sessions" not in render_access(auth_enabled=True, saved=True, said="signin", secret_in_env=False)


def test_from_anywhere_but_the_machine_itself_turning_on_needs_the_setup_code(world):
    import auth
    from fastapi.testclient import TestClient

    app, _ = world
    far = TestClient(app.server, follow_redirects=False, client=("93.184.216.34", 50000))
    bridge = TestClient(app.server, follow_redirects=False, client=("172.20.0.9", 50000))
    r = bridge.post("/settings/signin/on", data={"password": PASSWORD, "confirm": PASSWORD, "csrf_token": csrf(app, bridge)})
    assert r.status_code == 403 and app.stored == {}, "a Docker bridge address is not the home network"
    code = auth.setup_code(app.AUTH["secret"])
    r = far.post("/settings/signin/on", data={"password": PASSWORD, "confirm": PASSWORD, "csrf_token": csrf(app, far)})
    assert r.status_code == 403 and app.stored == {} and app.AUTH["enabled"] is False, "no code, no sign-in"
    r = far.post("/settings/signin/on", data={"password": PASSWORD, "confirm": PASSWORD, "code": "AAAA-BBBB-CCCC", "csrf_token": csrf(app, far)})
    assert r.status_code == 403 and app.stored == {}
    r = far.post("/settings/signin/on", data={"password": PASSWORD, "confirm": PASSWORD, "code": code.lower().replace("-", " "), "csrf_token": csrf(app, far)})
    assert r.status_code == 303 and app.AUTH["enabled"] is True, "the code, however it is typed"


def test_wrong_setup_codes_are_throttled(world):
    from fastapi.testclient import TestClient

    app, _ = world
    far = TestClient(app.server, follow_redirects=False, client=("93.184.216.34", 50000))
    answers = [far.post("/settings/signin/on", data={"password": PASSWORD, "confirm": PASSWORD, "code": f"WRONG{i}", "csrf_token": csrf(app, far)}).status_code for i in range(8)]
    assert answers[0] == 403 and 429 in answers


def test_the_page_asks_for_the_code_only_away_from_home():
    assert 'name="code"' in render_access(auth_enabled=False, signin_need_code=True)
    assert 'name="code"' not in render_access(auth_enabled=False, signin_need_code=False)
    assert 'name="code"' in render_access(auth_enabled=False, signin_need_code=True, password_in_env=True)


def test_the_alert_names_the_address_the_throttle_trusts(world):
    app, client = world
    r = client.post(
        "/settings/signin/on",
        data={"password": PASSWORD, "confirm": PASSWORD, "code": app.auth.setup_code(app.AUTH["secret"]), "csrf_token": csrf(app, client)},
        headers={"X-Forwarded-For": "1.2.3.4, 198.51.100.7"},
    )
    assert r.status_code == 303
    ((_, text),) = app.told
    assert "198.51.100.7" in text and "1.2.3.4" not in text, "not the leftmost address, which anyone can write"


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


def test_a_guess_is_counted_before_it_is_checked_and_the_check_runs_off_the_loop(world, monkeypatch):
    import asyncio

    app, client = world
    turn_on(app, client)
    sign_in(app, client)
    log = []
    monkeypatch.setattr(app, "FAILURES", Spy(app.FAILURES, log))
    real = app.auth.verify_password

    def verify(stored, given):
        log.append("verify-on-loop" if asyncio._get_running_loop() else "verify")
        return real(stored, given)

    monkeypatch.setattr(app.auth, "verify_password", verify)
    client.post("/settings/signin/off", data={"password": "wrong wrong wrong", "csrf_token": csrf(app, client)})
    assert log == ["wait", "record", "verify"], "wait and count with nothing awaited between, so parallel guesses cannot all slip past"
    log.clear()
    client.post("/settings/signin/off", data={"password": PASSWORD, "csrf_token": csrf(app, client)})
    assert log == ["wait", "record", "verify", "clear"]


def test_changing_the_password_is_counted_before_it_is_checked_too(world, monkeypatch):
    import asyncio

    app, client = world
    turn_on(app, client)
    sign_in(app, client)
    log = []
    monkeypatch.setattr(app, "FAILURES", Spy(app.FAILURES, log))
    real = app.auth.verify_password
    monkeypatch.setattr(app.auth, "verify_password", lambda s, g: (log.append("verify-on-loop" if asyncio._get_running_loop() else "verify"), real(s, g))[1])
    client.post("/settings/password", data={"current": "wrong wrong wrong", "password": "another long one", "confirm": "another long one", "csrf_token": csrf(app, client)})
    assert log == ["wait", "record", "verify"]


def test_load_auth_changes_every_setting_in_one_step(world, monkeypatch):
    app, _ = world

    class Spy(dict):
        sets, updates = [], []

        def __setitem__(self, k, v):
            Spy.sets.append(k)
            super().__setitem__(k, v)

        def update(self, *a, **kw):
            Spy.updates.append(set(dict(*a, **kw)))
            super().update(*a, **kw)

    app.stored["enabled"] = "1"
    app.stored["password"] = app.auth.hash_password(PASSWORD, n=2**10)
    monkeypatch.setattr(app, "AUTH", Spy(app.AUTH))
    Spy.sets, Spy.updates = [], []
    app.load_auth()
    assert Spy.sets == [] and len(Spy.updates) == 1 and {"enabled", "password", "secret"} <= Spy.updates[0], "never enabled without its password"


def test_there_is_a_way_back_in_when_locked_out():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    make = (root / "Makefile").read_text()
    assert "signin-reset:" in make and "signin_reset.py" in make and "restart web" in make
    import signin_reset

    assert set(signin_reset.KEYS) == {"enabled", "cf_team", "cf_aud"}, "the password is not among them"
    assert "signin_reset.py" in (Path(__file__).parent / "Dockerfile").read_text()
    assert "signin-reset" in (root / "docs" / "install.md").read_text()


def test_the_setup_code_can_be_printed_for_make_setup_code(monkeypatch):
    import auth
    import setup_code
    from pathlib import Path

    secret = auth.new_secret()

    class Conn:
        pass

    monkeypatch.delenv("CC_SECRET", raising=False)
    monkeypatch.setattr(auth, "store_get", lambda conn, key: secret if key == "secret" else None)
    assert setup_code.code(Conn()) == auth.setup_code(secret)
    monkeypatch.setenv("CC_SECRET", "pinned")
    assert setup_code.code(Conn()) == auth.setup_code("pinned")
    monkeypatch.delenv("CC_SECRET")
    monkeypatch.setattr(auth, "store_get", lambda conn, key: None)
    assert setup_code.code(Conn()) is None
    root = Path(__file__).resolve().parents[2]
    assert "setup-code:" in (root / "Makefile").read_text() and "setup_code.py" in (Path(__file__).parent / "Dockerfile").read_text()
    assert "make setup-code" in (root / "docs" / "install.md").read_text()


def test_turning_on_from_settings_always_asks_for_the_setup_code(world, monkeypatch):
    """Only /setup, a fresh site's own page, has the first-hour exemption. This switch also reaches a site that has
    run for a while, and a header-less proxy on this machine (nginx, socat, ssh -R, Tailscale Funnel) is loopback."""
    from fastapi.testclient import TestClient

    app, _ = world
    monkeypatch.setattr(app.auth, "_own_networks", ())
    code = app.auth.setup_code(app.AUTH["secret"])
    for peer in ("127.0.0.1", "::1", "192.168.1.40", "10.0.0.3"):
        c = TestClient(app.server, follow_redirects=False, client=(peer, 50000))
        no_code = c.post("/settings/signin/on", data={"password": PASSWORD, "confirm": PASSWORD, "csrf_token": csrf(app, c)})
        assert no_code.status_code == 403 and app.AUTH["enabled"] is False, peer
    me = TestClient(app.server, follow_redirects=False, client=("127.0.0.1", 50000))
    proxied = me.post("/settings/signin/on", data={"password": PASSWORD, "confirm": PASSWORD, "code": code, "csrf_token": csrf(app, me)}, headers={"Via": "1.1 cloudflared"})
    assert proxied.status_code == 303, "with the code it is the owner, whatever is in front"
    assert app.AUTH["enabled"] is True


def test_the_settings_form_always_shows_the_code_field():
    assert 'name="code"' in render_access(auth_enabled=False, signin_need_code=True)


def test_the_setup_code_is_printed_only_while_no_password_is_set(monkeypatch):
    import auth
    import setup_code

    secret = auth.new_secret()
    stored = {"secret": secret}
    monkeypatch.delenv("CC_SECRET", raising=False)
    monkeypatch.delenv("CC_PASSWORD", raising=False)
    monkeypatch.setattr(auth, "store_get", lambda conn, key: stored.get(key))
    assert setup_code.code(object()) == auth.setup_code(secret)
    stored["password"] = "scrypt$x"
    assert setup_code.code(object()) is None, "a password is set: the code no longer matters"
    stored.pop("password")
    monkeypatch.setenv("CC_PASSWORD", "from the environment")
    assert setup_code.code(object()) is None
    assert "turning sign-in on" in setup_code.__doc__ and "Cloudflare" in setup_code.__doc__
