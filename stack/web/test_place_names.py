"""The naming card's suggestions come from whatever place-name source Settings › Maps allows."""

from test_empty import bare, client  # noqa: F401 - fixtures


def ask(client):  # noqa: F811
    return client.get("/places/candidates?lat=39.80&lon=-89.64").json()


def test_openstreetmap_names_the_spot_when_the_local_add_on_is_not_set_up(bare, client, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(bare, "current_settings", lambda: {"public_geocoder": True})
    monkeypatch.setattr(bare, "public_place_name", lambda lat, lon: seen.append((lat, lon)) or {"name": "Lake Trail", "address": {"road": "Lake Rd", "city": "Springfield"}, "display_name": "x"})
    d = ask(client)
    assert d["available"] is True and d["candidates"] == [{"name": "Lake Trail", "kind": "", "sub": "OpenStreetMap"}] and d["address"]["road"] == "Lake Rd"
    assert seen == [(39.8, -89.64)]


def test_a_lookup_that_finds_nothing_is_available_but_empty(bare, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare, "current_settings", lambda: {"public_geocoder": True})
    monkeypatch.setattr(bare, "public_place_name", lambda lat, lon: None)
    d = ask(client)
    assert d["available"] is True and d["candidates"] == []


def test_with_no_source_allowed_nothing_is_asked_anywhere(bare, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare, "current_settings", lambda: {"public_geocoder": False})

    def never(lat, lon):
        raise AssertionError("a position went out with place names off")

    monkeypatch.setattr(bare, "public_place_name", never)
    d = ask(client)
    assert d["available"] is False and d["candidates"] == []


def test_a_failed_public_lookup_is_no_suggestion(bare, monkeypatch):  # noqa: F811
    def boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(bare, "discover_reverse_body", boom)
    assert bare.public_place_name(39.8, -89.64) is None
    monkeypatch.setattr(bare, "discover_reverse_body", lambda *a, **k: {"address": {"road": "Lake Rd", "city": "Springfield"}, "display_name": "Lake Rd, Springfield"})
    assert bare.public_place_name(39.8, -89.64)["name"] == "Lake Rd, Springfield"
    monkeypatch.setattr(bare, "discover_reverse_body", lambda *a, **k: {})
    assert bare.public_place_name(39.8, -89.64) is None


def test_the_card_words_each_case_in_plain_text_and_names_no_add_on():
    from pathlib import Path

    js = (Path(__file__).parent / "static" / "placecard.js").read_text()
    assert "add-on" not in js and "No name found nearby." in js and "Place names are off." in js and "Place names are not answering." in js
