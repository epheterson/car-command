from datetime import datetime, timedelta, timezone

from state import State

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
R = {"id": "us/illinois", "name": "Illinois", "pbf": "https://example.invalid/us/illinois.osm.pbf"}


def test_ensure_queues_once_and_remembers_why(tmp_path):
    s = State(tmp_path / "state.json")
    assert s.ensure(R, 450_000_000, "drive on Oct 3", NOW) == "queued"
    assert s.ensure(R, 450_000_000, "another drive", NOW) == "already queued"
    assert s.queue == ["us/illinois"] and s.regions["us/illinois"]["reason"] == "drive on Oct 3"


def test_a_built_region_is_covered(tmp_path):
    s = State(tmp_path / "state.json")
    s.ensure(R, 450_000_000, "drive", NOW)
    s.queue.clear()
    s.regions["us/illinois"]["refreshed"] = NOW.isoformat()
    assert s.ensure(R, 450_000_000, "drive", NOW) == "covered"


def test_auto_off_queues_nothing_but_a_manual_add_still_does(tmp_path):
    s = State(tmp_path / "state.json")
    s.auto = False
    assert s.ensure(R, 1, "drive", NOW) == "off" and s.queue == []
    s.add(R, 1, "added by hand", NOW)
    assert s.queue == ["us/illinois"]


def test_backoff_waits_longer_each_time_and_survives_a_restart(tmp_path):
    s = State(tmp_path / "state.json")
    s.add(R, 1, "x", NOW)
    s.backoff(NOW, 0)
    assert not s.ready(NOW + timedelta(minutes=59)) and s.ready(NOW + timedelta(minutes=61))
    s.backoff(NOW, 1)
    s.save()
    t = State.load(tmp_path / "state.json")
    assert not t.ready(NOW + timedelta(hours=5)) and t.ready(NOW + timedelta(hours=6, minutes=1))


def test_remove_drops_it_from_the_queue_too(tmp_path):
    s = State(tmp_path / "state.json")
    s.add(R, 1, "x", NOW)
    s.remove("us/illinois")
    assert s.queue == [] and "us/illinois" not in s.regions
