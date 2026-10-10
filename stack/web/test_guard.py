"""The gate as the app actually serves it: through the middleware, through all three mounts.

test_auth.py proves the rule; this proves the wiring — that the path the rule sees has the
mount prefix stripped, that the prefix comes back on every redirect, and that one cookie
works whether you arrive on the LAN port or through the tunnel.
"""

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def app_module():
    import os

    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    os.environ.setdefault("REST_USER", "u")
    os.environ.setdefault("REST_PASSWORD", "p")
    sys.path.insert(0, str(Path(__file__).parent))
    return importlib.import_module("app")


@pytest.fixture
def client(app_module):
    # No `with`: the lifespan would want the database. The middleware runs either way.
    from fastapi.testclient import TestClient

    import auth

    app_module.AUTH.update(enabled=True, secret=auth.new_secret(), password=auth.hash_password("a good long password", n=2**10))
    return TestClient(app_module.app, follow_redirects=False)


def signed_in(app_module, client):
    import auth

    cookie = auth.new_session(app_module.AUTH["secret"])
    client.cookies.set(auth.SESSION_COOKIE, cookie)
    return cookie


@pytest.mark.parametrize("mount", ["", "/cc", "/carcommand"])
def test_a_stranger_is_redirected_to_the_login_of_the_mount_they_arrived_on(client, mount):
    r = client.get(f"{mount}/drives?vin=V1")
    assert r.status_code == 303
    assert r.headers["location"] == f"{mount}/login?next=%2Fdrives%3Fvin%3DV1", "the prefix must come back, and the destination must survive"


@pytest.mark.parametrize("mount", ["", "/cc", "/carcommand"])
def test_the_healthcheck_answers_without_a_session_on_every_mount(client, mount):
    r = client.get(f"{mount}/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_the_login_page_renders_for_a_stranger(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "csrf_token" in r.text and "Sign in" in r.text


def test_the_right_password_sets_a_session_cookie_and_goes_where_you_were_headed(client):
    import auth

    page = client.get("/login?next=%2Fstats")
    token = _token_from(page.text)
    r = client.post("/login", data={"password": "a good long password", "next": "/stats", "csrf_token": token})
    assert r.status_code == 303 and r.headers["location"] == "/stats"
    assert auth.SESSION_COOKIE in r.cookies


def test_the_wrong_password_gets_no_cookie_and_says_nothing_useful(client):
    import auth

    token = _token_from(client.get("/login").text)
    r = client.post("/login", data={"password": "not it", "csrf_token": token})
    assert r.status_code == 401
    assert auth.SESSION_COOKIE not in r.cookies
    assert "not right" in r.text and "no password" not in r.text.lower()


HOSTILE_NEXT = [
    "//evil.example/",
    "https://evil.example/",
    "javascript:alert(1)",
    "/\\evil.example",  # a browser reads the backslash as a slash, so this is protocol-relative
    "\\/evil.example",
    "/\\\\evil.example/path",
    "/ok\r\nSet-Cookie: stolen=1",  # a newline in a Location header is a second header
    "/ok\x00",
    "http:/evil.example",
]


@pytest.mark.parametrize("hostile", HOSTILE_NEXT)
def test_a_hostile_next_never_leaves_the_site(app_module, hostile):
    assert app_module.safe_next(hostile) == "/", hostile


@pytest.mark.parametrize("ordinary", ["/", "/stats", "/drives?vin=V1", "/day?date=2026-09-05&vin=all"])
def test_an_ordinary_destination_survives(app_module, ordinary):
    assert app_module.safe_next(ordinary) == ordinary


def test_next_cannot_be_walked_off_the_site(client):
    for hostile in HOSTILE_NEXT:
        # A fresh door each time: signing in rotates the session, which retires the old token.
        client.cookies.clear()
        token = _token_from(client.get("/login").text)
        r = client.post("/login", data={"password": "a good long password", "next": hostile, "csrf_token": token})
        assert r.headers["location"] == "/", hostile


def test_a_write_without_a_token_is_refused_even_with_a_session(app_module, client):
    signed_in(app_module, client)
    r = client.post("/places", data={"name": "Work", "latitude": 1.0, "longitude": 2.0})
    assert r.status_code == 400 and "stale" in r.text


def test_a_token_minted_for_another_session_does_not_travel(app_module, client):
    import auth

    signed_in(app_module, client)
    someone_else = auth.csrf_token(app_module.AUTH["secret"], auth.new_session(app_module.AUTH["secret"]))
    r = client.post("/places", data={"name": "Work", "latitude": 1.0, "longitude": 2.0, "csrf_token": someone_else})
    assert r.status_code == 400


def test_signing_out_clears_the_cookie(app_module, client):
    import auth

    cookie = signed_in(app_module, client)
    r = client.post("/logout", data={"csrf_token": auth.csrf_token(app_module.AUTH["secret"], cookie)})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert r.cookies.get(auth.SESSION_COOKIE) in (None, "")


def test_an_unconfigured_install_serves_nothing_but_setup(app_module, client):
    app_module.AUTH["password"] = None
    for path in ("/", "/api/drives", "/health", "/cc/stats"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"].endswith("/setup"), path
    assert client.get("/setup").status_code == 200


def test_switching_sign_in_off_opens_the_site_but_still_wants_tokens(app_module, client):
    app_module.AUTH["enabled"] = False
    assert client.get("/health").status_code == 200
    assert client.get("/login").status_code == 303, "nothing to sign into"
    r = client.post("/places", data={"name": "Work", "latitude": 1.0, "longitude": 2.0})
    assert r.status_code == 400


def _token_from(html: str) -> str:
    import re

    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "every form must carry a token"
    return m.group(1)


def test_guessing_gets_throttled_and_the_page_says_so(app_module, client):
    app_module.FAILURES.clear("testclient")
    for _ in range(6):
        token = _token_from(client.get("/login").text)
        client.post("/login", data={"password": "wrong", "csrf_token": token})
    token = _token_from(client.get("/login").text)
    r = client.post("/login", data={"password": "a good long password", "csrf_token": token})
    assert r.status_code == 429 and "Too many tries" in r.text, "even the right password waits its turn"
    app_module.FAILURES.clear("testclient")
    app_module.FAILURES.total = 0


@pytest.mark.parametrize("bare,expected", [("/cc", "/cc/"), ("/carcommand", "/carcommand/")])
def test_the_short_url_without_a_trailing_slash_reaches_the_site(client, bare, expected):
    """Typing the prefix without a trailing slash used to answer {"detail":"Not Found"}.

    Starlette's Mount only matches "/cc/...", and the catch-all mount at "/" then takes
    "/cc" and 404s from inside the site, so the router never gets to redirect. It is the
    URL a person actually types, and it was the one that did not work.
    """
    r = client.get(bare)
    assert r.status_code in (307, 308), f"{bare} answered {r.status_code}"
    assert r.headers["location"] == expected


def test_a_token_opens_the_api_and_nothing_else(app_module, client):
    """With sign-in on, /api/ paths take a bearer token in place of the cookie. A token
    opens nothing but those paths, and one scoped to a car answers for that car only."""
    import auth

    raw = auth.mint_token()
    app_module.TOKENS.clear()
    app_module.TOKENS[auth.token_hash(raw)] = {"label": "script", "vin": None}
    assert client.get("/api/whoami").status_code == 303, "no token, no cookie: sent to sign in"
    r = client.get("/api/whoami", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200 and r.json() == {"label": "script", "vin": None, "kind": "read"}
    assert client.get("/api/whoami?token=" + raw).status_code == 303, "never from the query string: that is an access-log line"
    assert client.get("/settings", headers={"Authorization": f"Bearer {raw}"}).status_code == 303, "a token is not a session"
    assert client.get("/api/whoami", headers={"Authorization": "Bearer nonsense"}).status_code == 401, "an unknown token is told so (reconnect), not sent to a login page"

    scoped = auth.mint_token()
    app_module.TOKENS[auth.token_hash(scoped)] = {"label": "one car", "vin": "V1"}
    assert client.get("/api/whoami?vin=V1", headers={"Authorization": f"Bearer {scoped}"}).status_code == 200
    assert client.get("/api/whoami?vin=V2", headers={"Authorization": f"Bearer {scoped}"}).status_code == 403
    assert client.get("/api/whoami", headers={"Authorization": f"Bearer {scoped}"}).status_code == 403, "no vin means the whole garage, which a scoped token must not see"
    app_module.TOKENS.clear()


def _store(app_module, monkeypatch):
    stored = {}
    monkeypatch.setattr(app_module.auth, "store_set", lambda conn, k, v: stored.__setitem__(k, v))
    monkeypatch.setattr(app_module, "db", lambda: _Null())
    monkeypatch.setattr(app_module, "load_auth", lambda: app_module.AUTH.update(password=stored.get("password", app_module.AUTH["password"]), secret=stored.get("secret", app_module.AUTH["secret"])))
    monkeypatch.setattr(app_module, "settings_render", lambda request, section, **extra: app_module.HTMLResponse(extra.get("pw_error", "")))
    monkeypatch.setattr(app_module.recorder, "notify", lambda *a, **k: None)
    return stored


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _post_password(app_module, client, current, new, confirm):
    import auth

    cookie = signed_in(app_module, client)
    return client.post("/settings/password", data={"current": current, "password": new, "confirm": confirm, "csrf_token": auth.csrf_token(app_module.AUTH["secret"], cookie)})


def test_changing_the_password_keeps_this_device_and_signs_out_the_rest(app_module, client, monkeypatch):
    import auth

    monkeypatch.delenv("CC_SECRET", raising=False)
    stored = _store(app_module, monkeypatch)
    old_secret = app_module.AUTH["secret"]
    other = auth.new_session(old_secret)
    r = _post_password(app_module, client, "a good long password", "another good long one", "another good long one")
    assert r.status_code == 303 and r.headers["location"] == "/settings/access?saved=password"
    assert auth.verify_password(stored["password"], "another good long one") and stored["secret"] != old_secret
    assert not auth.valid_session(app_module.AUTH["secret"], other), "another device's session no longer opens anything"
    assert auth.SESSION_COOKIE in r.headers.get("set-cookie", ""), "this device gets a fresh session"


def test_wrong_current_password_is_refused_and_counted(app_module, client, monkeypatch):
    stored = _store(app_module, monkeypatch)
    app_module.FAILURES.clear("testclient")
    codes = [_post_password(app_module, client, "not it", "another good long one", "another good long one").status_code for _ in range(6)]
    assert codes[0] == 401 and 429 in codes, "wrong guesses lock out the way sign-in does"
    assert "password" not in stored
    app_module.FAILURES.clear("testclient")


def test_a_weak_new_password_is_refused(app_module, client, monkeypatch):
    stored = _store(app_module, monkeypatch)
    r = _post_password(app_module, client, "a good long password", "short", "short")
    assert r.status_code == 400 and "password" not in stored


def test_a_password_set_in_the_environment_is_not_changed_from_the_page(app_module, client, monkeypatch):
    """CC_PASSWORD wins at every start, so a change from the page would say done and change nothing."""
    monkeypatch.setenv("CC_PASSWORD", "a good long password")
    stored = _store(app_module, monkeypatch)
    old_secret = app_module.AUTH["secret"]
    r = _post_password(app_module, client, "a good long password", "another good long one", "another good long one")
    assert r.status_code == 409 and stored == {} and app_module.AUTH["secret"] == old_secret
    import jinja2

    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    from datetime import datetime, timezone

    html = env.get_template("settings_access.html").render(
        now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        root="",
        q="",
        car={"vin": "V1", "name": "Alpha"},
        vehicles=[],
        colors={},
        active="settings-access",
        label="Sign-in and API",
        st={},
        conv={},
        tokens=[],
        auth_enabled=True,
        password_in_env=True,
    )
    assert "CC_PASSWORD" in html and 'name="current"' not in html, "the page says where the password is set instead of offering a form that cannot work"


@pytest.mark.parametrize("mount", ["", "/carcommand"])
def test_the_tesla_key_path_is_open_to_a_stranger(app_module, client, monkeypatch, mount):
    """Tesla fetches the app's public key with no session; a login page there fails pairing."""
    monkeypatch.setattr(app_module, "state_get", lambda key: None)
    r = client.get(f"{mount}/.well-known/appspecific/com.tesla.3p.public-key.pem")
    assert r.status_code == 404, "reached the route (no key yet), not the login redirect"


def test_tesla_key_status_needs_a_session(client):
    r = client.get("/settings/sources/tesla/keys.json")
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_an_api_token_does_not_open_the_live_stream(app_module, client, monkeypatch):
    """The stream is a page's, outside /api/: a script's token is not a session."""
    import auth

    raw = "tok-springfield"
    monkeypatch.setitem(app_module.TOKENS, auth.token_hash(raw), {"vin": None, "label": "script"})
    r = client.get("/stream", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_the_csrf_token_is_not_taken_from_the_query_string(app_module, client):
    """A token in a URL lands in logs and history. Form field or header only, except the
    photo upload, whose body is too large for the guard to read for one."""
    import auth

    cookie = signed_in(app_module, client)
    token = auth.csrf_token(app_module.AUTH["secret"], cookie)
    r = client.post(f"/places?csrf_token={token}", data={"name": "Work", "latitude": 1.0, "longitude": 2.0})
    assert r.status_code == 400 and "stale" in r.text
    assert client.post("/logout", headers={"X-CSRF-Token": token}).status_code == 303, "the header still works"


def test_the_photo_upload_still_carries_its_token_in_the_url(app_module, client, monkeypatch):
    import auth
    from fastapi.testclient import TestClient

    def no_database():
        raise RuntimeError("no database here")

    monkeypatch.setattr(app_module, "db", no_database)
    cookie = signed_in(app_module, client)
    token = auth.csrf_token(app_module.AUTH["secret"], cookie)
    lenient = TestClient(app_module.app, follow_redirects=False, raise_server_exceptions=False)
    lenient.cookies.set(auth.SESSION_COOKIE, cookie)
    r = lenient.post(f"/service/7/files?csrf_token={token}", files={"files": ("a.jpg", b"x" * (2 << 20), "image/jpeg")})
    assert r.status_code != 400, "past the guard (the route then wants the database)"
    r = lenient.post("/service/7/files", files={"files": ("a.jpg", b"x" * (2 << 20), "image/jpeg")})
    assert r.status_code == 400


@pytest.mark.parametrize(
    "back",
    ["//evil.example/", "/\\evil.example", "https://evil.example/", "/places\r\nX: 1"],
)
def test_a_form_back_never_leaves_the_site(app_module, client, monkeypatch, back):
    import auth

    monkeypatch.setattr(app_module, "add_place", lambda *a, **k: None)
    cookie = signed_in(app_module, client)
    r = client.post("/places", data={"name": "Work", "latitude": 1.0, "longitude": 2.0, "back": back, "csrf_token": auth.csrf_token(app_module.AUTH["secret"], cookie)})
    assert r.status_code == 303 and r.headers["location"] == "/places", back


def test_a_redirect_keeps_the_mount_and_only_a_known_one(app_module):
    from starlette.requests import Request

    for root, expected in (("/cc", "/cc/drives?vin=V1"), ("/carcommand", "/carcommand/drives?vin=V1"), ("", "/drives?vin=V1"), ("//evil.example", "/drives?vin=V1")):
        req = Request({"type": "http", "method": "GET", "path": "/", "root_path": root, "headers": [], "query_string": b""})
        assert app_module.to_site(req, "/drives?vin=V1").headers["location"] == expected, root
