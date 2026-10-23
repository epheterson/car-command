"""A site with nothing in it must still be a site.

Someone who has not configured a connector, or who runs on imported history alone, used
to meet a 503 on every page: garage() raised when the connector did not answer, and every
page goes through it. These tests stand the app up with no connector and no rows.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

AGGREGATES = ("count(", "sum(", "min(", "max(", "avg(", "coalesce(sum")


class Result:
    """One query's worth of nothing.

    An empty table answers a plain SELECT with no row at all, but an aggregate with no
    GROUP BY still answers with one row of nulls. Getting that distinction wrong is the
    difference between testing an empty database and testing a fiction.
    """

    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []


class NullRow(dict):
    def __missing__(self, _key):
        return None


class EmptyConnection:
    def execute(self, sql="", *_a, **_kw):
        text = " ".join(str(sql).lower().split())
        head = text.split(" from ")[0]
        aggregate = any(fn in head for fn in AGGREGATES) and " group by " not in text
        return Result(NullRow() if aggregate else None)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def bare(monkeypatch):
    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    os.environ.setdefault("REST_USER", "u")
    os.environ.setdefault("REST_PASSWORD", "p")
    sys.path.insert(0, str(Path(__file__).parent))
    app = importlib.import_module("app")

    import httpx

    def refused(*_a, **_kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(app, "db", lambda: EmptyConnection())
    monkeypatch.setattr(app, "db_plain", lambda: EmptyConnection())
    monkeypatch.setattr(app.httpx, "get", refused)
    app._cache.clear()
    app.AUTH.update(enabled=False, secret="test", password=None)
    yield app
    app._cache.clear()


@pytest.fixture
def client(bare):
    from fastapi.testclient import TestClient

    return TestClient(bare.app, follow_redirects=False)


def test_a_connector_that_is_not_answering_no_longer_takes_the_site_with_it(bare):
    assert bare.garage() == {}
    assert bare.connector_down() == bare.NO_CONNECTOR, "an install that never had a connector is told so calmly, not warned"


def test_the_failure_is_remembered_so_every_page_does_not_pay_the_timeout(bare, monkeypatch):
    bare.garage()
    calls = []
    monkeypatch.setattr(bare.httpx, "get", lambda *a, **k: calls.append(1))
    for _ in range(5):
        bare.garage()
    assert calls == [], "a connector known to be down is not asked again for a quarter minute"


@pytest.fixture
def zoned(bare):
    """An install whose time zone is already set: the first-run questions are behind it."""
    from datetime import datetime, timezone

    bare._cache["settings"] = (datetime.now(timezone.utc), {**bare.settings_mod.DEFAULTS, "timezone": "America/Chicago"})


def test_with_no_connector_and_no_history_the_front_page_says_what_to_do(client, zoned):
    r = client.get("/")
    assert r.status_code == 200
    assert "Nothing here yet" in r.text
    assert "connector" in r.text.lower()
    assert 'href="/settings/cars#import"' in r.text and "Import history" in r.text
    assert "make import" not in r.text and "teslafi_load.py" not in r.text and "docker" not in r.text, "the import is a button, not commands"
    assert "make demo" not in r.text and "carcommand demo" not in r.text, "the demo car would stay in this install"


def test_the_one_container_is_pointed_at_the_same_import(client, monkeypatch, zoned):
    monkeypatch.setenv("CC_INSTALL", "one-container")
    text = client.get("/").text
    assert 'href="/settings/cars#import"' in text and "docker" not in text and "make import" not in text


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/drives",
        "/charges",
        "/timeline",
        "/calendar",
        "/stats",
        "/places",
        "/map",
        "/battery",
        "/sleep",
        "/software",
        "/tags",
        "/service",
        "/settings/cars",
        "/settings/money",
        "/settings/alerts",
        "/settings/look",
        "/settings/data",
        "/settings/access",
        "/day",
        "/charging",
    ],
)
def test_every_page_renders_on_an_empty_install(client, zoned, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} answered {r.status_code}: {r.text[:300]}"


@pytest.mark.parametrize("path", ["/api/now", "/api/drives", "/api/charges", "/drives.csv", "/charges.csv"])
def test_the_data_endpoints_answer_empty_rather_than_failing(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} answered {r.status_code}: {r.text[:300]}"


def test_the_empty_car_is_shaped_like_a_real_one(bare):
    empty, real = bare.empty_car(), bare.garage_car()
    assert set(empty) >= set(real), "a template written against a car must not meet a missing key"
    assert empty["empty"] is True and empty["garage"] is False


def test_a_site_with_only_imported_history_opens_on_that_car(bare, monkeypatch):
    # The import-only case: no connector at all, but the tables know a car. The front page
    # must land on it rather than on the welcome screen or a 503.
    monkeypatch.setattr(bare, "stored_vehicle", lambda vin: {"vin": vin, "name": "Alpha", "live": False})

    class OneCar(EmptyConnection):
        def execute(self, sql="", *a, **kw):
            if "from vehicles" in " ".join(str(sql).lower().split()):
                return Result({"vin": "V1"})
            return super().execute(sql, *a, **kw)

    monkeypatch.setattr(bare, "db", lambda: OneCar())
    car = bare.live_vehicle(None)
    assert car["vin"] == "V1" and car["live"] is False
    assert not car.get("empty")


def test_the_favicon_is_served(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and len(r.content) > 200
