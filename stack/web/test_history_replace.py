"""A rolling replay (the TeslaFi feed, TeslaMate live) must not take rows it will not rewrite."""

from datetime import datetime, timedelta, timezone

from history import INTERVAL_TABLES, clear_import, replace_span, with_kept_ids

T0 = datetime(2026, 9, 4, 23, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


class Conn:
    description = None  # no cursor columns: a statement it does not answer returns no rows (driveedits reads by column name)

    def __init__(self, deleted=None):
        self.calls = []
        self.deleted = deleted or {}
        self.last = ""

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.last = sql
        return self

    def fetchall(self):
        for table, rows in self.deleted.items():
            if f"DELETE FROM {table} " in self.last and "RETURNING" in self.last:
                return rows
        return []


def test_a_file_import_clears_its_previous_span_and_starts_over():
    lo, hi, first = replace_span((T0, T0 + 10 * H), T0 + 5 * H, T0 + 20 * H, rolling=False)
    assert (lo, hi) == (T0, T0 + 10 * H)
    assert first == T0 + 5 * H, "the import's span is what this run wrote"


def test_a_rolling_replay_clears_only_the_window_it_rewrites():
    # the previous rebuild wrote 36 h ending at T0+100h; this one starts at T0+65h
    prev = (T0 + 64 * H, T0 + 100 * H)
    lo, hi, first = replace_span(prev, T0 + 65 * H, T0 + 101 * H, rolling=True)
    assert lo == T0 + 65 * H, "nothing before the new window is touched"
    assert hi == T0 + 101 * H
    assert first == T0 + 64 * H, "the import still owns everything from its first replay"
    # a catch-up window opening back before the previous one keeps the earlier first date
    assert replace_span(prev, T0 + 30 * H, T0 + 101 * H, rolling=True) == (T0 + 30 * H, T0 + 101 * H, T0 + 30 * H)


def test_rolling_clear_is_bounded_and_keeps_by_hand_rows():
    conn = Conn()
    clear_import(conn, "VIN1", 7, "teslafi-live:VIN1", T0, T0 + H, rolling=True)
    sql = "\n".join(s for s, _ in conn.calls)
    assert sql.count("DELETE FROM") == len(INTERVAL_TABLES) + 10  # trips, charges, positions, software, three cc_ tables, trip and charge extras, imports
    for s, params in conn.calls:
        if "cc_imports" in s:
            assert params == ("teslafi-live:VIN1",)
            continue
        if s.startswith("SELECT"):
            assert params[-2:] == (T0, T0), "the stretch the window opens inside is read, to be handed back"
            continue
        assert params[-2:] == (T0, T0 + H), s
    assert "DELETE FROM cc_positions WHERE import_id = %s AND at >= %s AND at <= %s" in sql
    assert "seen_at >= %s" in sql, "software rows outside the window stay"
    assert "destination_date >= %s AND start_date <= %s" in sql, "a drive crossing the window start is rewritten, not doubled"
    assert sql.count("NOT IN (SELECT ref_id FROM cc_manual") == 2


def test_file_clear_keeps_the_old_shape():
    conn = Conn()
    clear_import(conn, "VIN1", 7, "file-1", T0, T0 + H, rolling=False)
    sql = "\n".join(s for s, _ in conn.calls)
    assert "DELETE FROM cc_positions WHERE import_id = %s\n" in sql + "\n"
    assert "first_date >= %s AND first_date <= %s" in sql and "last_date >= %s" not in sql


def test_a_rolling_import_clears_its_window_on_its_first_write():

    from history import write_history

    class Conn:
        description = None

        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append(sql)
            return self

        def fetchone(self):
            sql = self.calls[-1]
            if "FROM drives" in sql:
                return (1,)
            return None  # no previous import, no drive row yet

        def fetchall(self):
            return []

        def cursor(self):
            return self

        def commit(self):
            pass

        def copy(self, sql):
            return self

        def write_row(self, r):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    conn = Conn()
    out = {
        "first": T0,
        "last": T0 + H,
        "rows": 1,
        "vehicle": {},
        "states": [],
        "connection_states": [],
        "charging_states": [],
        "outside_temperatures": [],
        "charging_powers": [],
        "drive_levels": [],
        "drive_ranges": [],
        "trips": [],
        "charging_sessions": [],
        "positions": [],
        "software": [],
    }
    write_history(conn, "VIN1", "Alpha", "Tesla", "Model 3", 75.0, out, "teslafi-history:VIN1", "x", timezone.utc, rolling=True)
    assert any(s.startswith("DELETE FROM trips") for s in conn.calls), "the window is cleared even with nothing written before"


def test_a_rolling_rewrite_hands_back_the_ids_it_removed():
    """The hourly rebuild deleted and re-inserted its window, so every drive in it got a new
    id each hour: an open drive page went 404 and a tag or a hide keyed by the id was
    orphaned. The rows it removes now come back with their ids, keyed by their start."""
    conn = Conn(deleted={"trips": [(130778, T0, T0 + H / 2)], "charging_sessions": [(39204, T0 + H, T0 + 2 * H)]})
    kept = clear_import(conn, "VIN1", 7, "teslafi-live:VIN1", T0, T0 + 2 * H, rolling=True)
    assert kept["trips"] == {T0: 130778} and kept["charging_sessions"] == {T0 + H: 39204}
    sql = "\n".join(s for s, _ in conn.calls)
    assert kept["ends"] == {"trips": {130778: T0 + H / 2}, "charging_sessions": {39204: T0 + 2 * H}}, "with where each ended"
    assert "RETURNING id, start_date, destination_date" in sql and "RETURNING id, session_start_date, session_end_date" in sql


def test_rows_take_back_the_id_of_the_row_that_started_at_the_same_moment():
    rows = [("VIN1", T0, "a"), ("VIN1", T0 + H, "b"), ("VIN1", T0 + 2 * H, "c")]
    reuse, fresh = with_kept_ids(rows, {T0: 11, T0 + 2 * H: 13, T0 + 5 * H: 99}, at=1)
    assert reuse == [(11, "VIN1", T0, "a"), (13, "VIN1", T0 + 2 * H, "c")] and fresh == [("VIN1", T0 + H, "b")]
    reuse, fresh = with_kept_ids([("VIN1", T0, "x"), ("VIN1", T0, "y")], {T0: 11}, at=1)
    assert reuse == [(11, "VIN1", T0, "x")] and fresh == [("VIN1", T0, "y")], "an id is handed back once"


def test_the_writer_puts_a_rewritten_drive_back_under_its_old_id():
    from history import write_history

    class Conn:
        description = None

        def __init__(self):
            self.calls, self.copies = [], []

        def execute(self, sql, params=None):
            self.calls.append(sql)
            return self

        def fetchone(self):
            return (1,) if "FROM drives" in self.calls[-1] else None

        def fetchall(self):
            return [(130778, T0, T0 + H / 2)] if self.calls[-1].startswith("DELETE FROM trips") else []

        def cursor(self):
            return self

        def commit(self):
            pass

        def copy(self, sql):
            self.copies.append([sql])
            return self

        def write_row(self, r):
            self.copies[-1].append(r)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    trip = lambda at: {
        "start_date": at,
        "destination_date": at + H / 2,
        "start_odometer": 1.0,
        "destination_odometer": 2.0,
        "start_position_latitude": None,
        "start_position_longitude": None,
        "destination_position_latitude": None,
        "destination_position_longitude": None,
    }
    out = {
        "first": T0,
        "last": T0 + 2 * H,
        "rows": 1,
        "vehicle": {},
        "states": [],
        "connection_states": [],
        "charging_states": [],
        "outside_temperatures": [],
        "charging_powers": [],
        "drive_levels": [],
        "drive_ranges": [],
        "trips": [trip(T0), trip(T0 + H)],
        "charging_sessions": [],
        "positions": [],
        "software": [],
    }
    conn = Conn()
    write_history(conn, "VIN1", "Alpha", "Tesla", "Model 3", 75.0, out, "teslafi-live:VIN1", "x", timezone.utc, rolling=True)
    trips = [c for c in conn.copies if c[0].startswith("COPY trips")]
    with_id = [c for c in trips if c[0].startswith("COPY trips (id,")]
    assert with_id and with_id[0][1][:2] == (130778, "VIN1") and with_id[0][1][2] == T0, "the drive that started at the same moment keeps its id"
    assert [r[1] for c in trips if not c[0].startswith("COPY trips (id,") for r in c[1:]] == [T0 + H], "a new drive gets a new one"


def test_a_file_over_rows_another_file_loaded_replaces_them_instead_of_colliding():
    """The TeslaFi re-import that failed with UniqueViolation: a wider export of the same car, a different
    file (so a different import id), over drives and charges the first one wrote. Its first write owns its span."""
    from history import write_history

    class Rec(Conn):
        def __init__(self):
            super().__init__()

        def fetchone(self):
            return (1,) if "FROM drives" in self.last else None  # a drive row exists, no previous import of this file

        def cursor(self):
            return self

        def commit(self):
            pass

        def copy(self, sql):
            return self

        def write_row(self, r):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    conn = Rec()
    out = {k: [] for k in ("states", "connection_states", "charging_states", "outside_temperatures", "charging_powers", "drive_levels", "drive_ranges", "trips", "charging_sessions", "positions", "software")}
    out.update(first=T0, last=T0 + 10 * H, rows=1, vehicle={})
    write_history(conn, "VIN1", "Alpha", "Tesla", "Model 3", 75.0, out, "widerfile", "wide.csv", timezone.utc)
    sql = [(s, p) for s, p in conn.calls if s.startswith("DELETE")]
    trips = [p for s, p in sql if s.startswith("DELETE FROM trips")]
    assert trips == [("VIN1", T0, T0 + 10 * H)], "drives that start in the file's span go before its own are written"
    assert any(s.startswith("DELETE FROM charging_sessions") for s, _ in sql) and any(s.startswith("DELETE FROM cc_trip_extras") for s, _ in sql)
    others = [(s, p) for s, p in sql if "import_id <> %s" in s]
    assert len(others) == 2 and all(p == ("VIN1", T0, T0 + 10 * H, "widerfile") for _, p in others), "positions and software an earlier file left inside the span go too"
    assert all("import_id IS NOT NULL" in s for s, _ in others), "the live recorder's positions (no import id) stay"
