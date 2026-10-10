"""A status token may be allowed to see where each car is, as a place name and home or away, and nothing finer."""

from datetime import datetime, timezone

import pytest

import auth
import connect
from test_connect import GOOD, FakeDb, allow, code_of, exchange, states, world  # noqa: F401 - fixtures
from test_connect import full_car as base_car

UNGRANTED_GONE = {"lat", "lon", "address", "heading", "speed_mph", "odometer_mi", "meters", "place", "at_home"}


def car(vin="V1", **over):
    return {**base_car(vin), "place": "Home", "place_named": "Home", **over}


@pytest.fixture
def status(world, monkeypatch):  # noqa: F811
    """A status token, ungranted, and a client that holds it."""
    app, client = world
    app.told = []
    monkeypatch.setattr(app.recorder, "notify", lambda text, kind="other": app.told.append((kind, text)))
    monkeypatch.setattr(app, "live_vehicle", lambda vin=None: car(vin or "V1"))
    monkeypatch.setattr(app, "garage", lambda: {})
    raw = exchange(client, code_of(allow(app, client))).json()["token"]
    h = app.auth.token_hash(raw)
    app.status_raw, app.status_hash = raw, h
    return app, client, client.__class__(client.app, follow_redirects=False, headers={"Authorization": f"Bearer {raw}"})


def grant(app, on=True):
    app.TOKENS[app.status_hash]["share_place"] = on


# ---- what a status token sees


def test_by_default_nothing_about_where_is_shared(status):
    app, _, c = status
    body = c.get("/api/now?vin=V1").json()
    assert not UNGRANTED_GONE & set(body) and body["vin"] == "V1" and body["soc"] == 58


def test_with_the_grant_only_place_and_at_home_are_added(status):
    app, _, c = status
    plain = c.get("/api/now?vin=V1").json()
    grant(app)
    shared = c.get("/api/now?vin=V1").json()
    assert set(shared) - set(plain) == {"place", "at_home"} and set(plain) <= set(shared)
    assert shared["place"] == "Home" and shared["at_home"] is True
    assert {k: v for k, v in shared.items() if k not in ("place", "at_home")} == plain
    assert not (UNGRANTED_GONE - {"place", "at_home"}) & set(shared)


def test_the_whole_garage_follows_the_grant_too(status, monkeypatch):
    app, _, c = status
    monkeypatch.setattr(app, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}])
    monkeypatch.setattr(app, "live_vehicle", lambda vin=None: car(vin, place_named="Work" if vin == "V2" else "Home", at_home=vin == "V1"))
    grant(app)
    cars = c.get("/api/now?vin=all").json()
    assert [(x["vin"], x["place"], x["at_home"]) for x in cars] == [("V1", "Home", True), ("V2", "Work", False)]
    assert not {"lat", "lon", "address", "heading"} & set(cars[0])


def test_an_unnamed_place_and_an_unknown_position_are_null(status, monkeypatch):
    app, _, c = status
    grant(app)
    monkeypatch.setattr(app, "live_vehicle", lambda vin=None: car("V1", place="12 Main St", place_named=None, at_home=False))
    shared = c.get("/api/now?vin=V1").json()
    assert shared["place"] is None and shared["at_home"] is False, "a street is not a place the owner named"
    assert "12 Main St" not in str(shared)
    monkeypatch.setattr(app, "live_vehicle", lambda vin=None: car("V1", lat=None, lon=None, place=None, place_named=None, at_home=False))
    shared = c.get("/api/now?vin=V1").json()
    assert shared["place"] is None and shared["at_home"] is None, "no position: unknown, not away"


def test_a_read_token_is_unchanged(status):
    app, client, _ = status
    raw = "tok-springfield-read"
    app.TOKENS[app.auth.token_hash(raw)] = {"label": "r", "vin": None, "kind": "read", "last4": "read", "share_place": True}
    body = client.__class__(client.app, follow_redirects=False, headers={"Authorization": f"Bearer {raw}"}).get("/api/now?vin=V1").json()
    assert body["lat"] == 1.5 and body["address"] == "1 Main St" and body["place"] == "Home"


# ---- where the names come from


class Idx(list):
    def near(self, lat, lon):
        return self


def place(name, auto):
    return {"id": 1, "name": name, "latitude": 40.0, "longitude": -90.0, "radius_m": 50, "auto": auto, "user_address": None, "icon": None}


def leaf(v):
    return {"val": v}


def garage_car():
    return {"name": leaf("Alpha"), "state": leaf("parked"), "connection_state": leaf("reachable"), "position": {"latitude": leaf(40.0), "longitude": leaf(-90.0)}}


def test_the_live_car_names_a_place_only_when_the_owner_did(world, monkeypatch):  # noqa: F811
    app, _ = world
    monkeypatch.setattr(app, "garage", lambda: {"V1": garage_car()})
    monkeypatch.setattr(app, "places", lambda: Idx([place("Home", False)]))
    now = app.live_vehicle("V1")
    assert now["place_named"] == "Home" and now["at_home"] is True
    monkeypatch.setattr(app, "places", lambda: Idx([place("12 Main St", True)]))
    now = app.live_vehicle("V1")
    assert now["place"] == "12 Main St" and now["place_named"] is None and now["at_home"] is False
    monkeypatch.setattr(app, "places", lambda: Idx([]))
    assert app.live_vehicle("V1")["place_named"] is None


def test_the_stored_car_names_a_place_only_when_the_owner_did(world, monkeypatch):  # noqa: F811
    from test_tesla_locked import VehicleDb

    app, _ = world

    class Db(VehicleDb):
        def execute(self, sql="", *a, **k):
            text = " ".join(str(sql).lower().split())
            if "from cc_positions" in text:
                from test_tesla_locked import Rows

                return Rows({"at": datetime.now(timezone.utc), "latitude": 40.0, "longitude": -90.0, "level": 50, "odometer": 1000})
            return super().execute(sql, *a, **k)

    monkeypatch.setattr(app, "db", lambda: Db())
    monkeypatch.setattr(app, "places", lambda: Idx([place("12 Main St", True)]))
    got = app.stored_vehicle("5YJ3E1EA0JF000001")
    assert got["place"] == "12 Main St" and got["place_named"] is None
    monkeypatch.setattr(app, "places", lambda: Idx([place("Work", False)]))
    assert app.stored_vehicle("5YJ3E1EA0JF000001")["place_named"] == "Work"


# ---- the switch in Settings


def flip(app, client, h, on=True, **kw):
    data = {"csrf_token": app.csrf, **({"share_place": "on"} if on else {}), **kw}
    return client.post(f"/settings/tokens/{h}/place", data=data)


def test_the_owner_turns_it_on_and_off_and_it_takes_effect_at_once(status):
    app, client, c = status
    assert c.get("/api/now?vin=V1").json().get("place") is None and "place" not in c.get("/api/now?vin=V1").json()
    r = flip(app, client, app.status_hash)
    assert r.status_code == 303 and app.TOKENS[app.status_hash]["share_place"] is True
    assert c.get("/api/now?vin=V1").json()["place"] == "Home", "no reconnect"
    assert [k for k, t in app.told if "Example" in t and "place each car" in t], "an alert says it was turned on"
    told = len(app.told)
    flip(app, client, app.status_hash, on=False)
    assert app.TOKENS[app.status_hash]["share_place"] is False and "place" not in c.get("/api/now?vin=V1").json()
    assert len(app.told) == told, "turning it off is quiet"


def test_the_switch_needs_the_forms_token_and_a_session(status):
    app, client, c = status
    assert client.post(f"/settings/tokens/{app.status_hash}/place", data={"share_place": "on"}).status_code == 400
    stranger = client.__class__(client.app, follow_redirects=False)
    assert stranger.post(f"/settings/tokens/{app.status_hash}/place", data={"share_place": "on", "csrf_token": app.csrf}).status_code in (303, 401, 400)
    assert not app.TOKENS[app.status_hash].get("share_place")


def test_a_status_token_cannot_turn_it_on_for_itself(status):
    app, _, c = status
    for path in (f"/settings/tokens/{app.status_hash}/place", f"/api/tokens/{app.status_hash}/place", f"/api/admin/tokens/{app.status_hash}/place"):
        r = c.post(path, data={"share_place": "on", "csrf_token": app.csrf})
        assert r.status_code in (303, 400, 401, 403, 404), path
    assert not app.TOKENS[app.status_hash].get("share_place")


def test_only_a_status_token_can_be_granted(world):  # noqa: F811
    app, client = world
    app.fake.tokens.append({"hash": "h-read", "label": "dash", "vin": None, "kind": "read", "last4": "read", "created_at": datetime.now(timezone.utc), "last_used_at": None})
    app.reload_tokens()
    flip(app, client, "h-read")
    assert not app.TOKENS["h-read"].get("share_place") and not app.fake.tokens[0].get("share_place")


def test_the_page_shows_the_switch_on_status_tokens_only():
    from test_signin_switch import render_access

    rows = [
        {"hash": "a" * 64, "label": "Example", "last4": "wxyz", "kind": "status", "car": "every car", "created_at": datetime.now(timezone.utc), "last_used_at": None, "share_place": True},
        {"hash": "b" * 64, "label": "dash", "last4": "read", "kind": "read", "car": "every car", "created_at": datetime.now(timezone.utc), "last_used_at": None},
    ]
    html = render_access(auth_enabled=True, tokens=rows)
    assert html.count('class="set-s">place name, home or away') == 1
    assert f'action="/carcommand/settings/tokens/{"a" * 64}/place"' in html and f"/settings/tokens/{'b' * 64}/place" not in html


# ---- streams follow the switch


def test_a_stream_follows_the_switch_within_a_tick(status, monkeypatch):
    app, client, c = status
    monkeypatch.setattr(app, "STREAM_S", 0.01)
    monkeypatch.setattr(app, "STREAM_MAX_S", 0.4)
    calls = []

    def live(vin=None):
        calls.append(vin)
        if len(calls) == 3:
            flip(app, client, app.status_hash)  # the owner flips it mid-stream
        return car(vin or "V1", soc=50 + (1 if len(calls) >= 3 else 0))

    monkeypatch.setattr(app, "live_vehicle", live)
    monkeypatch.setattr(app, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha"}])
    sent = states(c.get("/api/stream?vin=V1").text)
    assert sent and "place" not in sent[0] and "at_home" not in sent[0]
    assert sent[-1]["place"] == "Home" and sent[-1]["at_home"] is True and "lat" not in sent[-1] and "address" not in sent[-1]
    keys = {k for k in app._STATES}
    assert len({k[1:] for k in keys}) >= 1


def test_the_cached_readings_do_not_mix_the_variants(status):
    app, _, _ = status
    stripped = app.shared_states("V1", True, False)
    shared = app.shared_states("V1", True, True)
    full = app.shared_states("V1", False, False)
    assert "place" not in stripped["V1"] and shared["V1"]["place"] == "Home" and full["V1"]["lat"] == 1.5
    assert stripped is not shared and shared is not full
    assert app.shared_states("V1", True, True) is shared


# ---- the Allow page


def test_the_allow_page_offers_the_choice_unchecked(world):  # noqa: F811
    app, client = world
    html = client.get("/connect", params=GOOD).text
    assert 'name="share_place"' in html and "place name, home or away" in html
    assert "checked" not in html.split('name="share_place"')[1].split(">")[0]


def test_the_choice_made_on_the_allow_page_goes_to_the_token(world, monkeypatch):  # noqa: F811
    app, client = world
    app.told = []
    monkeypatch.setattr(app.recorder, "notify", lambda text, kind="other": app.told.append((kind, text)))
    plain = exchange(client, code_of(allow(app, client))).json()["token"]
    assert not app.TOKENS[auth.token_hash(plain)].get("share_place")
    told = len(app.told)
    shared = exchange(client, code_of(allow(app, client, share_place="on", client_name="x"))).json()["token"]
    assert app.TOKENS[auth.token_hash(shared)]["share_place"] is True
    assert any("place each car" in t for _, t in app.told[told:]), "a connect alert says it was granted"


def test_a_skipped_consent_keeps_what_the_remembered_app_had(world, monkeypatch):  # noqa: F811
    app, client = world
    monkeypatch.setattr(app, "APP_REDIRECTS", connect.allowlist("example-app://done"))
    first = exchange(client, code_of(allow(app, client, share_place="on"))).json()["token"]
    assert app.TOKENS[auth.token_hash(first)]["share_place"] is True
    skipped = client.get("/connect", params=GOOD)
    assert skipped.status_code == 303
    second = exchange(client, code_of(skipped)).json()["token"]
    assert app.TOKENS[auth.token_hash(second)]["share_place"] is True, "the app was allowed with it"
    app.fake.codes.clear()
    exchange(client, code_of(allow(app, client)))  # allowed again by hand, this time without
    again = exchange(client, code_of(client.get("/connect", params=GOOD))).json()["token"]
    assert not app.TOKENS[auth.token_hash(again)].get("share_place")


def test_the_columns_arrive_at_startup_additively():
    assert "ALTER TABLE cc_api_tokens ADD COLUMN IF NOT EXISTS share_place BOOLEAN NOT NULL DEFAULT false" in auth.API_DDL
    for table in ("cc_connect_codes", "cc_connect_apps"):
        assert f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS share_place BOOLEAN NOT NULL DEFAULT false" in connect.DDL


def test_the_switch_in_settings_also_moves_the_remembered_app(world, monkeypatch):  # noqa: F811
    """So a later skipped reconnect keeps the owner's latest choice, not the one from the Allow page."""
    app, client = world
    monkeypatch.setattr(app.recorder, "notify", lambda text, kind="other": None)
    monkeypatch.setattr(app, "APP_REDIRECTS", connect.allowlist("example-app://done"))
    key = ("example-app://done", "Example")
    raw = exchange(client, code_of(allow(app, client))).json()["token"]  # allowed without
    h = auth.token_hash(raw)
    assert app.fake.app_share[key] is False
    flip(app, client, h)  # the owner turns it on in Settings
    assert app.fake.app_share[key] is True
    skipped = exchange(client, code_of(client.get("/connect", params=GOOD))).json()["token"]
    assert app.TOKENS[auth.token_hash(skipped)]["share_place"] is True, "the reconnect keeps the latest choice"
    flip(app, client, auth.token_hash(skipped), on=False)
    assert app.fake.app_share[key] is False
    again = exchange(client, code_of(client.get("/connect", params=GOOD))).json()["token"]
    assert not app.TOKENS[auth.token_hash(again)].get("share_place")


def test_the_switch_leaves_other_apps_alone(world, monkeypatch):  # noqa: F811
    app, client = world
    monkeypatch.setattr(app.recorder, "notify", lambda text, kind="other": None)
    mine = exchange(client, code_of(allow(app, client))).json()["token"]
    other = ("example-app://elsewhere", "Other")
    app.fake.tokens.append({"hash": "h-other", "label": "Other", "vin": None, "kind": "status", "last4": "oth", "created_at": datetime.now(timezone.utc), "last_used_at": None})
    app.fake.apps[other], app.fake.app_share[other] = "h-other", False
    flip(app, client, auth.token_hash(mine))
    assert app.fake.app_share[other] is False
