import inspect

"""Connections: one pool for the site, and schema work that cannot leave a setting behind."""

import os

import psycopg
import pytest

for k, v in (("DATABASE_URL", "postgresql://u:p@localhost:1/x"), ("REST_USER", "u"), ("REST_PASSWORD", "p")):
    os.environ.setdefault(k, v)


class Result:
    def fetchone(self):
        return (None,)

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self, fail_on=None):
        self.log, self.fail_on = [], dict(fail_on or {})

    def execute(self, sql, *a, **k):
        self.log.append(sql)
        if self.fail_on.get(sql):
            self.fail_on[sql] -= 1
            raise psycopg.errors.LockNotAvailable("locked")
        return Result()

    def commit(self):
        self.log.append("COMMIT")

    def rollback(self):
        self.log.append("ROLLBACK")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_db_borrows_from_one_pool_instead_of_connecting_per_query(monkeypatch):
    import app

    lent = []

    class Pool:
        def connection(self, timeout=None):
            lent.append(1)
            return "a pooled connection"

    monkeypatch.setattr(app, "_POOL", Pool())
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("db() opened its own connection"))
    assert app.db() == "a pooled connection" and app.db() == "a pooled connection" and len(lent) == 2


def test_schema_sets_the_timeout_on_every_attempt_and_keeps_each_table(monkeypatch):
    """A rollback undoes a plain SET, so the retry ran with no lock timeout at all; and one
    transaction for every table meant a lock on the fifth rolled back the first four."""
    import app

    monkeypatch.setattr(app, "SCHEMA_RETRY_S", 0)
    conn = FakeConn()
    app.apply_schema(conn)
    ddl = [s for s in conn.log if s not in ("COMMIT", "ROLLBACK") and not s.startswith(("SET", "SELECT pg_advisory"))]
    locked = ddl[3]
    conn = FakeConn(fail_on={locked: 1})
    app.apply_schema(conn)
    i = conn.log.index(locked)
    assert conn.log[i - 2] == "SET LOCAL lock_timeout = '3s'" and "pg_advisory_xact_lock" in conn.log[i - 1] and conn.log[i + 1] == "ROLLBACK"
    retry = conn.log.index(locked, i + 1)
    assert conn.log[retry - 2] == "SET LOCAL lock_timeout = '3s'" and conn.log[retry + 1] == "COMMIT", "the retry is under the timeout too"
    assert conn.log.index("COMMIT") < i, "the tables before the locked one were already kept"
    assert "SET lock_timeout = '3s'" not in conn.log and "SET lock_timeout = 0" not in conn.log, "nothing session-wide to leak into a pooled connection"


def test_geocell_schema_and_its_backfill_run_at_startup_only(monkeypatch):
    """The `source` column and its one-time backfill are startup DDL, like every other
    table — never run per request or per background tick (a per-tick ALTER once took the
    site down)."""
    import app

    monkeypatch.setattr(app, "SCHEMA_RETRY_S", 0)
    conn = FakeConn()
    app.apply_schema(conn)
    assert any("cc_geocells" in s and "ALTER TABLE" in s for s in conn.log)
    assert any(s == app.GEOCELL_BACKFILL for s in conn.log)
    rename = conn.log.index(app.RENAME_LOCAL_ONCE)
    assert rename > conn.log.index(app.STATE_DDL), "the marker it checks lives in cc_state, created first"

    monkeypatch.setattr(app, "db", lambda: conn)
    conn.log.clear()
    app._cache.clear()
    app.geocells()
    assert not any("ALTER TABLE" in s or "CREATE TABLE" in s for s in conn.log), "geocells() must not run the DDL itself any more"
    app._cache.clear()


class StateFakeConn:
    """Records (sql, args) pairs, for checking what write_geocoder_state actually sends —
    sources.bump's key is a bound parameter, not SQL text, so the plain FakeConn's
    SQL-only log can't see it."""

    def __init__(self):
        self.calls, self.commits = [], 0

    def execute(self, sql, args=()):
        self.calls.append((" ".join(sql.split()), args))
        return self

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_write_geocoder_state_writes_and_bumps_in_one_transaction(monkeypatch):
    """Every decision is a stored string: a local URL, "public", or "" for off. A missing
    row means "not decided yet", which the connector reads as off. Either way the connector must see the bump in the same
    commit as the value, or it could restart on a half-written state."""
    import app

    conn = StateFakeConn()
    monkeypatch.setattr(app, "db", lambda: conn)
    app.write_geocoder_state("http://maps:8090/")
    assert conn.calls[0][0].startswith("INSERT INTO cc_state") and conn.calls[0][1] == ("geocoder_url", "http://maps:8090/")
    assert conn.calls[1][1][0] == "sources_version"
    assert conn.commits == 1

    conn = StateFakeConn()
    monkeypatch.setattr(app, "db", lambda: conn)
    app.write_geocoder_state("")
    assert conn.calls[0][1] == ("geocoder_url", "")
    assert conn.commits == 1

    conn = StateFakeConn()
    monkeypatch.setattr(app, "db", lambda: conn)
    app.write_geocoder_state("public")
    assert conn.calls[0][0].startswith("INSERT INTO cc_state") and conn.calls[0][1] == ("geocoder_url", "public"), "public is a stored decision, never a missing row"
    assert conn.calls[1][1][0] == "sources_version"
    assert conn.commits == 1
    assert "DELETE" not in inspect.getsource(app.write_geocoder_state)


def test_geocoder_tick_writes_the_decision_and_hands_back_what_the_loop_needs(monkeypatch):
    """geocoder_tick is the one decision both the startup call and the background loop
    use, so MAPS.status() (3 s timeout) is only ever paid once per tick."""
    import app

    class FakeMaps:
        base = "http://maps:8090"

        def status(self):
            return {"version": "1"}

    monkeypatch.setattr(app, "MAPS", FakeMaps())
    monkeypatch.setattr(app, "current_settings", lambda: {"public_geocoder": True})
    monkeypatch.setattr(app, "state_get", lambda key: None)
    written = []
    monkeypatch.setattr(app, "write_geocoder_state", lambda v: written.append(v))

    down_streak, url, up, public_ok, maps_url = app.geocoder_tick(3)
    assert (up, public_ok, maps_url) == (True, True, "http://maps:8090")
    assert url == "http://maps:8090/reverse"
    assert written == ["http://maps:8090/"], "local just came up: written at once, no debounce needed"
    assert down_streak == 0


def test_geocoder_tick_writes_nothing_when_the_decision_has_not_changed(monkeypatch):
    import app

    class FakeMaps:
        base = None

        def status(self):
            return None

    monkeypatch.setattr(app, "MAPS", FakeMaps())
    monkeypatch.setattr(app, "current_settings", lambda: {"public_geocoder": False})
    monkeypatch.setattr(app, "state_get", lambda key: "")
    monkeypatch.setattr(app, "write_geocoder_state", lambda v: pytest.fail("nothing changed; must not write"))

    down_streak, url, up, public_ok, maps_url = app.geocoder_tick(0)
    assert url is None and up is False and maps_url is None


def test_the_facts_refresh_scopes_its_timeout_to_its_own_transaction(monkeypatch):
    import history

    monkeypatch.setattr(history, "ensure_tables", lambda conn: conn.execute("CREATE ..."))
    monkeypatch.setattr(history, "fill_facts", lambda *a, **k: (0, 0))
    conn = FakeConn()
    history.fill_after_import(conn, "V1", None)
    i = next(n for n, s in enumerate(conn.log) if s.startswith("SET"))
    assert conn.log[i : i + 3] == ["SET LOCAL lock_timeout = '3s'", "CREATE ...", "COMMIT"] and "SET lock_timeout = 0" not in conn.log
