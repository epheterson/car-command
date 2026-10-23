"""Admin tokens through the middleware: who may reach /api/admin/, from where, and what is
written down about it. test_auth.py proves the rules; this proves the wiring."""

import importlib
import os
import sys
from pathlib import Path

import pytest

LAN = ("192.168.1.40", 50000)
TAILNET = ("100.101.102.103", 50000)
PUBLIC = ("203.0.113.9", 50000)
ADMIN, READ = "admin-token-springfield-alpha", "read-token-springfield-bravo"


@pytest.fixture
def app_module(monkeypatch):
    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    os.environ.setdefault("REST_USER", "u")
    os.environ.setdefault("REST_PASSWORD", "p")
    sys.path.insert(0, str(Path(__file__).parent))
    app = importlib.import_module("app")
    import auth

    monkeypatch.setitem(app.AUTH, "enabled", True)
    monkeypatch.setitem(app.AUTH, "secret", auth.new_secret())
    monkeypatch.setitem(app.AUTH, "password", auth.hash_password("a good long password", n=2**10))
    monkeypatch.setitem(app.AUTH, "admin_anywhere", False)
    monkeypatch.setattr(app, "TOKENS", {})
    app.TOKENS[auth.token_hash(ADMIN)] = {"label": "Home Assistant", "vin": None, "kind": "admin", "last4": ADMIN[-4:]}
    app.TOKENS[auth.token_hash(READ)] = {"label": "dashboard", "vin": None, "kind": "read", "last4": READ[-4:]}
    calls = []
    app.real_audit = app.audit
    monkeypatch.setattr(app, "audit", lambda token, method, path, status, ip, held=0: calls.append((token["label"], method, path, status, ip) + ((held,) if held else ())))
    app.audited = calls
    yield app
    del app.audited, app.real_audit


def client_from(app_module, where=LAN):
    from fastapi.testclient import TestClient

    return TestClient(app_module.server, follow_redirects=False, client=where)  # the server: what uvicorn runs


def bearer(raw):
    return {"Authorization": f"Bearer {raw}"}


# A path the middleware lets through to the router, which has nothing there: 404 means
# "the gate opened", 401/403 mean it did not.
NOWHERE = "/api/admin/nothing-here"


def test_no_token_and_no_session_is_401_in_json(app_module):
    r = client_from(app_module).get(NOWHERE)
    assert r.status_code == 401 and "admin token" in r.json()["error"]
    assert app_module.audited == [], "nobody to name: an anonymous knock is not written down"


def test_a_signed_in_session_does_not_open_the_admin_api(app_module):
    """A cookie never reaches these routes, so sparing them CSRF spares no cookie request."""
    import auth

    c = client_from(app_module)
    cookie = auth.new_session(app_module.AUTH["secret"])
    c.cookies.set(auth.SESSION_COOKIE, cookie)
    assert c.get(NOWHERE).status_code == 401
    csrf = auth.csrf_token(app_module.AUTH["secret"], cookie)
    assert c.post("/api/admin/money/averages/refresh", headers={"x-csrf-token": csrf}).status_code == 401, "not even with a good CSRF token"


def test_a_read_token_is_refused_and_the_attempt_logged(app_module):
    r = client_from(app_module).get(NOWHERE, headers=bearer(READ))
    assert r.status_code == 403 and "admin token" in r.json()["error"]
    assert app_module.audited == [("dashboard", "GET", NOWHERE, 403, "192.168.1.40")]


@pytest.mark.parametrize("where", [LAN, TAILNET, ("127.0.0.1", 1)])
def test_an_admin_token_opens_it_from_home(app_module, where):
    r = client_from(app_module, where).get(NOWHERE, headers=bearer(ADMIN))
    assert r.status_code == 404, r.text
    assert app_module.audited == [("Home Assistant", "GET", NOWHERE, 404, where[0])]


@pytest.mark.parametrize("mount", ["/cc", "/carcommand"])
def test_the_rule_holds_on_every_mount(app_module, mount):
    assert client_from(app_module, PUBLIC).get(mount + NOWHERE, headers=bearer(ADMIN)).status_code == 403
    r = client_from(app_module).get(mount + NOWHERE, headers=bearer(ADMIN))
    assert r.status_code == 404 and "No admin call" in r.json()["error"], "a JSON error under a mount too, not the HTML page"


def test_an_admin_token_from_a_public_address_is_refused(app_module):
    r = client_from(app_module, PUBLIC).get(NOWHERE, headers=bearer(ADMIN))
    assert r.status_code == 403 and "local network" in r.json()["error"]
    assert app_module.audited[0][3] == 403 and app_module.audited[0][4] == "203.0.113.9"


@pytest.mark.parametrize("header", [{"CF-Connecting-IP": "192.168.1.40"}, {"Cf-Access-Jwt-Assertion": "eyJ"}])
def test_an_admin_token_through_cloudflare_is_refused_from_a_lan_socket(app_module, header):
    r = client_from(app_module).get(NOWHERE, headers={**bearer(ADMIN), **header})
    assert r.status_code == 403 and "Cloudflare" in r.json()["error"]


def test_the_owner_can_allow_admin_tokens_from_anywhere(app_module, monkeypatch):
    monkeypatch.setitem(app_module.AUTH, "admin_anywhere", True)
    r = client_from(app_module, PUBLIC).get(NOWHERE, headers={**bearer(ADMIN), "CF-Connecting-IP": "203.0.113.9"})
    assert r.status_code == 404
    assert client_from(app_module, PUBLIC).get(NOWHERE, headers=bearer(READ)).status_code == 403, "the override is for admin tokens; a read token still cannot"


def test_a_read_token_still_reads_from_anywhere(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "garage", lambda: {})
    r = client_from(app_module, PUBLIC).get("/api/whoami", headers={**bearer(READ), "CF-Connecting-IP": "203.0.113.9"})
    assert r.status_code == 200 and r.json()["label"] == "dashboard"


def test_csrf_still_holds_for_cookie_posts(app_module):
    import auth

    c = client_from(app_module)
    cookie = auth.new_session(app_module.AUTH["secret"])
    c.cookies.set(auth.SESSION_COOKIE, cookie)
    r = c.post("/settings/tokens", data={"label": "x", "kind": "admin"})
    assert r.status_code == 400 and "stale" in r.text
    r = c.post("/settings/tokens/anywhere", data={"anywhere": "on"})
    assert r.status_code == 400 and "stale" in r.text
    assert app_module.AUTH["admin_anywhere"] is False


def test_the_owner_ticks_anywhere_on_and_off_and_it_is_kept_beside_the_password(app_module, monkeypatch):
    import auth

    stored = {}
    monkeypatch.setattr(app_module.auth, "store_set", lambda conn, k, v: stored.__setitem__(k, v))
    monkeypatch.setattr(app_module, "db", lambda: _TokenConn([]))
    c = client_from(app_module)
    cookie = auth.new_session(app_module.AUTH["secret"])
    c.cookies.set(auth.SESSION_COOKIE, cookie)
    csrf = auth.csrf_token(app_module.AUTH["secret"], cookie)
    r = c.post("/settings/tokens/anywhere", data={"anywhere": "on", "csrf_token": csrf})
    assert r.status_code == 303 and stored == {"admin_anywhere": "1"} and app_module.AUTH["admin_anywhere"] is True
    c.post("/settings/tokens/anywhere", data={"csrf_token": csrf})
    assert stored == {"admin_anywhere": "0"} and app_module.AUTH["admin_anywhere"] is False, "an unticked box is off"
    assert client_from(app_module, PUBLIC).post("/settings/tokens/anywhere", data={"anywhere": "on", "csrf_token": csrf}, headers=bearer(ADMIN)).status_code in (303, 401), "a token is not a session"
    assert app_module.AUTH["admin_anywhere"] is False


def test_a_bearer_write_outside_admin_still_needs_csrf(app_module):
    """Only /api/admin/ skips the CSRF check; a token's POST anywhere else is refused as before."""
    r = client_from(app_module).post("/api/nothing-here", headers=bearer(ADMIN))
    assert r.status_code == 400 and "control token" in r.json()["error"]


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _TokenConn:
    def __init__(self, rows):
        self.rows, self.sent = rows, []

    def execute(self, sql, params=()):
        self.sent.append((sql, params))
        return _Rows(self.rows)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_reload_reads_the_kind_and_an_admin_token_is_never_one_cars(app_module, monkeypatch):
    rows = [
        {"hash": "h1", "label": "old", "vin": "VIN-ALPHA", "kind": "read", "last4": None},
        {"hash": "h2", "label": "boss", "vin": "VIN-ALPHA", "kind": "admin", "last4": "zz99"},
        {"hash": "h3", "label": "odd", "vin": None, "kind": "superuser", "last4": None},
    ]
    monkeypatch.setattr(app_module, "db", lambda: _TokenConn(rows))
    app_module.reload_tokens()
    assert app_module.TOKENS["h1"] == {"label": "old", "vin": "VIN-ALPHA", "kind": "read", "last4": None, "share_place": False, "coords": False}
    assert app_module.TOKENS["h2"]["kind"] == "admin" and app_module.TOKENS["h2"]["vin"] is None
    assert app_module.TOKENS["h3"]["kind"] == "read", "a kind this code does not know reads"


def test_minting_an_admin_token_stores_its_kind_and_last_four_never_the_token(app_module, monkeypatch):
    import auth

    conn = _TokenConn([])
    monkeypatch.setattr(app_module, "db", lambda: conn)
    monkeypatch.setattr(app_module, "all_vehicles", lambda: [{"vin": "VIN-ALPHA", "name": "Alpha"}])
    monkeypatch.setattr(app_module, "settings_render", lambda request, section, **kw: app_module.JSONResponse({"minted": kw["minted"], "kind": kw["minted_kind"]}))
    c = client_from(app_module)
    cookie = auth.new_session(app_module.AUTH["secret"])
    c.cookies.set(auth.SESSION_COOKIE, cookie)
    r = c.post("/settings/tokens", data={"label": "boss", "kind": "admin", "vin": "VIN-ALPHA", "csrf_token": auth.csrf_token(app_module.AUTH["secret"], cookie)})
    raw = r.json()["minted"]
    insert = next(p for s, p in conn.sent if s.startswith("INSERT INTO cc_api_tokens"))
    assert insert == (auth.token_hash(raw), "boss", None, "admin", raw[-4:]), "admin: every car, kind kept, only the last four"
    assert raw not in str(conn.sent).replace(raw[-4:], "")


def test_the_audit_writes_one_row_per_call(app_module, monkeypatch):
    """The real audit, against a connection that records; pruning is a day apart."""
    conn = _TokenConn([])
    monkeypatch.setattr(app_module, "db", lambda: conn)
    monkeypatch.setitem(app_module.API_LOG_PRUNED, "at", 0.0)
    app_module.real_audit({"label": "Home Assistant", "last4": "pha"}, "POST", "/api/admin/tesla/register", 200, "192.168.1.40")
    app_module.real_audit({"label": "Home Assistant", "last4": "pha"}, "GET", "/api/admin/tesla", 200, "192.168.1.40")
    inserts = [p for s, p in conn.sent if s.startswith("INSERT INTO cc_api_log")]
    assert inserts == [("Home Assistant", "pha", "POST", "/api/admin/tesla/register", 200, "192.168.1.40"), ("Home Assistant", "pha", "GET", "/api/admin/tesla", 200, "192.168.1.40")]
    assert sum(1 for s, _ in conn.sent if s.startswith("DELETE FROM cc_api_log")) == 1, "pruned once, not on every call"


def test_an_audit_that_cannot_be_written_does_not_fail_the_call(app_module, monkeypatch):
    def down():
        raise OSError("database is down")

    monkeypatch.setattr(app_module, "db", down)
    app_module.real_audit({"label": "x"}, "GET", "/api/admin/tesla", 200, "192.168.1.40")


# ---- the true socket address, at the ASGI level ------------------------------------


def asgi_get(app, path, client, headers=()):
    """One GET straight through an ASGI app with `client` as the socket's address, as uvicorn
    hands it over: no test client in between to fill anything in. (status, body)."""
    import asyncio
    import json

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
        "client": client,
        "server": ("testserver", 80),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(body or b"null")


def test_a_public_socket_forging_a_private_forwarded_address_is_refused(app_module):
    """uvicorn's proxy headers would make the forged address the client; the rule reads the socket."""
    status, body = asgi_get(app_module.server, NOWHERE, PUBLIC, [("Authorization", f"Bearer {ADMIN}"), ("X-Forwarded-For", "192.168.1.5")])
    assert status == 403 and "local network" in body["error"]
    assert app_module.audited[-1][4] == "203.0.113.9", "the log names the socket, not the address it forged"


def test_the_log_names_the_proxy_a_call_came_through(app_module):
    status, _ = asgi_get(app_module.server, NOWHERE, LAN, [("Authorization", f"Bearer {ADMIN}"), ("X-Forwarded-For", "10.0.0.2")])
    assert app_module.audited[-1][4] == "10.0.0.2 via 192.168.1.40", "what the hop at home reported, and the hop itself"


def test_a_private_socket_with_no_cloudflare_headers_is_let_through(app_module):
    status, _ = asgi_get(app_module.server, NOWHERE, LAN, [("Authorization", f"Bearer {ADMIN}")])
    assert status == 404, "through the gate, to the JSON 404 behind it"


def test_without_the_true_socket_address_an_admin_call_is_refused(app_module):
    """The FastAPI app alone, without the wrapper uvicorn runs, has only the forwarded address:
    fail closed rather than trust it."""
    status, body = asgi_get(app_module.app, NOWHERE, LAN, [("Authorization", f"Bearer {ADMIN}")])
    assert status == 403 and "address" in body["error"]


def test_everything_else_still_sees_the_forwarded_client(app_module):
    """Sign-in throttling and the logs go on reading the forwarded address, as before."""
    import asyncio

    seen = {}

    async def probe(scope, receive, send):
        seen.update(client=scope["client"], peer=scope.get("cc_peer"))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    wrapped = app_module.TruePeer(probe)

    async def go():
        scope = {"type": "http", "method": "GET", "path": "/", "headers": [(b"x-forwarded-for", b"203.0.113.7, 10.1.2.2")], "client": ("192.168.1.40", 5000), "scheme": "http", "query_string": b""}

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(_m):
            pass

        await wrapped(scope, receive, send)

    asyncio.run(go())
    assert seen["peer"] == "192.168.1.40"
    assert seen["client"][0] == "203.0.113.7", "the forwarded client, as uvicorn's --proxy-headers gave it"


def test_the_tesla_network_is_never_home(app_module):
    """The Funnel node and the receiver face the internet; a request from them is not the owner's."""
    status, body = asgi_get(app_module.server, NOWHERE, ("192.168.62.5", 4000), [("Authorization", f"Bearer {ADMIN}")])
    assert status == 403 and "Tesla" in body["error"]


def test_refusals_are_capped_per_token_and_the_rest_summed_up(app_module, monkeypatch):
    import auth

    now = [1000.0]
    monkeypatch.setattr(app_module, "REFUSALS", auth.RefusalCap(limit=10, window=60, clock=lambda: now[0]))
    c = client_from(app_module)
    for _ in range(25):
        assert c.get(NOWHERE, headers=bearer(READ)).status_code == 403
    assert len(app_module.audited) == 10, "ten refusals a minute are written, the rest only counted"
    now[0] += 61
    c.get(NOWHERE, headers=bearer(READ))
    assert len(app_module.audited) == 11 and app_module.audited[-1][-1] == 15, "the next one carries how many were held back"
    for _ in range(3):
        c.get(NOWHERE, headers=bearer(ADMIN))
    assert len(app_module.audited) == 14, "calls that go through are always written"
