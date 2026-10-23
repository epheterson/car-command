"""Hourly rolling rebuilds must leave a night whole.

Each rebuild opens at a sleeping row and rewrites from there. The stretch the window
opens inside used to be deleted and written again only from the window's start, so
every hour ate the front of the night. A small in-memory store stands in for Postgres:
it understands the statements history.py sends for this path and nothing more."""

import re
from datetime import datetime, timedelta, timezone

from history import INTERVAL_TABLES, keep_heads, write_history

T0 = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)  # 18:00 on the first evening
H = timedelta(hours=1)
VIN = "VINALPHA"

COND = re.compile(r"(\w+) (>=|<=|<|>|=) %s")


class Store:
    """Tables as lists of dicts; WHERE clauses of `col op %s` joined by AND."""

    description = None  # no cursor columns: a statement it does not answer returns no rows (driveedits reads by column name)

    def __init__(self):
        self.tables = {}
        self.next_id = 1
        self.result = []

    def rows(self, t):
        return self.tables.setdefault(t, [])

    def match(self, where, params):
        conds = COND.findall(where.split(" AND id NOT IN")[0])
        ops = {"=": lambda a, b: a == b, "<": lambda a, b: a < b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b}
        return lambda r: all(ops[op](r.get(c), p) for (c, op), p in zip(conds, params))

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.result = []
        m = re.match(r"SELECT (.+?) FROM (\w+) WHERE (.+?)( ORDER BY .*)?$", sql)
        if m and not sql.startswith("SELECT 1"):
            cols = [c.strip() for c in m.group(1).split(",")]
            if any("(" in c for c in cols):
                return self
            hit = sorted((r for r in self.rows(m.group(2)) if self.match(m.group(3), params)(r)), key=lambda r: r.get(cols[0]) or 0)
            self.result = [tuple(r.get(c) for c in cols) for r in hit]
            return self
        m = re.match(r"DELETE FROM (\w+) WHERE (.+?)( RETURNING (.+))?$", sql)
        if m:
            t, keep = m.group(1), self.match(m.group(2), params)
            gone = [r for r in self.rows(t) if keep(r)]
            self.tables[t] = [r for r in self.rows(t) if not keep(r)]
            if m.group(4):
                cols = [c.strip() for c in m.group(4).split(",")]
                self.result = [tuple(r.get(c) for c in cols) for r in gone]
            return self
        m = re.match(r"INSERT INTO cc_imports \((.+?)\) VALUES", sql)
        if m:
            self.rows("cc_imports").append(dict(zip([c.strip() for c in m.group(1).split(",")], params)))
        return self

    def fetchall(self):
        return self.result

    def commit(self):
        pass

    def cursor(self):
        return self

    def copy(self, sql):
        m = re.match(r"COPY (\w+) \((.+?)\) FROM STDIN", sql)
        self.copying = (m.group(1), [c.strip() for c in m.group(2).split(",")])
        return self

    def write_row(self, r):
        t, cols = self.copying
        row = dict(zip(cols, r))
        if "id" not in row:
            row["id"] = self.next_id
            self.next_id += 1
        self.rows(t).append(row)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class Conn(Store):
    def execute(self, sql, params=None):
        if "FROM drives" in sql:
            self.result = [(7,)]
            return self
        if "MAX(last_at)" in sql:
            self.result = [(None,)]  # no file import before the feed
            return self
        if sql.startswith("UPDATE cc_imports SET last_at"):
            for r in self.rows("cc_imports"):
                if r["id"] == params[1] and r["last_at"] > params[2]:
                    r["last_at"] = params[0]
            return self
        return super().execute(sql, params)

    def fetchone(self):
        return self.result[0] if self.result else None


# What the car did: parked in the evening, asleep 22:00-07:00, a drive, parked again.
TRUTH = {
    "states": [(T0, T0 + 4 * H, "PARKED"), (T0 + 4 * H, T0 + 13 * H, "OFFLINE"), (T0 + 13 * H, T0 + 13.5 * H, "DRIVING"), (T0 + 13.5 * H, T0 + 15 * H, "PARKED")],
    "drive_levels": [(T0, T0 + 13 * H, 80.0), (T0 + 13 * H, T0 + 15 * H, 74.0)],
    "modes": [(T0, T0 + 15 * H, (False, False))],
}
TRIP = {
    "start_date": T0 + 13 * H,
    "destination_date": T0 + 13.5 * H,
    "start_odometer": 100.0,
    "destination_odometer": 112.0,
    "start_position_latitude": None,
    "start_position_longitude": None,
    "destination_position_latitude": None,
    "destination_position_longitude": None,
}


def replay(lo, hi):
    """A replay of the raw rows between lo and hi: the truth, clipped to the window."""
    out = {k: [] for k in INTERVAL_TABLES}
    for k, runs in TRUTH.items():
        out[k] = [(max(a, lo), min(b, hi), v) for a, b, v in runs if b >= lo and a <= hi]
    out.update(first=lo, last=hi, rows=1, vehicle={}, positions=[], software=[], charging_sessions=[])
    out["trips"] = [TRIP] if lo <= TRIP["start_date"] and TRIP["destination_date"] <= hi else []
    return out


def stored(conn, table, col):
    key = "drive_id" if INTERVAL_TABLES.get(table, (None, "vin"))[1] == "drive" else "vin"
    return sorted((r["first_date"], r["last_date"], r[col]) for r in conn.rows(table) if r.get(key) in (VIN, 7))


def test_a_night_survives_hourly_rebuilds_that_open_inside_it():
    conn = Conn()
    # every hour from 18:00 to 06:00 a 3-hour window, opening (as window_start does) inside
    # the stretch it lands in; then one after the drive
    for k in range(13):
        lo = T0 + k * H
        write_history(conn, VIN, "Alpha", "Tesla", "Model 3", 75.0, replay(lo, lo + 3 * H), "teslafi-live:" + VIN, "x", timezone.utc, rolling=True)
    write_history(conn, VIN, "Alpha", "Tesla", "Model 3", 75.0, replay(T0 + 12 * H, T0 + 15 * H), "teslafi-live:" + VIN, "x", timezone.utc, rolling=True)
    assert stored(conn, "states", "state") == TRUTH["states"], "one stretch per state, the night from 22:00 whole"
    assert stored(conn, "drive_levels", "level") == TRUTH["drive_levels"]
    modes = [(r["first_date"], r["last_date"], (r["sentry"], r["climate"])) for r in conn.rows("cc_modes")]
    assert modes == TRUTH["modes"]
    trips = conn.rows("trips")
    assert [(t["start_date"], t["destination_date"]) for t in trips] == [(TRIP["start_date"], TRIP["destination_date"])], "the drive once"


def test_a_drive_rewritten_by_later_rebuilds_keeps_its_id():
    conn = Conn()
    write_history(conn, VIN, "Alpha", "Tesla", "Model 3", 75.0, replay(T0 + 11 * H, T0 + 15 * H), "teslafi-live:" + VIN, "x", timezone.utc, rolling=True)
    first = conn.rows("trips")[0]["id"]
    for lo in (T0 + 12 * H, T0 + 12.5 * H, T0 + 12.75 * H):
        write_history(conn, VIN, "Alpha", "Tesla", "Model 3", 75.0, replay(lo, T0 + 15 * H), "teslafi-live:" + VIN, "x", timezone.utc, rolling=True)
    ids = [t["id"] for t in conn.rows("trips")]
    assert len(ids) == 1 and ids[0] == first, "the same drive, under the id it was first given"


def test_keep_heads_joins_clips_or_keeps():
    a, lo = T0, T0 + 2 * H
    # same state carries on: one stretch from its real start
    assert keep_heads([(lo, lo + H, "OFFLINE")], [(a, lo + H / 2, "OFFLINE")]) == [(a, lo + H, "OFFLINE")]
    # a different state from lo: the old one ends where the new one starts
    assert keep_heads([(lo, lo + H, "DRIVING")], [(a, lo + H / 2, "OFFLINE")]) == [(a, lo, "OFFLINE"), (lo, lo + H, "DRIVING")]
    # nothing new for this table (a summaries-only replay): the old stretch stays whole
    assert keep_heads([], [(a, lo + H, "OFFLINE")]) == [(a, lo + H, "OFFLINE")]
    assert keep_heads([(lo, lo + H, "X")], []) == [(lo, lo + H, "X")]


D1 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
M5 = timedelta(minutes=5)


def feed_rows(until):
    """A TeslaFi feed every five minutes: asleep 22:00-07:00, a 30-minute drive at 07:00,
    parked the rest of the day."""
    rows, at, odo, drive = [], D1, 1000.0, 0
    while at <= until:
        h = at.hour + at.minute / 60
        row = {"vin": VIN, "battery_level": "70", "latitude": "39.78", "longitude": "-89.65", "charging_state": "Disconnected", "odometer": f"{odo:.1f}"}
        if h >= 22 or h < 7:
            row.update(state="asleep", sleepNumber="5")
        elif 7 <= h < 7.5:
            if h == 7:
                drive += 1
            odo += 1.0
            row.update(state="online", shift_state="D", speed="30", driveNumber=str(drive), odometer=f"{odo:.1f}")
        else:
            row.update(state="online", shift_state="P")
        rows.append({"vin": VIN, "at": at, "data": row})
        at += M5
    return rows


def offline(conn):
    return [(a, b) for a, b, v in stored(conn, "states", "state") if v == "OFFLINE"]


def test_the_feed_rebuilt_every_hour_keeps_the_night_and_a_replay_restores_one(monkeypatch):
    import teslafi_live

    monkeypatch.setattr(teslafi_live, "fill_after_import", lambda *a, **k: None)
    end = D1 + timedelta(days=2, hours=8)
    conn = Conn()
    conn.tables["cc_teslafi_rows"] = feed_rows(end)
    p = teslafi_live.TeslaFiPoller([], connect=lambda: conn, tz=timezone.utc)
    night = (D1 + 10 * H, D1 + 19 * H)  # Sep 20 22:00 to Sep 21 07:00
    # the hourly rebuilds whose 36-hour window walks through the first night
    for k in range(13):
        p.rebuild(VIN, D1 + timedelta(hours=44 + k))
    nights = offline(conn)
    assert nights[0][0] == night[0] and nights[0][1] >= night[1] - M5, f"the first night whole: {nights[:2]}"
    assert len([t for t in conn.rows("trips")]) == 2, "each morning's drive once"

    # a night lost to the old rebuilds comes back from the raw rows on a replay
    conn.tables["states"] = [r for r in conn.rows("states") if not (r["state"] == "OFFLINE" and r["first_date"] < night[1])]
    teslafi_live.replay_from(p, VIN, D1, now=end)
    assert offline(conn)[0][0] == night[0] and offline(conn)[0][1] >= night[1] - M5
    assert len(conn.rows("trips")) == 2
    starts = [r["first_date"] for r in conn.rows("states")]
    assert len(starts) == len(set(starts)), "no stretch written twice"


class Col:
    def __init__(self, name):
        self.name = name


class FactsConn(Conn):
    """The store with a facts table that the rewrite may drop rows from by id."""

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.description = None
        if sql.startswith("SELECT to_regclass"):
            self.description, self.result = [Col("t")], [(params[0],)]
            return self
        m = re.match(r"DELETE FROM (\w+) WHERE (\w+) = ANY\(%s\)$", sql)
        if m:
            self.tables[m.group(1)] = [r for r in self.rows(m.group(1)) if r.get(m.group(2)) not in params[0]]
            return self
        return super().execute(sql, params)


def test_a_drive_written_while_going_loses_the_facts_of_its_old_end():
    """The hourly rebuild that lands mid-drive writes the drive as far as it has got, and the
    fill takes its levels there. The next rebuild gives the longer drive the same id, so the
    facts of minute twenty stood for good: a 49-mile drive read 83 to 81 %."""
    conn = FactsConn()
    going = {**TRIP, "destination_date": T0 + 13.25 * H, "destination_odometer": 106.0}
    out = replay(T0 + 11 * H, T0 + 13.25 * H)
    out["trips"] = [going]
    write_history(conn, VIN, "Alpha", "Tesla", "Model 3", 75.0, out, "teslafi-live:" + VIN, "x", timezone.utc, rolling=True)
    trip = conn.rows("trips")[0]["id"]
    conn.rows("cc_trip_facts").append({"trip_id": trip, "level_start": 80.0, "level_end": 79.0})
    write_history(conn, VIN, "Alpha", "Tesla", "Model 3", 75.0, replay(T0 + 12 * H, T0 + 15 * H), "teslafi-live:" + VIN, "x", timezone.utc, rolling=True)
    assert [t["id"] for t in conn.rows("trips")] == [trip], "the same drive, same id"
    assert conn.rows("cc_trip_facts") == [], "its facts go, to be worked out to the real end"


def test_a_drive_rewritten_unchanged_keeps_its_facts():
    conn = FactsConn()
    write_history(conn, VIN, "Alpha", "Tesla", "Model 3", 75.0, replay(T0 + 11 * H, T0 + 15 * H), "teslafi-live:" + VIN, "x", timezone.utc, rolling=True)
    trip = conn.rows("trips")[0]["id"]
    conn.rows("cc_trip_facts").append({"trip_id": trip, "level_start": 80.0, "level_end": 74.0})
    write_history(conn, VIN, "Alpha", "Tesla", "Model 3", 75.0, replay(T0 + 12 * H, T0 + 15 * H), "teslafi-live:" + VIN, "x", timezone.utc, rolling=True)
    assert len(conn.rows("cc_trip_facts")) == 1, "an hourly rewrite of a finished drive costs nothing"


def test_a_charge_written_while_charging_loses_the_facts_of_its_old_end():
    conn = FactsConn()
    ch = {
        "session_start_date": T0 + H,
        "session_end_date": T0 + 1.5 * H,
        "start_level": 50.0,
        "end_level": 55.0,
        "session_position_latitude": None,
        "session_position_longitude": None,
        "session_odometer": 100.0,
        "real_charged": 4.0,
        "charging_type": "AC",
    }
    out = replay(T0, T0 + 1.5 * H)
    out["charging_sessions"] = [ch]
    write_history(conn, VIN, "Alpha", "Tesla", "Model 3", 75.0, out, "teslafi-live:" + VIN, "x", timezone.utc, rolling=True)
    sid = conn.rows("charging_sessions")[0]["id"]
    conn.rows("cc_charge_facts").append({"session_id": sid, "kwh": 4.0, "range_added": 15.0})
    out = replay(T0, T0 + 4 * H)
    out["charging_sessions"] = [{**ch, "session_end_date": T0 + 3 * H, "end_level": 80.0, "real_charged": 22.0}]
    write_history(conn, VIN, "Alpha", "Tesla", "Model 3", 75.0, out, "teslafi-live:" + VIN, "x", timezone.utc, rolling=True)
    assert [c["id"] for c in conn.rows("charging_sessions")] == [sid]
    assert conn.rows("cc_charge_facts") == [], "range added was taken half an hour in"
