"""Each /api/admin/ endpoint: its happy path, a request it turns away, and no secret in any
answer. The Connect Tesla ones run on the same fake Tesla as the page's tests, through the
same functions the page's buttons call."""

import json
import time

import pytest

import tesla_fleet
from test_empty import bare, client  # noqa: F401 - fixtures
from test_tesla_routes import ALPHA, BRAVO, HOST, account, done_facts, streaming, tesla  # noqa: F401 - fixtures

ADMIN = "admin-token-springfield-charlie"
LAN = ("192.168.1.40", 50000)
SECRETS = ("ta-secret.springfield", "acc-SECRET-springfield", "ref-SECRET-springfield", "tf-SECRET-springfield", "PRIVATE KEY", "tmpw7", ADMIN)


@pytest.fixture
def api(bare, monkeypatch):  # noqa: F811
    """A client on the LAN holding an admin token, its answers kept to look for secrets."""
    import auth
    from fastapi.testclient import TestClient

    monkeypatch.setattr(bare, "TOKENS", {auth.token_hash(ADMIN): {"label": "assistant", "vin": None, "kind": "admin", "last4": ADMIN[-4:]}})
    calls = []
    monkeypatch.setattr(bare, "audit", lambda *a: calls.append(a))
    c = TestClient(bare.server, follow_redirects=False, client=LAN, headers={"Authorization": f"Bearer {ADMIN}"})
    c.audited = calls
    seen = []
    send = c.request

    def request(method, url, *a, **kw):
        r = send(method, url, *a, **kw)
        seen.append(r.text)
        return r

    c.request = request
    yield c
    for text in seen:
        for secret in SECRETS:
            assert secret not in text, f"{secret!r} in an answer: {text[:300]}"


def secret_account(t):
    account(t, time.time() + 3600)
    t.rows[-1]["plain"] = json.dumps({"access_token": "acc-SECRET-springfield", "refresh_token": "ref-SECRET-springfield", "expires_at": time.time() + 3600})


# ---- Connect Tesla ---------------------------------------------------------


def test_tesla_facts_come_back_as_steps_and_cars(api, tesla, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tesla.app, "TESLA_DOMAIN", "example.test")
    facts = done_facts(
        stream_host=HOST,
        stream_host_ok=True,
        stream_registered=True,
        receiver={"ok": True, "problem": None, "error": None, "expires_at": None},
        stream_cars={
            ALPHA: {"on": True, "config_known": True, "config": {"synced": True, "hostname": HOST}, "live": True, "last_at": None, "errors": [{"name": "Tesla", "body": "late", "at": None}]},
            BRAVO: {"on": False, "config_known": False, "config": None, "live": False, "last_at": None, "errors": []},
        },
    )
    monkeypatch.setattr(tesla.app, "tesla_facts", lambda **_: facts)
    r = api.get("/api/admin/tesla")
    assert r.status_code == 200
    body = r.json()
    assert [s["id"] for s in body["steps"]] and all(s["state"] in ("done", "next", "problem", "waiting") for s in body["steps"])
    assert body["domain"] == "example.test" and body["registered"] is True and body["stream"]["registered"] is True
    alpha, bravo = body["cars"]
    assert alpha == {
        "vin": ALPHA,
        "name": "Alpha",
        "known_as": "Alpha",
        "paired": True,
        "firmware": "2026.32.5",
        "streaming": True,
        "config_held": True,
        "config_synced": True,
        "config_limit_reached": False,
        "live": True,
        "last_message_at": None,
        "errors": [{"name": "Tesla", "body": "late", "at": None}],
    }
    assert bravo["paired"] is False and bravo["config_synced"] is None, "not asked of Tesla is unknown, not false"


def test_registering_through_the_api_is_the_pages_register(api, tesla, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tesla.app, "TESLA_DOMAIN", "example.test")
    monkeypatch.setattr(tesla.app, "tesla_public_check", lambda pem: None)
    fake = tesla.answer((200, {"access_token": "partner", "expires_in": 1}), (200, {"response": {}}))
    r = api.post("/api/admin/tesla/register")
    assert r.status_code == 200 and r.json() == {"ok": True, "domain": "example.test"}
    assert fake.sent[1][3] == {"domain": "example.test"}
    assert [a[1:4] for a in api.audited] == [("POST", "/api/admin/tesla/register", 200)], "written down once, with how it went"


def test_registering_before_step_one_is_a_409(api, tesla, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tesla.app, "TESLA_DOMAIN", "")
    r = api.post("/api/admin/tesla/register")
    assert r.status_code == 409 and "step 1" in r.json()["error"]


def test_registering_says_why_the_domain_failed(api, tesla, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tesla.app, "TESLA_DOMAIN", "example.test")
    monkeypatch.setattr(tesla.app, "tesla_public_check", lambda pem: "example.test answered 302 for the key.")
    fake = tesla.answer()
    r = api.post("/api/admin/tesla/register")
    assert r.status_code == 400 and "302" in r.json()["error"] and fake.sent == []


def test_the_stream_address_registers(api, streaming, monkeypatch):  # noqa: F811
    monkeypatch.setattr(streaming.app, "tesla_public_check", lambda pem, host=None: None)
    streaming.answer((200, {"access_token": "partner"}), (200, {"response": {}}))
    r = api.post("/api/admin/tesla/stream/register")
    assert r.status_code == 200 and r.json() == {"ok": True, "host": HOST}


def test_the_stream_address_needs_one_set(api, streaming, monkeypatch):  # noqa: F811
    monkeypatch.setattr(streaming.app, "TESLA_STREAM_HOST", "")
    r = api.post("/api/admin/tesla/stream/register")
    assert r.status_code == 409 and "TESLA_STREAM_HOST" in r.json()["error"]


def test_starting_a_car_through_the_api(api, streaming):  # noqa: F811
    streaming.rows[0]["plain"] = json.dumps({"access_token": "acc-SECRET-springfield", "refresh_token": "ref-SECRET-springfield", "expires_at": time.time() + 3600})
    fake = streaming.answer((200, {"response": {"updated_vehicles": 1, "skipped_vehicles": {}}}))
    r = api.post("/api/admin/tesla/stream/start", json={"vin": ALPHA})
    assert r.status_code == 200 and r.json() == {"ok": True, "vin": ALPHA}
    assert fake.sent[0][3]["vins"] == [ALPHA] and streaming.rows[0]["config"]["stream"] == [ALPHA]


@pytest.mark.parametrize("body", [None, {}, {"vin": ""}, {"vin": "  "}, ["5YJ"], "not json"])
def test_starting_needs_a_car(api, streaming, body):  # noqa: F811
    fake = streaming.answer()
    r = api.post("/api/admin/tesla/stream/start", content=body if isinstance(body, str) else json.dumps(body) if body is not None else b"")
    assert r.status_code == 400 and "error" in r.json()
    assert fake.sent == []


def test_a_car_not_on_the_account_is_not_started(api, streaming):  # noqa: F811
    fake = streaming.answer()
    r = api.post("/api/admin/tesla/stream/start", json={"vin": "5YJ3E1EA0JF000009"})
    assert r.status_code == 400 and "not on the signed-in Tesla account" in r.json()["error"] and fake.sent == []


def test_stopping_a_car_through_the_api(api, streaming):  # noqa: F811
    streaming.rows[0]["config"] = {"base": tesla_fleet.NA, "stream": [ALPHA, BRAVO]}
    fake = streaming.answer((200, {"response": {}}))
    r = api.post("/api/admin/tesla/stream/stop", json={"vin": ALPHA})
    assert r.status_code == 200 and streaming.rows[0]["config"]["stream"] == [BRAVO]
    assert fake.sent[0][0] == "DELETE"


def test_stopping_needs_a_car(api, streaming):  # noqa: F811
    r = api.post("/api/admin/tesla/stream/stop", json={})
    assert r.status_code == 400 and "vin" in r.json()["error"]


# ---- sources ---------------------------------------------------------------


@pytest.fixture
def kept(tesla, monkeypatch):  # noqa: F811
    """A Tesla account and a TeslaFi token, both with secrets that must never come back."""
    secret_account(tesla)
    tesla.rows.append(
        {
            "id": 8,
            "vin": ALPHA,
            "kind": "teslafi",
            "label": "Alpha",
            "config": {},
            "plain": "tf-SECRET-springfield",
            "enabled": True,
            "last_ok_at": None,
            "last_error": "TeslaFi said no to tf-SECRET-springfield",
        }
    )
    tesla.rows.append(
        {
            "id": 9,
            "vin": None,
            "kind": "teslamate",
            "label": "TeslaMate",
            "config": {"vins": [BRAVO]},
            "plain": "postgresql://tm:tmpw7@db/tm",
            "enabled": True,
            "last_ok_at": None,
            "last_error": None,
        }
    )
    monkeypatch.setattr(tesla.app, "all_vehicles", lambda: [{"vin": ALPHA, "name": "Alpha"}, {"vin": BRAVO, "name": "Bravo"}])
    monkeypatch.setattr(tesla.app.teslafi, "results", {ALPHA: (None, "TeslaFi refused tf-SECRET-springfield")})
    return tesla


def test_sources_list_without_their_secrets(api, kept):
    r = api.get("/api/admin/sources")
    assert r.status_code == 200
    rows = {s["id"]: s for s in r.json()["sources"]}
    assert rows[7]["kind"] == "tesla" and rows[7]["readable"] is True and rows[7]["streaming"] == []
    assert rows[8]["car"] == "Alpha" and "***" in rows[8]["last_error"] and "***" in rows[8]["status"]["error"]
    assert rows[9]["following"] == [BRAVO]
    assert not any("plain" in s or "secret" in s or "config" in s for s in rows.values())


def test_a_source_test_through_the_api(api, kept, monkeypatch):
    monkeypatch.setattr(kept.app.sources, "check_teslafi", lambda token, fetch: {"ok": True, "vin": ALPHA, "name": "Alpha", "model": "Model 3", "battery": 80})
    r = api.post("/api/admin/sources/8/test")
    assert r.status_code == 200 and r.json() == {"ok": True, "error": None, "vin": ALPHA, "name": "Alpha", "model": "Model 3"}
    assert ("noted", None) in kept.log


def test_a_failing_source_test_is_a_400_with_why(api, kept, monkeypatch):
    monkeypatch.setattr(kept.app.sources, "check_teslamate", lambda url, connect: {"ok": False, "error": "Could not read that database: refused"})
    r = api.post("/api/admin/sources/9/test")
    assert r.status_code == 400 and r.json() == {"ok": False, "error": "Could not read that database: refused"}


def test_testing_no_such_source_is_a_json_404(api, kept):
    r = api.post("/api/admin/sources/999/test")
    assert r.status_code == 404 and r.json() == {"error": "No such source."}


# ---- places ----------------------------------------------------------------

WORK = {
    "id": 3,
    "name": "Work",
    "latitude": 39.78,
    "longitude": -89.65,
    "radius_m": 150.0,
    "auto": False,
    "visits": 12,
    "address": "1 Main St, Springfield",
    "user_address": None,
    "icon": "work",
    "price": None,
}
HOME = {**WORK, "id": 1, "name": "Home", "icon": None, "address": "742 Evergreen Terrace"}


@pytest.fixture
def placed(bare, monkeypatch):  # noqa: F811
    rows = {1: dict(HOME), 3: dict(WORK)}
    renamed = []

    def rename(place_id, name, icon=None, price=(False, None), address=(False, None)):
        renamed.append((place_id, name, icon, price, address))
        r = rows[place_id]
        if name is not None:
            r.update(name=name, auto=False)
        if icon is not bare.KEEP:
            r["icon"] = icon
        if price[0]:
            r["price"] = price[1]
        if address[0]:
            r["user_address"] = address[1]

    monkeypatch.setattr(bare, "places_detail", lambda: [dict(r) for r in rows.values()])
    monkeypatch.setattr(bare, "rename_place", rename)
    monkeypatch.setattr(bare, "current_settings", lambda: {"currency": "$"})
    return renamed


def test_places_list(api, placed):
    r = api.get("/api/admin/places")
    assert r.status_code == 200
    home, work = r.json()["places"]
    assert home["name"] == "Home" and home["price_text"] == "Home plan", "a home with no price is priced on the home plan"
    assert work["price_text"] == "Not set" and work["address"] == "1 Main St, Springfield" and work["own_address"] is False


def test_a_place_is_patched_field_by_field(api, placed, bare):  # noqa: F811
    r = api.patch("/api/admin/places/3", json={"price": {"kind": "kwh", "rate": 0.3, "fee": 1}})
    assert r.status_code == 200
    body = r.json()
    assert body["price"] == {"kind": "kwh", "rate": 0.3, "fee": 1.0} and body["price_text"] == "$0.30/kWh + $1.00 a session"
    assert body["name"] == "Work" and body["icon"] == "work", "fields not sent are left as they were"
    assert placed[-1] == (3, None, bare.KEEP, (True, {"kind": "kwh", "rate": 0.3, "fee": 1.0}), (False, None))
    r = api.patch("/api/admin/places/3", json={"name": "  The   Office ", "icon": None, "address": "2 Side St"})
    assert r.json()["name"] == "The Office" and r.json()["icon"] is None and r.json()["address"] == "2 Side St" and r.json()["own_address"]
    r = api.patch("/api/admin/places/3", json={"address": None, "price": None})
    assert r.json()["address"] == "1 Main St, Springfield" and r.json()["price"] is None


@pytest.mark.parametrize(
    "body,said",
    [
        ({"icon": "rocketship"}, "icon"),
        ({"price": {"kind": "kwh"}}, "per kWh"),
        ({"price": {"kind": "kwh", "rate": 99}}, "between"),
        ({"price": {"kind": "gold"}}, "kind of price"),
        ({"price": "free"}, "price"),
        ({"name": ""}, "name"),
        ({"name": 7}, "name"),
        ({"colour": "red"}, "colour"),
        ({}, "JSON object"),
        ([], "JSON object"),
    ],
)
def test_a_bad_patch_says_why_and_changes_nothing(api, placed, body, said):
    r = api.patch("/api/admin/places/3", json=body)
    assert r.status_code == 400 and said in r.json()["error"], r.text
    assert placed == []


def test_patching_no_such_place_is_a_404(api, placed):
    r = api.patch("/api/admin/places/99", json={"name": "Gym"})
    assert r.status_code == 404 and r.json() == {"error": "No such place."} and placed == []


def test_rename_place_leaves_what_it_is_not_given(bare, monkeypatch):  # noqa: F811
    """The PATCH path writes only the fields it was sent; the form path writes name and icon as before."""
    sent = []

    row = {"id": 3, "name": "Alpha Office", "latitude": 39.8, "longitude": -89.6, "radius_m": 150.0, "auto": False, "icon": "work", "price": None, "merged_into": None}

    class Res:
        def fetchone(self):
            return row

        def fetchall(self):
            return [row]

    class Conn:
        def execute(self, sql, params=()):
            sent.append(" ".join(sql.split()))
            return Res()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(bare, "db", lambda: Conn())
    writes = lambda: [s for s in sent if not s.startswith("SELECT")]  # noqa: E731
    bare.rename_place(3, None, bare.KEEP, (False, None), (True, "2 Side St"))
    assert writes() == ["UPDATE cc_places SET user_address = %s WHERE id = %s"]
    sent.clear()
    bare.rename_place(3, "Work", None)
    assert writes() == ["UPDATE cc_places SET name = %s, auto = FALSE WHERE id = %s", "UPDATE cc_places SET icon = %s WHERE id = %s"]


# ---- Money -----------------------------------------------------------------


def test_money_says_the_plan_prices_and_averages(api, bare, springfield_plan, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare, "current_settings", lambda: {"currency": "$", "rate_plan": springfield_plan, "dc_price": 0.48, "sc_price": 0.52, "gas_mpg": 30})
    monkeypatch.setattr(
        bare,
        "rates_settings",
        lambda: {"origin": "bundled", "built": "2026-09", "fetched_at": None, "tried_at": None, "error": None, "sources": [], "homes": [], "url": "https://example.test/rates.json"},
    )
    r = api.get("/api/admin/money")
    assert r.status_code == 200
    body = r.json()
    assert body["plan"]["name"] == "Springfield EV" and body["plan"]["source"] == "manual" and len(body["plan"]["periods"]) == 3
    assert body["plan"]["schedule"], "the schedule as the page says it"
    assert body["fast"] == {"dc_price": 0.48, "dc_per_min": None, "sc_price": 0.52, "sc_per_min": None}
    assert body["averages"]["origin"] == "bundled" and body["petrol"] == {"mpg": 30, "price": None}


def test_refreshing_the_averages_asks_the_worker(api, bare, monkeypatch):  # noqa: F811
    asked = []
    monkeypatch.setattr(bare.rates_worker, "request", lambda: asked.append(1))
    r = api.post("/api/admin/money/averages/refresh")
    assert r.status_code == 202 and r.json() == {"ok": True, "queued": True} and asked == [1]


def test_refresh_is_a_post(api, bare, monkeypatch):  # noqa: F811
    asked = []
    monkeypatch.setattr(bare.rates_worker, "request", lambda: asked.append(1))
    r = api.get("/api/admin/money/averages/refresh")
    assert r.status_code == 404 and "error" in r.json() and asked == []
