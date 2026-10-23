"""The region id a form posts is never trusted straight to the add-on: it becomes part of a
URL path and (via builder.py) a filename there, so a bad one is refused before any call out."""

import pytest

from test_empty import bare, client  # noqa: F401 - fixtures


def form(bare, data):  # noqa: F811
    import auth

    return dict(data, csrf_token=auth.csrf_token(bare.AUTH["secret"], None))


@pytest.fixture
def calls(bare, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(bare.MAPS, "add", lambda rid: seen.append(("add", rid)))
    monkeypatch.setattr(bare.MAPS, "remove", lambda rid: seen.append(("remove", rid)))
    return seen


@pytest.mark.parametrize("bad", ["../../settings", "us/../x", ""])
def test_a_bad_region_id_is_refused_before_any_call(bare, client, calls, bad):  # noqa: F811
    assert client.post("/settings/maps/regions/add", data=form(bare, {"id": bad})).status_code == 400
    assert client.post("/settings/maps/regions/remove", data=form(bare, {"id": bad})).status_code == 400
    assert calls == []


def test_a_valid_region_id_is_passed_through(bare, client, calls):  # noqa: F811
    r = client.post("/settings/maps/regions/add", data=form(bare, {"id": "us/illinois"}))
    assert r.status_code == 303 and r.headers["location"].startswith("/settings/maps?saved=")
    assert calls == [("add", "us/illinois")]


def test_the_maps_save_sets_the_add_on_only_when_its_settings_changed(bare, client, monkeypatch):  # noqa: F811
    sent = []
    monkeypatch.setattr(bare.MAPS, "settings", lambda **kw: sent.append(kw))
    shown = {"maps_service_shown": "1", "auto_was": "1", "refresh_was": "monthly", "public_tiles_shown": "1"}
    assert client.post("/settings/maps", data=form(bare, dict(shown, auto="on", refresh="monthly"))).status_code == 303
    assert sent == [], "unchanged: no call to the add-on"
    assert client.post("/settings/maps", data=form(bare, dict(shown, refresh="quarterly"))).status_code == 303
    assert sent == [{"auto": False, "refresh": "quarterly"}]


def test_the_big_maps_say_the_background_is_off_and_offer_the_switch():
    """With map backgrounds off (the default) Map, Places and Now carry a chip with a button to
    Settings > Maps, not a bare rectangle; the small maps on drive and charge pages stay quiet."""
    from pathlib import Path

    here = Path(__file__).parent
    js = (here / "static" / "maps.js").read_text()
    off = js.split("if (!tilesOn()) {")[1].split("return map;")[0]
    assert "if (opts.hint) offHint(map)" in off and "Map backgrounds are off" in js and "/settings/maps" in js
    css = (here / "static" / "style.css").read_text()
    assert "bottom:10px" in css.split(".cc-off {")[1].split("}")[0], "a quiet note in the corner, not a pill in the middle of the empty box"
    assert "linear-gradient" in css.split(".cc-no-tiles {")[1].split("}")[0], "the empty box is a faint grid, not a blank white hole"
    assert "Plain background" in js and "Add a map" in js
    assert "attributionControl.remove()" in off and "map.getContainer()" in js.split("function offHint")[1].split("}")[0], "no credit under an empty box"
    for page in ("static/placesmap.js", "templates/map.html", "templates/now.html"):
        assert "hint: true" in (here / page).read_text(), page
    for page in ("templates/charge.html", "templates/parked.html", "templates/day.html", "templates/place.html"):
        assert "hint" not in (here / page).read_text().split("CCMap.base(")[1].split(")")[0], page


def test_the_one_time_offer_turns_on_both_services_or_neither_and_is_never_asked_twice(bare, client, monkeypatch):  # noqa: F811
    saved = {}
    monkeypatch.setattr(bare.settings_mod, "save", lambda conn, key, value: saved.__setitem__(key, value))
    r = client.post("/settings/maps/offer", data=form(bare, {"answer": "on", "back": "/places"}))
    assert r.status_code == 303 and r.headers["location"] == "/places"
    assert saved == {"public_tiles": True, "public_geocoder": True, "maps_offer": "on"}
    saved.clear()
    r = client.post("/settings/maps/offer", data=form(bare, {"answer": "no", "back": "/settings/cars#import"}))
    assert r.status_code == 303 and r.headers["location"] == "/settings/cars#import"
    assert saved == {"maps_offer": "no"}, "not now leaves both services off and ends the offer"
    saved.clear()
    assert client.post("/settings/maps/offer", data=form(bare, {"answer": "maybe"})).status_code == 400 and saved == {}


def test_the_offer_never_redirects_off_site(bare, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare.settings_mod, "save", lambda conn, key, value: None)
    r = client.post("/settings/maps/offer", data=form(bare, {"answer": "no", "back": "//evil.example"}))
    assert r.headers["location"] == "/"


def test_the_defaults_keep_every_service_off_until_it_is_answered():
    import settings

    assert settings.DEFAULTS["public_tiles"] is False and settings.DEFAULTS["public_geocoder"] is False and settings.DEFAULTS["maps_offer"] is None
