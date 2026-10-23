"""Settings › Maps keeps the ArcGIS key the satellite tiles need: sealed, never shown back, and
forgetting it switches satellite off."""

import json

import pytest

import esrikey
import sources as sources_mod
from test_empty import bare, client  # noqa: F401 - fixtures
from test_settings_routes import form
from test_urdb import Rows

KEY = "AAPK" + "c3D4" * 12


@pytest.fixture
def maps(bare, monkeypatch):  # noqa: F811
    store, saved = Rows(), {}
    monkeypatch.setattr(bare, "db", lambda: store)
    monkeypatch.setattr(bare, "VAULT", sources_mod.Vault(sources_mod.new_key()))
    monkeypatch.setattr(bare, "ESRI_KEY", None)
    monkeypatch.setattr(bare.settings_mod, "save", lambda conn, key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(bare, "settings_changed", lambda: None)
    return store, saved


def test_a_saved_key_is_sealed_loaded_and_never_shown_back(bare, maps, client):  # noqa: F811
    store, _ = maps
    r = client.post("/settings/maps/esri-key", data=form(bare, {"key": KEY}))
    assert r.status_code == 303 and r.headers["location"].startswith("/settings/maps")
    assert KEY not in store.state[esrikey.KEY_STATE] and bare.ESRI_KEY == KEY
    assert json.loads(store.state[esrikey.KEY_STATE])["last4"].endswith("c3D4")
    page = client.get("/settings/maps").text
    assert "saved, ends …c3D4" in page and KEY not in page


def test_a_key_that_is_not_one_is_refused_and_not_echoed(bare, maps, client):  # noqa: F811
    store, _ = maps
    r = client.post("/settings/maps/esri-key", data=form(bare, {"key": "<b>nope</b>"}))
    assert "does not look like an ArcGIS API key" in r.text and "<b>nope</b>" not in r.text
    assert esrikey.KEY_STATE not in store.state and bare.ESRI_KEY is None


def test_forgetting_the_key_switches_satellite_off(bare, maps, client):  # noqa: F811
    store, saved = maps
    client.post("/settings/maps/esri-key", data=form(bare, {"key": KEY}))
    r = client.post("/settings/maps/esri-key", data=form(bare, {"remove": "1"}))
    assert r.status_code == 303 and esrikey.KEY_STATE not in store.state and bare.ESRI_KEY is None
    assert saved["satellite"] is False


def test_without_a_sealing_key_the_key_is_not_kept(bare, maps, client, monkeypatch):  # noqa: F811
    store, _ = maps
    monkeypatch.setattr(bare, "VAULT", sources_mod.Vault(None))
    r = client.post("/settings/maps/esri-key", data=form(bare, {"key": KEY}))
    assert "cannot be kept" in r.text and esrikey.KEY_STATE not in store.state and KEY not in r.text
