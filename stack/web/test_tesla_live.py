"""The stream's live half: records off the receiver's socket, the chosen cars only, kept
raw in batches, silence turned into a disconnect, and the hourly replay into history."""

import json
import threading
from datetime import timedelta

import pytest

import tesla_live
import tesla_stream as ts
from test_tesla_stream import ALPHA, HOME, T0, commute, connectivity, payload

BRAVO = "5YJ3E1EA0JF000002"
NOW = T0 + timedelta(hours=20)


def frames(record, kind="V"):
    return [f"tesla_telemetry_{kind}".encode(), json.dumps(record).encode()]


class Result:
    def __init__(self, rows=(), rowcount=0):
        self.rows, self.rowcount = list(rows), rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Conn:
    """Answers the few queries the stream makes, and records what it writes."""

    def __init__(self, db):
        self.db = db

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.db.sql.append((s, params))
        if s.startswith("SELECT MAX(last_at) FROM cc_imports"):
            return Result([(self.db.live if "LIKE" in s else self.db.floor,)])
        if s.startswith("SELECT last_at FROM cc_imports"):
            return Result([(self.db.resume,)] if self.db.resume else [])
        if s.startswith("SELECT value FROM cc_state"):
            return Result([(self.db.choice,)] if self.db.choice else [])
        if s.startswith("SELECT at, record FROM cc_tesla_raw"):
            return Result([r for r in getattr(self.db, "stored", []) if r[0] >= params[1]])
        if s.startswith("SELECT record FROM cc_tesla_raw"):
            return Result([(r,) for r in self.db.raw])
        if s.startswith("DELETE FROM cc_tesla_raw"):
            n = min(self.db.old, params[1])
            self.db.old -= n
            return Result(rowcount=n)
        if s.startswith("INSERT INTO cc_state"):
            self.db.notes[params[0]] = params[1]
        return Result()

    def cursor(self):
        return self

    def executemany(self, sql, rows):
        if self.db.down:
            raise ConnectionError("database is away")
        self.db.batches.append(list(rows))

    def commit(self):
        self.db.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class DB:
    def __init__(self, raw=(), floor=None, resume=None, live=None, choice=None):
        self.raw, self.floor, self.resume, self.live, self.choice = list(raw), floor, resume, live, choice
        self.sql, self.batches, self.notes, self.commits, self.old, self.down = [], [], {}, 0, 0, False

    def connect(self):
        return Conn(self)


def stream(db=None, vins=(ALPHA,), **kw):
    s = tesla_live.TeslaStream(connect=(db or DB()).connect, url=None, clock=lambda: NOW, **kw)
    s.vins = set(vins)
    return s


# ---- one record in
def test_a_data_record_is_read_with_the_time_the_car_made_it():
    kind, vin, at, rec = tesla_live.parse(frames(payload(5, VehicleSpeed=30.0)), NOW)
    assert (kind, vin, at) == ("V", ALPHA, T0 + timedelta(minutes=5)) and rec["data"][0]["key"] == "VehicleSpeed"
    assert tesla_live.parse(frames(connectivity(6, "DISCONNECTED"), "connectivity"), NOW)[0] == "connectivity"


@pytest.mark.parametrize(
    "msg",
    [
        [b"tesla_telemetry_V"],  # no record
        [b"tesla_telemetry_metrics", b"{}"],  # not a car's record
        [b"tesla_telemetry_V", b"not json"],
        [b"tesla_telemetry_V", json.dumps({"data": []}).encode()],  # names no car
    ],
)
def test_what_is_not_a_cars_record_is_passed_over(msg):
    assert tesla_live.parse(msg, NOW) is None


def test_a_record_without_a_time_takes_the_time_it_arrived():
    assert tesla_live.parse(frames({"vin": ALPHA, "alerts": []}, "alerts"), NOW)[2] == NOW


def test_only_the_chosen_cars_are_kept_and_the_rest_are_counted():
    s = stream()
    assert s.handle(frames(payload(1, VehicleSpeed=10.0)))
    assert not s.handle(frames(payload(1, vin=BRAVO, VehicleSpeed=10.0)))
    assert not s.handle(frames(payload(2, vin=BRAVO, VehicleSpeed=12.0)))
    assert [h[0] for h in s.held] == [ALPHA] and s.dropped == {BRAVO: 2}


def test_a_car_is_live_while_it_speaks_and_not_once_it_leaves():
    s = stream()
    s.handle(frames(payload(1, VehicleSpeed=10.0)))
    assert s.live(ALPHA) and not s.live(ALPHA, NOW + timedelta(minutes=3))
    s.handle(frames(connectivity(2, "DISCONNECTED"), "connectivity"))
    assert not s.live(ALPHA)


def test_the_cars_own_errors_are_kept_for_the_page():
    s = stream()
    s.handle(frames({"vin": ALPHA, "createdAt": "2026-09-07T15:00:00Z", "errors": [{"name": "evotlb", "body": "dial tcp: timeout", "tags": {}}]}, "errors"))
    assert s.car_errors[ALPHA][0]["name"] == "evotlb" and "timeout" in s.car_errors[ALPHA][0]["body"]


def test_records_are_written_in_batches_and_held_while_the_database_is_away():
    db = DB()
    s = stream(db)
    for i in range(650):
        s.handle(frames(payload(i / 60, VehicleSpeed=float(i))))
    db.down = True
    with pytest.raises(ConnectionError):
        s.flush()
    assert len(s.held) == 650, "nothing is lost while the database is away"
    db.down = False
    assert s.flush() == 650 and [len(b) for b in db.batches] == [300, 300, 50] and not s.held
    assert db.batches[0][0][:3] == (ALPHA, T0, "V")


def positions(db):
    return [p for s, p in db.sql if s.startswith("INSERT INTO cc_positions")]


def test_a_car_the_stream_writes_gets_its_live_position_each_flush_when_it_moved():
    db = DB(choice="tesla")
    s = stream(db, vins=(ALPHA, BRAVO))
    s.tick()
    assert s.writing == {ALPHA, BRAVO}
    s.handle(frames(payload(0, Gear="ShiftStateD", Location=HOME, Odometer=12000.0, BatteryLevel=70.0, PackVoltage=390.0, PackCurrent=-40.0)))
    s.handle(frames(payload(0.1, VehicleSpeed=30.0)))
    s.flush()
    (row,) = positions(db)
    vin, at, lat, lon, _heading, level, odo, speed, state, power, imp = row
    assert (vin, lat, lon, level, odo, speed, state) == (ALPHA, HOME[0], HOME[1], 70.0, 12000.0, 30.0, "DRIVING")
    assert at == T0 + timedelta(minutes=0.1) and power == 15.6 and imp == "tesla-stream:" + ALPHA
    s.handle(frames(payload(0.2, PackCurrent=-30.0)))
    s.flush()
    assert len(positions(db)) == 1, "nothing new where it stands"
    s.handle(frames(payload(0.3, Location=(39.79, -89.63), Odometer=12000.4)))
    s.flush()
    assert len(positions(db)) == 2


def test_a_car_another_source_writes_gets_no_live_position_from_the_stream():
    db = DB(choice="teslafi")
    s = stream(db)
    s.tick()
    assert not s.writing
    s.handle(frames(payload(0, Location=HOME)))
    s.flush()
    assert not positions(db) and db.batches, "the raw record is kept, the position is the writer's"


def test_switched_away_mid_drive_the_stream_writes_no_more_positions_before_its_next_tick():
    db = DB(choice="tesla")
    s = stream(db)
    s.tick()
    s.handle(frames(payload(0, Gear="ShiftStateD", Location=HOME)))
    s.flush()
    assert len(positions(db)) == 1
    db.choice = "teslafi"  # the handover committed between two ticks
    s.handle(frames(payload(0.1, VehicleSpeed=30.0, Location=(39.79, -89.63))))
    s.flush()
    assert len(positions(db)) == 1 and ALPHA not in s.writing, "read again under the car's lock at every write"
    assert any(sql.startswith("SELECT pg_advisory_xact_lock") for sql, _ in db.sql)


def test_a_resent_record_does_not_move_the_car_back():
    s = stream()
    s.handle(frames(payload(5, Location=HOME)))
    s.handle(frames(payload(1, resend=True, Location=(39.70, -89.70))))
    assert s.carried[ALPHA]["Location"] == HOME


# ---- silence
def recs(*payloads):
    return ts.records(list(payloads))


def gaps(out):
    return [(t - T0, f) for t, _v, f in out if f == {"_connected": False}]


def test_a_moving_car_that_goes_quiet_is_gone_after_the_drive_grace():
    out = tesla_live.with_silence(recs(payload(0, Gear="ShiftStateD", VehicleSpeed=30.0), payload(10, VehicleSpeed=30.0)), until=T0 + timedelta(minutes=11))
    assert gaps(out) == [(timedelta(minutes=3), {"_connected": False})]


def test_a_parked_or_slowly_charging_car_may_be_quiet_for_half_an_hour():
    quiet = recs(payload(0, Gear="ShiftStateP", DetailedChargeState="DetailedChargeStateCharging"), payload(25, ACChargingEnergyIn=1.0))
    assert gaps(tesla_live.with_silence(quiet, until=T0 + timedelta(minutes=26))) == []
    assert gaps(tesla_live.with_silence(quiet, until=T0 + timedelta(minutes=70))) == [(timedelta(minutes=55), {"_connected": False})]


def test_a_car_that_said_goodbye_gets_no_second_disconnect():
    out = tesla_live.with_silence(recs(payload(0, Gear="ShiftStateD"), connectivity(1, "DISCONNECTED")), until=T0 + timedelta(hours=2))
    assert len(gaps(out)) == 1


def test_a_receiver_that_stops_mid_drive_does_not_leave_the_car_driving():
    p = commute()[:4]  # out of the drive, still at 30 mph when the records stop
    samples = ts.samples(tesla_live.with_silence(recs(*p), until=T0 + timedelta(hours=1)), until=T0 + timedelta(hours=1))
    assert not samples[-1]["driving"] and samples[-1]["asleep"], "gone, then asleep"
    assert sorted({s["drive_no"] for s in samples if s["drive_no"]}) == [1]


# ---- the window
def sample(minutes, asleep):
    return {"time": T0 + timedelta(minutes=minutes), "asleep": asleep}


def test_the_window_opens_at_the_last_sleep_before_a_day_and_a_half_ago():
    now = T0 + timedelta(hours=40)
    samples = [sample(m, m in (0, 60, 300)) for m in range(0, 40 * 60, 30)]
    assert tesla_live.window_start(samples, now, floor=None) == T0 + timedelta(minutes=60)
    assert tesla_live.window_start(samples, now, floor=T0 + timedelta(hours=10)) == T0 + timedelta(hours=10), "never before another import's end"
    assert tesla_live.window_start(samples, now, floor=None, resume=T0 + timedelta(hours=2)) == T0 + timedelta(minutes=60)
    assert tesla_live.window_start([], now, None) is None


# ---- the hourly replay
def a_day():
    day = commute(start=0)
    day += [payload(9 * 60, Gear="ShiftStateD"), payload(9 * 60 + 2, VehicleSpeed=30.0, Location=(39.79, -89.63)), payload(9 * 60 + 7, VehicleSpeed=25.0, Location=HOME)]
    day += [payload(9 * 60 + 8, VehicleSpeed=0.0, Gear="ShiftStateP", Odometer=12008.0, BatteryLevel=67.0)]
    day += [payload(9 * 60 + 15 + m, DetailedChargeState="DetailedChargeStateCharging", ACChargingPower=7.6, ACChargingEnergyIn=round(0.1 + m * 0.127, 3)) for m in range(0, 120)]
    day += [payload(9 * 60 + 135, DetailedChargeState="DetailedChargeStateComplete", ACChargingPower=0.0, ACChargingEnergyIn=15.2, BatteryLevel=80.0)]
    return day + [connectivity(9 * 60 + 150, "DISCONNECTED")]


@pytest.fixture
def written(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tesla_live,
        "write_history",
        lambda conn, vin, name, make, model, cap, out, import_id, label, tz, rolling=False: calls.append(locals()) or {"trips": len(out["trips"]), "charging_sessions": len(out["charging_sessions"])},
    )
    monkeypatch.setattr(tesla_live, "fill_after_import", lambda conn, vin, tz: None)
    return calls


def test_an_hours_replay_writes_the_day_as_its_own_import(written):
    db = DB(raw=a_day())
    counts = stream(db).rebuild(ALPHA, NOW)
    assert counts == {"trips": 2, "charging_sessions": 1}
    call = written[0]
    assert call["import_id"] == "tesla-stream:" + ALPHA and call["rolling"] is True and call["make"] == "Tesla"
    assert abs((call["out"]["charging_sessions"][0].get("energy_added") or call["out"]["charging_sessions"][0].get("real_charged") or 0) - 15.2) < 0.2
    assert db.notes[f"stream_rebuild:{ALPHA}"].startswith("ok ") and db.commits == 1


def test_a_car_another_live_source_writes_is_only_counted(written):
    db = DB(raw=a_day(), live=NOW - timedelta(minutes=50))  # the TeslaFi feed wrote this car an hour ago
    assert stream(db).rebuild(ALPHA, NOW) is None and not written
    assert "watching" in db.notes[f"stream_rebuild:{ALPHA}"] and "trips=2" in db.notes[f"stream_rebuild:{ALPHA}"]


def test_chosen_as_the_writer_the_stream_writes_though_the_feed_wrote_an_hour_ago(written):
    db = DB(raw=a_day(), live=NOW - timedelta(minutes=50), choice="tesla")
    assert stream(db).rebuild(ALPHA, NOW) == {"trips": 2, "charging_sessions": 1}
    assert db.sql[0][0].startswith("SELECT pg_advisory_xact_lock"), "never beside a handoff"


def test_another_writer_chosen_the_stream_only_counts_though_no_feed_has_written(written):
    db = DB(raw=a_day(), choice="teslafi")
    assert stream(db).rebuild(ALPHA, NOW) is None and not written
    assert "watching" in db.notes[f"stream_rebuild:{ALPHA}"]


def test_the_replay_starts_where_an_older_import_ends(written):
    db = DB(raw=a_day(), floor=T0 + timedelta(hours=5))  # the export reaches past the morning drive
    assert stream(db).rebuild(ALPHA, NOW) == {"trips": 1, "charging_sessions": 1}


def test_each_car_is_rebuilt_once_an_hour_and_one_failure_does_not_stop_the_other(monkeypatch):
    s = stream(vins=(ALPHA, BRAVO))
    seen = []

    def rebuild(vin, now):
        seen.append(vin)
        if vin == ALPHA:
            raise RuntimeError("bad record")
        return None

    monkeypatch.setattr(s, "rebuild", rebuild)
    monkeypatch.setattr(s, "prune", lambda now: 0)
    s.tick()
    s.tick()
    assert seen == [ALPHA, BRAVO], "the second tick inside the hour asks nothing"
    assert "bad record" in s.results[ALPHA][1] and s.results[BRAVO][1] is None


def test_old_raw_records_go_a_batch_at_a_time():
    db = DB()
    db.old = 700
    assert stream(db).prune(NOW) == 700 and db.old == 0
    deletes = [p for q, p in db.sql if q.startswith("DELETE")]
    assert len(deletes) == 3 and deletes[0] == (NOW - timedelta(days=120), 300)


# ---- the thread
class Socket:
    def __init__(self, msgs, stream_):
        self.msgs, self.stream, self.closed = list(msgs), stream_, False

    def recv(self, timeout_ms):
        if not self.msgs:
            self.stream.stop()
            return None
        return self.msgs.pop(0)

    def close(self):
        self.closed = True


def test_the_thread_reads_writes_and_stops(monkeypatch):
    db = DB()
    s = stream(db)
    sockets = []
    s.socket_factory = lambda url: sockets.append(Socket([frames(payload(1, VehicleSpeed=10.0)), frames(payload(2, VehicleSpeed=11.0))], s)) or sockets[-1]
    monkeypatch.setattr(s, "tick", lambda: None)
    s.url = "tcp://receiver.example.test:5284"
    s.run()
    assert sum(len(b) for b in db.batches) == 2 and sockets[0].closed


def test_a_database_that_is_away_never_closes_the_socket(monkeypatch):
    """Three failed flushes: the records wait in memory, the subscriber keeps receiving, then all are written."""
    db = DB()
    s = stream(db)
    clock = iter(range(0, 10_000, 10))  # ten seconds a look: every pass is due a flush
    monkeypatch.setattr(tesla_live.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(s, "tick", lambda: None)
    fails = {"n": 0}
    real = Conn.executemany

    def flaky(self, sql, rows):
        if fails["n"] < 3:
            fails["n"] += 1
            raise ConnectionError("database is away")
        return real(self, sql, rows)

    monkeypatch.setattr(Conn, "executemany", flaky)
    sockets = []
    msgs = [frames(payload(i, VehicleSpeed=float(i))) for i in range(5)]
    s.socket_factory = lambda url: sockets.append(Socket(msgs, s)) or sockets[-1]
    s.url = "tcp://receiver.example.test:5284"
    s.run()
    assert len(sockets) == 1, "one socket for the whole run: the database never tore it down"
    assert fails["n"] == 3 and sum(len(b) for b in db.batches) == 5 and not s.held


def test_records_held_past_the_limit_drop_the_oldest_and_are_counted(monkeypatch):
    monkeypatch.setattr(tesla_live, "HELD_MAX", 5)
    s = stream()
    for i in range(8):
        s.handle(frames(payload(i, VehicleSpeed=float(i))))
    assert len(s.held) == 5 and s.lost == 3 and s.held[0][1] == T0 + timedelta(minutes=3)


def test_the_pause_starts_short_again_after_the_receiver_is_heard():
    s = stream()
    calls = []

    class OneThenGone:
        def __init__(self):
            self.sent = False

        def recv(self, timeout_ms):
            if not self.sent:
                self.sent = True
                return frames(payload(1, VehicleSpeed=10.0))
            raise OSError("receiver went away")

        def close(self):
            pass

    def factory(url):
        calls.append(url)
        if len(calls) == 2:
            return OneThenGone()
        if len(calls) == 3:
            s.stop()
        raise OSError("receiver not up yet")

    s.socket_factory, s.url = factory, "tcp://receiver.example.test:5284"
    s.flush = lambda: 0
    waits = []
    s.stopped.wait = lambda t: waits.append(t) or s.stopped.is_set()
    s.run()
    assert waits == [1, 1, 2], "a record heard between failures resets the pause"


def test_trouble_is_said_and_the_thread_tries_again_after_a_pause():
    s = stream()
    tries = []

    def factory(url):
        tries.append(url)
        if len(tries) == 2:
            s.stop()
        raise OSError("receiver not up yet")

    s.socket_factory, s.url = factory, "tcp://receiver.example.test:5284"
    waits = []
    s.stopped.wait = lambda t: waits.append(t) or s.stopped.is_set()
    s.run()
    assert len(tries) == 2 and waits == [1, 2] and "receiver not up yet" in s.error


def test_the_thread_starts_only_with_an_address_and_a_car():
    s = tesla_live.TeslaStream(connect=DB().connect)
    started = threading.Event()
    s.run = lambda: started.set()
    s.use([ALPHA])
    assert s.thread is None, "no receiver address: nothing to listen to"
    s.use([], url="tcp://receiver.example.test:5284")
    assert s.thread is None, "no car chosen: nothing to keep"
    s.use([ALPHA, ""])
    assert s.thread is not None and started.wait(2) and s.vins == {ALPHA}


def test_a_restart_restores_each_cars_last_word_and_writes_nothing():
    db = DB()
    db.stored = [
        (T0 + timedelta(minutes=1), payload(1, Locked=False, Location={"latitude": 1.0, "longitude": 2.0})),
        (T0 + timedelta(minutes=2), payload(2, Locked=True, ChargeLimitSoc=80.0)),
        (NOW - tesla_live.RESTORE - timedelta(hours=1), payload(0, ChargeLimitSoc=50.0)),  # too old to read
    ]
    s = stream(db)
    assert s.restore() == 2
    c = s.carried[ALPHA]
    assert c["Locked"] is True and c["ChargeLimitSoc"] == 80.0 and c["_at"] == T0 + timedelta(minutes=2)
    assert not s.moved and not db.batches  # no position written, nothing stored again
