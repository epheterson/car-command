"""One writer per car: the choice, its default, and the handoff's boundary when it changes."""

from datetime import datetime, timedelta, timezone

import pytest

import history_writer as hw

ALPHA = "5YJ3E1EA0JF000001"
T0 = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
M = timedelta(minutes=1)
H = timedelta(hours=1)


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Conn:
    """cc_state and cc_imports as dicts, trips and charges as lists; every statement recorded."""

    def __init__(self, state=None, imports=None, trips=(), charges=()):
        self.state = dict(state or {})
        self.imports = dict(imports or {})  # id -> last_at
        self.trips, self.charges = list(trips), list(charges)
        self.sql, self.commits, self.rollbacks = [], 0, 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.sql.append((s, params))
        if s.startswith("SELECT value FROM cc_state"):
            return Result([(self.state[params[0]],)] if params[0] in self.state else [])
        if s.startswith("SELECT key, value FROM cc_state"):
            return Result([(k, v) for k, v in self.state.items() if k.startswith(hw.KEY)])
        if s.startswith("INSERT INTO cc_state"):
            self.state[params[0]] = params[1]
        if s.startswith("SELECT id, last_at FROM cc_imports"):
            return Result(list(self.imports.items()))
        if s.startswith("SELECT start_date, destination_date FROM trips"):
            return Result([t for t in self.trips if t[1] >= params[1]])
        if s.startswith("SELECT session_start_date"):
            return Result([c for c in self.charges if c[1] >= params[1]])
        if s.startswith("UPDATE cc_imports SET last_at"):
            for k, v in self.imports.items():
                if v > params[0]:
                    self.imports[k] = params[0]
        if s.startswith("INSERT INTO cc_imports"):
            self.imports[params[0]] = min(self.imports.get(params[0], params[3]), params[3])
        return Result()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


# ---- the choice
def test_with_no_choice_kept_each_source_keeps_its_own_rule():
    conn = Conn()
    assert hw.chosen(conn, ALPHA) is None
    assert {k: hw.writes(conn, ALPHA, k) for k in hw.PREFIX} == {"tesla": None, "teslafi": None, "teslamate": None}


def test_a_kept_choice_names_one_writer_and_silences_the_rest():
    conn = Conn(state={hw.KEY + ALPHA: "tesla"})
    assert hw.chosen(conn, ALPHA) == "tesla"
    assert {k: hw.writes(conn, ALPHA, k) for k in hw.PREFIX} == {"tesla": True, "teslafi": False, "teslamate": False}
    assert hw.all_chosen(conn) == {ALPHA: "tesla"}


def test_a_value_that_is_no_writer_counts_as_no_choice():
    assert hw.chosen(Conn(state={hw.KEY + ALPHA: "carrier pigeon"}), ALPHA) is None


def test_the_choices_offered_are_the_cars_sources_and_always_the_current_one():
    got = hw.options("teslafi", {"tesla": True, "teslafi": True, "teslamate": False})
    assert [(o["kind"], o["current"]) for o in got] == [("tesla", False), ("teslafi", True)]
    assert [o["kind"] for o in hw.options("teslamate", {})] == ["teslamate"]


def test_keeping_the_current_writer_hands_nothing_over():
    conn = Conn(imports={"teslafi-live:" + ALPHA: T0})
    assert hw.choose(conn, ALPHA, "teslafi", current="teslafi") == (None, None)
    assert conn.state[hw.KEY + ALPHA] == "teslafi" and conn.commits == 1
    assert not any(s.startswith(("UPDATE", "DELETE")) for s, _ in conn.sql)
    assert conn.sql[0][0].startswith("SELECT pg_advisory_xact_lock"), "a rebuild in flight finishes first"


def test_an_unknown_writer_is_refused():
    assert hw.choose(Conn(), ALPHA, "nope", current=None) == (None, "No such source.")


# ---- the boundary
def test_the_boundary_is_the_stored_end_when_nothing_is_under_way():
    assert hw.boundary(T0, [(T0 - 3 * H, T0 - 2 * H)]) == T0


def test_the_boundary_steps_back_before_a_drive_under_way_at_the_end():
    b = hw.boundary(T0, [(T0 - 20 * M, T0 + 10 * M)])
    assert b == T0 - 20 * M - hw.PAD - hw.CUT


def test_the_boundary_keeps_clear_of_a_session_that_just_ended_and_of_chains():
    # a charge that ended 2 minutes before the end, and a drive into it that ended 4 minutes before that
    busy = [(T0 - 60 * M, T0 - 2 * M), (T0 - 90 * M, T0 - 64 * M)]
    b = hw.boundary(T0, busy)
    assert b == T0 - 90 * M - hw.PAD - hw.CUT
    assert all(not (s - hw.PAD <= b <= e + hw.PAD) for s, e in busy)


def drives_at(*minutes, shift=timedelta(0), length=20):
    return [(T0 + m * M + shift, T0 + (m + length) * M + shift) for m in minutes]


def test_a_switch_mid_day_keeps_every_drive_once_and_leaves_no_hole():
    """The feed stored drives to its last replay (T0); the stream saw the same drives, its
    clock a minute earlier, plus the one under way at T0 the feed has only half of. After
    the handoff: the feed's drives before B, the stream's from B, each drive once."""
    real = [-300, -200, -100, -10, 40, 120]  # minutes from T0, each a 20-minute drive
    feed = [(s, min(e, T0)) for s, e in drives_at(*real[:4]) if s < T0]  # the drive at -10 is cut at T0 in the feed's last replay
    stream = drives_at(*real, shift=-M)
    b = hw.boundary(T0, feed + stream)
    assert all(e < b or s > b for s, e in feed + stream), "no drive of either source crosses the boundary"
    kept = [s for s, e in feed if e < b] + [s for s, e in stream if s >= b]
    starts = sorted(T0 + m * M for m in real)
    assert len(kept) == len(starts), "every drive once: none doubled, none lost"
    assert all(abs(k - r) <= 5 * M for k, r in zip(sorted(kept), starts))


# ---- the handoff
def test_the_handoff_cuts_stretches_at_the_boundary_and_hands_the_window_over():
    feed_id, stream_id = "teslafi-live:" + ALPHA, "tesla-stream:" + ALPHA
    conn = Conn(imports={"teslafi-export-1": T0 - 30 * 24 * H, feed_id: T0}, trips=[(T0 - 15 * M, T0 + 5 * M)])
    b, problem = hw.choose(conn, ALPHA, "tesla", current="teslafi", incoming=[(T0 - 16 * M, T0 + 4 * M)], first_raw=T0 - 2 * 24 * H)
    assert problem is None and b == T0 - 16 * M - hw.PAD - hw.CUT
    assert conn.imports[feed_id] == b, "the outgoing feed now ends at the boundary: the stream's floor"
    assert conn.imports[stream_id] == b, "the stream resumes from the boundary, so its window opens exactly there"
    assert conn.imports["teslafi-export-1"] == T0 - 30 * 24 * H, "older imports are not touched"
    sql = [s for s, _ in conn.sql]
    cuts = [p for s, p in conn.sql if s.startswith("UPDATE") and "SET last_date = GREATEST(first_date, %s)" in s]
    assert len(cuts) == len(hw.VIN_STRETCHES) + len(hw.DRIVE_STRETCHES) and all(p[0] == b - hw.CUT for p in cuts)
    deletes = [(s, p) for s, p in conn.sql if s.startswith("DELETE")]
    assert {s.split()[2] for s, _ in deletes} == {"cc_positions", "cc_software"}, "drives and charges wait for the stream's replay to rewrite them"
    assert all(stream_id not in p[2] and feed_id in p[2] for _s, p in deletes), "only the outgoing sources' rows from the boundary on"
    assert not any("DELETE FROM trips" in s or "DELETE FROM charging_sessions" in s for s in sql)
    assert conn.state[hw.KEY + ALPHA] == "tesla" and conn.state["history_handoff:" + ALPHA].startswith("tesla from ")


def test_a_handoff_to_a_source_without_records_from_before_the_boundary_is_refused():
    conn = Conn(imports={"teslafi-live:" + ALPHA: T0})
    b, problem = hw.choose(conn, ALPHA, "tesla", current="teslafi", first_raw=T0 + 5 * M)
    assert b is None and "has records from" in problem
    assert conn.rollbacks == 1 and hw.KEY + ALPHA not in conn.state


def test_a_car_with_nothing_stored_just_keeps_the_choice():
    conn = Conn()
    assert hw.choose(conn, ALPHA, "tesla", current=None) == (None, None)
    assert conn.state[hw.KEY + ALPHA] == "tesla"


@pytest.mark.parametrize("kind,table", [("tesla", "cc_tesla_raw"), ("teslafi", "cc_teslafi_rows")])
def test_the_incoming_sources_first_record_is_read_from_its_raw_table(kind, table):
    conn = Conn()
    hw.first_raw(conn, kind, ALPHA)
    assert table in conn.sql[-1][0]
    assert hw.first_raw(Conn(), "teslamate", ALPHA) is None


def test_a_switch_to_the_stream_keeps_clear_of_the_streams_own_drives(monkeypatch):
    import tesla_compare

    asked = []

    def spans(conn, vin, since, now):
        asked.append(since)
        return [(T0 - 12 * M, T0 + 8 * M)]  # the stream has a drive under way at the stored end; the feed has none

    monkeypatch.setattr(tesla_compare, "incoming_spans", spans)

    class Stored(Conn):
        def execute(self, sql, params=None):
            if sql.startswith("SELECT MAX(last_at) FROM cc_imports"):
                return Result([(T0,)])
            if sql.startswith("SELECT MIN(at)"):
                return Result([(T0 - 24 * H,)])
            return super().execute(sql, params)

    conn = Stored(imports={"teslafi-live:" + ALPHA: T0})
    b, problem = hw.switch(conn, ALPHA, "tesla", "teslafi", T0 + H, None)
    assert problem is None and asked == [T0 - hw.LOOK_BACK]
    assert b == T0 - 12 * M - hw.PAD - hw.CUT


# ---- the 14:30 / 14:40-14:55 scenario: the stream loses a drive in the middle
D = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
STREAM_ID, FEED_ID = "tesla-stream:" + ALPHA, "teslafi-live:" + ALPHA


def at(hh, mm):
    return D.replace(hour=hh, minute=mm)


def rec(t, **fields):
    data = [{"key": k, "value": {"shiftStateValue": "ShiftState" + v} if k == "Gear" else {"doubleValue": v}} for k, v in fields.items()]
    return {"vin": ALPHA, "createdAt": t.isoformat().replace("+00:00", "Z"), "data": data, "isResend": False}


def hello(t, status="CONNECTED"):
    return {"vin": ALPHA, "createdAt": t.isoformat().replace("+00:00", "Z"), "status": status}


def stream_with_a_hole():
    """Parked from 14:00, a drive from 14:35; the receiver is gone from 14:40 to 14:55; parked at 14:58."""
    out = [hello(at(14, 0))] + [rec(at(14, m), BatteryLevel=70.0 - m / 100) for m in range(0, 35)]
    out += [rec(at(14, 35), Gear="D")] + [rec(at(14, m), VehicleSpeed=30.0, Odometer=12000.0 + m / 2) for m in range(36, 41)]
    out += [hello(at(14, 55)), rec(at(14, 55), VehicleSpeed=25.0)] + [rec(at(14, 58), VehicleSpeed=0.0, Gear="P")]
    return out + [rec(at(14, m), BatteryLevel=60.0 + m / 100) for m in range(59, 60)]


class Scenario(Conn):
    def __init__(self, raw, newest_feed_row=None, **kw):
        super().__init__(**kw)
        self.raw, self.newest_feed_row = raw, newest_feed_row

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT MAX(last_at) FROM cc_imports"):
            return Result([(max(self.imports.values()),)])
        if s.startswith("SELECT last_at FROM cc_imports WHERE id"):
            return Result([(self.imports[params[0]],)] if params[0] in self.imports else [])
        if s.startswith("SELECT MIN(at)"):
            return Result([(at(13, 0),)])
        if s.startswith("SELECT MAX(at) FROM cc_teslafi_rows"):
            return Result([(self.newest_feed_row,)])
        if s.startswith("SELECT record FROM cc_tesla_raw"):
            return Result([(p,) for p in self.raw if params[1] <= hw_time(p) < params[2]])
        if s.startswith("SELECT at, data FROM cc_teslafi_rows"):
            return Result()
        return super().execute(sql, params)


def hw_time(p):
    import tesla_stream

    return tesla_stream.when(p["createdAt"])


def test_switching_to_a_stream_that_missed_part_of_a_drive_is_refused_with_why():
    """TeslaFi wrote to 14:59 and saw the whole 14:35 drive; the stream lost 14:40-14:55 of it."""
    conn = Scenario(stream_with_a_hole(), imports={FEED_ID: at(14, 59)}, trips=[(at(14, 35), at(14, 58))])
    b, problem = hw.switch(conn, ALPHA, "tesla", "teslafi", at(15, 0), None)
    assert b is None and "went silent from Oct 5 14:40 to 14:55" in problem and "step 5" in problem
    assert conn.rollbacks == 1 and hw.KEY + ALPHA not in conn.state and conn.imports[FEED_ID] == at(14, 59), "nothing handed over"


def test_switching_back_from_a_stream_that_went_silent_lets_the_feed_fill_the_hole():
    """Switched to the stream at 14:30; it lost 14:40-14:55 and its replay wrote a drive cut at
    14:40. Back to TeslaFi at 15:00: the handover reaches back before the cut drive, so
    TeslaFi's raw rows (kept all along) rewrite the whole drive."""
    conn = Scenario(stream_with_a_hole(), imports={FEED_ID: at(14, 30), STREAM_ID: at(15, 0)}, trips=[(at(14, 35), at(14, 40))])
    b, problem = hw.switch(conn, ALPHA, "teslafi", "tesla", at(15, 0), None, last_ok=at(14, 59))
    assert problem is None
    assert b == at(14, 35) - hw.PAD - hw.CUT, "before the drive the stream cut, not at the stream's last replay"
    assert conn.imports[STREAM_ID] == b and conn.imports[FEED_ID] == b, "the feed's next window opens at b and covers the hole"
    assert conn.state[hw.KEY + ALPHA] == "teslafi"


def test_without_a_hole_the_switch_back_hands_over_at_the_streams_end():
    clean = [p for p in stream_with_a_hole() if not (at(14, 40) < hw_time(p) < at(14, 56))] + [rec(at(14, m), VehicleSpeed=30.0) for m in range(41, 56)]
    conn = Scenario(clean, imports={FEED_ID: at(14, 30), STREAM_ID: at(15, 20)})
    b, problem = hw.switch(conn, ALPHA, "teslafi", "tesla", at(15, 20), None, last_ok=at(15, 19))
    assert problem is None and b == at(15, 20), "parked and quiet for 21 minutes is not a silence"


def test_a_feed_that_has_stopped_answering_is_refused():
    conn = Scenario([], imports={STREAM_ID: at(15, 0)}, newest_feed_row=at(13, 0))
    b, problem = hw.switch(conn, ALPHA, "teslafi", "tesla", at(15, 0), None, last_ok=at(13, 0))
    assert b is None and "TeslaFi has not answered for this car since Oct 5 13:00" in problem
    conn = Scenario([], imports={STREAM_ID: at(15, 0)}, newest_feed_row=at(14, 55))
    assert hw.switch(conn, ALPHA, "teslafi", "tesla", at(15, 0), None)[1] is None, "a row minutes old is a feed that follows the car"


def test_a_switch_to_teslamate_keeps_clear_of_its_own_drives_and_needs_it_answering():
    asked = []

    def spans(vin, since):
        asked.append((vin, since))
        return [(at(14, 50), at(15, 10))]  # TeslaMate has a drive under way at the stored end

    conn = Scenario([], imports={FEED_ID: at(15, 0)})
    b, problem = hw.switch(conn, ALPHA, "teslamate", "teslafi", at(15, 5), None, last_ok=at(15, 4), teslamate_spans=spans)
    assert problem is None and b == at(14, 50) - hw.PAD - hw.CUT and asked == [(ALPHA, at(15, 0) - hw.LOOK_BACK)]
    conn = Scenario([], imports={FEED_ID: at(15, 0)})
    assert "TeslaMate has not answered" in hw.switch(conn, ALPHA, "teslamate", "teslafi", at(15, 5), None, last_ok=None, teslamate_spans=spans)[1]
