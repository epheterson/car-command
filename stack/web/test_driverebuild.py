"""Drives rebuilt from states and readings: the shapes of 2026-10-15, with Springfield coordinates.

Each shape is a day the database plugin got wrong: a half-minute IGNITION_ON in the middle of a
150-mile drive ended its trip and the rest opened none; a dead zone left an 80-minute hole under
a DRIVING state; the afternoon's drives came after an ignition-on wait the plugin never closed."""

from datetime import datetime, timedelta, timezone

import driverebuild as dr
import history

D0 = datetime(2026, 10, 16, 3, 0, tzinfo=timezone.utc)  # 20:00 Pacific on the 15th
NOW = D0 + timedelta(hours=12)
LAT, LON = 39.7817, -89.6501  # Springfield


def at(h, m, s=0):
    return D0 + timedelta(hours=h, minutes=m, seconds=s)


def st(first, last, state):
    return {"first": first, "last": last, "state": state}


def fixes(t0, t1, odo0, odo1, step=46, speed=60.0):
    """Readings about every `step` seconds from t0 to t1 inclusive, the odometer climbing evenly."""
    n = max(1, round((t1 - t0).total_seconds() / step))
    return [{"at": t0 + (t1 - t0) * i / n, "lat": LAT + i * 1e-4, "lon": LON, "odo": odo0 + (odo1 - odo0) * i / n, "speed": speed} for i in range(n + 1)]


def still(t0, t1, odo, step=46):
    n = int((t1 - t0).total_seconds() // step)
    return [{"at": t0 + timedelta(seconds=i * step), "lat": LAT, "lon": LON, "odo": odo, "speed": 0.0} for i in range(n + 1)]


def trip(i, a, b, manual=False):
    return {"id": i, "start": a, "end": b, "manual": manual}


# a 150-mile drive with a 46-second ignition-on stop at its 50-mile mark
BLIP_STATES = [
    st(at(0, 0), at(0, 6, 40), "PARKED"),
    st(at(0, 7, 11), at(0, 12, 11), "IGNITION_ON"),
    st(at(0, 12, 40), at(1, 1, 56), "DRIVING"),
    st(at(1, 2, 25), at(1, 2, 41), "IGNITION_ON"),
    st(at(1, 3, 11), at(2, 45, 12), "DRIVING"),
    st(at(2, 45, 42), at(2, 45, 57), "IGNITION_ON"),
    st(at(2, 47, 12), at(2, 47, 12), "PARKED"),
]
BLIP_FIXES = (
    fixes(at(0, 12, 40), at(1, 2, 20), 1878.3, 1913.9) + still(at(1, 2, 29), at(1, 3, 9), 1914.0) + fixes(at(1, 3, 15), at(2, 45, 0), 1914.0, 2029.0) + still(at(2, 45, 50), at(2, 45, 50), 2029.1)
)


def test_a_brief_ignition_on_stop_does_not_end_the_drive():
    drives = dr.plan_drives(BLIP_STATES, BLIP_FIXES, now=NOW)
    assert len(drives) == 1
    assert drives[0]["start"] == at(0, 12, 40)  # the 5-minute ignition-on wait before it is not part of the drive
    assert drives[0]["end"] == at(2, 45, 42)
    assert 150 < drives[0]["miles"] < 152
    assert drives[0]["unlogged"] is None


def test_the_plugins_half_drive_is_given_the_rest_and_keeps_its_id_and_its_start():
    wanted = dr.plan_drives(BLIP_STATES, BLIP_FIXES, now=NOW)
    acts = dr.plan_changes(wanted, [trip(130985, at(0, 7, 11), at(1, 2, 25))])
    assert [a["do"] for a in acts] == ["change"]
    assert acts[0]["id"] == 130985 and acts[0]["start"] == at(0, 7, 11) and acts[0]["drive"]["end"] == at(2, 45, 42)


def dead_zone_day():
    """Fixes 15:49:56-16:16:52 (odometer 1856.35-1872.13), nothing until 17:37:06 (1877.16), then to 17:39:55."""
    base = datetime(2026, 10, 15, 22, 0, tzinfo=timezone.utc)  # 15:00 Pacific
    t = lambda h, m, s=0: base + timedelta(hours=h, minutes=m, seconds=s)  # noqa: E731
    states = [
        st(t(0, 7, 23), t(0, 49, 38), "IGNITION_ON"),
        st(t(0, 49, 52), t(2, 39, 55), "DRIVING"),
        st(t(2, 40, 24), t(2, 44, 10), "IGNITION_ON"),
        st(t(2, 44, 39), t(2, 45, 40), "PARKED"),
    ]
    fx = fixes(t(0, 49, 56), t(1, 16, 52), 1856.35, 1872.13) + fixes(t(2, 37, 6), t(2, 39, 55), 1877.16, 1878.28)
    return states, fx, t


def test_a_dead_zone_makes_two_drives_and_unlogged_miles_not_a_line():
    states, fx, t = dead_zone_day()
    drives = dr.plan_drives(states, fx, now=t(10, 0))
    assert [(d["start"], d["end"]) for d in drives] == [(t(0, 49, 52), t(1, 16, 52)), (t(2, 37, 6), t(2, 40, 24))]
    assert drives[0]["unlogged"] is None
    assert round(drives[1]["unlogged"]["miles"], 2) == 5.03 and round(drives[1]["unlogged"]["minutes"]) == 80
    assert round(drives[0]["miles"], 1) == 15.8 and round(drives[1]["miles"], 1) == 1.1


def test_the_afternoons_lost_drives_are_created_around_the_one_the_plugin_kept():
    base = datetime(2026, 10, 15, 21, 0, tzinfo=timezone.utc)  # 14:00 Pacific
    t = lambda h, m, s=0: base + timedelta(hours=h, minutes=m, seconds=s)  # noqa: E731
    states = [
        st(t(0, 8, 38), t(0, 8, 38), "IGNITION_ON"),
        st(t(0, 8, 53), t(0, 14, 8), "DRIVING"),
        st(t(0, 14, 38), t(0, 49, 9), "IGNITION_ON"),  # 35 minutes parked with the ignition on
        st(t(0, 49, 38), t(1, 6, 54), "DRIVING"),
        st(t(1, 7, 23), t(1, 49, 38), "IGNITION_ON"),  # 42 more
        st(t(1, 49, 52), t(1, 51, 30), "DRIVING"),
        st(t(1, 52, 0), t(1, 56, 0), "PARKED"),
    ]
    fx = fixes(t(0, 8, 56), t(0, 14, 0), 1845.66, 1846.84) + fixes(t(0, 49, 42), t(1, 6, 50), 1846.96, 1856.35) + fixes(t(1, 49, 56), t(1, 51, 20), 1856.4, 1857.6)
    drives = dr.plan_drives(states, fx, now=t(9, 0))
    assert [d["start"] for d in drives] == [t(0, 8, 38), t(0, 49, 38), t(1, 49, 52)]
    acts = dr.plan_changes(drives, [trip(5, t(0, 8, 38), t(0, 14, 38))])
    assert [a["do"] for a in acts] == ["keep", "create", "create"]


def apply(existing, acts):
    """acts applied to an in-memory trips list, as history.apply_trip_plan applies them to rows."""
    out = {e["id"]: dict(e) for e in existing}
    n = max(out, default=0)
    for a in acts:
        w = a.get("drive")
        if a["do"] == "create":
            n += 1
            out[n] = trip(n, w["start"], w["end"])
        elif a["do"] == "change":
            out[a["id"]].update(start=a["start"], end=w["end"])
        elif a["do"] == "merge":
            del out[a["id"]]
    return list(out.values())


def test_a_second_run_changes_nothing():
    wanted = dr.plan_drives(BLIP_STATES, BLIP_FIXES, now=NOW)
    existing = [trip(1, at(0, 7, 11), at(1, 2, 25))]
    once = dr.plan_changes(wanted, existing)
    after = apply(existing, once)
    again = dr.plan_changes(wanted, after)
    assert [a["do"] for a in once] == ["change"] and [a["do"] for a in again] == ["keep"]
    states, fx, t = dead_zone_day()
    wanted = dr.plan_drives(states, fx, now=t(10, 0))
    once = dr.plan_changes(wanted, [])
    assert [a["do"] for a in dr.plan_changes(wanted, apply([], once))] == ["keep", "keep"]


def test_a_trip_spanning_a_dead_zone_is_cut_and_the_rest_created():
    states, fx, t = dead_zone_day()
    wanted = dr.plan_drives(states, fx, now=t(10, 0))
    acts = dr.plan_changes(wanted, [trip(9, t(0, 7, 23), t(2, 40, 24))])
    assert [a["do"] for a in acts] == ["change", "create"]
    assert acts[0]["id"] == 9 and acts[0]["drive"]["end"] == t(1, 16, 52)


def test_trips_inside_one_drive_are_merged_into_the_best_one():
    wanted = dr.plan_drives(BLIP_STATES, BLIP_FIXES, now=NOW)
    existing = [trip(1, at(0, 7, 11), at(1, 2, 25)), trip(2, at(1, 3, 11), at(2, 45, 42))]
    acts = dr.plan_changes(wanted, existing)
    assert [a["do"] for a in acts] == ["change", "merge"]
    assert acts[0]["id"] == 2 and acts[1]["id"] == 1  # the trip that covers more keeps its id


def test_a_long_ignition_on_stop_or_a_charge_ends_the_drive_a_short_one_does_not():
    base = at(0, 0)
    t = lambda m, s=0: base + timedelta(minutes=m, seconds=s)  # noqa: E731

    def day(stop_min):
        states = [st(t(0), t(10), "DRIVING"), st(t(10, 30), t(10, 30 + stop_min * 60 - 30), "IGNITION_ON"), st(t(10 + stop_min, 30), t(30), "DRIVING"), st(t(31), t(40), "PARKED")]
        fx = fixes(t(0, 20), t(10), 100, 105) + still(t(10, 30), t(10 + stop_min), 105) + fixes(t(10 + stop_min, 30), t(30), 105, 112)
        return states, fx

    assert len(dr.plan_drives(*day(4), now=NOW)) == 1
    assert len(dr.plan_drives(*day(6), now=NOW)) == 2
    states, fx = day(4)
    assert len(dr.plan_drives(states, fx, charges=[(t(11), t(13))], now=NOW)) == 2


def test_a_stationary_run_breaks_the_drive_only_after_five_minutes():
    t = lambda m, s=0: at(0, 0) + timedelta(minutes=m, seconds=s)  # noqa: E731
    states = [st(t(0), t(30), "DRIVING"), st(t(31), t(40), "PARKED")]

    def day(stop_min):
        return fixes(t(0, 10), t(10), 100, 105) + still(t(10, 46), t(10 + stop_min), 105) + fixes(t(10 + stop_min, 46), t(30), 105, 112)

    assert len(dr.plan_drives(states, day(4), now=NOW)) == 1
    assert len(dr.plan_drives(states, day(7), now=NOW)) == 2


def test_a_drive_going_now_or_just_over_waits():
    states = [st(at(0, 0), at(0, 30), "DRIVING")]
    fx = fixes(at(0, 0, 10), at(0, 30), 100, 120)
    assert dr.plan_drives(states, fx, now=at(0, 35)) == []
    assert len(dr.plan_drives(states + [st(at(0, 31), at(0, 32), "PARKED")], fx, now=at(0, 50))) == 1


def test_a_drive_entered_by_hand_or_still_open_is_not_touched():
    wanted = dr.plan_drives(BLIP_STATES, BLIP_FIXES, now=NOW)
    assert [a["do"] for a in dr.plan_changes(wanted, [trip(7, at(1, 3, 0), at(2, 46), manual=True)])] == ["hand"]
    assert [a["do"] for a in dr.plan_changes(wanted, [trip(7, at(1, 3, 0), None)])] == ["wait"]


def test_a_trip_with_no_drive_under_it_is_left_alone():
    assert dr.plan_changes([], [trip(3, at(0, 0), at(0, 1))]) == []


class Cur:
    def __init__(self, rows=None):
        self.rows, self.description = rows or [], None

    def fetchall(self):
        return self.rows


class Conn:
    def __init__(self):
        self.sql = []

    def execute(self, sql, args=()):
        self.sql.append((sql, args))
        if "to_regclass" in sql:
            cur = Cur([{"t": "x"}])
            cur.description = [type("D", (), {"name": "t"})()]
            return cur
        return Cur()


def test_the_plan_is_written_with_ids_kept_and_what_was_worked_out_dropped():
    wanted = dr.plan_drives(BLIP_STATES, BLIP_FIXES, now=NOW)
    conn = Conn()
    counts = history.apply_trip_plan(conn, "V1", dr.plan_changes(wanted, [trip(1, at(0, 7, 11), at(1, 2, 25)), trip(2, at(1, 3, 11), at(1, 30))]))
    assert counts == {"create": 0, "change": 1, "merge": 1, "remove": 0}
    sql = " ".join(s for s, _ in conn.sql)
    assert "UPDATE trips SET" in sql and "DELETE FROM trips" in sql and "DELETE FROM cc_trip_facts" in sql and "INSERT INTO trips" not in sql
    update = next(a for s, a in conn.sql if s.startswith("UPDATE trips"))
    assert update[-1] in (1, 2)
    assert dr.line(dr.plan_changes(wanted, [])[0]).startswith("create ")


def test_a_trip_that_only_sat_a_while_past_the_last_reading_is_left_as_it_is():
    states = [st(at(0, 0), at(0, 10), "DRIVING"), st(at(0, 11), at(0, 12), "PARKED")]
    wanted = dr.plan_drives(states, fixes(at(0, 0, 10), at(0, 10), 100, 105), now=NOW)
    assert [a["do"] for a in dr.plan_changes(wanted, [trip(1, at(0, 0), at(0, 10) + timedelta(minutes=13))])] == ["keep"]
    assert [a["do"] for a in dr.plan_changes(wanted, [trip(1, at(0, 0), at(0, 10) + timedelta(minutes=40))])] == ["change"]


def test_the_background_rebuild_runs_for_connector_cars_at_most_every_five_minutes(monkeypatch):
    import importlib
    import os

    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    os.environ.setdefault("REST_USER", "u")
    os.environ.setdefault("REST_PASSWORD", "p")
    app = importlib.import_module("app")
    ran = []

    class Db:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(app, "db", lambda: Db())
    monkeypatch.setattr(app.driverebuild, "connector_cars", lambda conn, since: [("V1", "Alpha")])
    monkeypatch.setattr(app.driverebuild, "run", lambda conn, vin, since: ran.append(vin) or [])
    monkeypatch.setattr(app.chargestart, "run", lambda conn, vin, since: ran.append("charges " + vin) or [])
    monkeypatch.setattr(app, "_rebuild_at", float("-inf"))
    app.rebuild_connector_drives()
    app.rebuild_connector_drives()
    assert ran == ["V1", "charges V1"]
    monkeypatch.setattr(app, "_rebuild_at", float("-inf"))
    monkeypatch.setenv("CC_DRIVE_REBUILD", "0")
    app.rebuild_connector_drives()
    assert ran == ["V1", "charges V1"]


# --- a car on and never moved: trip 131074's shape, 2026-10-21 22:38:07 to 23:26:22 Pacific ----------

P0 = datetime(2026, 10, 22, 5, 38, 7, tzinfo=timezone.utc)
P1 = datetime(2026, 10, 22, 6, 26, 22, tzinfo=timezone.utc)
PNOW = P1 + timedelta(hours=2)
SIT_STATES = [
    st(P0 - timedelta(hours=3), P0 - timedelta(seconds=30), "PARKED"),
    st(P0, P0 + timedelta(minutes=2), "IGNITION_ON"),
    st(P0 + timedelta(minutes=2, seconds=30), P1 - timedelta(seconds=40), "DRIVING"),  # keyless driving tested: the state moved, the car did not
    st(P1 - timedelta(seconds=10), P1, "IGNITION_ON"),
    st(P1 + timedelta(seconds=30), P1 + timedelta(hours=1), "PARKED"),
]


def sitting(t0=P0, t1=P1, odo=4210.4, jitter=0.0):
    n = int((t1 - t0).total_seconds() // 46)
    return [{"at": t0 + timedelta(seconds=i * 46), "lat": LAT + (jitter * 1e-5 if i % 2 else 0), "lon": LON, "odo": odo, "speed": None} for i in range(n + 1)]


def sit_trip(**kw):
    return {**trip(131074, P0, P1), "odo0": 4210.4, "odo1": 4210.4, "lat0": LAT, "lon0": LON, "lat1": LAT, "lon1": LON, **kw}


def test_trip_131074_as_polled_one_reading_inside_and_its_own_ends_agreeing():
    """As it was really read: the car on and still is polled rarely, so the trip held one fix
    (22:38:43), with others just outside (22:36:42, 23:26:25). Its own two ends carry the same
    odometer and place: with the one fix that is enough to say it never moved."""
    real = [{"at": t, "lat": LAT, "lon": LON, "odo": 4210.4, "speed": 0} for t in (P0 - timedelta(seconds=85), P0 + timedelta(seconds=36), P1 + timedelta(seconds=3))]
    gone = dr.phantom_trips([sit_trip()], real, [], PNOW)
    assert [(a["do"], a["id"]) for a in gone] == [("remove", 131074)]
    assert dr.car_on(SIT_STATES, real, [], PNOW), "the readings either side say it stood: car-on time"
    moved_end = sit_trip(odo1=4210.6)
    assert dr.phantom_trips([moved_end], real, [], PNOW) == [], "ends that disagree: a real drive, kept"
    assert dr.phantom_trips([sit_trip(odo0=None)], real, [], PNOW) == [], "one fix and no odometer at an end: not enough to call it still"


def test_trip_131074_never_moved_so_the_rebuild_removes_it():
    assert dr.plan_drives(SIT_STATES, sitting(), now=PNOW) == []
    gone = dr.phantom_trips([sit_trip()], sitting(), [], PNOW)
    assert [(a["do"], a["id"]) for a in gone] == [("remove", 131074)]
    assert dr.line(gone[0]).startswith("remove  #131074 10-21 22:38-10-21 23:26")
    assert len(dr.phantom_trips([sit_trip()], sitting(jitter=3.0), [], PNOW)) == 1  # GPS wander of a few metres is still one place


def test_a_real_drive_is_never_taken_for_one_that_sat():
    moved = [{"at": P0 + timedelta(seconds=i * 46), "lat": LAT + i * 1.2e-5, "lon": LON, "odo": 4210.4, "speed": None} for i in range(60)]  # 130 m, odometer unmoved
    assert dr.phantom_trips([sit_trip(end=P0 + timedelta(seconds=59 * 46))], moved, [], PNOW) == []
    step = sitting()
    step[-1] = dict(step[-1], odo=4210.45)  # one odometer step of MIN_MI
    assert dr.phantom_trips([sit_trip()], step, [], PNOW) == []
    speed = sitting()
    speed[10] = dict(speed[10], speed=2.0)
    assert dr.phantom_trips([sit_trip()], speed, [], PNOW) == []
    assert dr.phantom_trips([sit_trip(odo1=4210.5)], sitting(), [], PNOW) == []  # the trip's own odometer says it moved
    assert dr.phantom_trips([sit_trip(lat1=LAT + 5e-4)], sitting(), [], PNOW) == []  # and its own end position


def test_a_short_real_drive_of_a_tenth_of_a_mile_stays():
    states = [st(P0, P0 + timedelta(minutes=3), "DRIVING"), st(P0 + timedelta(minutes=4), P0 + timedelta(minutes=9), "PARKED")]
    fx = fixes(P0 + timedelta(seconds=5), P0 + timedelta(minutes=3), 4210.4, 4210.5, step=20, speed=12.0)
    for f in fx:
        f["lat"] = LAT + (f["odo"] - 4210.4) * 1.4e-2  # 0.1 mi north: about 160 m
    short = {**trip(7, P0, P0 + timedelta(minutes=3)), "odo0": 4210.4, "odo1": 4210.5, "lat0": LAT, "lon0": LON, "lat1": fx[-1]["lat"], "lon1": LON}
    assert dr.phantom_trips([short], fx, dr.plan_drives(states, fx, now=PNOW), PNOW) == []
    assert dr.phantom_trips([short], fx, [], PNOW) == []  # nor on its readings alone, with no drive worked out
    fx0 = [dict(f, speed=None, odo=None) for f in fx]  # no odometer, no speed: the positions still say it moved
    assert dr.phantom_trips([dict(short, odo0=None, odo1=None)], fx0, [], PNOW) == []


def test_a_trip_is_left_when_nothing_proves_it_sat():
    assert dr.phantom_trips([sit_trip()], [], [], PNOW) == []  # no readings
    assert dr.phantom_trips([sit_trip(odo1=None)], sitting()[:1], [], PNOW) == []  # one reading, and an end that says nothing
    assert dr.phantom_trips([sit_trip(manual=True)], sitting(), [], PNOW) == []  # drive entered by hand
    assert dr.phantom_trips([sit_trip(end=None)], sitting(), [], PNOW) == []  # still open
    assert dr.phantom_trips([sit_trip()], sitting(), [], P1 + timedelta(minutes=3)) == []  # ended a moment ago
    wanted = [{"start": P0 + timedelta(minutes=30), "end": P1 + timedelta(minutes=10)}]  # a real drive over the end of it: plan_changes' to keep
    assert dr.phantom_trips([sit_trip()], sitting(), wanted, PNOW) == []


def test_the_time_in_the_car_is_its_own_stretch_not_a_drive():
    car = dr.car_on(SIT_STATES, sitting(), [], PNOW)
    assert [(a["do"], a["start"], a["end"]) for a in car] == [("caron", P0, P1 + timedelta(seconds=30))]
    assert dr.line(car[0]).startswith("car on  10-21 22:38-10-21 23:26 49 min parked")
    assert dr.car_on(SIT_STATES, sitting(), [(P0, P1)], PNOW) == []  # a trip that stays holds that time
    assert dr.car_on(SIT_STATES, sitting(), [], P1 + timedelta(minutes=3)) == []  # not settled yet
    assert dr.car_on(SIT_STATES, [], [], PNOW) == []  # nothing says it stood
    short = [st(P0, P0 + timedelta(minutes=3), "IGNITION_ON"), st(P0 + timedelta(minutes=4), P0 + timedelta(minutes=9), "PARKED")]
    assert dr.car_on(short, sitting(P0, P0 + timedelta(minutes=4)), [], PNOW) == []  # a light, not a sit
    waiting = [st(P0, P0 + timedelta(minutes=29), "IGNITION_ON"), st(P0 + timedelta(minutes=30), P0 + timedelta(minutes=60), "PARKED")]
    assert [round(_s / 60) for _s in [(a["end"] - a["start"]).total_seconds() for a in dr.car_on(waiting, sitting(P0, P0 + timedelta(minutes=30)), [], PNOW)]] == [30]
    held = dr.car_on(SIT_STATES, sitting(), [(P0 - timedelta(minutes=1), P0 + timedelta(minutes=10))], PNOW)  # a drive over the first ten minutes
    assert held and held[0]["start"] == P0 + timedelta(minutes=10)


class RunConn(Conn):
    def commit(self):
        pass


def test_the_background_run_removes_the_trip_and_records_the_time(monkeypatch):
    conn = RunConn()
    monkeypatch.setattr(dr.driveedits, "lock", lambda *a: None)
    monkeypatch.setattr(dr.driveedits, "unapply", lambda *a: None)
    monkeypatch.setattr(dr.driveedits, "reapply", lambda *a: None)
    monkeypatch.setattr(dr, "load", lambda c, vin, lo, hi: (SIT_STATES, sitting(), [], [sit_trip(), trip(5, P1 - timedelta(days=1), P1 - timedelta(days=1) + timedelta(minutes=9), manual=True)]))
    acts = dr.run(conn, "VIN-ALPHA", P0 - timedelta(days=1), now=PNOW)
    assert [(a["do"], a.get("id")) for a in acts] == [("remove", 131074), ("caron", None)]
    sql = " ".join(s for s, _ in conn.sql)
    assert "DELETE FROM trips WHERE id = ANY" in sql and "NOT IN (SELECT ref_id FROM cc_manual" in sql  # a hand-entered drive is never deleted
    deleted = next(a for s, a in conn.sql if s.startswith("DELETE FROM trips"))
    assert deleted == ([131074],)
    assert "INSERT INTO cc_car_on" in sql
    dry = RunConn()
    dr.run(dry, "VIN-ALPHA", P0 - timedelta(days=1), now=PNOW, dry_run=True)
    assert dry.sql == []  # a dry run reads and writes nothing here


def test_car_on_rows_already_right_are_not_rewritten():
    class Have(Conn):
        def execute(self, sql, args=()):
            self.sql.append((sql, args))
            return Cur([(P0, P1 + timedelta(seconds=30))] if sql.startswith("SELECT") else [])

    conn = Have()
    car = [{"do": "caron", "start": P0, "end": P1 + timedelta(seconds=30)}]
    assert history.apply_car_on(conn, "VIN-ALPHA", P0 - timedelta(days=1), PNOW, car) is False
    assert [s for s, _ in conn.sql if not s.startswith("SELECT")] == []
    assert history.apply_car_on(conn, "VIN-ALPHA", P0 - timedelta(days=1), PNOW, []) is True
