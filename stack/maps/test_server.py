import json
import threading
import time
import urllib.error
import urllib.request

from server import make_server


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
        return r.status, json.loads(r.read())


def test_status_answers_before_anything_is_built(tmp_path):
    srv = make_server(port=0, data=tmp_path)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        status, body = _get(srv.server_address[1], "/status")
        assert status == 200 and body["version"] is None and body["regions"] == [] and body["building"] == "idle"
    finally:
        srv.shutdown()


import json as _json
from pathlib import Path as _P

from placeindex import build as _build
from regions import Index as _Index
from server import Service, _now

FIXD = _P(__file__).parent / "fixtures"


def _post(port, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=_json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return _json.loads(r.read())


def _svc(tmp_path, built=True, popen=None):
    idx = _Index(_json.loads((FIXD / "index-mini.json").read_text())["features"])
    kwargs = {}
    if popen is not None:
        kwargs["popen"] = popen
    svc = Service(
        tmp_path,
        index=idx,
        sizes={"us/illinois/springfield-area": 80_000_000, "us/illinois": 450_000_000, "us": 11_000_000_000, "north-america": 15_000_000_000},
        locate=lambda lat, lon: _json.loads((FIXD / "locate.json").read_text()),
        **kwargs,
    )
    svc.builder.free = lambda: 10**13  # these tests are not about disk space
    if built:
        out = tmp_path / "builds" / "v1"
        out.mkdir(parents=True)
        _build(FIXD / "places.geojsonseq", out / "places.sqlite")
        (tmp_path / "current").symlink_to("builds/v1")
        svc.reopen()
    return svc


def _serve(svc):
    srv = make_server(port=0, data=svc.data, service=svc)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_ensure_picks_the_region_and_queues_it(tmp_path):
    srv = _serve(_svc(tmp_path, built=False))
    try:
        port = srv.server_address[1]
        assert _post(port, "/regions/ensure", {"lat": 39.70, "lon": -89.50, "reason": "drive on Oct 3"}) == {"result": "queued", "region": "us/illinois"}
        assert _post(port, "/regions/ensure", {"lat": 0, "lon": 0, "reason": "x"}) == {"result": "nowhere", "region": None}
        assert _get(port, "/status")[1]["queue"] == ["us/illinois"]
    finally:
        srv.shutdown()


def test_reverse_answers_like_nominatim(tmp_path):
    srv = _serve(_svc(tmp_path))
    try:
        status, body = _get(srv.server_address[1], "/reverse?lat=39.801&lon=-89.65&format=jsonv2&zoom=17")
        assert status == 200 and body["address"]["road"] == "Evergreen Terrace" and body["address"]["city"] == "Springfield"
    finally:
        srv.shutdown()


def test_reverse_before_any_build_says_unable(tmp_path):
    srv = _serve(_svc(tmp_path, built=False))
    try:
        assert _get(srv.server_address[1], "/reverse?lat=39.801&lon=-89.65&format=json")[1] == {"error": "Unable to geocode"}
    finally:
        srv.shutdown()


def test_candidates_carry_the_address_choice(tmp_path):
    srv = _serve(_svc(tmp_path))
    try:
        body = _get(srv.server_address[1], "/candidates?lat=39.801&lon=-89.65&limit=10")[1]
        assert body["candidates"][0]["name"] == "Bravo Market"
        assert body["address"] == {"house_number": "742", "road": "Evergreen Terrace", "city": "Springfield"}
    finally:
        srv.shutdown()


def test_bad_coordinates_are_a_400(tmp_path):
    srv = _serve(_svc(tmp_path))
    try:
        try:
            _get(srv.server_address[1], "/reverse?lat=abc&lon=1")
            raise AssertionError("expected 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        srv.shutdown()


# ---- fix round 1 ----


def test_loop_survives_a_step_that_raises(tmp_path):
    svc = _svc(tmp_path, built=False)
    svc.builder.step = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    t = threading.Thread(target=svc.loop, daemon=True)
    t.start()
    time.sleep(0.2)
    assert t.is_alive()  # the loop logged and kept going, it did not die


class _FakePopen:
    instances = []

    def __init__(self, argv):
        self.argv = argv
        self.terminated = False
        self.killed = False
        self._poll = None  # still running
        _FakePopen.instances.append(self)

    def poll(self):
        return self._poll

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def test_valhalla_is_restarted_when_it_exits(tmp_path):
    _FakePopen.instances = []
    out = tmp_path / "builds" / "v1"
    out.mkdir(parents=True)
    (out / "valhalla.json").write_text("{}")
    (tmp_path / "current").symlink_to("builds/v1")
    svc = _svc(tmp_path, built=False, popen=_FakePopen)
    svc.start_valhalla()
    first = svc.valhalla
    assert first is not None and len(_FakePopen.instances) == 1
    first._poll = 1  # it died
    svc.tick()  # the real loop body, not a copy of its condition
    assert svc.valhalla is not first
    assert len(_FakePopen.instances) == 2
    assert first.terminated is True


def test_tick_stops_valhalla_and_clears_places_when_the_last_region_is_removed(tmp_path):
    _FakePopen.instances = []
    svc = _svc(tmp_path, built=False, popen=_FakePopen)
    fake = _FakePopen(["valhalla_service", "x", "1"])
    svc.valhalla = fake
    svc.places = object()  # pretend a Places connection is open
    svc.builder.step = lambda: "cleared"
    result = svc.tick()
    assert result == "cleared"
    assert svc.valhalla is None
    assert svc.places is None
    assert fake.terminated is True


def test_regions_add_stores_none_for_an_unknown_size(tmp_path, monkeypatch):
    svc = _svc(tmp_path, built=False)
    svc.state.sizes.pop("us/illinois", None)  # the fixture seeds a known size: clear it
    monkeypatch.setattr("server._size", lambda url: None)
    srv = _serve(svc)
    try:
        body = _post(srv.server_address[1], "/regions/add", {"id": "us/illinois"})
        assert body == {"result": "queued", "region": "us/illinois"}
        assert svc.state.regions["us/illinois"]["bytes"] is None  # never coerced to 0
        assert "us/illinois" not in svc.state.sizes  # an unknown size is not cached either
    finally:
        srv.shutdown()


def test_delete_unknown_region_is_a_404(tmp_path):
    svc = _svc(tmp_path, built=False)
    srv = _serve(svc)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/regions/does-not-exist", method="DELETE")
        try:
            urllib.request.urlopen(req, timeout=3)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()


def test_delete_sets_dirty_and_removes_the_pbf(tmp_path):
    svc = _svc(tmp_path, built=False)
    idx = svc.index()
    il = idx.by_id["us/illinois"]
    svc.state.add(il, 1, "x", _now())
    svc.state.queue.clear()
    pbf_dir = svc.data / "pbf"
    pbf_dir.mkdir(parents=True)
    (pbf_dir / "us__illinois.osm.pbf").write_bytes(b"x")
    srv = _serve(svc)
    try:
        port = srv.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/regions/us%2Fillinois", method="DELETE")
        with urllib.request.urlopen(req, timeout=3) as r:
            body = _json.loads(r.read())
        assert "us/illinois" not in svc.state.regions
        assert svc.state.dirty is True
        assert not (pbf_dir / "us__illinois.osm.pbf").exists()
        assert body["queue"] == []
    finally:
        srv.shutdown()


def test_builder_rebuilds_without_a_removed_region_and_clears_current_when_empty(tmp_path):
    svc = _svc(tmp_path, built=False)
    idx = svc.index()
    il = idx.by_id["us/illinois"]
    svc.state.add(il, 1, "x", _now())
    svc.state.queue.clear()  # already "built" once, conceptually; now just mark it dirty
    svc.state.dirty = True

    calls = []

    def fake_run(argv):
        calls.append(argv[0])
        if "-o" in argv:
            out = argv[argv.index("-o") + 1]
            from pathlib import Path as _Path

            _Path(out).parent.mkdir(parents=True, exist_ok=True)
            _Path(out).write_bytes(b"x")

    svc.builder.run = fake_run
    (svc.data / "pbf").mkdir(parents=True, exist_ok=True)
    (svc.data / "pbf" / "us__illinois.osm.pbf").write_bytes(b"x")
    result = svc.builder.step()
    assert result == "built v1"
    assert svc.state.dirty is False

    # now remove the only region: nothing left to merge, `current` must go away
    svc.state.remove("us/illinois")
    svc.state.dirty = True
    result = svc.builder.step()
    assert result == "cleared"
    assert not (svc.data / "current").exists()
    assert svc.state.dirty is False


def test_refresh_cadence_requeues_a_stale_monthly_region_but_off_does_not(tmp_path):
    from datetime import timedelta

    svc = _svc(tmp_path, built=False)
    idx = svc.index()
    il = idx.by_id["us/illinois"]
    svc.state.add(il, 1, "x", _now())
    old = (_now() - timedelta(days=40)).isoformat()
    svc.state.queue.clear()
    svc.state.regions["us/illinois"]["refreshed"] = old
    svc.state.regions["us/illinois"]["md5"] = "stale-md5-from-the-last-download"
    svc.state.refresh = "monthly"
    svc._apply_refresh_cadence(_now())
    assert svc.state.queue == ["us/illinois"]
    assert svc.state.regions["us/illinois"]["reason"] == "refresh"
    assert svc.state.regions["us/illinois"]["md5"] is None  # the old pbf is not trusted blind

    svc.state.queue.clear()
    svc.state.refresh = "off"
    svc._apply_refresh_cadence(_now())
    assert svc.state.queue == []


def test_refresh_requeue_actually_refetches_through_a_real_build_step(tmp_path):
    """Drives the real download path (builder.step(), not a copied condition): a region
    re-queued for refresh must hit fetch() again even though a pbf with an old md5 already
    sits on disk."""
    svc = _svc(tmp_path, built=False)
    idx = svc.index()
    il = idx.by_id["us/illinois"]
    svc.state.add(il, 1, "x", _now())
    svc.state.queue.clear()
    svc.state.regions["us/illinois"]["refreshed"] = _now().isoformat()

    pbf_dir = svc.data / "pbf"
    pbf_dir.mkdir(parents=True)
    old_pbf = pbf_dir / "us__illinois.osm.pbf"
    old_pbf.write_bytes(b"old-bytes")
    import hashlib

    svc.state.regions["us/illinois"]["md5"] = hashlib.md5(b"old-bytes").hexdigest()

    from datetime import timedelta as _timedelta

    svc._apply_refresh_cadence(_now() + _timedelta(days=40))
    assert svc.state.queue == ["us/illinois"]
    assert svc.state.regions["us/illinois"]["md5"] is None

    fetch_calls = []

    def fetch(url, dest):
        fetch_calls.append(url)
        dest.write_bytes(b"new-bytes")
        return 200

    def fetch_text(url):
        return hashlib.md5(b"new-bytes").hexdigest() + "  x\n"

    def run(argv):
        if "-o" in argv:
            out = argv[argv.index("-o") + 1]
            from pathlib import Path as _Path

            _Path(out).parent.mkdir(parents=True, exist_ok=True)
            _Path(out).write_bytes(b"x")

    svc.builder.fetch = fetch
    svc.builder.fetch_text = fetch_text
    svc.builder.run = run
    result = svc.builder.step()
    assert result == "built v1"
    assert fetch_calls == [il["pbf"]]  # the network was actually hit
    assert old_pbf.read_bytes() == b"new-bytes"


def test_lock_is_not_held_during_a_slow_download(tmp_path):
    import hashlib

    svc = _svc(tmp_path, built=False)
    idx = svc.index()
    il = idx.by_id["us/illinois"]
    svc.state.add(il, 9, "x", _now())

    gate = threading.Event()
    started = threading.Event()

    def slow_fetch(url, dest):
        started.set()
        gate.wait(5)
        dest.write_bytes(b"pbf-bytes")
        return 200

    svc.builder.fetch = slow_fetch
    svc.builder.fetch_text = lambda url: hashlib.md5(b"pbf-bytes").hexdigest() + "  x\n"
    svc.builder.run = lambda argv: None  # the build itself isn't under test here

    t = threading.Thread(target=svc.builder.step, daemon=True)
    t.start()
    try:
        assert started.wait(3)
        srv = _serve(svc)
        try:
            t0 = time.time()
            body = _post(srv.server_address[1], "/settings", {"auto": True})
            assert time.time() - t0 < 1.0  # /settings answered without waiting on the download
            assert body["auto"] is True
        finally:
            srv.shutdown()
    finally:
        gate.set()
        t.join(5)


def test_handler_503_on_an_unexpected_exception(tmp_path):
    svc = _svc(tmp_path, built=False)
    svc.status = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    srv = _serve(svc)
    try:
        try:
            _get(srv.server_address[1], "/status")
            raise AssertionError("expected 503")
        except urllib.error.HTTPError as e:
            assert e.code == 503
            assert _json.loads(e.read()) == {"error": "unavailable"}
    finally:
        srv.shutdown()


def test_reopen_closes_the_old_place_index(tmp_path):
    """A deleted build's files are only freed once nothing holds them open."""
    import sqlite3

    import pytest

    svc = _svc(tmp_path)
    old = svc.places
    svc.reopen()
    with pytest.raises(sqlite3.ProgrammingError):
        old.con.execute("SELECT 1")
    assert svc.places is not old and svc.places.con.execute("SELECT 1").fetchone() == (1,)


def test_startup_deletes_builds_that_current_does_not_point_at(tmp_path):
    out = tmp_path / "builds" / "v1"
    out.mkdir(parents=True)
    _build(FIXD / "places.geojsonseq", out / "places.sqlite")
    (tmp_path / "current").symlink_to("builds/v1")
    (tmp_path / "builds" / "v2").mkdir()  # a build the last run never finished
    (tmp_path / "builds" / "v2" / "merged.osm.pbf").write_bytes(b"x")
    svc = _svc(tmp_path, built=False)
    assert sorted(p.name for p in (tmp_path / "builds").iterdir()) == ["v1"]
    assert svc.places is not None


def test_a_finished_build_frees_the_old_one_after_reopening(tmp_path):
    svc = _svc(tmp_path)  # serving v1
    old = svc.places
    out = tmp_path / "builds" / "v2"
    out.mkdir()
    _build(FIXD / "places.geojsonseq", out / "places.sqlite")

    def step():
        (tmp_path / "current").unlink()
        (tmp_path / "current").symlink_to("builds/v2")
        return "built v2"

    svc.builder.step = step
    svc.start_valhalla = lambda: None
    assert svc.tick() == "built v2"
    assert sorted(p.name for p in (tmp_path / "builds").iterdir()) == ["v2"]
    import sqlite3

    import pytest

    with pytest.raises(sqlite3.ProgrammingError):
        old.con.execute("SELECT 1")


def test_status_reports_a_cached_disk_size_refreshed_after_each_step(tmp_path, monkeypatch):
    import server

    walks = []
    monkeypatch.setattr(server, "_disk_usage", lambda p: walks.append(p) or 1234)
    svc = _svc(tmp_path, built=False)
    n = len(walks)
    assert svc.status()["disk_bytes"] == 1234 and svc.status()["disk_bytes"] == 1234
    assert len(walks) == n, "status() reads the cached figure, it does not walk the disk"
    monkeypatch.setattr(server, "_disk_usage", lambda p: walks.append(p) or 5678)
    svc.builder.step = lambda: "idle"
    svc.start_valhalla = lambda: None
    svc.tick()
    assert svc.status()["disk_bytes"] == 5678


def test_ensure_heads_from_the_top_and_stops_at_the_first_region_that_fits(tmp_path, monkeypatch):
    """Fewer HEAD requests, and the coarsest region that fits is the only one asked about
    below the continent: the smaller areas the point is in never leave the box."""
    import server

    asked = []
    sizes = {"us": 11_000_000_000, "us/illinois": 450_000_000, "us/illinois/springfield-area": 80_000_000}

    def head(url):
        rid = url.removeprefix("https://example.invalid/").removesuffix(".osm.pbf")
        asked.append(rid)
        return sizes.get(rid)

    monkeypatch.setattr(server, "_size", head)
    svc = _svc(tmp_path, built=False)
    svc.state.sizes.clear()
    assert svc.ensure(39.70, -89.50, "x") == {"result": "queued", "region": "us/illinois"}
    assert asked == ["us", "us/illinois"], "the continent is never a candidate, and the smaller area is never asked"


def test_locate_asks_for_car_roads_within_150_m(monkeypatch):
    import io

    import server

    sent = {}

    def fake_urlopen(req, timeout):
        sent.update(_json.loads(req.data))
        return io.BytesIO(b"[]")

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    assert server._locate(39.80, -89.65) == []
    assert sent["locations"] == [{"lat": 39.80, "lon": -89.65, "radius": 150}]
    assert sent["costing"] == "auto" and sent["verbose"] is True


def test_search_answers_typed_text_from_the_index(tmp_path):
    srv = _serve(_svc(tmp_path))
    try:
        body = _get(srv.server_address[1], "/search?q=742%20everg&lat=39.80&lon=-89.65")[1]
        assert body["built"] is True and body["results"][0]["label"] == "742 Evergreen Terrace" and body["results"][0]["city"] == "Springfield"
        assert _get(srv.server_address[1], "/search?q=bravo")[1]["results"][0]["label"] == "Bravo Market", "no centre: anywhere"
    finally:
        srv.shutdown()


def test_search_before_any_build_is_empty_not_an_error(tmp_path):
    srv = _serve(_svc(tmp_path, built=False))
    try:
        assert _get(srv.server_address[1], "/search?q=742%20everg")[1] == {
            "results": [],
            "built": False,
        }
    finally:
        srv.shutdown()


def test_a_stalled_or_busy_download_is_tried_again_and_a_refusal_is_not():
    from server import TRIES, _tried

    calls, pauses = [], []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError()
        if len(calls) == 2:
            raise urllib.error.HTTPError("u", 504, "Gateway Time-out", {}, None)
        return 200

    assert _tried(flaky, sleep=pauses.append) == 200 and len(calls) == 3 and len(pauses) == 2

    def stalled():
        calls.append(1)
        raise TimeoutError()

    calls.clear()
    try:
        _tried(stalled, sleep=pauses.append)
        raise AssertionError("a download that never answers must fail the step")
    except TimeoutError:
        pass
    assert len(calls) == TRIES

    def missing():
        calls.append(1)
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    calls.clear()
    try:
        _tried(missing, sleep=pauses.append)
    except urllib.error.HTTPError:
        pass
    assert len(calls) == 1, "a 404 is an answer, not a stall"
