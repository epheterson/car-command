"""What an /api/ caller gets when it is wrong: JSON with a status it can act on, never the sign-in page."""

import pytest
from test_connect import world  # noqa: F401 - fixture


def stranger(client):
    return client.__class__(client.app, follow_redirects=False)


def with_token(app, client, raw="tok-read-api-errors", kind="read"):
    app.TOKENS[app.auth.token_hash(raw)] = {"label": "dash", "vin": None, "kind": kind, "last4": raw[-4:]}
    return client.__class__(client.app, follow_redirects=False, headers={"Authorization": f"Bearer {raw}"})


@pytest.mark.parametrize("path", ["/api/now", "/api/drives", "/api/month-routes"])
def test_no_token_is_401_json_that_says_what_to_send(world, path):  # noqa: F811
    app, client = world
    r = stranger(client).get(path)
    assert r.status_code == 401 and r.headers["content-type"].startswith("application/json")
    assert "Authorization: Bearer" in r.json()["error"] and r.headers["www-authenticate"] == "Bearer"


def test_a_token_in_the_url_is_401_and_says_to_use_the_header(world):  # noqa: F811
    app, client = world
    with_token(app, client, "tok-in-the-url")
    r = stranger(client).get("/api/now?token=tok-in-the-url")
    assert r.status_code == 401 and r.headers["content-type"].startswith("application/json") and "header" in r.json()["error"] and "URL" in r.json()["error"]


def test_pages_still_send_a_stranger_to_sign_in(world):  # noqa: F811
    app, client = world
    r = stranger(client).get("/timeline")
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_a_wrong_method_is_405_json_naming_the_right_one(world):  # noqa: F811
    app, client = world
    c = with_token(app, client)
    r = c.post("/api/now")
    assert r.status_code == 405 and r.headers["content-type"].startswith("application/json")
    assert r.json()["error"] == "POST is not allowed here; use GET" and r.headers["allow"] == "GET"
    assert c.delete("/api/drives").status_code == 405


def test_a_write_a_token_may_not_make_is_json_not_the_stale_form_page(world):  # noqa: F811
    app, client = world
    c = with_token(app, client)
    r = c.post("/api/nothing-here")
    assert r.status_code == 400 and r.headers["content-type"].startswith("application/json") and "control token" in r.json()["error"]


def test_a_bad_month_is_400_json_naming_it(world, monkeypatch):  # noqa: F811
    app, client = world
    c = with_token(app, client)
    for bad in ("xx", "2026-13", "2026-3", "March"):
        r = c.get("/api/month-routes", params={"month": bad})
        assert r.status_code == 400 and "month is YYYY-MM" in r.json()["error"], bad


class Req:
    def __init__(self, query=None, referer=None, host="cc.springfield.test"):
        self.query_params = query or {}
        self.headers = {"host": host, **({"referer": referer} if referer else {})}
        self.scope = {"root_path": ""}


def test_a_redirect_into_settings_keeps_the_car_that_was_selected(monkeypatch):
    import app

    monkeypatch.setattr(app, "live_vehicle", lambda vin=None: {"vin": "ALPHA1"})
    keep = app.keep_car
    assert keep(Req({"vin": "BRAVO2"}), "/settings/cars?saved=1") == "/settings/cars?saved=1&vin=BRAVO2"
    assert (
        keep(Req(referer="https://cc.springfield.test/settings/cars?vin=CHARLIE3"), "/settings/alerts#chan-discord") == "/settings/alerts?vin=CHARLIE3#chan-discord"
    ), "a form without the car on it: the page it was on says"
    assert keep(Req(), "/settings/cars?saved=1") == "/settings/cars?saved=1&vin=ALPHA1", "no car anywhere: the one this page showed, before the import adds another"
    assert keep(Req({"vin": "BRAVO2"}), "/settings/cars?vin=ALPHA1") == "/settings/cars?vin=ALPHA1", "a target that names its car is left alone"
    assert keep(Req({"vin": "BRAVO2"}), "/login?next=/") == "/login?next=/" and keep(Req({"vin": "BRAVO2"}), "/timeline") == "/timeline", "only Settings pages"
    assert keep(Req(referer="https://evil.example/x?vin=EVIL"), "/settings/cars") == "/settings/cars?vin=ALPHA1", "another site's page does not choose the car"
    assert keep(Req({"vin": "a b&c=d"}), "/settings/cars") == "/settings/cars", "a vin is a plain id"


@pytest.mark.parametrize("path", ["/api/drives", "/api/charges"])
def test_a_bad_date_is_400_json_naming_the_parameter(world, monkeypatch, path):  # noqa: F811
    app, client = world
    c = with_token(app, client)
    for param in ("since", "until"):
        for bad in ("bad", "2026-13-01", "2026-3-1", "yesterday"):
            r = c.get(path, params={param: bad})
            assert r.status_code == 400 and r.headers["content-type"].startswith("application/json"), (param, bad)
            assert r.json()["error"] == f"{param} is a date, YYYY-MM-DD"


def test_since_and_until_bound_the_drives_and_charges_asked_for(world, monkeypatch):  # noqa: F811
    app, client = world
    c = with_token(app, client)
    seen = []
    monkeypatch.setattr(app, "api_vin", lambda vin: vin)
    monkeypatch.setattr(app, "drives", lambda vin, limit, start, end: seen.append(("drives", limit, start, end)) or [])
    monkeypatch.setattr(app, "charges", lambda vin, limit, start, end: seen.append(("charges", limit, start, end)) or [])
    assert c.get("/api/drives").status_code == 200 and c.get("/api/charges?since=2026-10-01&until=2026-10-08").status_code == 200
    assert c.get("/api/drives?since=2026-10-01").status_code == 200
    (_, limit0, a0, b0), (_, limit1, a1, b1), (_, limit2, a2, b2) = seen
    assert (limit0, a0, b0) == (50, None, None), "no window: the 50 most recent, as before"
    assert (a1.date().isoformat(), b1.date().isoformat(), limit1) == ("2026-10-01", "2026-10-08", app.API_WINDOW_MAX)
    assert a2.date().isoformat() == "2026-10-01" and b2 is None and limit2 == app.API_WINDOW_MAX
