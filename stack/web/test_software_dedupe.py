"""Only real software changes are stored and shown; the one-off cleanup finds the rest."""

from datetime import datetime, timedelta, timezone

from history import real_software_changes, software_dedupe, write_history

ALPHA = "5YJ3E1EA0JF000001"
BRAVO = "5YJ3E1EA0JF000002"
T0 = datetime(2026, 9, 4, 23, tzinfo=timezone.utc)
H = timedelta(hours=1)


class Conn:
    """cc_software as a list of dicts; answers the statements under test."""

    description = None  # no cursor columns: a statement it does not answer returns no rows (driveedits reads by column name)

    def __init__(self, rows=()):
        self.rows = [dict(id=i + 1, vin=ALPHA, import_id=None, **r) for i, r in enumerate(rows)]
        self.deleted, self.commits, self.last = [], 0, []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.last = []
        if s.startswith("SELECT version FROM cc_software"):
            vin, at = params
            prior = sorted((r for r in self.rows if r["vin"] == vin and r["seen_at"] <= at), key=lambda r: (r["seen_at"], r["id"]))
            self.last = [(prior[-1]["version"],)] if prior else []
        elif "LAG(version)" in s:
            keep = sorted((r for r in self.rows if not params or r["vin"] == params[0]), key=lambda r: (r["vin"], r["seen_at"], r["id"]))
            prev = {}
            for r in keep:
                if prev.get(r["vin"]) == r["version"]:
                    self.last.append((r["id"], r["vin"], r["version"], r["seen_at"], r["import_id"]))
                prev[r["vin"]] = r["version"]
        elif s.startswith("DELETE FROM cc_software WHERE id = ANY"):
            self.deleted.append(list(params[0]))
            self.rows = [r for r in self.rows if r["id"] not in params[0]]
        return self

    def fetchone(self):
        return self.last[0] if self.last else None

    def fetchall(self):
        return self.last

    def commit(self):
        self.commits += 1


def v(n, hours=0):
    return dict(version=n, seen_at=T0 + hours * H)


def test_a_replay_window_over_the_same_version_stores_nothing():
    conn = Conn([v("2026.26.6.5", -50)])
    assert real_software_changes(conn, ALPHA, [(T0, T0, "2026.26.6.5")]) == []
    assert real_software_changes(conn, ALPHA, [(T0 + 5 * H, T0 + 5 * H, "2026.26.6.5")]) == []


def test_the_first_ever_version_is_stored_once_however_many_times_the_window_repeats_it():
    rows = [(T0 + i * H, T0 + i * H, "2026.26.6.5") for i in range(4)]
    assert real_software_changes(Conn(), ALPHA, rows) == rows[:1]


def test_a_real_change_inside_the_window_is_stored_once():
    conn = Conn([v("2026.26.6.5", -50)])
    rows = [(T0, T0, "2026.26.6.5"), (T0 + H, T0 + H, "2026.26.6.5"), (T0 + 2 * H, T0 + 2 * H, "2026.26.7"), (T0 + 3 * H, T0 + 3 * H, "2026.26.7")]
    assert real_software_changes(conn, ALPHA, rows) == [rows[2]]


def test_write_history_copies_only_the_changes():
    class W(Conn):
        def __init__(self, rows):
            super().__init__(rows)
            self.copies, self.cur, self.sql_last = {}, None, ""

        def execute(self, sql, params=None):
            self.sql_last = sql.strip()
            if self.sql_last.startswith("SELECT id FROM drives"):
                self.last = [(1,)]
                return self
            return super().execute(sql, params)

        def cursor(self):
            return self

        def copy(self, sql):
            self.cur = self.copies.setdefault(sql.split()[1], [])
            return self

        def write_row(self, r):
            self.cur.append(r)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    out = {k: [] for k in ("states", "connection_states", "charging_states", "outside_temperatures", "charging_powers", "drive_levels", "drive_ranges", "trips", "charging_sessions", "positions")}
    out.update(first=T0, last=T0 + 3 * H, rows=1, vehicle={}, software=[(T0, T0, "A"), (T0 + H, T0 + H, "A"), (T0 + 2 * H, T0 + 2 * H, "B")])
    conn = W([v("A", -10)])
    write_history(conn, ALPHA, "Alpha", "Tesla", "Model 3", 75.0, out, "teslafi-live:" + ALPHA, "x", timezone.utc, rolling=True)
    assert conn.copies["cc_software"] == [(ALPHA, "B", T0 + 2 * H, "teslafi-live:" + ALPHA)]


def test_dedupe_finds_consecutive_duplicates_across_imports_and_keeps_the_first():
    conn = Conn([v("A", 0), v("A", 1), v("A", 2), v("B", 3), v("B", 4), v("A", 5)])
    conn.rows[1]["import_id"], conn.rows[2]["import_id"] = "teslafi-history", "teslafi-live:x"
    found = software_dedupe(conn)
    assert [d["id"] for d in found] == [2, 3, 5]
    assert {d["import_id"] for d in found[:2]} == {"teslafi-history", "teslafi-live:x"}


def test_dedupe_dry_run_deletes_nothing_and_a_real_run_deletes_in_batches():
    conn = Conn([v("A", i) for i in range(701)])
    assert len(software_dedupe(conn)) == 700 and conn.deleted == []
    assert len(software_dedupe(conn, dry_run=False)) == 700
    assert [len(b) for b in conn.deleted] == [300, 300, 100] and conn.commits == 3
    assert [r["id"] for r in conn.rows] == [1]


def test_dedupe_can_be_limited_to_one_car():
    conn = Conn([v("A", 0), v("A", 1)])
    conn.rows.append(dict(id=9, vin=BRAVO, import_id=None, **v("X", 0)))
    conn.rows.append(dict(id=10, vin=BRAVO, import_id=None, **v("X", 1)))
    assert [d["id"] for d in software_dedupe(conn, BRAVO)] == [10]


def test_the_timeline_reads_changes_only(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    monkeypatch.setenv("REST_USER", "u")
    monkeypatch.setenv("REST_PASSWORD", "p")
    import app

    seen = {}

    class C:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, sql, params=None):
            seen["sql"] = " ".join(sql.split())
            return self

        def fetchall(self):
            return []

    monkeypatch.setattr(app, "db", lambda timeout=None: C())
    app.software_events(None, T0, T0 + H)
    sql = seen["sql"]
    assert "LAG(t.version) OVER (PARTITION BY t.vin" in sql and "t.prev IS DISTINCT FROM t.version" in sql
    assert sql.index("LAG(") < sql.index("t.seen_at >= %s"), "the window cuts after the previous version is known"
