"""The charge lists' query: a row with no start is a plug-in record, not a charge, and a
session with no end is marked as such for the rows to say so."""

import importlib
import os
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def app():
    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    os.environ.setdefault("REST_USER", "u")
    os.environ.setdefault("REST_PASSWORD", "p")
    return importlib.import_module("app")


class Conn:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def execute(self, sql, args=()):
        self.sql.append(sql)
        return self

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def row(**k):
    base = dict.fromkeys(("road", "city", "session_position_latitude", "session_position_longitude", "start_level", "end_level", "cost_source", "at_home"))
    return {**base, "id": 1, "vin": "V1", "session_start_date": None, "session_end_date": None, **k}


def test_charges_are_the_sessions_that_started_and_an_endless_one_is_open(app, monkeypatch):
    now = datetime.now(timezone.utc)
    live = row(id=1, session_start_date=now - timedelta(hours=2))
    lost = row(id=2, session_start_date=now - timedelta(days=9))
    done = row(id=3, session_start_date=now - timedelta(days=1, hours=3), session_end_date=now - timedelta(days=1))
    conn = Conn([live, lost, done])
    monkeypatch.setattr(app, "db", lambda: conn)
    monkeypatch.setattr(app, "places", lambda: [])
    monkeypatch.setattr(app, "geocells", lambda: {})
    got = {r["id"]: (r["open"], r["live"]) for r in app.charges("V1", None)}
    assert " WHERE c.session_start_date IS NOT NULL" in conn.sql[0] and "NULLS LAST" not in conn.sql[0]
    assert got == {1: (True, True), 2: (True, False), 3: (False, False)}, "charging now; an end the logger lost; finished"


def test_the_stats_count_charges_and_top_ups_apart_and_wait_for_the_end(app, monkeypatch):
    conn = Conn([])
    monkeypatch.setattr(app, "db", lambda: conn)
    app.summarize_sql("V1", "month", None)
    sql = conn.sql[1]
    assert "AS charges" in sql and "AS topups" in sql and f"< {app.stats_mod.TOPUP_KWH}) AS topups" in sql
    assert "t.session_end_date IS NOT NULL" in sql, "a charge still going is counted once it ends"
