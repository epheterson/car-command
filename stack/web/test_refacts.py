"""The facts repair: which drives and charges it finds, what it does about them, and that it
pages a batch at a time and touches nothing on a dry run."""

from datetime import datetime, timezone

import facts
import refacts

T0 = datetime(2026, 10, 13, 23, 0, tzinfo=timezone.utc)


class Col:
    def __init__(self, name):
        self.name = name


class Conn:
    """Answers the two stale-facts queries from lists, a page at a time; records the rest."""

    def __init__(self, trips, charges):
        self.data = {facts.STALE_TRIPS_SQL: trips, facts.STALE_CHARGES_SQL: charges}
        self.calls, self.commits, self.description, self.result = [], 0, None, []

    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        self.description, self.result = None, []
        if sql in self.data:
            _vin, _since, after, batch = args
            rows = [r for r in self.data[sql] if r["id"] > after][:batch]
            self.description = [Col(k) for k in (rows[0] if rows else {"id": 0})]
            self.result = [tuple(r.values()) for r in rows]
        elif sql.startswith("SELECT to_regclass"):
            self.description, self.result = [Col("t")], [(args[0],)]
        return self

    def fetchall(self):
        return self.result

    def commit(self):
        self.commits += 1


def trip(i, end, now_end, range_end=200.0, now_range_end=200.0):
    return {
        "id": i,
        "start_date": T0,
        "miles": 49.0,
        "level_start": 83.0,
        "level_end": end,
        "range_start": 229.0,
        "range_end": range_end,
        "now_level_start": 83.0,
        "now_level_end": now_end,
        "now_range_start": 229.0,
        "now_range_end": now_range_end,
    }


def charge(i, added, now_added):
    return {"id": i, "start_date": T0, "range_added": added, "now_range_added": now_added}


def test_finds_the_drive_whose_end_level_is_not_its_readings():
    conn = Conn([trip(1, 80.83, 62.87, 223.25, 173.5), trip(2, 70.0, 70.0)], [charge(5, 2.92, 42.08), charge(6, 30.0, 30.0)])
    trips, charges = facts.stale_ends(conn, "VINALPHA", T0)
    assert [t["id"] for t in trips] == [1] and [c["id"] for c in charges] == [5]


def test_unfilled_and_sentinel_values_are_not_stale():
    assert not facts._differs("range_end", None, 200.0), "a range not filled yet is the backfill's"
    assert not facts._differs("range_added", -999, None), "looked, nothing there, and still nothing"
    assert not facts._differs("level_end", 62.871, 62.87), "rounding"
    assert facts._differs("level_end", None, 62.87), "a level the fill missed is wrong"
    assert facts._differs("level_end", 80.83, None)


def test_pages_a_batch_at_a_time():
    conn = Conn([trip(i, 80.0, 60.0 if i % 2 else 80.0) for i in range(1, 8)], [])
    trips, _ = facts.stale_ends(conn, "VINALPHA", T0, batch=3)
    assert [t["id"] for t in trips] == [1, 3, 5, 7]
    pages = [a for s, a in conn.calls if s == facts.STALE_TRIPS_SQL]
    assert [p[2] for p in pages] == [0, 3, 6], "each page starts after the last id of the one before"


def test_a_dry_run_touches_nothing():
    conn = Conn([trip(1, 80.83, 62.87)], [charge(5, 2.92, 42.08)])
    trips, charges = facts.repair_ends(conn, "VINALPHA", T0, dry_run=True)
    assert len(trips) == 1 and len(charges) == 1
    assert not [s for s, _ in conn.calls if s.startswith(("DELETE", "UPDATE"))] and conn.commits == 0


def test_a_repair_drops_the_drives_facts_and_route_and_clears_range_added():
    conn = Conn([trip(1, 80.83, 62.87), trip(2, 70.0, 70.0)], [charge(5, 2.92, 42.08)])
    facts.repair_ends(conn, "VINALPHA", T0, dry_run=False)
    writes = [(s, a) for s, a in conn.calls if s.startswith(("DELETE", "UPDATE"))]
    assert ("DELETE FROM cc_trip_facts WHERE trip_id = ANY(%s)", ([1],)) in writes
    assert any(s.startswith("DELETE FROM cc_trip_routes") and a == ("VINALPHA", [T0]) for s, a in writes), "the road line was drawn to the same wrong end"
    assert ("UPDATE cc_charge_facts SET range_added = NULL WHERE session_id = ANY(%s)", ([5],)) in writes


def test_the_report_names_each_drive_with_stored_and_true_levels():
    out = refacts.lines("Alpha", [trip(1, 80.83, 62.87, 223.25, 173.5)], [charge(5, -999, 42.08)], tz=timezone.utc)
    assert out[0] == "Alpha: 1 drive(s), 1 charge(s)"
    assert out[1] == "  drive 1 2026-10-13 23:00 49.0 mi: level 83->80.83 now 83->62.87, range 229->223.25 now 229->173.5"
    assert out[2] == "  charge 5 2026-10-13 23:00: range added - now 42.08"
