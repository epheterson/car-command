"""The terrain model: tile names, the shrink from an SRTM tile, bilinear reads, a drive's
heights replaced by the ground under it, and the download pass, all on tiny synthetic grids."""

import gzip
import json
import math
import os
import struct
import urllib.error
import sys
from array import array
from datetime import datetime, timedelta, timezone

import pytest

import terrain
from day import elevation_profile

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def hgt(side, fn):
    """An SRTM tile (big-endian int16, north row first) whose reading at row i, column j is fn(i, j)."""
    a = array("h", [fn(i, j) for i in range(side) for j in range(side)])
    if sys.byteorder == "little":
        a.byteswap()
    return a.tobytes()


def tile_file(n, fn):
    """A finished tile file n per side, height fn(north_frac, east_frac) metres."""
    a = array("h", [round(fn(i / (n - 1), j / (n - 1))) for i in range(n) for j in range(n)])
    return terrain.MAGIC + struct.pack("<HH", n, 0) + a.tobytes()


def ramp_terrain(tmp_path, name="N39W090", fn=lambda north, east: 1000 * (1 - north)):
    """A square rising 1000 m from its south edge to its north edge."""
    (tmp_path / f"{name}.cct").write_bytes(tile_file(101, fn))
    return terrain.Terrain(tmp_path)


def test_tile_names_for_every_hemisphere():
    assert terrain.tile_name(37.77, -122.42) == "N37W123"
    assert terrain.tile_name(39.8, -89.65) == "N39W090"
    assert terrain.tile_name(-33.9, 151.2) == "S34E151"
    assert terrain.tile_name(0.5, 0.5) == "N00E000" and terrain.tile_name(-0.5, -0.5) == "S01W001"
    assert terrain.tile_corner("S34E151") == (-34, 151) and terrain.tile_corner("N39W090") == (39, -90)
    assert terrain.tiles_for([(39.8, -89.65, 1), (39.9, -89.7, 2), (None, 1, 3)]) == {"N39W090"}
    assert terrain.tiles_for([(63.4, 10.4)]) == {"N63E010"}, "the archive reaches past 60 degrees north"
    stray = [(39.5, -89.5)] * 3 + [(41.5, -89.5)]
    assert terrain.tiles_for(stray, k=3) == {"N39W090"}, "one stray fix asks for nothing"


def test_convert_averages_each_block_and_keeps_the_edges_on_the_tile_edges():
    side = 7  # 3 per side after the shrink, at readings 0, 3 and 6
    out = terrain.convert(hgt(side, lambda i, j: 100 * i + j), factor=3)
    assert out[:4] == terrain.MAGIC and struct.unpack_from("<HH", out, 4) == (3, 0)
    t = terrain.Tile(out)
    # centre of the grid: the mean of rows 2-4, columns 2-4 = 300 + 3
    assert t.cells[4] == 303
    # the north-west corner: rows 0-1, columns 0-1 only (clipped at the edge) = 50 + 0.5, rounded
    assert t.cells[0] == round((0 + 1 + 100 + 101) / 4)
    with pytest.raises(ValueError):
        terrain.convert(b"\x00" * 10)
    with pytest.raises(ValueError):
        terrain.convert(hgt(8, lambda i, j: 0))  # 7 steps do not divide by 3


def test_convert_leaves_voids_out_of_the_mean():
    raw = hgt(7, lambda i, j: -32768 if (i, j) == (3, 3) else 500)
    t = terrain.Tile(terrain.convert(raw, factor=3))
    assert t.cells[4] == 500, "one void in the block does not drag the mean"
    t = terrain.Tile(terrain.convert(hgt(7, lambda i, j: -32768 if i < 4 and j < 4 else 500), factor=3))
    assert t.cells[4] == 500 and t.cells[0] == terrain.VOID, "a block that is all void stays void"


def test_bilinear_read_hits_the_grid_points_and_blends_between_them():
    t = terrain.Tile(tile_file(3, lambda north, east: 100 * east))  # 0, 50, 100 west to east
    assert t.height(0.0, 0.5) == 0 and t.height(1.0, 0.5) == 100 and t.height(0.5, 0.5) == 50
    assert t.height(0.25, 0.5) == pytest.approx(25) and t.height(0.75, 0.0) == 75
    assert t.height(1.0, 1.0) == pytest.approx(100), "the far edge is inside the tile"
    n = terrain.Tile(tile_file(3, lambda north, east: 100 * (1 - north)))
    assert n.height(0.5, 1.0) == 100 and n.height(0.5, 0.0) == 0, "row 0 is the north edge"


def test_a_void_corner_is_averaged_around_and_all_void_is_none():
    cells = array("h", [terrain.VOID, 10, 20, 30])
    t = terrain.Tile(terrain.MAGIC + struct.pack("<HH", 2, 0) + cells.tobytes())
    assert t.height(0.5, 0.5) == 20
    assert terrain.Tile(terrain.MAGIC + struct.pack("<HH", 2, 0) + array("h", [terrain.VOID] * 4).tobytes()).height(0.5, 0.5) is None


def test_terrain_reads_by_square_and_says_what_is_missing(tmp_path):
    ter = ramp_terrain(tmp_path)
    assert ter.height(39.5, -89.5) == pytest.approx(500, abs=0.1)
    assert ter.height(39.25, -89.9) == pytest.approx(250, abs=0.1)
    assert ter.height(40.5, -89.5) is None, "no tile there"
    assert ter.height(None, 1) is None and ter.height("x", 1) is None and ter.height(61, 0) is None
    (tmp_path / "N40W090.none").write_bytes(b"")
    assert ter.missing([(39.5, -89.5), (40.5, -89.5), (41.5, -89.5)], k=1) == {"N41W090"}, "a downloaded square and a sea square are not missing"
    assert ter.have() == {"N39W090"} and ter.names() == {"N39W090", "N40W090"} and ter.size_bytes() > 0 and ter
    assert not terrain.Terrain(None) and not terrain.Terrain(tmp_path / "nowhere") and terrain.Terrain(tmp_path / "nowhere").height(39.5, -89.5) is None


def test_a_damaged_tile_reads_as_no_tile(tmp_path):
    (tmp_path / "N39W090.cct").write_bytes(b"junk")
    assert terrain.Terrain(tmp_path).height(39.5, -89.5) is None


def test_only_the_recent_tiles_stay_in_memory(tmp_path):
    for lat in range(30, 40):
        (tmp_path / f"N{lat}W090.cct").write_bytes(tile_file(3, lambda a, b: 5))
    ter = terrain.Terrain(tmp_path, keep=3)
    for lat in range(30, 40):
        assert ter.height(lat + 0.5, -89.5) == 5
    assert len(ter._tiles) == 3


def drive(points):
    return [{"at": T0 + timedelta(seconds=30 * i), "latitude": la, "longitude": lo, "elevation": 7.0} for i, (la, lo) in enumerate(points)]


def test_a_drive_takes_the_ground_under_it(tmp_path):
    ter = ramp_terrain(tmp_path)
    track = drive([(39.1 + 0.01 * i, -89.5) for i in range(60)])  # north, 0.6 degrees: 600 m up
    ground, ok = terrain.ground_track(track, ter)
    assert ok and ground[0]["elevation"] == pytest.approx(100, abs=0.5) and ground[-1]["elevation"] == pytest.approx(690, abs=0.5)
    assert track[0]["elevation"] == 7.0, "the input is untouched"
    _series, climb_ft, descent_ft = elevation_profile(ground)
    assert climb_ft == pytest.approx(590 * 3.28084, rel=0.01) and descent_ft == 0


def test_ground_ignores_gps_wander_and_counts_the_same_hill_for_every_car(tmp_path):
    """Two cars on one road: one with no height at all, one whose GPS height wanders by 6 m.
    On the ground model they climb and descend the same."""
    ter = ramp_terrain(tmp_path, fn=lambda north, east: 300 + 200 * (1 - abs(2 * north - 1)))  # up 200 m, then down 200 m
    pts = [(39.0 + 0.01 * i, -89.5) for i in range(101)]
    blank = [dict(p, elevation=None) for p in drive(pts)]
    wander = [dict(p, elevation=300 + (6 if i % 2 else -6)) for i, p in enumerate(drive(pts))]
    a, b = (elevation_profile(terrain.ground_track(t, ter)[0]) for t in (blank, wander))
    assert a[1:] == b[1:] and a[1] == pytest.approx(200 * 3.28084, rel=0.02) and a[2] == pytest.approx(200 * 3.28084, rel=0.02)
    gps = elevation_profile(wander)
    assert gps[1] > 6 * 3.28084 * 3, "the GPS reading is the noisier of the two"


def test_flat_ground_is_not_a_climb_whatever_the_hysteresis(tmp_path):
    ter = ramp_terrain(tmp_path, fn=lambda north, east: 100 + 3 * ((round(north * 100) % 2) * 2 - 1))  # +/-3 m ripple, under the 8 m turn
    _s, climb, descent = elevation_profile(terrain.ground_track(drive([(39.0 + 0.01 * i, -89.5) for i in range(90)]), ter)[0])
    assert climb == 0 and descent == 0


def test_a_drive_is_ground_only_when_the_model_covers_it(tmp_path):
    ter = ramp_terrain(tmp_path)
    half = drive([(39.5, -89.5)] * 5 + [(40.5, -89.5)] * 5)  # half of it off the square
    kept, ok = terrain.ground_track(half, ter)
    assert not ok and kept is half, "mixing two sources in one drive would draw a step"
    mostly = drive([(39.5, -89.5)] * 19 + [(40.5, -89.5)])
    g, ok = terrain.ground_track(mostly, ter)
    assert ok and g[-1]["elevation"] is None, "the odd reading with no ground is left without a height"
    assert terrain.ground_track(half, terrain.Terrain(None)) == (half, False) and terrain.ground_track([], ter) == ([], False)


def test_fetch_converts_and_files_a_tile_and_marks_the_sea(tmp_path):
    asked = []

    def get(url):
        asked.append(url)
        return None if "N10" in url else gzip.compress(hgt(7, lambda i, j: 42))

    assert terrain.fetch("N39W090", tmp_path, get, factor=3) == "ok"
    assert asked == ["https://elevation-tiles-prod.s3.amazonaws.com/skadi/N39/N39W090.hgt.gz"]
    assert terrain.Terrain(tmp_path).height(39.5, -89.5) == 42 and not list(tmp_path.glob("*.part"))
    assert terrain.fetch("N10E010", tmp_path, get, factor=3) == "sea" and (tmp_path / "N10E010.none").exists()


def test_a_download_pass_takes_one_at_a_time_and_stops_when_the_net_or_the_disk_does(tmp_path):
    ter = terrain.Terrain(tmp_path)
    body = gzip.compress(hgt(7, lambda i, j: 1))
    want = {"N39W090", "N39W091", "N39W092", "N39W093"}
    out = terrain.download_pass(ter, want, get=lambda u: body, limit=2, factor=3)
    assert out == {"ok": 2, "sea": 0, "left": 2, "failed": 0, "stopped": None} and ter.have() == {"N39W090", "N39W091"}
    assert ter.height(39.5, -89.5) == 1, "the new tile is readable at once"
    assert terrain.download_pass(ter, want, get=lambda u: body, factor=3)["ok"] == 1, "the default is one a pass: the shrink is CPU work"

    def down(url):
        raise urllib.error.URLError("no route")

    out = terrain.download_pass(ter, {"N38W092"}, get=down)
    assert out["stopped"] == "network" and out["left"] == 1 and out["failed"] == 0
    out = terrain.download_pass(ter, {"N38W092"}, get=lambda u: body, free_bytes=lambda: 1_000_000_000, factor=3)
    assert out["stopped"] == "disk" and out["ok"] == 0
    assert terrain.download_pass(ter, ter.names(), get=down) == {"ok": 0, "sea": 0, "left": 0, "failed": 0, "stopped": None}, "nothing wanted that is not here"


def test_a_tile_that_arrives_unusable_backs_off_and_is_given_up_not_asked_for_forever(tmp_path):
    ter, fails, asked = terrain.Terrain(tmp_path), {}, []

    def junk(url):
        asked.append(url)
        return gzip.compress(b"not a tile")

    clock = [1000.0]
    run = lambda **kw: terrain.download_pass(ter, {"N39W090", "N39W091"}, get=junk, fails=fails, now=lambda: clock[0], **kw)  # noqa: E731
    out = run(limit=2)
    assert out["ok"] == 0 and fails["N39W090"]["n"] == 1 and fails["N39W091"]["n"] == 1 and "ValueError" in fails["N39W090"]["error"], "one bad tile does not stop the next"
    assert out["stopped"] is None and out["left"] == 2 and not list(tmp_path.glob("*.part"))
    run(limit=2)
    assert len(asked) == 2, "an hour has not passed: neither is asked for again"
    for hours, tries in ((1, 4), (6, 6)):
        clock[0] += hours * 3600 + 1
        run(limit=2)
        assert len(asked) == tries
    clock[0] += 100 * 86400
    out = run(limit=2)
    assert len(asked) == 6 and out["failed"] == 2 and out["left"] == 0, "given up after the third try"
    s = terrain.status(ter, {"N39W090", "N39W091"}, True, fails=fails)
    assert s["gave_up"] == 2 and s["error"] and s["mb_left"] == 0


def test_a_folder_that_refuses_the_write_is_found_before_anything_is_downloaded(tmp_path):
    asked = []
    ter, fails = terrain.Terrain(tmp_path / "terrain"), {}
    tmp_path.chmod(0o500)  # the parent refuses the folder; a pre-made folder owned by someone else refuses the probe the same way
    try:
        if os.access(tmp_path, os.W_OK):
            pytest.skip("running as a user the mode does not bind")
        out = terrain.download_pass(ter, {"N39W090"}, get=lambda u: asked.append(u), fails=fails, factor=3)
    finally:
        tmp_path.chmod(0o700)
    assert asked == [], "nothing is fetched for a folder that cannot take it"
    assert out["stopped"] == "disk" and "Can't write the terrain folder" in out["error"] and fails == {}


def test_the_worker_keeps_a_disk_stop_for_settings_and_clears_it_when_it_mends(tmp_path):
    on = [True]
    folder = tmp_path / "t"
    state = MemState()
    w = terrain.Worker(terrain.Terrain(folder), lambda: on[0], lambda: [(39.5, -89.5, T0)] * 3, state, lambda: 0, lambda: 0, set(), get=lambda u: None)
    tmp_path.chmod(0o500)
    try:
        if os.access(tmp_path, os.W_OK):
            pytest.skip("running as a user the mode does not bind")
        assert w.tick()["stopped"] == "disk"
        assert "Can't write" in state.d["terrain_disk"]
    finally:
        tmp_path.chmod(0o700)
    w.tick()
    assert "terrain_disk" not in state.d
    s = terrain.status(terrain.Terrain(folder), {"N39W090"}, True, disk_error="Can't write the terrain folder (x)")
    assert s["disk"] and s["error"].startswith("Can't write")


def test_status_counts_squares_names_regions_and_the_download_still_to_come(tmp_path):
    ter = ramp_terrain(tmp_path)
    rof, nof = (lambda la, lo: "US-IL"), (lambda c: {"US-IL": "Illinois"}.get(c))
    s = terrain.status(ter, {"N39W090", "N39W091", "N40W090"}, True, rof, nof)
    assert (s["need"], s["have"], s["left"], s["mb_left"], s["state"], s["regions"]) == (3, 1, 2, 2 * terrain.TILE_MB, "waiting", ["Illinois"])
    assert terrain.status(ter, {"N39W090"}, False)["state"] == "ready"
    assert terrain.status(ter, {"N39W090", "N39W091"}, False)["state"] == "paused"
    assert terrain.status(terrain.Terrain(None), set(), False)["state"] == "off"
    assert terrain.status(terrain.Terrain(None), set(), True)["state"] == "waiting"


def test_a_truncated_tile_with_a_good_header_reads_as_no_tile_not_an_error(tmp_path):
    whole = tile_file(11, lambda a, b: 50)
    (tmp_path / "N39W090.cct").write_bytes(whole[: len(whole) // 2])
    (tmp_path / "N40W090.cct").write_bytes(terrain.MAGIC + struct.pack("<HH", 1, 0) + b"\x00\x00")
    ter = terrain.Terrain(tmp_path)
    assert ter.height(39.5, -89.5) is None and ter.height(40.5, -89.5) is None
    with pytest.raises(ValueError):
        terrain.Tile(whole + b"\x00\x00")


def test_southern_and_eastern_squares_read_the_right_way_round(tmp_path):
    # S34E151: north edge 300 m, south edge 0, rising to the east by 100 m
    (tmp_path / "S34E151.cct").write_bytes(tile_file(101, lambda north, east: 300 * (1 - north) + 100 * east))
    ter = terrain.Terrain(tmp_path)
    assert ter.height(-33.0001, 151.0) == pytest.approx(300, abs=1), "-33.0 is the north edge of S34"
    assert ter.height(-33.9999, 151.0) == pytest.approx(0, abs=1)
    assert ter.height(-33.5, 151.99) == pytest.approx(150 + 99, abs=1)


def test_the_two_sides_of_a_tile_edge_agree(tmp_path):
    """One slope across two neighbouring squares: reading either side of the seam gives the
    same height to within a grid step."""
    side = 7

    def tile(first_col):
        return terrain.convert(hgt(side, lambda i, j: 10 * (first_col + j)), factor=3)

    (tmp_path / "N39W091.cct").write_bytes(tile(0))  # columns 0..6 of the slope, its east edge is column 6
    (tmp_path / "N39W090.cct").write_bytes(tile(6))  # which is this one's first
    ter = terrain.Terrain(tmp_path)
    west, east = ter.height(39.5, -90.0000001), ter.height(39.5, -90.0)
    assert abs(west - east) <= 30 and 40 < west < 80


def test_a_real_sized_tile_shrinks_to_1201_a_side_in_good_time_and_the_sea_floor_is_a_void():
    import time

    side = 3601
    a = array("h", [120]) * (side * side)
    a[: side * 10] = array("h", [-3000]) * (side * 10)  # ten rows of sea floor along the north edge
    if sys.byteorder == "little":
        a.byteswap()
    t = time.perf_counter()
    out = terrain.convert(a.tobytes())
    took = time.perf_counter() - t
    tile = terrain.Tile(out)
    assert tile.n == 1201 and len(out) == terrain.HEADER + 2 * 1201 * 1201
    assert tile.cells[0] == terrain.VOID and tile.cells[600 * 1201 + 600] == 120
    assert took < 20, f"{took:.1f} s"
    already = terrain.Tile(terrain.convert(hgt(1201, lambda i, j: 7)))
    assert already.n == 1201, "a 3 arc-second tile is kept, not shrunk again"
    with pytest.raises(ValueError):
        terrain.convert(hgt(501, lambda i, j: 0))


class Notched:
    """Ground that rises 1 % along the road with a 60 m gorge between 1.0 and 1.1 km."""

    def height(self, lat, lon):
        x = lon * 111_320 * math.cos(math.radians(lat))
        return 100 + x * 0.01 - (60 if 1000 <= x < 1100 else 0)


def test_a_bridge_over_a_gorge_does_not_become_a_climb_and_a_drop():
    step = 10 / (111_320 * math.cos(math.radians(39.5)))
    track = [{"at": T0 + timedelta(seconds=i), "latitude": 39.5, "longitude": i * step, "elevation": 7.0} for i in range(200)]
    ground, ok = terrain.ground_track(track, Notched())
    assert ok and sum(p["elevation"] is None for p in ground) >= 10, "the gorge's fixes are left out"
    _s, climb, descent = elevation_profile(ground)
    assert climb == pytest.approx(0.01 * 1990 * 3.28084, rel=0.05) and descent == 0, "a steady 1 % rise, no dip"

    # a hill that really is steep for most of the drive is the model's failure: fall back to GPS
    class Cliff:
        def height(self, lat, lon):
            return lon * 5_000_000

    kept, ok = terrain.ground_track(track, Cliff())
    assert not ok and kept is track


class MemState:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v):
        self.d[k] = v

    def delete(self, k):
        self.d.pop(k, None)


def worker(tmp_path, on, fixes, get=None, state=None, backfill=lambda: 0, reset=None, clock=None):
    state = state if state is not None else MemState()
    resets = []
    w = terrain.Worker(
        terrain.Terrain(tmp_path),
        lambda: on[0],
        lambda: [(la, lo, T0) for la, lo in fixes],
        state,
        backfill,
        reset or (lambda: resets.append(1) or 0),
        set(),
        get=get,
        now=(lambda: clock[0]) if clock else (lambda: 1000.0),
    )
    return w, state, resets


def test_with_the_switch_off_nothing_is_fetched_but_the_need_is_worked_out_for_the_settings_row(tmp_path):
    asked = []
    on = [False]
    fixes = [(39.5, -89.5)] * 4 + [(41.5, -89.5)] * 2 + [(38.5, -89.5)] * 3
    w, state, _ = worker(tmp_path, on, fixes, get=lambda u: asked.append(u))
    out = w.tick()
    assert asked == [] and out["idle"] and "ok" not in out, "the core privacy promise: no request while off"
    assert state.d["terrain_need"] == "N38W090,N39W090", "squares with three or more fixes; the two strays ask for nothing"
    assert "terrain_fail" not in state.d and not list(tmp_path.iterdir())


def test_with_the_switch_on_one_square_arrives_and_the_drives_are_looked_at_again_only_at_the_end(tmp_path, monkeypatch):
    on, asked = [True], []
    fixes = [(39.5, -89.5)] * 3 + [(38.5, -89.5)] * 3
    real = terrain.convert
    monkeypatch.setattr(terrain, "convert", lambda raw, factor=None: real(raw, 3))  # a 7-sided test tile

    def get(url):
        asked.append(url)
        return gzip.compress(hgt(7, lambda i, j: 90))

    w, state, resets = worker(tmp_path, on, fixes, get=get)
    a = w.tick()
    assert len(asked) == 1 and a["ok"] == 1 and a["left"] == 1 and resets == [], "one square a pass, no re-look while another is due"
    b = w.tick()
    assert len(asked) == 2 and b["left"] == 0 and resets == [1], "re-looked once, when the last square is in"
    w.tick()
    assert resets == [1] and len(asked) == 2 and state.get("terrain_reset") is None


def test_the_worker_fills_drives_even_with_the_switch_off_once_there_are_tiles(tmp_path):
    (tmp_path / "N39W090.cct").write_bytes(tile_file(5, lambda a, b: 1))
    calls = []
    w, _s, _r = worker(tmp_path, [False], [(39.5, -89.5)] * 3, backfill=lambda: calls.append(1) or 7)
    assert w.tick()["filled"] == 7 and calls == [1]
    w2, _s, _r = worker(tmp_path / "empty", [False], [(39.5, -89.5)] * 3, backfill=lambda: calls.append(2) or 7)
    assert "filled" not in w2.tick() and calls == [1], "no tiles, no backfill"


def test_a_failure_the_worker_sees_is_kept_for_the_settings_row(tmp_path):
    on = [True]
    w, state, _ = worker(tmp_path, on, [(39.5, -89.5)] * 3, get=lambda u: gzip.compress(b"nope"))
    w.tick()
    fails = json.loads(state.d["terrain_fail"])
    assert fails["N39W090"]["n"] == 1 and "ValueError" in fails["N39W090"]["error"]


def test_the_disk_check_works_before_the_folder_exists(tmp_path):
    import shutil

    folder = tmp_path / "terrain"
    ter = terrain.Terrain(folder)
    out = terrain.download_pass(ter, {"N39W090"}, get=lambda u: gzip.compress(hgt(7, lambda i, j: 1)), free_bytes=lambda: shutil.disk_usage(folder).free, factor=3)
    assert out["ok"] == 1 and out["stopped"] is None
