"""A sealed secret opens only for the row it was sealed for; a bare one is bound to the row it is in."""

import json
import os
from pathlib import Path

import pytest

import sealing
import sources
from test_sources import ALPHA, Store


def vault():
    return sources.Vault(sources.new_key())


def test_the_copy_here_is_the_shared_one():
    here = Path(__file__).parent
    assert (here / "sealing.py").read_bytes() == (here.parent / "shared" / "sealing.py").read_bytes()
    assert (here / "sealing.py").read_bytes() == (here.parent / "cc" / "sealing.py").read_bytes()


def test_a_blob_moved_to_another_row_or_kind_opens_as_nothing():
    v = vault()
    tesla = v.seal(json.dumps({"refresh_token": "rt-tesla"}), "tesla", 3)
    assert json.loads(v.open(tesla, "tesla", 3)) == {"refresh_token": "rt-tesla"}
    assert v.open(tesla, "lucid", 3) is None, "a Tesla token in a Lucid row"
    assert v.open(tesla, "tesla", 4) is None, "another row of the same kind"
    assert v.open(tesla, "carconnectivity", 3) is None
    assert v.open(v.seal("pem", "state", "tesla_private_key"), "lucid", 1) is None, "an app value in a source row"


def test_a_value_from_before_binding_opens_only_where_legacy_is_allowed():
    v = vault()
    bare = v._fernet.encrypt(b"rt-old")
    assert v.open(bare, "lucid", 1) is None, "never for a source row"
    assert v.open(bare, "state", "tesla_private_key", legacy=True) == "rt-old"


def test_rows_are_stored_sealed_for_their_own_id():
    conn, v = Store(), vault()
    a = sources.add(conn, v, "teslafi", ALPHA, "Alpha", {}, "tok-a")
    b = sources.add_connector(conn, v, "cupra", "Cupra", {"connector": "cupra"}, {"username": "u@example.com", "password": "pw"})
    by_id = {r["id"]: r for r in conn.rows}
    assert v.open(by_id[a]["secret"], "teslafi", a) == "tok-a" and v.open(by_id[a]["secret"], "teslafi", b) is None
    assert json.loads(v.open(by_id[b]["secret"], "carconnectivity", b))["password"] == "pw"
    by_id[b]["secret"] = by_id[a]["secret"]  # what a process that can write the table might do
    got = {r["id"]: r for r in sources.load(conn, v)}
    assert got[b]["plain"] is None and got[a]["plain"] == "tok-a"


def legacy_rows(v):
    return [
        {"id": 1, "vin": None, "kind": "lucid", "label": "", "config": "{}", "secret": v._fernet.encrypt(b'{"refresh_token": "rt-1"}'), "enabled": True, "last_ok_at": None, "last_error": None},
        {"id": 2, "vin": ALPHA, "kind": "teslafi", "label": "", "config": "{}", "secret": v._fernet.encrypt(b"tok-2"), "enabled": True, "last_ok_at": None, "last_error": None},
    ]


def test_bare_secrets_are_bound_at_startup_under_the_lock_and_the_flag_set_when_none_is_left():
    conn, v = Store(), vault()
    conn.rows = legacy_rows(v)
    assert sources.bind_sealed(conn, v) == 2 and conn.locks == [sealing.LOCK_KEY]
    assert conn.state[sealing.DONE_KEY] == sealing.DONE_VALUE
    assert {r["id"]: r["plain"] for r in sources.load(conn, v)} == {1: '{"refresh_token": "rt-1"}', 2: "tok-2"}
    assert sources.bind_sealed(conn, v) == 0 and conn.locks == [sealing.LOCK_KEY], "done: not even the lock again"


def test_a_value_this_key_cannot_open_keeps_the_flag_off_and_is_kept():
    conn, v = Store(), vault()
    other = sources.new_key().encode()
    conn.rows = legacy_rows(v) + [{"id": 3, "vin": None, "kind": "tesla", "label": "", "config": "{}", "secret": other, "enabled": True, "last_ok_at": None, "last_error": None}]
    assert sources.bind_sealed(conn, v) == 2
    assert sealing.DONE_KEY not in conn.state, "not finished: the next start looks again"
    assert conn.rows[2]["secret"] == other, "kept as it is, never dropped"


def test_a_bare_value_written_after_the_flag_is_bound_where_it_is_read():
    """A split deploy: the old site writes a bare value after the new connector set the flag."""
    conn, v = Store(), vault()
    conn.rows = legacy_rows(v)
    sources.bind_sealed(conn, v)
    conn.locks = []
    conn.rows[1]["secret"] = v._fernet.encrypt(b"tok-written-by-old-code")
    got = {r["id"]: r["plain"] for r in sources.load(conn, v)}
    assert got[2] == "tok-written-by-old-code" and conn.locks == [sealing.LOCK_KEY]
    assert v.open(conn.rows[1]["secret"], "teslafi", 2) == "tok-written-by-old-code", "bound to its row in place"
    tesla = v.seal("rt-tesla", "tesla", 9)
    conn.rows[0]["secret"] = tesla  # but a bound blob copied in from another row
    assert {r["id"]: r["plain"] for r in sources.load(conn, v)}[1] is None and conn.rows[0]["secret"] == tesla, "still refused, never rebound"


def test_a_rebind_never_overwrites_a_value_replaced_since_it_was_read():
    conn, v = Store(), vault()
    conn.rows = legacy_rows(v)
    old = conn.rows[1]["secret"]
    fresh = v.seal("tok-rotated", "teslafi", 2)
    conn.rows[1]["secret"] = fresh  # rotated between the read and the write
    sources.rebind(conn, [(2, old, v.seal("tok-2", "teslafi", 2))])
    assert conn.rows[1]["secret"] == fresh


@pytest.mark.skipif(not os.environ.get("CC_TEST_PG_URL"), reason="needs a scratch Postgres: CC_TEST_PG_URL")
def test_against_postgres_the_site_waits_for_the_connectors_binding_and_writes_nothing_stale():
    """Two connections, as the connector and the site at the same start."""
    import threading
    import time

    import psycopg
    from psycopg.rows import dict_row

    url = os.environ["CC_TEST_PG_URL"]
    v = vault()
    with psycopg.connect(url, autocommit=True) as setup:
        setup.execute("CREATE TABLE IF NOT EXISTS cc_sources (id BIGSERIAL PRIMARY KEY, kind TEXT, config JSONB DEFAULT '{}', secret BYTEA, enabled BOOLEAN DEFAULT TRUE)")
        setup.execute("CREATE TABLE IF NOT EXISTS cc_state (key TEXT PRIMARY KEY, value TEXT)")
        setup.execute("DELETE FROM cc_state WHERE key = %s", (sealing.DONE_KEY,))
        sid = setup.execute("INSERT INTO cc_sources (kind, secret) VALUES ('tesla', %s) RETURNING id", (v._fernet.encrypt(b"rt-old"),)).fetchone()[0]
    cc = psycopg.connect(url)
    cc.execute("SELECT pg_advisory_xact_lock(%s)", (sealing.LOCK_KEY,))  # the connector is binding
    got = {}

    def site():
        with psycopg.connect(url, row_factory=dict_row) as conn:
            got["n"] = sources.bind_sealed(conn, v)
            conn.commit()

    t = threading.Thread(target=site)
    t.start()
    time.sleep(1.0)
    assert t.is_alive(), "the site waits for the connector's lock"
    cc.execute("UPDATE cc_sources SET secret = %s WHERE id = %s", (v.seal("rt-rotated", "tesla", sid), sid))
    cc.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (sealing.DONE_KEY, sealing.DONE_VALUE))
    cc.commit()
    t.join(10)
    assert got == {"n": 0}
    with psycopg.connect(url) as check:
        blob = check.execute("SELECT secret FROM cc_sources WHERE id = %s", (sid,)).fetchone()[0]
    assert v.open(bytes(blob), "tesla", sid) == "rt-rotated"
    cc.close()
