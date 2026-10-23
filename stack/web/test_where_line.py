"""Where a car with no connector address is: its cell's cached street, else the town around it."""

import importlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_tesla_locked import ALPHA, Rows

AT = datetime.now(timezone.utc) - timedelta(minutes=2)


class ParkedDb:
    """A vehicle row and its newest position: parked in Springfield, nowhere it has a place."""

    def execute(self, sql="", *a, **k):
        text = " ".join(str(sql).lower().split())
        if "from vehicles where vin" in text:
            return Rows({"vin": ALPHA, "name": "Alpha", "manufacturer": "Tesla", "model": "Model 3"})
        if "from cc_positions" in text:
            return Rows({"at": AT, "latitude": 39.80123, "longitude": -89.65311, "level": 70.0, "odometer": 1000.0})
        if "to_regclass" in text:
            return Rows({"t": None})
        return Rows(None)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def app(monkeypatch):
    from test_empty import bare  # noqa: F401 - the app module, imported the way the other tests do

    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    os.environ.setdefault("REST_USER", "u")
    os.environ.setdefault("REST_PASSWORD", "p")
    sys.path.insert(0, str(Path(__file__).parent))
    app = importlib.import_module("app")
    monkeypatch.setattr(app, "db", lambda: ParkedDb())
    monkeypatch.setattr(app, "places", lambda: [])
    monkeypatch.setitem(app.tesla_stream_worker.carried, ALPHA, {})
    return app


def test_a_tesla_in_a_named_cell_reads_as_its_street(app, monkeypatch):
    monkeypatch.setattr(app, "geocells", lambda: {(39.801, -89.653): "Oak Avenue, Springfield"})
    monkeypatch.setattr(app, "geotowns", lambda: {})
    car = app.stored_vehicle(ALPHA)
    assert car["address"] == "Oak Avenue, Springfield" and car["place"] is None


def test_a_tesla_at_an_unnamed_spot_reads_as_its_town(app, monkeypatch):
    monkeypatch.setattr(app, "geocells", lambda: {})
    monkeypatch.setattr(app, "geotowns", lambda: {(39.80, -89.65): "Springfield, IL"})
    car = app.stored_vehicle(ALPHA)
    assert car["address"] is None and car["locality"] == "Springfield, IL"


def test_the_naming_loop_reloads_the_caches_before_a_page_would(app, monkeypatch):
    loads = []
    monkeypatch.setattr(app, "geocells", lambda: loads.append("geocells") or app._cache.__setitem__("geocells", (now, {})))
    monkeypatch.setattr(app, "geotowns", lambda: loads.append("geotowns") or app._cache.__setitem__("geotowns", (now, {})))
    now = datetime.now(timezone.utc)
    monkeypatch.setitem(app._cache, "geocells", (now - timedelta(seconds=250), {}))
    monkeypatch.setitem(app._cache, "geotowns", (now - timedelta(seconds=30), {}))
    app.warm_where(now)
    assert loads == ["geocells"], "only the one about to expire"
    app.warm_where(now)
    assert loads == ["geocells"], "fresh again: nothing reloaded"
