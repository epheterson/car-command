"""The schema statements run once at startup, against whatever database is there.

On an installed system every table already exists, so a statement in the wrong order is
invisible: `ALTER TABLE x` before `CREATE TABLE IF NOT EXISTS x` succeeds on a database
that has run before and fails only on an empty one. One bad ordering —
`cc_charge_facts` altered a line before it was created — would stop the site starting
on a fresh install at all.

These checks read the DDL as text. They need no database, which is the point: the bug
only appears where there is no database to test against.
"""

import re
from pathlib import Path

import pytest

HERE = Path(__file__).parent
CREATE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)", re.I)
ALTER = re.compile(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w]*)", re.I)
INDEX = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?[\w]+\s+ON\s+([A-Za-z_][\w]*)", re.I)


def ddl_blocks() -> list[tuple[str, str, str]]:
    """(module, constant name, sql) for every DDL string in the package."""
    found = []
    for path in sorted(HERE.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        text = path.read_text()
        for match in re.finditer(r"^([A-Z][A-Z0-9_]*)\s*=\s*(\"\"\"|''')(.*?)\2", text, re.S | re.M):
            if "DDL" not in match.group(1):
                continue
            found.append((path.name, match.group(1), match.group(3)))
        for match in re.finditer(r"conn\.execute\(\s*(\"\"\"|''')(\s*CREATE.*?)\1", text, re.S):
            found.append((path.name, "inline", match.group(2)))
    return found


def test_there_are_ddl_blocks_to_check():
    """A scan that silently finds nothing is worse than no scan."""
    blocks = ddl_blocks()
    assert len(blocks) >= 5, [b[:2] for b in blocks]
    names = {name for _, name, _ in blocks}
    assert "DDL" in names or any(n.endswith("DDL") for n in names)


@pytest.mark.parametrize("module,const,sql", ddl_blocks(), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "")
def test_a_table_is_created_before_it_is_altered(module, const, sql):
    created: set[str] = set()
    for statement in sql.split(";"):
        create = CREATE.search(statement)
        if create:
            created.add(create.group(1).lower())
            continue
        for pattern, verb in ((ALTER, "ALTER TABLE"), (INDEX, "CREATE INDEX ON")):
            hit = pattern.search(statement)
            if hit and hit.group(1).lower() not in created:
                pytest.fail(
                    f"{module}:{const} runs `{verb} {hit.group(1)}` before creating it. "
                    f"That works on a database that has run before and fails on an empty one, "
                    f"which is the only kind a new install has."
                )


def test_every_ddl_statement_is_safe_to_run_twice():
    """Startup runs these on every boot, so each must be idempotent."""
    offenders = []
    for module, const, sql in ddl_blocks():
        for statement in sql.split(";"):
            s = " ".join(statement.split())
            if not s:
                continue
            if re.match(r"CREATE\s+TABLE\s", s, re.I) and not re.match(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS", s, re.I):
                offenders.append(f"{module}:{const}: {s[:70]}")
            if re.match(r"ALTER\s+TABLE\s+\w+\s+ADD\s+COLUMN\s", s, re.I) and "IF NOT EXISTS" not in s.upper():
                offenders.append(f"{module}:{const}: {s[:70]}")
            if re.match(r"CREATE\s+(UNIQUE\s+)?INDEX\s", s, re.I) and "IF NOT EXISTS" not in s.upper():
                offenders.append(f"{module}:{const}: {s[:70]}")
    assert not offenders, "these fail on the second start:\n  " + "\n  ".join(offenders)


def test_the_scan_would_catch_the_bug_it_was_written_for():
    """The real thing, as it stood in facts.py: an ALTER one line above its CREATE."""
    broken = """
    ALTER TABLE cc_charge_facts ADD COLUMN IF NOT EXISTS range_added DOUBLE PRECISION;
    CREATE TABLE IF NOT EXISTS cc_charge_facts (session_id INTEGER PRIMARY KEY);
    """
    created: set[str] = set()
    failed = False
    for statement in broken.split(";"):
        create = CREATE.search(statement)
        if create:
            created.add(create.group(1).lower())
            continue
        hit = ALTER.search(statement)
        if hit and hit.group(1).lower() not in created:
            failed = True
    assert failed, "the check must reject the ordering that broke every fresh install"


# Tables owned by the CarConnectivity database plugin, not by this code. It creates them
# before the site ever runs, so nothing here declares them.
PLUGIN_TABLES = {
    # Verbatim from the plugin's own metadata, not from memory: the first version of this
    # invented three tables that do not exist and missed two that do.
    "battery_temperatures",
    "charging_powers",
    "charging_rates",
    "charging_sessions",
    "charging_sessions_tags",
    "charging_states",
    "charging_stations",
    "climatization_states",
    "connection_states",
    "drive_consumptions",
    "drive_levels",
    "drive_ranges",
    "drive_ranges_estimated_full",
    "drives",
    "locations",
    "outside_temperatures",
    "refuel_sessions",
    "refuel_sessions_tags",
    "states",
    "tags",
    "trips",
    "trips_tags",
    "vehicles",
}
# `IS DISTINCT FROM x` is a comparison, not a table reference.
QUERIED = re.compile(r"(?<!DISTINCT )\b(?:FROM|JOIN|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_][a-z0-9_]*)", re.I)
SQL_COMMENT = re.compile(r"--[^\n]*")
# EXTRACT(year FROM c.session_start_date) is not a query against a table called "c".
# SQL spells a few function arguments with keywords, and FROM is one of them, so the
# call is removed before anything looks for table names. Nested parentheses are left
# alone deliberately: EXTRACT(EPOCH FROM (a - b)) puts a bracket where a name would go,
# which the name pattern already declines to match.
SQL_FROM_ARG = re.compile(r"\b(?:EXTRACT|SUBSTRING|TRIM|POSITION|OVERLAY)\s*\([^()]*\)", re.I)
SQL_KEYWORDS = {"select", "lateral", "unnest", "generate_series", "values", "only", "set"}
CTE = re.compile(r"(?:WITH|,)\s+([a-z_][a-z0-9_]*)\s+AS\s*\(", re.I)


def created_tables() -> set[str]:
    return {m.group(1).lower() for _, _, sql in ddl_blocks() for m in CREATE.finditer(sql)}


# A literal is SQL when it *starts* with a SQL verb. Merely containing one matches every
# docstring that says "select", and a check that reports prose gets switched off.
SQL_VERB = re.compile(r"^\s*(WITH|SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE|ALTER)\b", re.I)


def sql_literals(source: str) -> list[str]:
    """Every string constant in the file, via the parser rather than a quote-matching
    regex. An apostrophe in a comment — "CarConnectivity's" — desynchronised the regex
    and made it report tables that appear nowhere, which is the fastest way to get a
    check ignored."""
    import ast

    return [node.value for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


# Modules whose SELECTs run against somebody else's database: TeslaMate's positions,
# charges and states live in TeslaMate's Postgres and are read, never created, here.
FOREIGN_DB_MODULES = {"teslamate_live.py"}


def queried_tables() -> dict[str, set[str]]:
    """table -> the modules that query it."""
    out: dict[str, set[str]] = {}
    for path in sorted(HERE.glob("*.py")):
        if path.name.startswith("test_") or path.name in FOREIGN_DB_MODULES:
            continue
        for body in sql_literals(path.read_text()):
            if not SQL_VERB.search(body):
                continue
            # A SQL comment saying "facts from before the split" is not a query.
            body = SQL_FROM_ARG.sub(" ", SQL_COMMENT.sub(" ", body))
            # Names introduced by a WITH clause are not tables; they exist for the
            # length of the statement and no DDL will ever create them.
            ctes = {c.lower() for c in CTE.findall(body)}
            for m in QUERIED.finditer(body):
                name = m.group(1).lower()
                if name not in SQL_KEYWORDS and name not in ctes:
                    out.setdefault(name, set()).add(path.name)
    return out


def test_every_table_the_site_reads_is_one_this_code_creates():
    """The third instance of one bug: cc_inside_temperatures was created only by the
    importer, so /day returned 500 on any install that had never imported anything.
    A table the site queries has to be created by something that runs at startup."""
    created, missing = created_tables(), {}
    for table, modules in queried_tables().items():
        if table in created or table in PLUGIN_TABLES or table.startswith("pg_"):
            continue
        missing[table] = sorted(modules)
    assert not missing, "queried but never created here:\n  " + "\n  ".join(f"{t} — read by {', '.join(m)}" for t, m in sorted(missing.items()))


def test_the_startup_path_runs_every_ddl_block():
    """Creating a table in a module nobody executes at boot is the same bug again."""
    app_src = (HERE / "app.py").read_text()
    start = app_src.index("def apply_schema(")
    lifespan = app_src[start : app_src.index("async def lifespan", start)]
    for module, const, _ in ddl_blocks():
        if const == "inline" or module in ("teslafi_import.py", "teslamate_import.py", "teslafi_live.py"):
            continue
        assert const in lifespan or f"{module[:-3]}.DDL" in lifespan or "DDL" in lifespan, f"{module}:{const} is never run at startup"


def test_the_geocoder_decision_is_written_before_the_warm_up():
    """The connector names nothing until the site writes a decision, so the decision has
    to be made and written before anything else starts, including the driveprint
    warm-up that used to come first."""
    app_src = (HERE / "app.py").read_text()
    start = app_src.index("async def lifespan(")
    end = app_src.index("geo_task = asyncio.create_task", start)
    body = app_src[start:end]
    call = "asyncio.to_thread(geocoder_tick, 0)"
    assert "await " + call in body, "the startup decision must be awaited before the loop exists"
    assert body.index(call) < body.index("target=warm_driveprints"), "it must run before the warm-up"


def test_the_startup_schema_cannot_hold_the_site_shut():
    """A CREATE or ALTER that cannot get its lock queues, and every reader queues behind
    it. That can turn a deploy into a site-wide outage while a backfill
    holds a table. The schema step gives up
    instead of waiting."""
    src = (HERE / "app.py").read_text()
    body = src[src.index("def apply_schema(") : src.index("async def lifespan", src.index("def apply_schema("))]
    assert "SET LOCAL lock_timeout" in body, "the schema step can still queue forever (LOCAL: a rollback undoes a plain SET)"
    assert "LockNotAvailable" in body, "a lock timeout raises; something has to catch it"
    assert "conn.rollback()" in body, "a failed statement leaves the transaction unusable"


def test_what_has_already_been_sent_survives_a_restart():
    """A daily summary went out twice in one day because the marker for 'already sent'
    was a module-level dict and every deploy reset it. Notification state belongs in the
    database, not in the process that keeps being replaced."""
    src = (HERE / "app.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS cc_state" in src
    assert 'key = f"last_summary:{car[\'vin\']}"' in src and "state_set(key, datetime.now(TZ)" in src, "the summary marker is not persisted, a car"
    assert 'state_set(key, now_local.date().isoformat())' in src and 'f"plug_reminder:{' in src, "the plug-in reminder marker is not persisted; a restart would repeat it"
    assert '_last_summary' not in src, "the in-memory marker is still there"


def test_the_scanner_is_not_fooled_by_an_apostrophe_in_a_comment():
    """It used to match quotes with a regex, so writing "CarConnectivity's" in a Python
    comment desynchronised it and it reported tables that appear nowhere. It parses the
    file now. A check that cries wolf is a check that gets switched off."""
    source = "# CarConnectivity's model\nQ = \"SELECT 1 FROM cc_places\"\n"
    assert sql_literals(source) == ["SELECT 1 FROM cc_places"]


def test_a_sql_comment_is_not_a_query():
    body = "SELECT 1 FROM cc_places -- facts from before the split\n"
    body = SQL_COMMENT.sub(" ", body)
    assert [m.group(1) for m in QUERIED.finditer(body)] == ["cc_places"]


def test_is_distinct_from_is_not_a_table():
    body = "SELECT a FROM cc_software WHERE prev IS DISTINCT FROM version"
    assert [m.group(1) for m in QUERIED.finditer(body)] == ["cc_software"]


def test_a_car_idling_on_the_driveway_is_not_listed_as_a_drive():
    """The database plugin opens a trip the moment the state leaves park, so sitting in
    the car with the ignition on made a ninety-minute, zero-mile drive. Twice in two days.
    The Day page already counts those minutes as idle, so they only need keeping out of
    the lists."""
    src = (HERE / "app.py").read_text()
    assert "def moved_sql()" in src
    # Every query that lists or totals drives has to apply it, or the lists disagree.
    # a point lookup by id is not a listing; the Hidden list is the one listing that is
    # meant to show what the clause hides, so it names cc_hidden itself
    listing = [line for line in src.splitlines() if "TRIP_SELECT +" in line and "t.id = %s" not in line and "cc_hidden" not in line]
    assert listing, "no drive listings found"
    for line in listing:
        assert "moved_sql()" in line, f"a drive listing without the filter: {line.strip()[:90]}"
    assert "{moved_sql()}" in src, "the stats totals do not apply it"


def test_an_unfinished_drive_counts_once_the_car_moves():
    """A drive in progress has no destination odometer yet. It shows once the car's newest
    odometer has moved past the threshold, so a moving car never vanishes from the list; ignition
    on in Park (no movement) is idling, not a drive. A car with no live odometer keeps it."""
    src = (HERE / "app.py").read_text()
    body = src[src.index("def moved_sql()") : src.index("def charged_sql()")]
    assert "t.destination_odometer IS NULL AND" in body and "t.start_odometer IS NULL" in body
    assert "FROM cc_positions p WHERE p.vin = t.vin AND p.odometer IS NOT NULL ORDER BY p.at DESC LIMIT 1" in body, "the newest fix, by the table's index"
    assert "- t.start_odometer >= {floor}, TRUE)" in body, "no live odometer must keep the drive"


def test_the_two_places_that_price_a_rated_mile_agree():
    """One rule, written twice: rated_mile_kwh() applies it row by row in Python, and the
    Stats aggregation writes it into SQL because it adds up eight years in the database
    rather than loading them. If one prefers the drive's own year and the other does not,
    the Stats table and every page beside it quietly disagree about the same drives."""
    app_src = (HERE / "app.py").read_text()
    start = app_src.index("def summarize_sql(")
    sql = app_src[start : app_src.index("\ndef ", start + 1)]
    assert "cc_vehicle_year_facts vy ON vy.vin = t.vin AND vy.year = EXTRACT(year FROM t.start_date AT TIME ZONE" in sql, "the aggregation does not match the drive to its own year"
    assert "vf.kwh_per_rated_mi IS NOT NULL" not in sql, "the aggregation still reads the all-time figure without falling back to it"
    assert sql.count("COALESCE(vy.kwh_per_rated_mi, vf.kwh_per_rated_mi)") == 4, "every use has to fall back to the all-time figure, energy and cost alike"

    units_src = (HERE / "units.py").read_text()
    picker = units_src[units_src.index("def rated_mile_kwh(") : units_src.index("def drive_kwh(")]
    assert "by_year.get((vin, local.year))" in picker and "return all_time" in picker


def test_no_table_created_at_startup_references_another_with_a_foreign_key():
    """pg_restore --clean of a dump taken before a table existed drops the referenced
    table's primary key and fails on the dependency, so an upgrade-with-data restore
    dies. Found by a staging run: cc_service_files pointed at cc_service.
    Deletes are done by the routes instead."""
    import re

    src = (HERE / "app.py").read_text()
    for block in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\);", src, re.S):
        assert "REFERENCES" not in block[1], f"{block[0]} carries a foreign key; a restore of an older dump cannot drop what it depends on"


def test_the_lifespan_is_the_context_manager_not_the_schema_step():
    """The decorator sat on apply_schema unnoticed: the schema still ran (the helper calls
    the function as it wraps it) and Starlette fell back to its deprecated async-generator
    path for the lifespan, which a future Starlette drops, and the site with it."""
    src = (HERE / "app.py").read_text()
    assert "@asynccontextmanager\nasync def lifespan(" in src and "@asynccontextmanager\ndef apply_schema(" not in src


def test_the_schema_step_and_the_connectors_grants_take_turns():
    """The connector's supervisor revokes and grants on every table at its start while the site
    alters its own; neither locks the other out, and the catalogue row both rewrite answered
    'tuple concurrently updated' on a first start. Each schema transaction here holds the same
    advisory lock the grants hold (stack/cc/plugin_role.py GRANTS_LOCK), under the timeout."""
    import re

    src = (HERE / "app.py").read_text()
    body = src[src.index("def apply_schema(") : src.index("async def lifespan", src.index("def apply_schema("))]
    assert body.index("SET LOCAL lock_timeout") < body.index("pg_advisory_xact_lock") < body.index("conn.execute(ddl)")
    ours = int(re.search(r"^GRANTS_LOCK = (0x[0-9a-fA-F_]+)", src, re.M).group(1), 16)
    theirs = int(re.search(r"^GRANTS_LOCK = (0x[0-9a-fA-F_]+)", (HERE.parent / "cc" / "plugin_role.py").read_text(), re.M).group(1), 16)
    assert ours == theirs


def test_each_car_gets_its_own_daily_summary(monkeypatch):
    """One shared marker meant the first car's summary marked the day sent for every car."""
    for k, v in (("DATABASE_URL", "postgresql://u:p@localhost:1/x"), ("REST_USER", "u"), ("REST_PASSWORD", "p")):
        monkeypatch.setenv(k, v)
    import app

    store, sent = {"last_summary": "2026-10-12"}, []
    monkeypatch.setattr(app, "state_get", lambda k: store.get(k))
    monkeypatch.setattr(app, "state_set", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(app, "summary_due", lambda now, last, at: None if last and last.isoformat() == now.date().isoformat() else "day")
    monkeypatch.setattr(app, "compose_daily_summary", lambda car, due: car["vin"])
    monkeypatch.setattr(app.recorder, "notify", lambda text, kind, vin=None: sent.append(text))
    for vin in ("ALPHA", "BRAVO", "ALPHA", "BRAVO"):
        app.maybe_send_summary({"vin": vin})
    assert sent == ["ALPHA", "BRAVO"], "each car once, none twice"
    store2, sent2 = {"last_summary": app.datetime.now(app.TZ).date().isoformat()}, []
    monkeypatch.setattr(app, "state_get", lambda k: store2.get(k))
    monkeypatch.setattr(app.recorder, "notify", lambda text, kind, vin=None: sent2.append(text))
    app.maybe_send_summary({"vin": "CHARLIE"})
    assert sent2 == [], "a car with no marker of its own reads the shared one, so the switch sends nothing twice"


def test_no_daily_summary_for_a_car_without_a_live_source_or_a_day_with_nothing(monkeypatch):
    for k, v in (("DATABASE_URL", "postgresql://u:p@localhost:1/x"), ("REST_USER", "u"), ("REST_PASSWORD", "p")):
        monkeypatch.setenv(k, v)
    import app

    monkeypatch.setattr(app, "source_of", lambda car: "history")
    assert app.compose_daily_summary({"vin": "ALPHA", "name": "Alpha"}, app.date(2026, 9, 4)) is None
    store, sent = {}, []
    monkeypatch.setattr(app, "state_get", lambda k: store.get(k))
    monkeypatch.setattr(app, "state_set", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(app, "summary_due", lambda now, last, at=None: app.date(2026, 9, 4))  # due whatever the clock says
    monkeypatch.setattr(app, "compose_daily_summary", lambda car, due: None)
    monkeypatch.setattr(app.recorder, "notify", lambda text, kind, vin=None: sent.append(text))
    monkeypatch.setattr(app, "SUMMARY_AT", app.dtime(0, 0))  # due at any hour: the test would fail before 07:00 local
    app.maybe_send_summary({"vin": "ALPHA"})
    assert sent == [] and "last_summary:ALPHA" in store, "nothing sent, and not retried all day"


def test_staging_subnets_avoid_the_compose_defaults_and_env_example_leaves_the_stack_subnet_commented():
    import re

    root = HERE.parent.parent
    compose = (root / "stack" / "docker-compose.yml").read_text()
    prod = set(re.findall(r"CC_[A-Z_]*SUBNET:-([\d.]+/\d+)", compose))
    staging = set(re.findall(r'CC_STAGING_[A-Z_]*SUBNET:-([\d.]+/\d+)', (root / "scripts" / "staging.sh").read_text()))
    assert len(prod) == 4 and len(staging) == 4 and not prod & staging
    assert not re.search(r"^CC_SUBNET=", (root / "stack" / ".env.example").read_text(), re.M)
