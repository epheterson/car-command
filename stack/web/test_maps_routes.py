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
