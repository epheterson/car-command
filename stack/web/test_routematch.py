from datetime import datetime, timedelta, timezone
from pathlib import Path

import routematch as R

T0 = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)


def test_sparse_is_a_median_over_eight_seconds_between_distinct_readings():
    every = lambda s, n=20: [T0 + timedelta(seconds=s * i) for i in range(n)]  # noqa: E731
    assert R.median_gap(every(1)) == 1 and not R.is_sparse(R.median_gap(every(1))), "a Tesla streaming at 1 Hz draws its own roads"
    assert R.is_sparse(R.median_gap(every(45))), "a Lucid's cloud position"
    assert R.is_sparse(R.median_gap(every(30))), "a TeslaFi history"
    assert R.is_sparse(R.median_gap(every(10))) and not R.is_sparse(R.median_gap(every(8)))
    doubled = sorted(every(45) + every(45))  # two sources stored the same moments
    assert R.median_gap(doubled) == 45
    assert R.median_gap([T0]) is None and not R.is_sparse(None)


def test_the_trace_opens_and_closes_at_the_parked_spots_only_when_near():
    rows = [(T0 + timedelta(seconds=45 * i), 39.80 + 0.001 * i, -89.65) for i in range(5)]
    near_before, near_after = [39.799, -89.65], [39.8045, -89.65]  # about 110 m and 55 m off
    tr = R.trace(rows, near_before, near_after, T0, T0 + timedelta(minutes=4))
    assert tr[0][:2] == near_before and tr[-1][:2] == near_after and len(tr) == 7
    assert tr[0][2] < tr[1][2] and tr[-1][2] > tr[-2][2], "time still moves forward"
    far = [39.81, -89.65]  # over a kilometre: another day's spot, or positions missing
    tr = R.trace(rows, far, far, T0, T0 + timedelta(minutes=4))
    assert len(tr) == 5 and tr[0][:2] == [39.80, -89.65]
    assert R.trace(rows, [39.80, -89.65], None, T0, T0)[0][:2] == [39.80, -89.65] and len(R.trace(rows, [39.80, -89.65], None, T0, T0)) == 5, "the same spot is not added twice"
    assert R.trace([], near_before, near_after, T0, T0) == []


def test_pin_carries_the_matched_line_to_the_kerb():
    line = [[39.8001, -89.65], [39.804, -89.65]]
    out = R.pin(line, [39.7999, -89.6503], [39.8043, -89.6504])
    assert out[0] == [39.7999, -89.6503] and out[-1] == [39.8043, -89.6504] and len(out) == 4
    assert R.pin(line, [39.70, -89.65], None) == line, "a spot kilometres off is not this drive's"
    assert R.pin(line, [39.8001, -89.65], None) == line, "already there"
    assert R.pin([], [39.8, -89.6], None) == []


def test_quality_guard_keeps_raw_when_the_line_disagrees_with_the_car():
    mile = R.MILE_M
    assert R.judge(1.0, 3 * mile, 3 * mile) is None
    assert R.judge(1.0, 3.3 * mile, 3 * mile) is None and R.judge(1.0, 2.2 * mile, 3 * mile) is None
    assert R.judge(1.0, 4.2 * mile, 3 * mile) == "1.40× the odometer's distance"
    assert R.judge(1.0, 1.9 * mile, 3 * mile) == "0.63× the odometer's distance"
    assert R.judge(0.6, 3 * mile, 3 * mile) == "low confidence (0.60)"
    assert R.judge(None, 3 * mile, 3 * mile).startswith("low confidence")
    assert R.judge(1.0, 3 * mile, None) is None, "no odometer: confidence alone"
    assert R.judge(1.0, 400, 0.0) == "longer than the odometer moved", "a drive the odometer says went nowhere is not drawn down a street"
    assert R.judge(1.0, 120, 0.0) is None
    assert R.judge(1.0, 220, 0.05 * mile) is None and R.judge(1.0, 10, 0.05 * mile) is None, "below a tenth of a mile the odometer's rounding is most of it"


def test_polyline_round_trips_compactly():
    pts = [[39.80001, -89.65002], [39.80123, -89.64877], [39.79, -89.7]]
    assert R.decode(R.encode(pts)) == pts
    assert len(R.encode(pts)) < 30


class FakeClient:
    def __init__(self, answers, version=3, up=True):
        self.answers, self.sent, self.version, self.up = list(answers), [], version, up

    def status(self):
        return {"version": self.version} if self.up else None

    def match(self, pts):
        self.sent.append(pts)
        a = self.answers.pop(0) if self.answers else None
        return a(pts) if callable(a) else a


def _trip(i, odo_mi=1.0):
    s = T0 - timedelta(hours=i)
    return {"vin": "V1", "start_date": s, "destination_date": s + timedelta(minutes=5), "start_odometer": 100.0, "destination_odometer": 100.0 + odo_mi}


def _rows(trip, every=45, n=7):
    return [(trip["start_date"] + timedelta(seconds=every * k), 39.80 + 0.0023 * k, -89.65) for k in range(n)]


def _line(mi):
    return [[39.80, -89.65], [39.80 + mi * R.MILE_M / 111_320, -89.65]]


MATCHED = {"result": "matched", "points": _line(1.0), "confidence": 1.0}


def _worker(client, trips, rows_for, saved, slept=None, failed=None, batch=4):
    return R.Worker(
        client,
        lambda version, limit: trips[:limit],
        lambda t: (rows_for(t), None, None),
        lambda t, row: saved.append((t["start_date"], row)),
        lambda t, reason, version: (failed if failed is not None else []).append((t["start_date"], reason, version)),
        sleep=(slept.append if slept is not None else (lambda s: None)),
        batch=batch,
    )


def test_worker_matches_sparse_drives_and_keeps_raw_with_a_reason():
    trips = [_trip(i) for i in range(4)]
    client = FakeClient(
        [
            MATCHED,
            {"result": "matched", "points": _line(2.0), "confidence": 1.0},  # twice the odometer
            {"result": "no match", "reason": "outside the built regions"},
        ]
    )
    saved, slept = [], []
    got = _worker(client, trips, lambda t: _rows(t, 1, 300) if t is trips[3] else _rows(t), saved, slept).tick()
    assert got == {"matched": 1, "raw": 2, "dense": 1, "failed": 0, "waiting": 0}
    rows = [r for _s, r in saved]
    assert rows[0]["status"] == "matched" and R.decode(rows[0]["geometry"])[0] == [39.8, -89.65] and rows[0]["version"] == "3"
    assert rows[0]["geometry_simple"] and rows[0]["geometry_thumb"], "the small lines are made once, here"
    assert rows[1]["status"] == "raw" and rows[1]["reason"] == "2.00× the odometer's distance" and "geometry" not in rows[1]
    assert rows[2] == {**rows[2], "status": "raw", "reason": "outside the built regions"}
    assert rows[3]["status"] == "dense" and len(client.sent) == 3, "a 1 Hz drive is never sent"
    assert slept == [2.0, 2.0], "calls to the add-on are spaced; the first goes at once"
    assert all(len(p) == 3 for p in client.sent[0]), "positions go with their times"


def test_one_drive_that_fails_is_charged_and_the_tick_moves_on():
    trips = [_trip(i) for i in range(3)]
    for bad in (None, {"result": "busy"}, {"result": "unavailable"}):
        saved, failed = [], []
        got = _worker(FakeClient([bad, MATCHED, MATCHED]), trips, _rows, saved, failed=failed).tick()
        assert got == {"matched": 2, "raw": 0, "dense": 0, "failed": 1, "waiting": 0}, bad
        assert failed == [(trips[0]["start_date"], (bad or {}).get("result") or "no answer", "3")]
        assert [s for s, _r in saved] == [trips[1]["start_date"], trips[2]["start_date"]], "the drives after it still get matched"


def test_two_failures_in_a_row_are_an_outage_and_charge_no_drive():
    trips = [_trip(i) for i in range(4)]
    saved, failed = [], []
    client = FakeClient([None, None, MATCHED])
    got = _worker(client, trips, _rows, saved, failed=failed).tick()
    assert got["waiting"] == 2 and failed == [] and saved == [] and len(client.sent) == 2, "stops; nothing charged"


def test_a_last_failure_is_charged_only_while_the_add_on_still_answers():
    trips = [_trip(0)]
    failed = []
    assert _worker(FakeClient([None]), trips, _rows, [], failed=failed).tick()["failed"] == 1 and len(failed) == 1
    failed = []
    client = FakeClient([None])
    w = _worker(client, trips, _rows, [], failed=failed)
    client.status = iter([{"version": 3}, None]).__next__  # up at the start of the tick, gone after
    assert w.tick()["waiting"] == 1 and failed == []


def test_a_drive_that_always_fails_stops_being_asked_after_three_tries():
    """The whole loop with a stand-in for the table: a drive whose match always times out
    is tried MAX_ATTEMPTS times on this build, then the backfill goes past it for good."""
    trips = [_trip(i) for i in range(5)]
    poison = trips[0]["start_date"]
    table: dict = {}

    def candidates(version, limit):
        out = []
        for t in trips:
            r = table.get(t["start_date"])
            if r is None or (r["status"] != "dense" and r.get("version") != version and not (r.get("attempt_version") == version and r["attempts"] >= R.MAX_ATTEMPTS)):
                out.append(t)
        return out[:limit]

    def save(t, row):
        table[t["start_date"]] = {**row, "attempts": 0}

    def fail(t, reason, version):
        r = table.setdefault(t["start_date"], {"status": "failed", "attempts": 0})
        r["attempts"] = r["attempts"] + 1 if r.get("attempt_version") == version else 1
        r["attempt_version"] = version

    client = FakeClient([])
    client.match = lambda pts: None if abs(pts[0][2] - poison.timestamp()) < 1 else MATCHED
    w = R.Worker(client, candidates, lambda t: (_rows(t), None, None), save, fail, batch=2)
    asked = []
    for _ in range(6):
        asked.append([t["start_date"] for t in candidates("3", 2)])
        w.tick()
    assert table[poison] == {"status": "failed", "attempts": 3, "attempt_version": "3"}
    assert all(table[t["start_date"]]["status"] == "matched" for t in trips[1:]), "every other drive got matched"
    assert poison not in asked[-1] and candidates("3", 10) == [], "the backfill is past it"
    assert candidates("4", 10)[0]["start_date"] == poison, "a new build tries it again"


def test_worker_does_nothing_before_a_build():
    saved = []
    client = FakeClient([], version=None)
    assert _worker(client, [_trip(0)], _rows, saved).tick() == {"matched": 0, "raw": 0, "dense": 0, "failed": 0, "waiting": 0}
    assert client.sent == [] and saved == []


def test_each_tick_that_did_anything_says_so_in_one_line():
    assert R.summary({"matched": 0, "raw": 0, "dense": 0, "failed": 0, "waiting": 0}) is None
    assert R.summary({"matched": 0, "raw": 0, "dense": 0, "failed": 1, "waiting": 0}) == "routes: 0 matched, 0 kept raw, 0 dense, 1 failed, 0 waiting on the maps add-on"
    assert "2 waiting" in R.summary({"matched": 0, "raw": 0, "dense": 0, "failed": 0, "waiting": 2})
    src = (Path(__file__).parent / "app.py").read_text()
    body = src[src.index("def routes_tick(") : src.index("def geocoder_tick(")]
    assert "routematch.summary(w.tick())" in body and "LOG.info(line)" in body


def test_candidates_are_newest_first_and_come_back_on_a_new_build_or_a_changed_drive():
    sql = " ".join(R.CANDIDATES_SQL.split())
    assert "ORDER BY t.start_date DESC LIMIT %s" in sql
    assert "r.trip_end IS DISTINCT FROM t.destination_date" in sql
    assert "r.status <> 'dense' AND r.maps_version IS DISTINCT FROM %s" in sql, "dense drives are never re-sent for a new build"
    assert "NOT (r.attempt_version IS NOT DISTINCT FROM %s AND r.attempts >= %s)" in sql, "three failed tries on this build and it waits for the next"
    assert R.candidates_args("3", 6) == ("3", "3", R.MAX_ATTEMPTS, 6) and sql.count("%s") == 4
    fail = " ".join(R.FAIL_SQL.split())
    assert "THEN cc_trip_routes.attempts + 1 ELSE 1 END" in fail, "counted per build"
    assert "WHEN cc_trip_routes.status = 'matched' AND cc_trip_routes.trip_end IS NOT DISTINCT FROM EXCLUDED.trip_end THEN 'matched'" in fail, "an earlier match of the same drive is kept"
    assert "attempts = 0, attempt_version = NULL" in " ".join(R.SAVE_SQL.split())
    for q in (R.SAVE_SQL, R.FAIL_SQL, R.CANDIDATES_SQL):
        assert "cc_positions" not in q, "raw positions are never touched"


def test_simplified_lines_keep_the_turns_and_drop_the_straight():
    road = [[39.80 + i * 0.00005, -89.65] for i in range(200)] + [[39.81, -89.65 + i * 0.00005] for i in range(1, 200)]
    simple = R.simplify(road)
    assert simple[0] == road[0] and simple[-1] == road[-1] and len(simple) <= 4 and any(R.meters(p, [39.81, -89.65]) < 10 for p in simple), "the corner stays"
    wiggle = [[39.8 + i * 0.0001, -89.65 + (0.00003 if i % 2 else 0)] for i in range(50)]  # 2.5 m side to side
    assert len(R.simplify(wiggle)) == 2, "GPS-sized wobble under 10 m goes"
    g = R.geometries(road)
    assert R.decode(g["geometry"]) == [[round(a, 5), round(b, 5)] for a, b in road]
    assert len(R.decode(g["geometry_thumb"])) <= R.THUMB_POINTS


def test_line_for_only_draws_a_match_that_still_fits_its_drive():
    end = T0 + timedelta(minutes=5)
    lines = {T0: (end, R.encode([[39.8, -89.65], [39.81, -89.65]]))}
    assert R.line_for(lines, T0, end) == [[39.8, -89.65], [39.81, -89.65]]
    assert R.line_for(lines, T0, end + timedelta(minutes=1)) is None, "the drive changed since it was matched"
    assert R.line_for(lines, T0 + timedelta(hours=1), end) is None
    assert R.choose([[1, 2], [3, 4]], None) == [[1, 2], [3, 4]] and R.choose([[1, 2], [3, 4]], [[5, 6], [7, 8]]) == [[5, 6], [7, 8]]
    assert R.choose([[1, 2], [3, 4]], [[5, 6]]) == [[1, 2], [3, 4]]


def test_the_route_worker_never_runs_on_a_request():
    """Matching is the background loop's: no page or API handler may call the worker, the
    candidates or the add-on's /match, and the loop hands its tick to a thread."""
    src = (Path(__file__).parent / "app.py").read_text()
    loop = src[src.index("    async def routes_loop():") : src.index("    routes_task = asyncio.create_task")]
    assert "await asyncio.to_thread(routes_tick, w)" in loop and "w.tick()" not in loop
    handlers = src.split("\n@site.")[1:]
    for h in handlers:
        body = h.split("\n\n\n")[0]
        for call in ("routes_tick(", "route_candidates(", "route_save(", "MAPS.match(", "routematch.Worker("):
            assert call not in body, (h.split("\n")[0], call)
