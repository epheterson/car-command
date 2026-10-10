"""/api/energy: the current reading and today's totals. A status token may read it (it carries no location);
a token for one car may not (it is the house's, not that car's)."""

import pytest

from datetime import datetime, timezone

import energy
from test_connect import status_client, world  # noqa: F401 - fixtures

READING = {"solar_w": 4200.0, "home_w": 1800.0, "grid_w": -300.0, "battery_w": -2100.0, "soe_pct": 61.0, "grid_status": "SystemGridConnected"}
TODAY = {"solar_kwh": 21.4, "home_kwh": 12.0, "grid_in_kwh": 1.5, "grid_out_kwh": 3.2, "battery_in_kwh": 5.0, "battery_out_kwh": 2.0}


@pytest.fixture
def house(world, monkeypatch):  # noqa: F811
    app, client = world
    monkeypatch.setattr(app, "energy_today", lambda: TODAY)
    app.ENERGY_LIVE.set(READING, "cloud", datetime.now(timezone.utc))
    yield app, client
    app.ENERGY_LIVE.reading = None


def test_the_reading_and_the_days_totals_come_as_one_answer(house):
    app, client = house
    r = client.get("/api/energy")
    body = r.json()
    assert r.status_code == 200 and body["reading"]["solar_w"] == 4200.0 and body["reading"]["source"] == "cloud" and body["reading"]["soe_pct"] == 61.0
    assert body["reading"]["age_s"] <= 5 and body["today"] == TODAY and "grid_status" in body["reading"]


def test_no_reading_is_null_not_an_error(house):
    app, client = house
    app.ENERGY_LIVE.reading = None
    body = client.get("/api/energy").json()
    assert body["reading"] is None and body["today"] == TODAY


def test_a_status_token_may_not_read_the_house(status_client, monkeypatch):  # noqa: F811
    app, c = status_client
    monkeypatch.setattr(app, "energy_today", lambda: TODAY)
    app.ENERGY_LIVE.set(READING, "cloud", datetime.now(timezone.utc))
    r = c.get("/api/energy")
    assert r.status_code == 403 and "status token" in r.json()["error"], "home power over time shows when nobody is home; the grid state shows outages"
    assert "/api/energy" not in app.auth.STATUS_PATHS and c.get("/energy/row").status_code in (401, 403, 404, 303)
    app.ENERGY_LIVE.reading = None


def test_a_token_for_one_car_is_refused_the_house(world, monkeypatch):  # noqa: F811
    app, client = world
    monkeypatch.setattr(app, "energy_today", lambda: TODAY)
    for kind in ("read",):
        raw = f"tok-{kind}-one-car"
        app.TOKENS[app.auth.token_hash(raw)] = {"label": "dash", "vin": "V1", "kind": kind, "last4": raw[-4:]}
        c = client.__class__(client.app, follow_redirects=False, headers={"Authorization": f"Bearer {raw}"})
        assert c.get("/api/energy?vin=V1").status_code == 403, kind
        assert "one car" in c.get("/api/energy?vin=V1").json()["error"]
    raw = "tok-read-all"
    app.TOKENS[app.auth.token_hash(raw)] = {"label": "all", "vin": None, "kind": "read", "last4": raw[-4:]}
    c = client.__class__(client.app, follow_redirects=False, headers={"Authorization": f"Bearer {raw}"})
    assert c.get("/api/energy").status_code == 200


def test_a_bearer_that_is_not_a_token_is_401(world):  # noqa: F811
    app, client = world
    c = client.__class__(client.app, follow_redirects=False, headers={"Authorization": "Bearer nonsense"})
    assert c.get("/api/energy").status_code == 401


class Conn:
    def __init__(self, row):
        self.row, self.asked = row, []

    def execute(self, sql, params=()):
        self.asked.append((sql, params))
        return self

    def fetchone(self):
        return self.row


def test_totals_round_the_sums_and_say_nothing_without_rows():
    lo, hi = datetime(2026, 7, 14, 7, tzinfo=timezone.utc), datetime(2026, 7, 15, 7, tzinfo=timezone.utc)
    conn = Conn({"solar_kwh": 21.43219, "home_kwh": 12.0, "grid_in_kwh": None, "grid_out_kwh": 3.2, "battery_in_kwh": 5.0, "battery_out_kwh": 2.0, "rows": 1440})
    got = energy.totals(conn, lo, hi)
    assert got["solar_kwh"] == 21.432 and got["grid_in_kwh"] == 0.0 and conn.asked[0][1] == ("home", lo, hi)
    assert energy.totals(Conn({"rows": 0}), lo, hi) is None and energy.totals(Conn(None), lo, hi) is None
    assert "5.0 / 60" in energy.TOTALS_SQL, "a row is five minutes"


def test_charges_carry_the_split_figures_but_not_the_pages_bar(world, monkeypatch):  # noqa: F811
    app, client = world
    row = {"id": 1, "vin": "V1", "kwh": 10.0, "src_status": "ok", "src_solar_kwh": 6.0, "src_battery_kwh": 2.0, "src_grid_kwh": 2.0, "mix": {"parts": []}}
    monkeypatch.setattr(app, "charges", lambda vin, *a, **k: [row])
    monkeypatch.setattr(app, "api_vin", lambda vin: None)
    body = client.get("/api/charges").json()
    assert body[0]["src_solar_kwh"] == 6.0 and "mix" not in body[0]


def test_the_row_comes_back_as_markup_for_the_page_to_redraw_and_empty_when_stale(house):
    app, client = house
    r = client.get("/energy/row")
    assert r.status_code == 200 and r.text.startswith('<section class="pwrow"') and "4.2 kW" in r.text and r.headers["cache-control"] == "no-store"
    app.ENERGY_LIVE.reading = None
    assert client.get("/energy/row").text == ""


def test_the_page_redraws_its_row_every_twenty_seconds_and_only_when_it_has_one():
    from pathlib import Path

    js = (Path(__file__).parent / "static" / "shell.js").read_text()
    part = js[js.index("The Powerwall row on Now") :]
    assert 'document.querySelector(".pwrow")' in part and "20000" in part and "/energy/row" in part and "document.hidden" in part
