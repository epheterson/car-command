#!/usr/bin/env python3
"""CarConnectivity's database plugin gets its own Postgres role, with its own tables only.

The plugin (and so every connector and plugin running beside it, as the unprivileged user)
needs a database login in its config. With the main role, that login could read and write
cc_sources: sealed secrets, which a process holding it could move between rows. So it gets
`cc_plugin` instead: SELECT, INSERT, UPDATE and DELETE on the plugin's own tables and their
sequences, nothing on cc_sources, cc_state or any other Car Command table, no CREATE.

The supervisor runs this as root, with the main role's URL, before every start of
CarConnectivity, with a fresh password each time (it is never stored; the supervisor keeps
it in its root-only secrets dir and writes it only into the child's 0600 config). First it
brings the plugin's schema to its head with the main role, exactly as the plugin would on
its own start (create_all, then Alembic stamp or upgrade), since the plugin's role can no
longer create anything.

    DATABASE_URL=postgresql://... PLUGIN_DB_PASSWORD=... python3 -s plugin_role.py

Exit codes: 0 done; 2 nothing to work with; 3 the database or the plugin refused.
Nothing secret is ever printed.
"""

from __future__ import annotations

import logging
import os
import sys

ROLE = "cc_plugin"
# Advisory locks shared with the other startup steps, so none of them rewrites a catalogue row
# another has in hand ("tuple concurrently updated"). SCHEMA_LOCK: whoever creates the plugin's
# tables (init_db.py, migrate below). GRANTS_LOCK: the grants below and the site's schema step
# (stack/web/app.py apply_schema), which alters the site's tables while REVOKE ALL rewrites them.
SCHEMA_LOCK = 0x6363_7363_686D  # "ccschm"
GRANTS_LOCK = 0x6363_6772_6E74  # "ccgrnt"


def statements(role: str, dbname: str, tables: list[str], sequences: list[str]) -> list[tuple]:
    """(sql template, identifiers, params): the role, everything taken away, then the
    plugin's tables and sequences given back. Pure, for the tests."""
    out = [
        # SUPERUSER, CREATEDB, REPLICATION and BYPASSRLS are off from CREATE ROLE and only a role
        # holding them may set them, even to off; the one container's site role holds none.
        ("ALTER ROLE {} WITH LOGIN NOCREATEROLE NOINHERIT PASSWORD %s", (role,), ("PASSWORD",)),
        ("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}", (role,), ()),
        ("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}", (role,), ()),
        ("REVOKE CREATE ON SCHEMA public FROM {}", (role,), ()),
        ("GRANT CONNECT ON DATABASE {} TO {}", (dbname, role), ()),
        ("GRANT USAGE ON SCHEMA public TO {}", (role,), ()),
    ]
    out += [("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {} TO {}", (t, role), ()) for t in sorted(tables)]
    out += [("GRANT USAGE, SELECT, UPDATE ON SEQUENCE {} TO {}", (s, role), ()) for s in sorted(sequences)]
    return out


def plugin_tables(metadata) -> list[str]:
    names = set(metadata.tables) | {"alembic_version"}
    if any(n.startswith("cc_") for n in names):
        raise SystemExit("plugin_role: a plugin table is named like Car Command's own; refusing to grant it")
    return sorted(names)


def migrate(url: str):
    """The plugin's schema at its head, with the main role: what the plugin does on start."""
    import sqlalchemy

    import init_db
    from carconnectivity_plugins.database.model.migrations import run_database_migrations

    metadata = init_db.plugin_metadata()
    engine = sqlalchemy.create_engine(url)
    with init_db.holding(engine, SCHEMA_LOCK):
        had = sqlalchemy.inspect(engine).has_table("alembic_version")
        metadata.create_all(engine)
        run_database_migrations(dsn=url, stamp_only=not had)
    return metadata


def apply(conn, role: str, password: str, dbname: str, tables: list[str]) -> None:
    from psycopg2 import sql

    cur = conn.cursor()
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (GRANTS_LOCK,))  # until the commit below
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    if not cur.fetchone():
        cur.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(%s)", (tables,))
    present = [r[0] for r in cur.fetchall()]
    cur.execute(
        "SELECT s.relname FROM pg_class s JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i') " "JOIN pg_class t ON d.refobjid = t.oid WHERE s.relkind = 'S' AND t.relname = ANY(%s)",
        (present,),
    )
    sequences = [r[0] for r in cur.fetchall()]
    for template, idents, params in statements(role, dbname, present, sequences):
        query = sql.SQL(template).format(*[sql.Identifier(i) for i in idents])
        cur.execute(query, tuple(password if p == "PASSWORD" else p for p in params))
    conn.commit()


def setup(env, run=None, secrets_dir: str | None = None, script: str | None = None) -> tuple[str, str] | None:
    """What the supervisor calls (as root): this script in a fresh interpreter with the main
    role's URL and a new password. (role, password) when it worked, None when not; the
    supervisor then keeps other brands out until it does."""
    import secrets
    import subprocess
    from urllib.parse import quote

    run = run or subprocess.run
    password = secrets.token_urlsafe(24)
    url = f"postgresql://{quote(env.get('DB_USER', ''), safe='')}:{quote(env.get('DB_PASSWORD', ''), safe='')}@{env.get('DB_HOST', '')}:5432/{env.get('DB_NAME', '')}"
    child = {k: env[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if k in env}
    child.update(DATABASE_URL=url, PLUGIN_DB_PASSWORD=password, PYTHONNOUSERSITE="1", PYTHONPATH=os.path.dirname(os.path.abspath(__file__)))
    try:
        done = run([sys.executable, "-s", script or os.path.abspath(__file__)], env=child, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        logging.getLogger("supervise").warning("plugin_role: no answer in three minutes")
        return None
    if done.returncode != 0:
        tail = (done.stderr or "").strip().splitlines()
        logging.getLogger("supervise").warning("%s", tail[-1] if tail else "plugin_role: failed")
        return None
    if secrets_dir:
        path = os.path.join(secrets_dir, "PLUGIN_DB_PASSWORD")
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o400)
        with os.fdopen(fd, "w") as fh:
            fh.write(password)
    return ROLE, password


def main() -> int:
    url, password = os.environ.get("DATABASE_URL"), os.environ.get("PLUGIN_DB_PASSWORD")
    if not url or not password:
        print("plugin_role: DATABASE_URL and PLUGIN_DB_PASSWORD are both needed", file=sys.stderr)
        return 2
    sa_url = url.replace("postgresql://", "postgresql+psycopg2://", 1) if url.startswith("postgresql://") else url
    try:
        import psycopg2
        from psycopg2.extensions import parse_dsn

        metadata = migrate(sa_url)
        conn = psycopg2.connect(url, connect_timeout=10)
        try:
            apply(conn, ROLE, password, parse_dsn(url)["dbname"], plugin_tables(metadata))
        finally:
            conn.close()
    except SystemExit:
        raise
    except Exception as err:  # noqa: BLE001 - never the URL, which carries the main password
        print(f"plugin_role: could not set up the plugin's role ({err.__class__.__name__})", file=sys.stderr)
        return 3
    print(f"plugin_role: {ROLE} may use the plugin's tables and nothing else")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main())
