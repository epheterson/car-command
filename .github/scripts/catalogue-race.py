"""The connector's grants and the site's schema step at once, as a first start runs them.

    DATABASE_URL=postgresql://... python catalogue-race.py <stack dir> <rounds>

Thread A runs plugin_role.apply (REVOKE ALL ON ALL TABLES, then the grants) in a loop; thread B
drops the site's tables and runs app.apply_schema, round after round. Before the two shared
GRANTS_LOCK, a few rounds in forty failed with "tuple concurrently updated". Exits 1 on any
error from either side.
"""

import os
import sys
import threading
import traceback

stack, rounds = sys.argv[1], int(sys.argv[2])
sys.path[:0] = [os.path.join(stack, "web"), os.path.join(stack, "cc")]
os.environ.setdefault("REST_USER", "u")
os.environ.setdefault("REST_PASSWORD", "p")

import psycopg  # noqa: E402
import psycopg2  # noqa: E402

import app  # noqa: E402
import plugin_role  # noqa: E402

URL = os.environ["DATABASE_URL"]
app.SCHEMA_RETRY_S = 0.1
errors = {"grants": [], "schema": []}
stop = threading.Event()

with psycopg.connect(URL, autocommit=True) as c:
    c.execute("CREATE TABLE IF NOT EXISTS vehicles (vin VARCHAR PRIMARY KEY, name VARCHAR)")
    c.execute("CREATE TABLE IF NOT EXISTS drives (id SERIAL PRIMARY KEY, vin VARCHAR)")


def grants():
    conn = psycopg2.connect(URL)
    n = 0
    while not stop.is_set():
        try:
            plugin_role.apply(conn, "cc_plugin", "pw-trial", "carcommand", ["drives", "vehicles"])
        except Exception as err:  # noqa: BLE001
            conn.rollback()
            errors["grants"].append(f"{type(err).__name__}: {str(err).strip().splitlines()[0]}")
        n += 1
    print(f"grants ran {n} times")


def schema():
    for _ in range(rounds):
        with psycopg.connect(URL) as conn:
            # The reset is the harness, not a start: it holds the grants' lock so only
            # apply_schema itself is on trial.
            try:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (app.GRANTS_LOCK,))
                names = [r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'cc\\_%'").fetchall()]
                for name in names:
                    conn.execute(f'DROP TABLE IF EXISTS "{name}" CASCADE')
                conn.commit()
            except Exception as err:  # noqa: BLE001
                conn.rollback()
                errors.setdefault("reset (harness)", []).append(type(err).__name__)
                continue
            try:
                app.apply_schema(conn)
            except Exception as err:  # noqa: BLE001
                conn.rollback()
                errors["schema"].append(f"{type(err).__name__}: {str(err).strip().splitlines()[0]}")
    stop.set()


a = threading.Thread(target=grants)
a.start()
try:
    schema()
except Exception:
    traceback.print_exc()
    stop.set()
a.join()
for side, errs in errors.items():
    print(f"{side}: {len(errs)} error(s)")
    for e in sorted(set(errs)):
        print(f"  {errs.count(e)} x {e}")
concurrent = sum("concurrently updated" in e for side in ("grants", "schema") for e in errors[side])
print(f"tuple concurrently updated: {concurrent}")
sys.exit(1 if errors["grants"] or errors["schema"] else 0)
