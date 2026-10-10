#!/usr/bin/env python3
"""Create the CarConnectivity database plugin's schema, then exit.

The plugin owns these twenty-three tables and creates them when it starts. That is fine
for an install that logs a car live, and useless for one that only imports history: the
site reads `vehicles` and `trips` on every page, the importers write to them, and neither
exists until a connector has run. A fresh install answered 500 everywhere because of it.

So the schema gets made once, explicitly, by a container that exits — from the plugin's
own metadata rather than a second copy of the same DDL, which would drift.

Exit codes: 0 the schema is there; 2 no DATABASE_URL; 3 the database refused.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager

SCHEMA_LOCK = 0x6363_7363_686D  # "ccschm"; the same in plugin_role.py, which also creates these tables


@contextmanager
def holding(engine, key: int):
    """A session advisory lock on its own connection for the length of the block. On a fresh
    compose install this step and the connector's start create the same tables at once."""
    import sqlalchemy

    with engine.connect() as held:
        held.execute(sqlalchemy.text("SELECT pg_advisory_lock(:k)"), {"k": key})
        try:
            yield
        finally:
            held.execute(sqlalchemy.text("SELECT pg_advisory_unlock(:k)"), {"k": key})


def plugin_metadata():
    """The database plugin's tables. The plugin does not export a `Base`, and where the
    declarative base lives has moved between versions. Every mapped class carries the same
    MetaData, so take it from the first one that has it rather than depending on a name that
    is not part of the API."""
    from carconnectivity_plugins.database import model

    return next((getattr(model, n).metadata for n in dir(model) if hasattr(getattr(model, n, None), "metadata")), None)


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("init_db: DATABASE_URL is not set", file=sys.stderr)
        return 2

    # SQLAlchemy wants a driver in the scheme; the rest of the stack speaks psycopg.
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)

    try:
        import sqlalchemy

        from carconnectivity_plugins.database import model
    except ImportError as err:
        print(f"init_db: the database plugin is not installed here ({err})", file=sys.stderr)
        return 3

    del model
    metadata = plugin_metadata()
    if metadata is None or not metadata.tables:
        print("init_db: found the database plugin but no mapped tables in it", file=sys.stderr)
        return 3

    try:
        engine = sqlalchemy.create_engine(url)
        with holding(engine, SCHEMA_LOCK):
            before = set(sqlalchemy.inspect(engine).get_table_names())
            metadata.create_all(engine)
            after = set(sqlalchemy.inspect(engine).get_table_names())
    except Exception as err:  # noqa: BLE001 - any failure here is the same failure
        # Never print the URL: it carries the database password.
        print(f"init_db: could not create the schema ({err.__class__.__name__})", file=sys.stderr)
        return 3

    made = sorted(after - before)
    print(f"init_db: {len(after)} tables present" + (f", created {len(made)}: {', '.join(made)}" if made else ", nothing to do"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
