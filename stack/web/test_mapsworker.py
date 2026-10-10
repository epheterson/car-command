from datetime import datetime, timezone

from mapsworker import Worker, cells

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


def test_cells_are_distinct_tenths():
    assert cells([(39.801, -89.651), (39.849, -89.649), (40.01, -89.65)]) == [(39.8, -89.7), (40.0, -89.7)]


class FakeClient:
    def __init__(self):
        self.asked = []

    def ensure(self, lat, lon, reason):
        self.asked.append((lat, lon))
        return {"result": "queued" if len(self.asked) == 1 else "covered", "region": "us/illinois"}

    def status(self):
        return {"version": None}


def test_each_cell_is_asked_once_a_day():
    c = FakeClient()
    w = Worker(c, lambda since: [(39.801, -89.651, NOW), (39.802, -89.652, NOW), (40.01, -89.65, NOW)], lambda: NOW)
    assert w.tick() == {"asked": 2, "queued": 1, "covered": 1}
    assert w.tick() == {"asked": 0, "queued": 0, "covered": 0}


def test_no_service_asks_nothing():
    class Dead:
        def ensure(self, *a):
            return None

        def status(self):
            return None

    w = Worker(Dead(), lambda since: [(39.8, -89.65, NOW)], lambda: NOW)
    assert w.tick() == {"asked": 0, "queued": 0, "covered": 0}


def test_a_failed_ensure_mid_pass_does_not_drop_the_points_after_it():
    """since must only advance past points whose cell was actually handled before the
    break, or the next tick's endpoints_since(since) filters out everything after the
    failure and those cells are never asked again."""
    A = datetime(2026, 10, 3, 10, tzinfo=timezone.utc)
    B = datetime(2026, 10, 3, 11, tzinfo=timezone.utc)
    pts = [(39.8, -89.65, A), (41.0, -90.0, B)]  # two distinct cells, in time order

    class FlakyClient:
        def __init__(self):
            self.calls = 0

        def status(self):
            return {"version": None}

        def ensure(self, lat, lon, reason):
            self.calls += 1
            return None if self.calls == 2 else {"result": "queued"}

    def endpoints_since(since):
        return [p for p in pts if since is None or p[2] > since]

    c = FlakyClient()
    w = Worker(c, endpoints_since, lambda: B)
    assert w.tick() == {"asked": 1, "queued": 1, "covered": 0}
    assert w.since == A  # B's cell failed; since must not jump past it
    assert w.tick() == {"asked": 1, "queued": 1, "covered": 0}  # the service is back
    assert w.since == B


def test_the_reason_a_region_was_fetched_never_carries_a_coordinate():
    """Settings › Maps shows this reason; it used to read "a drive near 39.8, -89.7"."""
    reasons = []

    class Recording(FakeClient):
        def ensure(self, lat, lon, reason):
            reasons.append(reason)
            return super().ensure(lat, lon, reason)

    Worker(Recording(), lambda since: [(39.801, -89.651, NOW)], lambda: NOW).tick()
    assert reasons == ["where your cars drive"]
