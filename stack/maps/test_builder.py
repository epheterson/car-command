import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from builder import Builder, RateLimited
from state import State

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
R = {"id": "us/illinois", "name": "Illinois", "pbf": "https://example.invalid/us/illinois.osm.pbf"}


def make(tmp_path, body=b"pbf-bytes", md5=None, status=200, fail_on=None):
    s = State(tmp_path / "state.json")
    s.add(R, len(body), "drive", NOW)
    calls = []

    def fetch(url, dest):
        if status == 429:
            raise RateLimited(url)
        dest.write_bytes(body)
        return status

    def fetch_text(url):
        return f"{md5 or hashlib.md5(body).hexdigest()}  illinois-latest.osm.pbf\n"

    def run(argv):
        calls.append(argv[0])
        if fail_on and argv[0] == fail_on:
            import subprocess

            raise subprocess.CalledProcessError(1, argv)
        out = argv[argv.index("-o") + 1] if "-o" in argv else None
        if out:
            from pathlib import Path

            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"x")

    b = Builder(tmp_path, s, fetch, fetch_text, run, lambda: NOW)
    b.free = lambda: 10**13  # these tests are not about disk space
    return s, b, calls


def test_a_good_download_builds_and_swaps(tmp_path):
    s, b, calls = make(tmp_path)
    assert b.step() == "built v1"
    assert (tmp_path / "current").resolve() == (tmp_path / "builds" / "v1").resolve()
    assert s.queue == [] and s.regions["us/illinois"]["refreshed"] == NOW.isoformat()
    assert calls[:1] == ["osmium"] and "valhalla_build_tiles" in calls


def test_a_bad_md5_builds_nothing_and_keeps_current(tmp_path):
    s, b, _ = make(tmp_path, md5="0" * 32)
    assert b.step().startswith("failed: checksum")
    assert not (tmp_path / "current").exists() and s.queue == ["us/illinois"]


def test_a_429_waits_and_builds_nothing(tmp_path):
    s, b, calls = make(tmp_path, status=429)
    assert b.step() == "waiting"
    assert s.wait_until is not None and calls == [] and s.queue == ["us/illinois"]


def test_a_failed_tile_build_leaves_the_old_version_serving(tmp_path):
    s, b, _ = make(tmp_path)
    assert b.step() == "built v1"
    s.add({**R, "id": "us/indiana", "pbf": "https://example.invalid/us/indiana.osm.pbf"}, 9, "drive", NOW)
    b.run = make(tmp_path, fail_on="valhalla_build_tiles")[1].run
    assert b.step().startswith("failed: valhalla_build_tiles")
    assert (tmp_path / "current").resolve() == (tmp_path / "builds" / "v1").resolve()


def test_a_download_error_waits_and_builds_nothing(tmp_path):
    def fetch_raises(url, dest):
        dest.write_bytes(b"partial data")
        raise OSError("connection reset")

    s = State(tmp_path / "state.json")
    s.add(R, 9, "drive", NOW)
    b = Builder(tmp_path, s, fetch_raises, lambda url: "abc", lambda argv: None, lambda: NOW)
    result = b.step()
    assert result.startswith("failed: download")
    assert s.wait_until is not None
    assert s.queue == ["us/illinois"]
    assert not (tmp_path / "current").exists()
    # .part file should be cleaned up
    assert not (tmp_path / "pbf" / "us__illinois.osm.pbf.part").exists()


def test_a_non_200_status_builds_nothing(tmp_path):
    s, b, _ = make(tmp_path, status=500)
    result = b.step()
    assert result.startswith("failed: HTTP 500")
    assert not (tmp_path / "current").exists()
    assert s.queue == ["us/illinois"]
    # .part file should be cleaned up
    assert not (tmp_path / "pbf" / "us__illinois.osm.pbf.part").exists()


def test_after_a_failed_build_the_next_build_uses_a_fresh_directory(tmp_path):
    s, b, _ = make(tmp_path)
    assert b.step() == "built v1"
    s.add({**R, "id": "us/indiana", "pbf": "https://example.invalid/us/indiana.osm.pbf"}, 9, "drive", NOW)
    b.run = make(tmp_path, fail_on="valhalla_build_tiles")[1].run
    result = b.step()
    assert result.startswith("failed: valhalla_build_tiles")
    # v2 was attempted but removed after failure
    assert not (tmp_path / "builds" / "v2").exists()
    # But state.build remembers the version was tried
    assert s.build.get("version") == 1
    # Now add another region and build again, should use v2 (not reuse the same attempt).
    # A failed build now backs off like any other failure (fix round 1, item 1), so an
    # explicit retry (what POST /retry does) is needed before the next step() runs.
    s.add({"id": "us/missouri", "name": "Missouri", "pbf": "https://example.invalid/us/missouri.osm.pbf"}, 9, "drive", NOW)
    s.reset_backoff()
    b.run = make(tmp_path)[1].run
    result = b.step()
    assert result == "built v2"
    assert (tmp_path / "current").resolve() == (tmp_path / "builds" / "v2").resolve()


def test_three_consecutive_failures_stop_automatic_retries(tmp_path):
    s, b, _ = make(tmp_path, md5="0" * 32)
    for _ in range(3):
        assert b.step().startswith("failed: checksum")
        s.wait_until = None  # simulate the backoff window elapsing
    assert s.failures == 3
    assert b.step() == "stopped"
    # a user action (what POST /retry does) resets failures and wait_until
    s.reset_backoff()
    assert b.step().startswith("failed: checksum")
    assert s.failures == 1


def test_a_verified_pbf_is_not_re_downloaded(tmp_path):
    s, b, calls = make(tmp_path)
    assert b.step() == "built v1"
    md5 = s.regions["us/illinois"]["md5"]
    assert md5 is not None
    # requeue the same region; its pbf is already on disk with the recorded md5
    s.queue = ["us/illinois"]
    fetch_calls = []

    def fetch(url, dest):
        fetch_calls.append(url)
        dest.write_bytes(b"pbf-bytes")
        return 200

    b.fetch = fetch
    assert b.step() == "built v2"
    assert fetch_calls == []  # never hit the network: the pbf on disk already matched


def test_quiet_valhalla_logging_sets_a_very_large_long_request():
    from builder import quiet_valhalla_logging

    config = {"logging": {"type": "std_out", "color": True, "file_name": "path_to_some_file.log"}}
    quiet_valhalla_logging(config)
    assert config["logging"]["long_request"] >= 100_000_000
    assert config["logging"]["type"] == "std_out"  # untouched


def test_quiet_valhalla_logging_is_a_no_op_when_loki_has_no_logging_block():
    from builder import quiet_valhalla_logging

    config = {"logging": {"type": "std_out"}, "loki": {"service_defaults": {"radius": 0}}}
    quiet_valhalla_logging(config)
    assert "logging" not in config["loki"]
    assert config["logging"]["long_request"] >= 100_000_000


def test_a_region_removed_before_the_merge_reads_it_is_superseded_not_failed(tmp_path):
    # A DELETE arrives (and its pbf is unlinked) exactly as osmium tries to read it: the merge
    # fails, but this is stale work, not a real failure.
    s, b, _ = make(tmp_path)
    assert b.step() == "built v1"
    s.add({**R, "id": "us/indiana", "pbf": "https://example.invalid/us/indiana.osm.pbf"}, 9, "drive", NOW)

    def run_missing_input(argv):
        if argv[0] == "osmium" and "merge" in argv:
            s.remove("us/indiana")
            (tmp_path / "pbf" / "us__indiana.osm.pbf").unlink(missing_ok=True)
            raise subprocess.CalledProcessError(1, argv)
        out = argv[argv.index("-o") + 1] if "-o" in argv else None
        if out:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"x")

    b.run = run_missing_input
    result = b.step()
    assert result == "superseded"
    assert s.failures == 0
    assert s.wait_until is None
    assert s.dirty is True


def test_a_region_removed_after_the_merge_still_builds_but_leaves_dirty(tmp_path):
    # The merge already read the old pbf successfully when the removal lands: the build
    # finishes (and goes live), but dirty stays True so the very next step corrects it.
    s, b, _ = make(tmp_path)
    assert b.step() == "built v1"
    s.add({**R, "id": "us/indiana", "pbf": "https://example.invalid/us/indiana.osm.pbf"}, 9, "drive", NOW)
    removed = []

    def run_remove_after_merge(argv):
        out = argv[argv.index("-o") + 1] if "-o" in argv else None
        if out:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"x")
        if argv[0] == "osmium" and "merge" in argv and not removed:
            removed.append(True)
            s.remove("us/indiana")  # a DELETE lands right after the merge captured the old data

    b.run = run_remove_after_merge
    result = b.step()
    assert result == "built v2"
    assert s.dirty is True
    assert s.failures == 0


def test_any_build_exception_is_a_failure_with_backoff_and_no_crash(tmp_path):
    s, b, _ = make(tmp_path)

    def run_out_of_space(argv):
        raise OSError(28, "No space left on device")  # ENOSPC

    b.run = run_out_of_space
    result = b.step()  # must not raise
    assert result.startswith("failed: OSError")
    assert s.build["status"] == "failed"
    assert "No space left" in s.build["error"]
    assert s.wait_until is not None
    assert s.failures == 1


def test_a_build_keeps_only_what_serves(tmp_path):
    """The merged extract and the place export are only inputs: a build that kept them would
    hold two or three copies of every region on disk."""
    s, b, _ = make(tmp_path)
    assert b.step() == "built v1"
    left = {p.name for p in (tmp_path / "builds" / "v1").iterdir()}
    assert not left & {"merged.osm.pbf", "places.osm.pbf", "places.geojsonseq"}, left
    assert "valhalla.json" in left


def test_prune_deletes_every_build_but_the_one_current_points_at(tmp_path):
    s, b, _ = make(tmp_path)
    assert b.step() == "built v1"
    s.queue = ["us/illinois"]
    assert b.step() == "built v2"
    (tmp_path / "builds" / "v3").mkdir()  # half-finished: nothing points at it
    (tmp_path / "builds" / "v3" / "merged.osm.pbf").write_bytes(b"x" * 10)
    b.prune()
    assert sorted(p.name for p in (tmp_path / "builds").iterdir()) == ["v2"]
    assert (tmp_path / "current").resolve() == (tmp_path / "builds" / "v2").resolve()


def test_prune_with_nothing_current_deletes_every_build(tmp_path):
    s, b, _ = make(tmp_path)
    (tmp_path / "builds" / "v4").mkdir(parents=True)
    b.prune()
    assert list((tmp_path / "builds").iterdir()) == []


def test_not_enough_disk_fails_the_build_before_it_starts(tmp_path):
    """Three times the extracts plus a gigabyte, or no build: a disk that fills mid-build
    breaks more than the build."""
    s, b, calls = make(tmp_path, body=b"x" * 1000)
    b.free = lambda: 1_000_000_000  # 1 GB free; 3 x 1000 B + 1 GB is needed
    result = b.step()
    assert result.startswith("failed: not enough disk"), result
    assert s.build["status"] == "failed" and s.build["error"] == "not enough disk: need 1.0 GB, have 1.0 GB"
    assert "valhalla_build_tiles" not in calls and "osmium" not in calls
    assert s.failures == 1 and s.wait_until is not None
    assert not (tmp_path / "current").exists()
    b.free = lambda: 10**13
    s.wait_until = None
    assert b.step() == "built v1"


def test_three_failures_stop_for_a_day_then_try_again_on_their_own(tmp_path):
    from datetime import timedelta

    clock = {"now": NOW}
    s, b, _ = make(tmp_path, md5="0" * 32)
    b.now = lambda: clock["now"]
    for _ in range(3):
        assert b.step().startswith("failed: checksum")
        s.wait_until = None
    assert b.step() == "stopped"
    clock["now"] = NOW + timedelta(hours=23)
    assert b.step() == "stopped"
    clock["now"] = NOW + timedelta(hours=24, minutes=1)
    assert b.step().startswith("failed: checksum"), "a day stopped resets the count by itself"
    assert s.failures == 1


def test_a_region_whose_pbf_is_missing_is_downloaded_even_when_not_queued(tmp_path):
    """A region re-added during a build, or left without its file by a removal, must fetch
    its extract rather than fail the merge three times and stop."""
    s, b, calls = make(tmp_path)
    assert b.step() == "built v1"
    indiana = {**R, "id": "us/indiana", "pbf": "https://example.invalid/us/indiana.osm.pbf"}
    s.add(indiana, 9, "drive", NOW)
    s.queue = []  # its queue entry was lost; only dirty says to rebuild
    s.dirty = True
    fetched = []
    inner = b.fetch

    def fetch(url, dest):
        fetched.append(url)
        return inner(url, dest)

    b.fetch = fetch
    assert b.step() == "built v2"
    assert fetched == [indiana["pbf"]], "only the missing file is fetched"
    assert (tmp_path / "pbf" / "us__indiana.osm.pbf").exists()


def test_clearing_keeps_a_region_added_after_the_snapshot(tmp_path):
    s, b, _ = make(tmp_path)
    assert b.step() == "built v1"
    s.remove("us/illinois")
    s.dirty = True
    indiana = {**R, "id": "us/indiana", "pbf": "https://example.invalid/us/indiana.osm.pbf"}
    real_unlink = Path.unlink

    def unlink_and_add(self, *a, **k):
        if self.name == "current" and "us/indiana" not in s.regions:
            s.add(indiana, 9, "drive", NOW)  # a request lands while the clear runs
        return real_unlink(self, *a, **k)

    Path.unlink = unlink_and_add
    try:
        assert b.step() == "cleared"
    finally:
        Path.unlink = real_unlink
    assert s.queue == ["us/indiana"], "the new region's queue entry survives the clear"
    assert b.step() == "built v2"


def test_a_failed_swap_is_a_build_failure_with_backoff(tmp_path):
    import os as _os

    import builder as builder_mod

    s, b, _ = make(tmp_path)
    real = builder_mod.os.replace

    def broken(src, dst):
        if str(dst).endswith("current"):
            raise OSError(5, "Input/output error")
        return real(src, dst)

    builder_mod.os.replace = broken
    try:
        result = b.step()
    finally:
        builder_mod.os.replace = real
    assert result.startswith("failed: OSError"), result
    assert s.failures == 1 and s.wait_until is not None and s.build["status"] == "failed"
    assert not (tmp_path / "current").exists() and not (tmp_path / "builds" / "v1").exists()
    assert not _os.path.lexists(tmp_path / "current.tmp")
