"""CarConnectivity's database plugin logs in as a role that can reach its own tables and nothing else."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import plugin_role  # noqa: E402

TABLES = ["alembic_version", "drives", "vehicles"]


def test_the_role_is_stripped_first_then_given_only_the_plugins_tables():
    got = plugin_role.statements("cc_plugin", "carcommand", TABLES, ["drives_id_seq"])
    sqls = [t for t, _i, _p in got]
    assert sqls[0].startswith("ALTER ROLE {} WITH LOGIN NOCREATEROLE NOINHERIT") and got[0][2] == ("PASSWORD",)
    # Attributes only a superuser may set, even to off; CREATE ROLE leaves them off, and the
    # one container's site role, which runs this, is not a superuser.
    assert not any(a in sqls[0] for a in ("SUPERUSER", "REPLICATION", "BYPASSRLS", "CREATEDB"))
    revokes = [i for i, t in enumerate(sqls) if t.startswith("REVOKE")]
    grants = [i for i, t in enumerate(sqls) if t.startswith("GRANT")]
    assert revokes and max(revokes) < min(grants), "everything taken away before anything is given"
    tables = sorted(i[0] for t, i, _p in got if "ON TABLE" in t)
    assert tables == TABLES and not any(x.startswith("cc_") and x != "cc_plugin" for _t, i, _p in got for x in i)
    assert ("GRANT USAGE, SELECT, UPDATE ON SEQUENCE {} TO {}", ("drives_id_seq", "cc_plugin"), ()) in got
    assert not any("CREATE" in t and "REVOKE" not in t for t in sqls[1:]), "no CREATE anywhere"


def test_a_plugin_table_named_like_ours_is_refused():
    class Meta:
        tables = {"vehicles": 1, "cc_sources": 2}

    with pytest.raises(SystemExit):
        plugin_role.plugin_tables(Meta())


def test_setup_runs_the_script_with_the_main_url_and_keeps_the_new_password_root_only(tmp_path):
    seen = {}

    def run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return subprocess.CompletedProcess(cmd, 0, "plugin_role: ok", "")

    env = {"DB_USER": "carcommand", "DB_PASSWORD": "example pw/with:odd@chars", "DB_HOST": "db", "DB_NAME": "cc", "PATH": "/bin", "CC_SECRET_KEY": "k"}
    role, password = plugin_role.setup(env, run=run, secrets_dir=str(tmp_path))
    assert role == "cc_plugin" and len(password) >= 24 and password != plugin_role.setup(env, run=run)[1], "a new one every start"
    assert seen["env"]["DATABASE_URL"] == "postgresql://carcommand:example%20pw%2Fwith%3Aodd%40chars@db:5432/cc"
    assert "CC_SECRET_KEY" not in seen["env"] and seen["env"]["PYTHONNOUSERSITE"] == "1" and seen["cmd"][1] == "-s"
    kept = tmp_path / "PLUGIN_DB_PASSWORD"
    assert oct(kept.stat().st_mode & 0o777) == "0o400"
    failed = plugin_role.setup(env, run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 3, "", "plugin_role: could not set up the plugin's role (OperationalError)"))
    assert failed is None


def test_the_script_says_what_it_needs_and_never_the_url():
    r = subprocess.run([sys.executable, str(HERE / "plugin_role.py")], env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert r.returncode == 2
    r = subprocess.run(
        [sys.executable, str(HERE / "plugin_role.py")],
        env={"PATH": "/usr/bin:/bin", "DATABASE_URL": "postgresql://u:hunter2-main@nowhere:5432/x", "PLUGIN_DB_PASSWORD": "p"},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 3 and "hunter2" not in r.stdout + r.stderr


@pytest.mark.skipif(not os.environ.get("CC_TEST_PG_URL"), reason="needs a scratch Postgres: CC_TEST_PG_URL=postgresql://superuser:pw@host:5432/db")
def test_against_postgres_the_plugin_role_cannot_touch_car_commands_tables():
    """Run inside the connector image against a scratch database (see docs/sources.md)."""
    import psycopg2

    url = os.environ["CC_TEST_PG_URL"]
    main = psycopg2.connect(url)
    main.autocommit = True
    cur = main.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS cc_sources (id BIGSERIAL PRIMARY KEY, kind TEXT, secret BYTEA)")
    cur.execute("CREATE TABLE IF NOT EXISTS cc_state (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("INSERT INTO cc_sources (kind, secret) VALUES ('tesla', 'sealed')")
    done = subprocess.run([sys.executable, "-s", str(HERE / "plugin_role.py")], env={**os.environ, "DATABASE_URL": url, "PLUGIN_DB_PASSWORD": "pw-plugin-1"}, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    host = psycopg2.extensions.parse_dsn(url)
    plugin = psycopg2.connect(host=host["host"], port=host.get("port", 5432), dbname=host["dbname"], user="cc_plugin", password="pw-plugin-1")
    plugin.autocommit = True
    pc = plugin.cursor()
    pc.execute("SELECT count(*) FROM vehicles")  # its own tables: yes
    pc.execute("INSERT INTO vehicles (vin) VALUES ('ALPHA000000000001') ON CONFLICT DO NOTHING")
    pc.execute("SELECT version_num FROM alembic_version")
    pc.fetchall()  # stamped by the main role (empty while the plugin ships no migrations), readable by the plugin
    for sql in ("SELECT * FROM cc_sources", "UPDATE cc_sources SET secret = NULL", "SELECT * FROM cc_state", "CREATE TABLE x (y int)"):
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            pc.execute(sql)
    print(json.dumps({"checked": True}))


class _Cursor:
    def __init__(self, log):
        self.log, self.rows = log, []

    def execute(self, query, params=None):
        text = query if isinstance(query, str) else repr(query)
        self.log.append((text, params))
        self.rows = [("cc_plugin",)] if "pg_roles" in text else []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self):
        self.log = []

    def cursor(self):
        return _Cursor(self.log)

    def commit(self):
        self.log.append(("COMMIT", None))


def test_the_grants_wait_for_the_sites_schema_step(monkeypatch):
    """REVOKE ALL ON ALL TABLES rewrites every table's catalogue row, the site's too, and takes
    no lock on them; the site's schema step altering one of its tables at the same moment made
    the first start of the one container fail with 'tuple concurrently updated'. Both hold one
    advisory lock for the length of their transaction."""
    import types

    class _SQL(str):
        def format(self, *parts):
            return _SQL(str.format(self, *parts))

    fake = types.ModuleType("psycopg2")
    fake.sql = types.SimpleNamespace(SQL=_SQL, Identifier=lambda name: f'"{name}"')
    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    monkeypatch.setitem(sys.modules, "psycopg2.sql", fake.sql)
    conn = _Conn()
    plugin_role.apply(conn, "cc_plugin", "pw", "carcommand", TABLES)
    first = conn.log[0]
    assert "pg_advisory_xact_lock" in first[0] and first[1] == (plugin_role.GRANTS_LOCK,), conn.log[:2]
    assert conn.log[-1][0] == "COMMIT"


def test_the_schema_is_made_under_one_lock_by_whoever_makes_it():
    """The schema container and the connector's start both create the plugin's tables; on a
    fresh compose install they start together. One session lock, the same in both."""
    import init_db

    src = (HERE / "init_db.py").read_text()
    assert "pg_advisory_lock" in src and "SCHEMA_LOCK" in src
    assert init_db.SCHEMA_LOCK == plugin_role.SCHEMA_LOCK and plugin_role.SCHEMA_LOCK != plugin_role.GRANTS_LOCK
    body = (HERE / "plugin_role.py").read_text()
    body = body[body.index("def migrate(") : body.index("def apply(")]
    assert "init_db.holding(engine, SCHEMA_LOCK)" in body
