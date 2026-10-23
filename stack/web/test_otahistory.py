"""The connector's version history becomes software rows of its own, rewritten without touching any other."""

import json
from datetime import datetime, timezone

from otahistory import SOURCE, parse, plan, store

ALPHA = "5YJ3E1EA0JF000001"
T = lambda d, h=0: datetime(2026, 1, d, h, tzinfo=timezone.utc)  # noqa: E731


def text(*pairs):
    return json.dumps([{"version": v, "at": a} for v, a in pairs])


def test_parse_reads_utc_times_oldest_first():
    rows = parse(text(("3.2.0", "2026-03-01T08:30:00Z"), ("3.1.1", "2025-12-19T20:42:10Z")))
    assert rows == [("3.1.1", datetime(2025, 12, 19, 20, 42, 10, tzinfo=timezone.utc)), ("3.2.0", datetime(2026, 3, 1, 8, 30, tzinfo=timezone.utc))]


def test_parse_reads_a_naive_time_as_utc_and_trims_the_version():
    assert parse(text((" 3.1.1 ", "2026-01-02T03:04:05"))) == [("3.1.1", datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))]


def test_parse_skips_what_it_cannot_read():
    assert parse(None) == [] and parse("") == [] and parse("not json") == [] and parse('{"a": 1}') == []
    mixed = json.dumps([{"version": "", "at": "2026-01-01T00:00:00Z"}, {"version": "3.1.1"}, {"version": "3.1.1", "at": "soon"}, 7, {"version": "3.1.2", "at": "2026-01-02T00:00:00Z"}])
    assert [v for v, _ in parse(mixed)] == ["3.1.2"]


def test_plan_deletes_the_stale_and_adds_the_new():
    assert plan([("a", T(1)), ("b", T(2))], [("b", T(2)), ("c", T(3))]) == ([("a", T(1))], [("c", T(3))])
    assert plan([("a", T(1))], [("a", T(1))]) == ([], [])


class Conn:
    """cc_software as a list of dicts; answers the statements store() sends."""

    def __init__(self, rows=()):
        self.rows = [{"vin": ALPHA, "import_id": None, **r} for r in rows]
        self.commits, self.last = 0, []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT version, seen_at"):
            vin, source = params
            self.last = [{"version": r["version"], "seen_at": r["seen_at"]} for r in self.rows if r["vin"] == vin and r["import_id"] == source]
        elif s.startswith("DELETE"):
            vin, source, version, at = params
            self.rows = [r for r in self.rows if not (r["vin"], r["import_id"], r["version"], r["seen_at"]) == (vin, source, version, at)]
        elif s.startswith("INSERT"):
            vin, version, at, source = params
            self.rows.append(dict(vin=vin, version=version, seen_at=at, import_id=source))
        return self

    def fetchall(self):
        return self.last

    def commit(self):
        self.commits += 1


def test_store_writes_only_its_own_rows_and_leaves_the_rest():
    live = dict(version="3.2.0", seen_at=T(9))
    hand = dict(version="3.0.0", seen_at=T(1), import_id="by-hand")
    conn = Conn([live, hand])
    assert store(conn, ALPHA, [("3.1.1", T(2)), ("3.2.0", T(5))]) == 2
    assert live in [{k: r[k] for k in live} for r in conn.rows] and hand["version"] in [r["version"] for r in conn.rows]
    assert {(r["version"], r["import_id"]) for r in conn.rows} == {("3.0.0", "by-hand"), ("3.1.1", SOURCE), ("3.2.0", None), ("3.2.0", SOURCE)}


def test_store_twice_changes_nothing_and_a_corrected_time_replaces_the_row():
    conn = Conn()
    store(conn, ALPHA, [("3.1.1", T(2))])
    assert store(conn, ALPHA, [("3.1.1", T(2))]) == 0 and conn.commits == 1
    assert store(conn, ALPHA, [("3.1.1", T(3))]) == 2
    assert [r["seen_at"] for r in conn.rows] == [T(3)]


def test_store_with_nothing_incoming_keeps_what_is_there():
    conn = Conn([dict(version="3.1.1", seen_at=T(2), import_id=SOURCE)])
    assert store(conn, ALPHA, []) == 0 and len(conn.rows) == 1


def test_the_snapshot_hook_stores_a_history_once_per_change(monkeypatch):
    import importlib
    from contextlib import contextmanager

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    monkeypatch.setenv("REST_USER", "u")
    monkeypatch.setenv("REST_PASSWORD", "p")
    app = importlib.import_module("app")

    conn = Conn()

    @contextmanager
    def fake_db(*_a, **_k):
        yield conn

    raw = text(("3.1.1", "2026-01-02T00:00:00Z"))
    monkeypatch.setattr(app, "db", fake_db)
    monkeypatch.setattr(app, "_ota_stored", {})
    monkeypatch.setitem(app._cache, "garage_tree", (T(1), {ALPHA: {"software_history": {"val": raw}}, "OTHER": {}}))
    app.record_ota_history(ALPHA)
    app.record_ota_history(ALPHA)
    assert conn.commits == 1 and [r["version"] for r in conn.rows] == ["3.1.1"]
    app.record_ota_history("OTHER")  # a car whose connector publishes no history
    assert conn.commits == 1
