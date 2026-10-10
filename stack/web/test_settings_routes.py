"""Each Settings section at its own URL, saving only its own settings."""

import pytest

from test_empty import bare, client  # noqa: F401 - fixtures
from settingsform import OWNS, SECTIONS


def form(bare, data):  # noqa: F811
    """What the page posts: its fields and the CSRF token every write carries."""
    import auth

    return dict(data, csrf_token=auth.csrf_token(bare.AUTH["secret"], None))


@pytest.fixture
def saved(bare, monkeypatch):  # noqa: F811
    keys = []
    monkeypatch.setattr(bare.settings_mod, "save", lambda conn, key, value: keys.append(key))
    return keys


def test_settings_opens_on_a_grouped_list_of_every_section(client):  # noqa: F811
    r = client.get("/settings")
    assert r.status_code == 200 and "<h1>Settings</h1>" in r.text
    for key, label in SECTIONS:
        assert f'href="/settings/{key}"' in r.text and f'<span class="set-l">{label}</span>' in r.text, key
    assert "No plan" in r.text and "Sign-in off" in r.text and 'class="subpages" aria-label="Settings"' not in r.text


def test_each_section_page_leads_back_to_settings(client):  # noqa: F811
    r = client.get("/settings/money")
    assert '<a class="pbar-back" href="/settings">‹ Settings</a>' in r.text


def test_the_settings_landing_rows_preview_their_values():
    from settingsform import GROUPS, index

    assert sorted(k for _, keys in GROUPS for k in keys) == sorted(k for k, _ in SECTIONS), "every section once"
    plan = __import__("cost").compile_rows("Springfield EV", [{"from": "2026-01-01", "off_peak": 0.1, "part_peak": 0.2, "peak": 0.3}], [])
    rows = {r["key"]: r["value"] for g in index({"rate_plan": plan, "units": "metric", "clock": "24"}, 3, ["discord", "pushover"], True) for r in g["rows"]}
    assert rows["money"] == "Springfield EV" and rows["cars"] == "3 cars" and rows["alerts"] == "Discord, Pushover" and rows["look"] == "km · 24-hour" and rows["access"] == "Sign-in on"
    rows = {r["key"]: r["value"] for g in index({"rate_plan": {"versions": "junk"}}, 1, [], False) for r in g["rows"]}
    assert rows["money"] == "No plan" and rows["cars"] == "1 car" and rows["alerts"] == "Off" and rows["look"] == "mi · 12-hour"


@pytest.mark.parametrize("section", [s for s, _ in SECTIONS])
def test_every_section_renders(client, section):  # noqa: F811
    r = client.get(f"/settings/{section}")
    assert r.status_code == 200, section
    assert "<h1>" in r.text and 'aria-current="page"' in r.text


@pytest.mark.parametrize("section", [s for s, _ in SECTIONS if s != "access"])
def test_saving_a_section_writes_only_its_own_settings(bare, client, saved, section):  # noqa: F811
    everything = {"currency": "€", "units": "metric", "theme": "dark", "min_drive_mi": "1", "weather_shown": "1", "tire_low_psi": "30", "summary_at": "08:00", "dc_price": "0.5", "timezone": ""}
    r = client.post(f"/settings/{section}", data=form(bare, everything))
    assert r.status_code == 303 and r.headers["location"].startswith(f"/settings/{section}?saved=")
    assert set(saved) <= set(OWNS[section]), (section, set(saved) - set(OWNS[section]))


def test_old_and_unknown_settings_posts_save_nothing(bare, client, saved):  # noqa: F811
    assert client.post("/settings", data=form(bare, {"currency": "€", "units": "metric"})).status_code == 405
    assert client.post("/settings/nonsense", data=form(bare, {"currency": "€"})).status_code == 404
    assert client.get("/settings/nonsense").status_code == 404
    assert client.post("/settings/access", data=form(bare, {"currency": "€"})).status_code == 404
    assert saved == []


def test_the_staging_check_asks_for_pages_not_redirects():
    """scripts/staging.sh fetches each page without following redirects and wants a 200;
    /settings is a redirect now, so the check names the section pages."""
    from pathlib import Path

    script = (Path(__file__).parent.parent.parent / "scripts" / "staging.sh").read_text()
    line = next(l for l in script.splitlines() if l.strip().startswith("for p in / "))
    pages = line.split(" in ", 1)[1].split(";")[0].split()
    assert "/settings" not in pages
    assert {f"/settings/{s}" for s, _ in SECTIONS} <= set(pages)


import sources as sources_mod

ALPHA = "ALPHA0000000000001"


@pytest.fixture
def kept(bare, monkeypatch):  # noqa: F811
    """The store in memory, a key, and pollers that only record what they were told."""
    from test_sources import Store

    store = Store()
    monkeypatch.setattr(bare, "db", lambda: _Ctx(store))
    monkeypatch.setattr(bare, "VAULT", sources_mod.Vault(sources_mod.new_key()))
    told = {}
    monkeypatch.setattr(bare.teslafi, "use", lambda tokens: told.__setitem__("teslafi", tokens))
    monkeypatch.setattr(bare.teslamate, "use", lambda url, vins=None: told.__setitem__("teslamate", (url, vins)))
    monkeypatch.setattr(bare.teslafi, "fetch", lambda tok, cmd=None: {"vin": ALPHA, "display_name": "Alpha", "battery_level": 60} if tok.startswith("good") else {})
    monkeypatch.delenv("TESLAFI_TOKENS", raising=False)
    monkeypatch.delenv("TESLAMATE_DB_URL", raising=False)
    return store, told


class _Ctx:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self.store

    def __exit__(self, *_):
        return False


def test_adding_a_teslafi_token_tests_it_seals_it_and_shows_only_its_last_four(bare, client, kept):  # noqa: F811
    store, told = kept
    r = client.post("/settings/sources", data=form(bare, {"kind": "teslafi", "token": "good-token-9876"}))
    assert r.status_code == 200 and "…9876" in r.text and "good-token" not in r.text and "Alpha" in r.text
    assert len(store.rows) == 1 and store.rows[0]["vin"] == ALPHA and told["teslafi"] == [f"{ALPHA}:good-token-9876"]
    r = client.post("/settings/sources", data=form(bare, {"kind": "teslafi", "token": "bad-token-5555"}))
    assert r.status_code == 400 and "bad-token" not in r.text and len(store.rows) == 1


def test_without_a_key_nothing_secret_is_saved(bare, client, kept, monkeypatch):  # noqa: F811
    store, _ = kept
    monkeypatch.setattr(bare, "VAULT", sources_mod.Vault(None))
    r = client.post("/settings/sources", data=form(bare, {"kind": "teslafi", "token": "good-token-9876"}))
    assert r.status_code == 409 and store.rows == [] and "make secret-key" in r.text
    assert client.get("/settings/cars").text.count("make secret-key") >= 1


def test_moving_twice_makes_one_source_per_car(bare, client, kept, monkeypatch):  # noqa: F811
    store, told = kept
    monkeypatch.setenv("TESLAFI_TOKENS", f"{ALPHA}:good-env-1111")
    page = client.get("/settings/cars").text
    assert "from an older token setting" in page and "Move into the app" in page and "good-env" not in page
    for _ in range(2):
        assert client.post("/settings/sources/move", data=form(bare, {})).status_code == 303
    assert len(store.rows) == 1 and told["teslafi"] == [f"{ALPHA}:good-env-1111"]
    page = client.get("/settings/cars").text
    assert "TESLAFI_TOKENS" in page and "no longer read" in page and "good-env" not in page


def test_removing_a_source_hands_the_car_back_to_env(bare, client, kept, monkeypatch):  # noqa: F811
    store, told = kept
    client.post("/settings/sources", data=form(bare, {"kind": "teslafi", "token": "good-token-9876"}))
    monkeypatch.setenv("TESLAFI_TOKENS", f"{ALPHA}:good-env-1111")
    sid = store.rows[0]["id"]
    assert client.post(f"/settings/sources/{sid}/remove", data=form(bare, {})).status_code == 303
    assert store.rows == [] and told["teslafi"] == [f"{ALPHA}:good-env-1111"]


def test_an_env_token_without_a_vin_is_named_by_what_the_poller_learned(bare, client, kept, monkeypatch):  # noqa: F811
    """TESLAFI_TOKENS is often bare tokens; the poller learns each one's car on its first poll."""
    from datetime import datetime, timezone

    monkeypatch.setenv("TESLAFI_TOKENS", "good-env-1111")
    monkeypatch.setattr(bare.teslafi, "vin_for_token", {"good-env-1111": ALPHA})
    monkeypatch.setattr(bare.teslafi, "results", {ALPHA: (datetime.now(timezone.utc), None)})
    page = client.get("/settings/cars").text
    assert ALPHA in page and "Working" in page and "a car" not in page and "good-env" not in page


def test_a_move_that_cannot_take_every_car_takes_none(bare, client, kept, monkeypatch):  # noqa: F811
    """Two bare tokens: one the poller has named, one TeslaFi refuses right now. Moving only the
    first would leave the second unread in .env with no button to bring it back."""
    store, told = kept
    monkeypatch.setenv("TESLAFI_TOKENS", "good-env-1111,bad-env-2222")
    monkeypatch.setattr(bare.teslafi, "vin_for_token", {"good-env-1111": ALPHA})
    calls = []
    real = bare.teslafi.fetch
    monkeypatch.setattr(bare.teslafi, "fetch", lambda tok, cmd=None: calls.append(tok) or real(tok, cmd))
    r = client.post("/settings/sources/move", data=form(bare, {}))
    assert r.status_code == 400 and store.rows == [] and "Nothing moved" in r.text and "bad-env" not in r.text
    assert "good-env-1111" not in calls, "a car the poller already named is not asked again"
    assert "Move into the app" in r.text, "the button stays while .env is still what is read"


BRAVO = "BRAVO0000000000002"


class _TM:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, *a):
        class R:
            def fetchall(self_inner):
                return [{"id": 1, "vin": ALPHA, "name": "Alpha", "model": "3"}, {"id": 2, "vin": BRAVO, "name": "Bravo", "model": "Y"}]

        return R()


def test_teslamate_cars_can_be_unfollowed_and_followed_again_and_show_how_they_are(bare, client, kept, monkeypatch):  # noqa: F811
    import json
    from datetime import datetime, timezone

    store, told = kept
    monkeypatch.setattr(bare, "_teslamate_connect", lambda url: _TM())
    r = client.post("/settings/sources", data=form(bare, {"kind": "teslamate", "url": "postgresql://tm:pw@tm.lan:5432/teslamate"}))
    assert r.status_code == 200 and "following 2 cars" in r.text and "tm.lan" not in r.text
    sid = store.rows[0]["id"]
    assert told["teslamate"] == ("postgresql://tm:pw@tm.lan:5432/teslamate", [ALPHA, BRAVO])
    assert client.post(f"/settings/sources/{sid}/cars", data=form(bare, {f"vin_{ALPHA}": "on"})).status_code == 303
    config = json.loads(store.rows[0]["config"])
    assert config["vins"] == [ALPHA] and [c["vin"] for c in config["cars"]] == [ALPHA, BRAVO], "an unfollowed car stays on the list"
    monkeypatch.setattr(bare.teslamate, "results", {ALPHA: (datetime.now(timezone.utc), None)})
    page = client.get("/settings/cars").text
    assert f'name="vin_{BRAVO}"' in page and f'name="vin_{BRAVO}" checked' not in page and f'name="vin_{ALPHA}" checked' in page
    row = page[page.index('TeslaMate database<span class="set-s">') :]
    assert "Working" in row[:600], "the TeslaMate row shows how its cars are doing"


def test_the_test_button_records_what_it_found(bare, client, kept):  # noqa: F811
    store, _ = kept
    with bare.db() as conn:
        sid = bare.sources.add(conn, bare.VAULT, "teslafi", BRAVO, "Bravo", {}, "stale-token-3333")
    r = client.post(f"/settings/sources/{sid}/test", data=form(bare, {}))
    assert r.status_code == 400 and store.rows[0]["last_error"] and "stale-token" not in r.text
    with bare.db() as conn:
        bare.sources.add(conn, bare.VAULT, "teslafi", BRAVO, "Bravo", {}, "good-token-4444")
    assert client.post(f"/settings/sources/{sid}/test", data=form(bare, {})).status_code == 200 and store.rows[0]["last_ok_at"] is not None and store.rows[0]["last_error"] is None


def test_a_check_runs_off_the_event_loop(bare, client, kept, monkeypatch):  # noqa: F811
    """A check waits on TeslaFi (up to 20 s) or a TeslaMate host (10 s); on the event loop that
    stalls every page of the site for as long."""
    import asyncio

    where = []

    def check(*a, **k):
        try:
            asyncio.get_running_loop()
            where.append("loop")
        except RuntimeError:
            where.append("thread")
        return {"ok": False, "error": "no"}

    monkeypatch.setattr(bare.sources, "check_teslafi", check)
    monkeypatch.setattr(bare.sources, "check_teslamate", check)
    monkeypatch.setenv("TESLAFI_TOKENS", "bare-token-7777")
    client.post("/settings/sources", data=form(bare, {"kind": "teslafi", "token": "x-token-12345678"}))
    client.post("/settings/sources", data=form(bare, {"kind": "teslamate", "url": "postgresql://tm:pw@h/tm"}))
    client.post("/settings/sources/move", data=form(bare, {}))
    with bare.db() as conn:
        sid = bare.sources.add(conn, bare.VAULT, "teslafi", ALPHA, "Alpha", {}, "any-token-5555")
    client.post(f"/settings/sources/{sid}/test", data=form(bare, {}))
    assert where and set(where) == {"thread"}, where


def test_signing_in_to_lucid_seals_it_for_the_connector_and_never_shows_it(bare, client, kept):  # noqa: F811
    import json

    store, _ = kept
    r = client.post("/settings/sources", data=form(bare, {"kind": "lucid", "email": "owner@example.com", "password": "hunter-lucid-1"}))
    assert r.status_code == 200 and "Signing in" in r.text and "hunter-lucid" not in r.text
    row = store.rows[0]
    assert row["kind"] == "lucid" and json.loads(row["config"]) == {"pending": True} and b"hunter-lucid" not in bytes(row["secret"])
    assert "sources_version" in store.state
    assert "Signing in" in client.get("/settings/cars").text


def test_a_kind_the_page_does_not_know_still_renders(bare, client, kept):  # noqa: F811
    with bare.db() as conn:
        bare.sources.add(conn, bare.VAULT, "someday", None, "Someday", {}, "x-secret-000")
    assert client.get("/settings/cars").status_code == 200


def _signed_in_lucid(bare):  # noqa: F811
    import json

    with bare.db() as conn:
        return bare.sources.add(conn, bare.VAULT, "lucid", None, "Lucid", {"pending": False}, json.dumps({"refresh_token": "rt-live-123"}))


def test_a_lucid_row_shows_what_the_connector_reports(bare, client, kept, monkeypatch):  # noqa: F811
    """The connector's own cars are the Lucid source's status: working while it reports them,
    and saying so when it reports none (a token Lucid stopped taking)."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(bare, "garage", lambda: {"LUCID00000000000001": {"state": {"upd": now.isoformat()}}})
    assert bare.connector_status() == (now, None)
    monkeypatch.setattr(bare, "garage", lambda: {})
    ok, err = bare.connector_status()
    assert ok is None and err

    store, _ = kept
    _signed_in_lucid(bare)
    monkeypatch.setattr(bare, "connector_status", lambda: (now, None))
    page = client.get("/settings/cars").text
    assert "Working" in page[page.index('Lucid account<span class="set-s">') :][:400]
    monkeypatch.setattr(bare, "connector_status", lambda: (None, "The connector reports no car."))
    page = client.get("/settings/cars").text
    row = page[page.index('Lucid account<span class="set-s">') :][:500]
    assert "reports no car" in row and "Working" not in row


def test_test_on_a_lucid_row_writes_no_bogus_error(bare, client, kept, monkeypatch):  # noqa: F811
    store, _ = kept
    sid = _signed_in_lucid(bare)
    page = client.get("/settings/cars").text
    assert f'form="src-test-{sid}"' not in page, "no Test button: the connector's sign-in is the test"
    r = client.post(f"/settings/sources/{sid}/test", data=form(bare, {}))
    assert "address starts with postgresql://" not in r.text and store.rows[0]["last_error"] is None


# ---- another brand through CarConnectivity


def test_another_brand_is_sealed_never_shown_or_logged(bare, client, kept, caplog):  # noqa: F811
    import json
    import logging

    caplog.set_level(logging.DEBUG)
    store, _ = kept
    page = client.get("/settings/cars").text
    assert 'id="add-carconnectivity"' in page and 'id="cc-pane-cupra"' in page and "<style>" not in page
    r = client.post(
        "/settings/sources",
        data=form(
            bare,
            {
                "kind": "carconnectivity",
                "connector": "cupra",
                "username": "owner@example.com",
                "password": "hunter-cupra-1",
                "spin": "4321",
            },
        ),
    )
    assert r.status_code == 200 and "installs its plugin" in r.text
    for text in (r.text, client.get("/settings/cars").text, caplog.text):
        assert "hunter-cupra" not in text and "owner@example.com" not in text and "4321" not in text
    row = store.rows[0]
    config = json.loads(row["config"])
    assert row["kind"] == "carconnectivity" and row["vin"] is None
    assert config == {
        "connector": "cupra",
        "brand": "Cupra",
        "makes": ["Cupra", "CUPRA"],
        "package": "carconnectivity-connector-seatcupra",
        "version": "0.6.4",
        "type": "seatcupra",
        "state": "install",
        "connector_id": "cupra",
    }
    assert b"hunter-cupra" not in bytes(row["secret"]) and json.loads(bare.VAULT.open(row["secret"], "carconnectivity", row["id"])) == {
        "username": "owner@example.com",
        "password": "hunter-cupra-1",
        "spin": "4321",
        "brand": "cupra",
    }
    assert "sources_version" in store.state
    page = client.get("/settings/cars").text
    assert "Cupra (CarConnectivity)" in page and "Installing carconnectivity-connector-seatcupra 0.6.4" in page
    assert f'name="uninstall" form="src-rm-{row["id"]}" checked' in page and f'form="src-test-{row["id"]}"' not in page
    # the same brand again replaces the account, it does not add a second
    client.post(
        "/settings/sources",
        data=form(
            bare,
            {
                "kind": "carconnectivity",
                "connector": "cupra",
                "username": "owner@example.com",
                "password": "hunter-cupra-2",
            },
        ),
    )
    assert len(store.rows) == 1 and "hunter-cupra-2" in bare.VAULT.open(store.rows[0]["secret"], "carconnectivity", store.rows[0]["id"])


def test_a_bad_brand_form_saves_and_echoes_nothing(bare, client, kept):  # noqa: F811
    store, _ = kept
    r = client.post(
        "/settings/sources",
        data=form(
            bare,
            {
                "kind": "carconnectivity",
                "connector": "volkswagen",
                "username": "not-an-address",
                "password": "hunter-vw-1",
            },
        ),
    )
    assert r.status_code == 400 and "e-mail" in r.text and "hunter-vw" not in r.text and store.rows == []
    r = client.post(
        "/settings/sources",
        data=form(
            bare,
            {
                "kind": "carconnectivity",
                "connector": "volvo",
                "username": "o@example.com",
                "password": "hunter-vw-1",
            },
        ),
    )
    assert r.status_code == 400 and "Pick a brand" in r.text and store.rows == []


def test_removing_another_brand_leaves_it_to_the_connector(bare, client, kept):  # noqa: F811
    import json

    store, _ = kept
    client.post(
        "/settings/sources",
        data=form(
            bare,
            {
                "kind": "carconnectivity",
                "connector": "audi",
                "username": "o@example.com",
                "password": "hunter-audi-1",
            },
        ),
    )
    sid = store.rows[0]["id"]
    store.state.clear()
    assert client.post(f"/settings/sources/{sid}/remove", data=form(bare, {"uninstall": "on"})).status_code == 303
    row = store.rows[0]
    assert row["secret"] is None and not row["enabled"] and json.loads(row["config"])["state"] == "remove" and json.loads(row["config"])["uninstall"] is True
    assert "sources_version" in store.state, "the connector is told"
    assert "Removing" in client.get("/settings/cars").text
    assert client.post(f"/settings/sources/{sid}/remove", data=form(bare, {})).status_code == 303
    again = json.loads(store.rows[0]["config"])
    assert again["state"] == "remove" and again["uninstall"] is True, "a second Remove before the connector acts still leaves it to uninstall"


def test_a_failed_install_shows_why(bare, client, kept):  # noqa: F811
    import json

    store, _ = kept
    client.post(
        "/settings/sources",
        data=form(
            bare,
            {
                "kind": "carconnectivity",
                "connector": "skoda",
                "api_key": "k-123",
                "vins": "ALPHA000000000001",
            },
        ),
    )
    row = store.rows[0]
    row.update(
        config=json.dumps({**json.loads(row["config"]), "state": "failed"}),
        secret=None,
        last_error="pip could not install carconnectivity-connector-skoda 0.13.1: ERROR: no network",
    )
    page = client.get("/settings/cars").text
    assert "Škoda (CarConnectivity)" in page and "ERROR: no network" in page
