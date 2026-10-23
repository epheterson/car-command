"""A drive's route given times and speeds, its time per speed band, and the row's elevation strip."""

from datetime import datetime, timedelta, timezone

from driveview import BANDS, band_labels, band_of, elevation_spark, speed_shares, timed_route

T0 = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)


def fix(i, lat, lon, speed=None, elevation=None):
    return {"at": T0 + timedelta(seconds=60 * i), "latitude": lat, "longitude": lon, "speed": speed, "elevation": elevation}


# Springfield, a drive north along one street, a reading a minute
TRACK = [fix(i, 39.80 + 0.01 * i, -89.65, speed=s) for i, s in enumerate([0, 20, 40, 55, 70, 30, 0])]


def test_bands_are_the_same_steps_on_every_drive():
    steps = BANDS["mph"]
    assert [band_of(v, steps) for v in (0, 24.9, 25, 59, 60, 80)] == [0, 0, 1, 2, 3, 4]
    assert band_of(None, steps) is None
    assert band_labels(steps) == ["0–25", "25–45", "45–60", "60–75", "75+"]


def test_the_readings_as_the_route_keep_their_own_times_and_speeds():
    route = [[r["latitude"], r["longitude"]] for r in TRACK]
    out = timed_route(route, TRACK)
    assert [p[2] for p in out] == [(T0 + timedelta(minutes=i)).timestamp() for i in range(7)]
    assert [p[3] for p in out] == [0, 20, 40, 55, 70, 30, 0]


def test_a_denser_matched_line_shares_the_time_between_readings_by_distance():
    # four points to every reading: the in-between ones get times between the readings', in order
    route = [[39.80 + 0.0025 * k, -89.6501] for k in range(25)]
    out = timed_route(route, TRACK, speed=lambda v: v * 1.609344)
    ts = [p[2] for p in out]
    assert ts == sorted(ts), "times only go forward, so a marker can follow them"
    assert ts[0] == T0.timestamp() and ts[-1] == (T0 + timedelta(minutes=6)).timestamp()
    assert T0.timestamp() < ts[1] < (T0 + timedelta(minutes=1)).timestamp()
    assert out[16][3] == round(70 * 1.609344, 1), "speed in the units given"


def test_a_route_that_doubles_back_keeps_going_forward_in_time():
    # out and back on the same street: the way back must not take the way out's readings
    track = [fix(i, 39.80 + 0.01 * min(i, 6 - i), -89.65, speed=30) for i in range(7)]
    route = [[r["latitude"], r["longitude"]] for r in track]
    out = timed_route(route, track)
    assert [p[2] for p in out] == [(T0 + timedelta(minutes=i)).timestamp() for i in range(7)]


def test_nothing_to_time_gives_nothing():
    assert timed_route([], TRACK) == [] and timed_route([[39.8, -89.65]], []) == []


def test_time_per_band_leaves_out_gaps_and_readings_with_no_speed():
    track = TRACK + [fix(30, 39.9, -89.65, speed=10)]  # 24 minutes unlogged before this one
    shares = speed_shares(track, BANDS["mph"])
    assert [round(s["minutes"]) for s in shares] == [2, 2, 1, 1, 0]
    assert round(sum(s["share"] for s in shares), 6) == 1
    assert speed_shares([fix(0, 39.8, -89.65), fix(1, 39.81, -89.65)], BANDS["mph"]) == []


def test_the_elevation_strip_keeps_the_peak_and_sits_flat_when_the_road_does():
    hill = [(T0 + timedelta(seconds=10 * i), 300.0 + (60 if i == 50 else 0)) for i in range(200)]
    s = elevation_spark(hill)
    assert s["lo"] == 300 and s["hi"] == 360
    assert " L" in s["d"] and s["d"].startswith("M0,20") and s["d"].endswith("Z")
    assert min(float(seg.split(",")[1]) for seg in s["d"].split(" L")[1:-1]) == 2.0, "the one high reading is drawn at the top"
    flat = elevation_spark([(T0 + timedelta(minutes=i), 500.0) for i in range(10)])
    assert flat and "16.0" in flat["d"]
    assert elevation_spark([(T0, 1.0), (T0, 2.0)]) is None
