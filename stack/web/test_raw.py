"""Raw readings are kept for good, and only when a value changed."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from raw import DDL, RawKeeper, digest, state_at, values_only

NOW = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)


def tree(soc=65.9, upd="2026-09-28T18:00:00+00:00"):
    return {"vin": {"val": "V1", "upd": upd}, "drive": {"soc": {"val": soc, "upd": upd}, "range": {"val": 254.1, "upd": upd}}}


def test_upd_stamps_do_not_count_as_change():
    assert digest(tree()) == digest(tree(upd="2026-09-28T18:05:00+00:00"))
    assert digest(tree()) != digest(tree(soc=65.8))
    assert "upd" not in json.dumps(values_only(tree()))


class Conn:
    def __init__(self, rows):
        self.rows, self.ddl = rows, 0

    def execute(self, sql, params=None):
        if sql.startswith("CREATE"):
            self.ddl += 1
        elif sql.startswith("INSERT"):
            self.rows.append(params)
        return self

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_keeper_stores_once_per_change_and_creates_its_table_once():
    rows = []
    conn = Conn(rows)
    k = RawKeeper(connect=lambda: conn)
    assert k.keep("V1", tree(), NOW) is True
    assert k.keep("V1", tree(upd="later"), NOW + timedelta(minutes=1)) is False, "only the stamps moved"
    assert k.keep("V1", tree(soc=65.0), NOW + timedelta(minutes=2)) is True
    assert k.keep("V1", {}, NOW) is False, "an empty tree (connector down) is not a reading"
    assert len(rows) == 2 and conn.ddl == 1
    vin, at, source, data = rows[0]
    assert (vin, at, source) == ("V1", NOW, "connector") and json.loads(data)["drive"]["soc"]["val"] == 65.9
    assert "upd" in data, "what is stored is the whole tree, stamps and all"


def test_state_at_reads_the_newest_row_at_or_before():
    class Q:
        def __init__(self):
            self.params = None

        def execute(self, sql, params):
            self.params = params
            assert "at <= %s" in sql and "ORDER BY at DESC" in sql
            return self

        def fetchone(self):
            return {"data": json.dumps(tree())} if self.params[1] >= NOW else None

    assert state_at(Q(), "V1", NOW)["drive"]["soc"]["val"] == 65.9
    assert state_at(Q(), "V1", NOW - timedelta(hours=1)) is None


def test_ddl_has_no_foreign_key():
    assert "REFERENCES" not in DDL and "PRIMARY KEY (vin, at)" in DDL


def test_feed_rows_are_no_longer_trimmed():
    src = (Path(__file__).parent / "teslafi_live.py").read_text(encoding="utf-8")
    assert "DELETE FROM cc_teslafi_rows" not in src


def test_replay_from_moves_the_mark_back_then_rebuilds():
    from teslafi_live import TeslaFiPoller, replay_from

    calls = []

    class C:
        def execute(self, sql, params=None):
            calls.append((sql, params))
            return self

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    p = TeslaFiPoller([], connect=lambda: C())
    p.rebuild = lambda vin, now: {"trips": 14}  # type: ignore[method-assign]
    since = NOW - timedelta(days=24)
    assert replay_from(p, "V1", since, NOW) == {"trips": 14}
    sql, params = calls[0]
    assert sql.startswith("UPDATE cc_imports SET last_at") and params == (since, "teslafi-live:V1", since)
