"""First run: the time zone, units and currency are asked once, from the browser's own answers."""

from datetime import datetime, timezone

import pytest

from test_empty import bare, client  # noqa: F401 - fixtures
from test_settings_routes import form


@pytest.fixture
def saved(bare, monkeypatch):  # noqa: F811
    keys = {}
    monkeypatch.setattr(bare.settings_mod, "save", lambda conn, key, value: keys.__setitem__(key, value))
    monkeypatch.setattr(bare, "settings_changed", lambda: None)
    return keys


def _set(bare, **kw):  # noqa: F811
    bare._cache["settings"] = (datetime.now(timezone.utc), {**bare.settings_mod.DEFAULTS, **kw})


def test_an_empty_install_with_no_zone_set_is_sent_to_the_questions(client):  # noqa: F811
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/hello"
    page = client.get("/hello").text
    assert 'name="timezone"' in page and 'name="units"' in page and 'name="currency"' in page
    assert "Intl.DateTimeFormat().resolvedOptions().timeZone" in page, "the browser's zone pre-fills the field"
    assert '<select class="sel" id="tz" name="timezone" data-tz required>' in page and 'data-tz-find="tz"' in page and 'list="zones"' not in page
    assert '<option value="America/New_York">New York (UTC' in page and '<optgroup label="Europe">' in page, "grouped by region, each with its city and offset"
    assert "o.value === zone" in page, "the browser's zone is taken only when the list has it"


def test_an_install_whose_zone_is_set_is_never_asked(bare, client):  # noqa: F811
    _set(bare, timezone="Europe/Paris")
    assert client.get("/").status_code == 200
    r = client.get("/hello")
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_the_answers_are_saved_and_the_zone_checked(bare, client, saved):  # noqa: F811
    r = client.post("/hello", data=form(bare, {"timezone": "Europe/Paris", "units": "metric", "currency": "€"}))
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert saved == {"timezone": "Europe/Paris", "units": "metric", "currency": "€"}


@pytest.mark.parametrize(
    "data",
    [
        {"timezone": "Mars/Base", "units": "metric", "currency": "$"},
        {"timezone": "Europe/Paris", "units": "furlongs", "currency": "$"},
        {"timezone": "Europe/Paris", "units": "metric", "currency": ""},
    ],
)
def test_a_bad_answer_asks_again_and_saves_nothing(bare, client, saved, data):  # noqa: F811
    r = client.post("/hello", data=form(bare, data))
    assert r.status_code == 400 and saved == {}
    wanted = {"Mars/Base": "time zone", "furlongs": "imperial or metric", "": "currency symbol"}
    assert any(w in r.text for w in wanted.values())
    assert r.text.count('aria-invalid=true') == 1, "only the bad field is marked"


def test_the_error_names_the_bad_field_and_keeps_what_was_typed(bare, client, saved):  # noqa: F811
    r = client.post("/hello", data=form(bare, {"timezone": "Mars/Base", "units": "metric", "currency": "kr"}))
    assert "Pick a time zone" in r.text and "currency" not in r.text.split("<form")[0].lower()
    assert 'id="tz" name="timezone" data-tz required aria-invalid=true>' in r.text and "Mars/Base" not in r.text.split("<form")[1], "a zone that is not listed is never offered back"
    assert 'value="kr"' in r.text and '<option value="metric" selected>' in r.text
    assert 'data-typed="1"' in r.text, "the browser prefill leaves an error page alone"


def test_the_first_page_prefills_from_the_browser_and_labels_the_currency(client):  # noqa: F811
    page = client.get("/hello").text
    assert 'data-typed="0"' in page and "Currency symbol" in page


def test_a_set_zone_is_not_overwritten_from_here(bare, client, saved):  # noqa: F811
    _set(bare, timezone="Europe/Paris")
    r = client.post("/hello", data=form(bare, {"timezone": "Asia/Tokyo", "units": "metric", "currency": "¥"}))
    assert r.status_code == 303 and saved == {}
