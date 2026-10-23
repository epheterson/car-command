"""Rename a car in Settings › Cars: a display override every page uses, never the source's data."""

from datetime import datetime, timezone

import pytest

from test_empty import EmptyConnection, Result, bare, client  # noqa: F401 - fixtures
from test_settings_routes import form


class Cars(EmptyConnection):
    """A database that knows two cars with one name."""

    ROWS = [{"vin": "VINAAAA1111", "name": "Comet"}, {"vin": "VINBBBB2222", "name": "Comet"}]

    def execute(self, sql="", *a, **kw):
        text = " ".join(str(sql).lower().split())
        if text.startswith("select vin, name from vehicles"):
            return type("R", (), {"fetchall": lambda s: [dict(r) for r in self.ROWS], "fetchone": lambda s: None})()
        return super().execute(sql, *a, **kw)


def _settings(bare, **kw):  # noqa: F811
    bare._cache["settings"] = (datetime.now(timezone.utc), {**bare.settings_mod.DEFAULTS, **kw})


@pytest.fixture
def garage(bare, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare, "db", lambda: Cars())
    monkeypatch.setattr(bare, "garage", lambda: {})
    bare.SHOWN.clear()
    return bare


def test_two_cars_with_one_name_are_told_apart_everywhere_the_garage_is_listed(garage):
    names = {v["vin"]: v["name"] for v in garage.all_vehicles()}
    assert names == {"VINAAAA1111": "Comet (1111)", "VINBBBB2222": "Comet (2222)"}
    assert garage.car_names() == names, "pickers read the same names"


def test_a_rename_is_what_every_list_shows_and_the_source_name_stays_beside_it(garage):
    _settings(garage, car_names={"VINAAAA1111": "Family car"})
    cars = {v["vin"]: v for v in garage.all_vehicles()}
    assert cars["VINAAAA1111"]["name"] == "Family car" and cars["VINAAAA1111"]["source_name"] == "Comet"
    assert cars["VINBBBB2222"]["name"] == "Comet", "no longer a twin, so no suffix"


def test_a_page_built_from_the_live_car_uses_the_rename(garage, monkeypatch):
    _settings(garage, car_names={"VINAAAA1111": "Family car"})
    monkeypatch.setattr(garage, "reported_vehicle", lambda vin=None: {"vin": "VINAAAA1111", "name": "Comet", "live": True, "status": "Parked"})
    garage.all_vehicles()
    assert garage.live_vehicle("VINAAAA1111")["name"] == "Family car"


def test_rows_that_carry_a_car_name_take_the_shown_one(garage):
    _settings(garage, car_names={"VINAAAA1111": "Family car"})
    garage.all_vehicles()
    rows = garage.with_shown([{"vin": "VINAAAA1111", "car_name": "Comet"}, {"vin": "VINBBBB2222", "car_name": "Comet"}, {"vin": None, "car_name": "x"}])
    assert [r["car_name"] for r in rows] == ["Family car", "Comet", "x"]


def test_renaming_saves_only_the_override(garage, client, monkeypatch):  # noqa: F811
    saved = {}
    monkeypatch.setattr(garage.settings_mod, "save", lambda conn, key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(garage, "settings_changed", lambda: None)
    r = client.post("/settings/cars/VINAAAA1111/name", data=form(garage, {"name": "  Family   car "}))
    assert r.status_code == 303 and saved == {"car_names": {"VINAAAA1111": "Family car"}}
    _settings(garage, car_names={"VINAAAA1111": "Family car", "VINBBBB2222": "Other"})
    client.post("/settings/cars/VINAAAA1111/name", data=form(garage, {"name": ""}))
    assert saved == {"car_names": {"VINBBBB2222": "Other"}}, "blank goes back to the source's name"
    _settings(garage, car_names={})
    client.post("/settings/cars/VINAAAA1111/name", data=form(garage, {"name": "Comet"}))
    assert saved == {"car_names": {}}, "the field opens holding the name; saved as the source's own it is no rename"


def test_renaming_a_car_that_is_not_there_is_refused(garage, client):  # noqa: F811
    assert client.post("/settings/cars/NOPE/name", data=form(garage, {"name": "x"})).status_code == 404


def test_the_cars_page_offers_the_rename_with_the_source_name_as_placeholder(garage, client):  # noqa: F811
    _settings(garage, car_names={"VINAAAA1111": "Family car"})
    page = client.get("/settings/cars").text
    assert 'action="/settings/cars/VINAAAA1111/name' in page and 'value="Family car"' in page and 'placeholder="Comet"' in page


def test_the_name_row_says_the_shown_name_once_with_the_source_name_in_a_tooltip(garage, client):  # noqa: F811
    page = client.get("/settings/cars").text
    assert 'title="Source name: Comet">Comet (1111)</span>' in page and "Comet (1111) · " not in page
    _settings(garage, car_names={"VINAAAA1111": "Family car"})
    assert 'title="Source name: Comet">Family car</span>' in client.get("/settings/cars").text


def test_renaming_a_car_does_not_move_it_in_the_list(garage):
    assert [v["vin"] for v in garage.all_vehicles()] == ["VINAAAA1111", "VINBBBB2222"]
    _settings(garage, car_names={"VINAAAA1111": "Zulu"})
    assert [v["vin"] for v in garage.all_vehicles()] == ["VINAAAA1111", "VINBBBB2222"], "Zulu stays first"


def test_a_form_answered_with_a_page_keeps_the_car_it_was_posted_from(garage, monkeypatch):
    """A refused add or a new token answers with the page itself, not a redirect: the car is the
    one in the address of the page the form was on, so the sidebar does not jump."""
    from starlette.requests import Request

    monkeypatch.setattr(garage, "live_vehicle", lambda vin=None: {"vin": vin or "FIRST"})
    monkeypatch.setattr(garage, "garage_car", lambda: {"vin": "all", "garage": True})

    def req(method, referer):
        headers = [(b"host", b"testserver")] + ([(b"referer", referer.encode())] if referer else [])
        return Request({"type": "http", "method": method, "path": "/settings/cars", "query_string": b"", "headers": headers, "root_path": ""})

    assert garage.pick(req("POST", "http://testserver/settings/cars?vin=VINBBBB2222"))["vin"] == "VINBBBB2222"
    assert garage.pick(req("POST", "http://testserver/settings/cars?vin=all")).get("garage"), "All cars stays All cars"
    assert garage.pick(req("POST", "http://elsewhere/settings/cars?vin=VINBBBB2222"))["vin"] == "FIRST", "another site's address is not trusted"
    assert garage.pick(req("GET", "http://testserver/settings/cars?vin=VINBBBB2222"))["vin"] == "FIRST", "a page view names its own car"
