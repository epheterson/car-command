import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import match as M

FIX = json.loads((Path(__file__).parent / "fixtures" / "trace_attributes.json").read_text())


def enc6(pts):
    k, out, pl, pn = 10**6, [], 0, 0
    for la, lo in pts:
        a, b = round(la * k), round(lo * k)
        for v in (a - pl, b - pn):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        pl, pn = a, b
    return "".join(out)


def echo(calls=None):
    """A Valhalla stand-in that matches every position to itself, along a road through them all."""

    def call(payload, timeout=None):
        pts = [[p["lat"], p["lon"]] for p in payload["shape"]]
        if calls is not None:
            calls.append(len(pts))
        n = len(pts)
        edges = [{"begin_shape_index": i, "end_shape_index": i + 1, "length": M._meters(pts[i], pts[i + 1]) / 1000} for i in range(n - 1)]
        mp = [{"lat": la, "lon": lo, "type": "matched", "edge_index": min(i, n - 2), "distance_from_trace_point": 0.0} for i, (la, lo) in enumerate(pts)]
        return {"shape": enc6(pts), "edges": edges, "matched_points": mp, "units": "kilometers"}

    return call


def test_a_recorded_valhalla_answer_reads_as_a_line_on_the_roads():
    got = M.parse(FIX["response"])
    assert len(got["shape"]) > 100 and len(got["matched"]) == len(FIX["points"])
    assert abs(got["length_m"] - 9704) < 1
    assert all(m["type"] == "matched" and m["span"] for m in got["matched"])
    out = M.match(FIX["points"], lambda payload, timeout=None: FIX["response"])
    assert out["result"] == "matched" and out["chunks"] == 1 and out["confidence"] == 1.0
    assert 0.97 < out["length_m"] / 9704 < 1.03, "the line's own length agrees with Valhalla's edges"
    straight = sum(M._meters(a, b) for a, b in zip(FIX["points"], FIX["points"][1:]))
    assert out["length_m"] > straight * 1.03, "the roads are longer than the corners cut between readings"


def test_the_request_asks_the_local_matcher_for_a_snapped_trace_with_times():
    b = M.body(FIX["points"])
    assert b["shape_match"] == "map_snap" and b["costing"] == "auto" and b["use_timestamps"] is True
    assert b["shape"][0]["time"] == FIX["points"][0][2]
    assert b["trace_options"]["gps_accuracy"] == M.GPS_ACCURACY_M and b["trace_options"]["search_radius"] == M.SEARCH_RADIUS_M
    assert not M.body([[39.8, -89.6], [39.81, -89.6]])["use_timestamps"]


def test_chunks_overlap_cover_everything_and_stay_short():
    pts = [[39.0 + i * 0.001, -89.6, i * 30] for i in range(250)]
    rs = M.chunks(pts)
    assert rs[0][0] == 0 and rs[-1][1] == 250
    assert all(b - a <= M.CHUNK_POINTS for a, b in rs)
    assert all(rs[i + 1][0] == rs[i][1] - M.OVERLAP for i in range(len(rs) - 1))
    far = [[39.0 + i * 0.05, -89.6] for i in range(60)]  # 5.5 km apart: 330 km in all
    rs = M.chunks(far)
    assert len(rs) > 1 and all(sum(M._meters(far[j], far[j + 1]) for j in range(a, b - 1)) < 200_000 for a, b in rs), "under Valhalla's trace limit"
    assert M.chunks([]) == [] and M.chunks([[1, 2]]) == [(0, 1)]


def test_a_long_drive_is_stitched_back_into_one_line_without_doubling_back():
    pts = [[39.0 + i * 0.001, -89.6 + (i % 7) * 0.0001, 1_780_000_000 + i * 45] for i in range(230)]
    calls = []
    out = M.match(pts, echo(calls))
    assert len(calls) == 3 and max(calls) <= M.CHUNK_POINTS
    lats = [p[0] for p in out["points"]]
    assert lats == sorted(lats), "never steps back over the overlap"
    assert len(out["points"]) == 230, "each position once: the overlap is cut, not repeated"
    whole = sum(M._meters(a[:2], b[:2]) for a, b in zip(pts, pts[1:]))
    assert abs(out["length_m"] - whole) < 1


def test_clean_drops_repeats_and_refuses_nonsense():
    got = M.clean([[39.8, -89.6, 10], [39.8, -89.6, 20], [39.81, -89.6, 20], [39.83, -89.6, 15], [39.82, -89.6, 30]])
    assert got == [[39.8, -89.6, 10.0], [39.81, -89.6, 20.0], [39.82, -89.6, 30.0]], "a repeat and a step back in time are dropped"
    for bad in ([[91, 0]], [["x", 0]], [[1]], ["nope"], [[None, 1]]):
        with pytest.raises(ValueError):
            M.clean(bad)


def test_what_valhalla_refuses_is_a_no_match_with_its_reason():
    assert M._reason({"error_code": 171}) == "outside the built regions"
    assert M._reason({"error_code": 442}) == "no road path fits"

    def refuse(payload, timeout=None):
        raise M.NoMatch("outside the built regions")

    assert M.match(FIX["points"], refuse) == {"result": "no match", "reason": "outside the built regions"}
    assert M.match([[39.8, -89.6]], refuse)["reason"] == "fewer than two positions"


def test_tune_config_lets_a_minute_between_positions_through():
    cfg = M.tune_config({"meili": {"default": {"breakage_distance": 2000, "search_radius": 50}}})
    assert cfg["meili"]["default"] == {"breakage_distance": M.BREAKAGE_M, "search_radius": 50}
    assert M.tune_config({})["meili"]["default"]["breakage_distance"] == M.BREAKAGE_M


# ---- the endpoint ----


def _svc(tmp_path, trace, built=True, popen=None):
    from regions import Index
    from server import Service

    idx = Index(json.loads((Path(__file__).parent / "fixtures" / "index-mini.json").read_text())["features"])
    kw = {"popen": popen} if popen else {}
    svc = Service(tmp_path, index=idx, sizes={}, locate=lambda la, lo: [], trace=trace, **kw)
    if built:
        (tmp_path / "builds" / "v1").mkdir(parents=True)
        (tmp_path / "current").symlink_to("builds/v1")
        svc.state.build["version"] = 1
    return svc


def _post(port, body, raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/match", data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _serve(svc):
    from server import make_server

    srv = make_server(port=0, data=svc.data, service=svc)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_match_endpoint_answers_the_line_and_its_build(tmp_path):
    srv = _serve(_svc(tmp_path, lambda payload, timeout=None: FIX["response"]))
    try:
        code, body = _post(srv.server_address[1], {"points": FIX["points"]})
        assert code == 200 and body["result"] == "matched" and body["version"] == 1 and len(body["points"]) > 100
    finally:
        srv.shutdown()


def test_input_it_cannot_use_is_a_no_match_never_an_error_to_retry(tmp_path):
    """A 400 reads as a failure to the site's client, which would ask again for the same
    drive every tick; a "no match" is recorded and the backfill moves on."""
    srv = _serve(_svc(tmp_path, lambda payload, timeout=None: FIX["response"]))
    port = srv.server_address[1]
    try:
        for body, why in (
            ({"points": "nope"}, "points is not a list"),
            ({"points": [[999, 0], [1, 1]]}, "bad points: out of range"),
            ({"points": [["x", 0], [1, 1]]}, "bad points: points are numbers"),
            ({"nothing": 1}, "points is not a list"),
            ({"points": [[39.8, -89.6]] * (M.MAX_POINTS + 1)}, "too many points"),
        ):
            code, out = _post(port, body)
            assert code == 200 and out == {"result": "no match", "reason": why, "version": 1}, body
        assert _post(port, None, raw=b"{not json")[0] == 400, "not a request the site ever sends"
    finally:
        srv.shutdown()


def test_a_drive_that_takes_too_long_gives_up_and_lets_go_of_valhalla(tmp_path):
    now = [0.0]
    timeouts = []

    def slow(payload, timeout=None):
        timeouts.append(timeout)
        now[0] += 20.0  # each chunk takes twenty seconds
        return echo()(payload)

    pts = [[39.0 + i * 0.001, -89.6, 1_780_000_000 + i * 45] for i in range(400)]
    out = M.match(pts, slow, budget_s=45, clock=lambda: now[0])
    assert out == {"result": "no match", "reason": "too long"} and len(timeouts) == 3
    assert timeouts[0] == M.CALL_TIMEOUT_S and timeouts[-1] == 5.0, "the last call only gets what is left"

    def stuck(payload, timeout=None):
        now[0] += timeout
        raise M.Unavailable("timeout")

    now[0] = 0.0
    assert M.match(pts, stuck, budget_s=8, clock=lambda: now[0]) == {"result": "no match", "reason": "too long"}
    now[0] = 0.0
    with pytest.raises(M.Unavailable):
        M.match(pts, stuck, budget_s=45, clock=lambda: now[0])  # one full call timing out: Valhalla, not the drive

    svc = _svc(tmp_path, lambda payload, timeout=None: (_ for _ in ()).throw(M.Unavailable("x")))
    assert svc.match(FIX["points"]) == {"result": "unavailable"}
    assert svc._matching.acquire(blocking=False), "released after a failure"


def test_the_sites_client_waits_longer_than_the_add_on_works():
    import sys
    from pathlib import Path as P

    sys.path.insert(0, str(P(__file__).parent.parent / "web"))
    import mapsclient

    assert mapsclient.MATCH_TIMEOUT_S > M.BUDGET_S + M.CALL_TIMEOUT_S


def test_no_build_busy_and_down_are_told_apart(tmp_path):
    svc = _svc(tmp_path / "a", lambda p, timeout=None: FIX["response"], built=False)
    assert svc.match(FIX["points"]) == {"result": "no match", "reason": "no local maps built yet", "version": None}

    def down(payload, timeout=None):
        raise M.Unavailable("URLError")

    assert _svc(tmp_path / "b", down).match(FIX["points"]) == {"result": "unavailable"}
    busy = _svc(tmp_path / "c", lambda p, timeout=None: FIX["response"])
    busy._matching.acquire()
    assert busy.match(FIX["points"]) == {"result": "busy"}, "a second drive waits for the next tick, not on the lock"


def test_valhalla_starts_with_the_matcher_tuned(tmp_path):
    started = []

    class P:
        def __init__(self, argv):
            started.append(argv)

        def poll(self):
            return None

    svc = _svc(tmp_path, lambda p, timeout=None: {}, popen=P)
    (tmp_path / "builds" / "v1" / "valhalla.json").write_text(json.dumps({"meili": {"default": {"breakage_distance": 2000}}}))
    svc.start_valhalla()
    assert started and json.loads((tmp_path / "builds" / "v1" / "valhalla.json").read_text())["meili"]["default"]["breakage_distance"] == M.BREAKAGE_M
