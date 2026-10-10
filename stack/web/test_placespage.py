"""A place's stays, from the drives that arrived there."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from placespage import stays

TZ = ZoneInfo("America/Los_Angeles")
P = [{"id": 1, "latitude": 40.0, "longitude": -74.0, "radius_m": 150}, {"id": 2, "latitude": 41.0, "longitude": -75.0, "radius_m": 150}]


def at(day, h):  # a local time in Springfield's zone; Sep 1 2026 is a Tuesday
    return datetime(2026, 9, day, h, tzinfo=TZ)


def test_stays_are_counted_by_car_with_their_usual_time():
    arr = [("V1", 40.0001, -74.0001, at(d, 8), at(d, 10)) for d in (1, 2, 3, 4)] + [("V2", 40.0, -74.0, at(5, 20), at(6, 7)), ("V1", 45.0, -80.0, at(6, 9), at(6, 11))]
    s = stays(arr, P, TZ)
    assert s[1]["n"] == 5 and s[1]["by_car"] == {"V1": 4, "V2": 1} and s[1]["median"] == timedelta(hours=2) and s[1]["usual"] == "weekday mornings"
    assert 2 not in s or s[2]["n"] == 0, "nobody stopped at the second place"
    assert len(s[1]["points"]) == 5


def test_the_last_arrival_has_no_length_and_a_small_place_has_no_usual():
    s = stays([("V1", 40.0, -74.0, at(1, 8), None), ("V1", 40.0, -74.0, at(2, 8), at(2, 9))], P, TZ)
    assert s[1]["n"] == 2 and s[1]["median"] == timedelta(hours=1) and s[1]["usual"] is None


def test_points_are_capped_and_rounded():
    s = stays([("V1", 40.0 + i * 1e-6, -74.0, at(1, 8), at(1, 9)) for i in range(100)], P, TZ, keep=40)
    assert s[1]["n"] == 100 and len(s[1]["points"]) == 40 and all(len(str(p[0]).split(".")[-1]) <= 5 for p in s[1]["points"])


def test_a_short_stop_keeps_its_minutes():
    s = stays([("V1", 40.0, -74.0, at(1, 8), at(1, 8).replace(minute=5)) for _ in range(3)], P, TZ)
    assert s[1]["median"] == timedelta(minutes=5), "a five-minute stop is not '0 h'"
