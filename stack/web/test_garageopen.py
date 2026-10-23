from datetime import datetime, timedelta, timezone

import garageopen

HOME = {"lat": 37.0, "lon": -122.0, "radius_m": 150}
T0 = datetime(2026, 10, 18, 1, 0, tzinfo=timezone.utc)
M_PER_DEG = 111_195  # metres per degree of latitude


def south(m):
    """A point m metres due south of home."""
    return HOME["lat"] - m / M_PER_DEG


def dense(metres_per_s=11.0, start=2000, stop_at=None, stop_s=0):
    """A car driving north to home at a steady speed, a fix a second, parking on arrival;
    optionally stopping stop_s seconds at stop_at metres out."""
    track, t, m = [], T0, float(start)
    while m > 0:
        track.append({"t": t, "lat": south(m), "lon": HOME["lon"], "mph": metres_per_s / 0.44704, "gear": "D"})
        if stop_at is not None and m > stop_at >= m - metres_per_s:
            for _ in range(stop_s):
                t += timedelta(seconds=1)
                track.append({"t": t, "lat": south(stop_at), "lon": HOME["lon"], "mph": 0, "gear": "D"})
        t += timedelta(seconds=1)
        m -= metres_per_s
    for _ in range(30):
        track.append({"t": t, "lat": south(0), "lon": HOME["lon"], "mph": 0, "gear": "P"})
        t += timedelta(seconds=1)
    return track


def model(stops=()):
    trips = garageopen.arrivals(dense(), dict(HOME))
    return {"home": HOME, "roads": garageopen.learn(trips), "stops": list(stops)}


def test_a_trip_home_is_learned_as_its_road_with_seconds_to_the_driveway():
    trips = garageopen.arrivals(dense(), dict(HOME))
    assert len(trips) == 1 and round(trips[0]["bearing"]) == 180
    assert garageopen.directions(trips) == [{"bearing": 180, "trips": 1}]
    roads = garageopen.learn(trips)
    # 11 m/s, the driveway where it stays within 60 m of the parking spot: 300 m out is about 22 s from it
    assert 20 <= garageopen.time_to_driveway(roads, south(300), HOME["lon"]) <= 26
    assert garageopen.time_to_driveway(roads, south(300), HOME["lon"] + 60 / 88_800) is None  # a street over: not a road home


def test_stops_near_home_count_only_where_cars_stayed():
    ends = [{"lat": south(220), "lon": HOME["lon"], "stay_min": 40}, {"lat": south(225), "lon": HOME["lon"], "stay_min": 55}, {"lat": south(600), "lon": HOME["lon"], "stay_min": 1}]
    (spot,) = garageopen.stops(ends, HOME)
    assert spot["n"] == 2 and 215 <= spot["d"] <= 230 and spot["bearing"] == 180


def fixes(track, every=45, phase=0):
    out, last = [], None
    for x in track:
        if last is None or (x["t"] - last).total_seconds() >= every:
            if last is None and phase:
                last = x["t"] - timedelta(seconds=every - phase)
                continue
            out.append(x)
            last = x["t"]
    return out


def run(a, track, every=45, phase=0):
    """Feeds the fixes as the recorder would (a tick every 10 s repeats the last fix) and
    returns the events and when the door would have started."""
    events, started, fx = [], None, fixes(track, every, phase)
    i, t = 0, track[0]["t"]
    end = track[-1]["t"]
    while t <= end:
        while i + 1 < len(fx) and fx[i + 1]["t"] <= t:
            i += 1
        f = fx[i]
        events += [k for k, _ in a.step("V1", {"lat": f["lat"], "lon": f["lon"], "mph": f["mph"]}, t)]
        if a.due("V1", t):
            started = t
        t += timedelta(seconds=10)
    return events, started


def test_every_phase_of_a_45_s_fix_starts_the_door_so_it_is_open_before_the_driveway_and_not_long_before():
    track = dense()
    entry = garageopen.arrivals(dense(), dict(HOME))[0]["entry"]["t"]
    for phase in range(0, 45, 5):
        events, started = run(garageopen.Approach(model=model()), track, phase=phase)
        assert "schedule" in events and "arrived" in events, phase
        open_before = (entry - (started + timedelta(seconds=garageopen.DOOR_S))).total_seconds()
        assert -12 <= open_before <= 25, (phase, open_before)


def test_a_car_stopping_short_calls_it_off_or_says_so_at_once_when_the_door_already_started():
    spot = {"lat": south(220), "lon": HOME["lon"], "d": 220, "bearing": 180, "n": 5}
    seen = set()
    for phase in range(0, 45, 5):
        events, started = run(garageopen.Approach(model=model([spot])), dense(stop_at=220, stop_s=900), phase=phase)
        assert "cancel" in events or "short" in events, (phase, events)
        assert "arrived" in events or "short" in events  # called off: its later arrival is timed again; said: that trip is over
        seen.add("short" if "short" in events else "cancel")
    assert seen  # a stop inside the last 45 s is mostly seen only after the door started: said at once


def test_a_car_coming_from_a_direction_never_learned_or_going_past_opens_nothing():
    m = model()
    east = [{"t": T0 + timedelta(seconds=45 * i), "lat": HOME["lat"], "lon": HOME["lon"] + d / 88_800, "mph": 25, "gear": "D"} for i, d in enumerate([2000, 900, 400, 100])]
    events, started = run(garageopen.Approach(model=m), east, every=1)
    assert "schedule" not in events and started is None


def test_a_car_faster_than_the_learned_trips_is_decided_before_its_next_fix_lands_inside_home():
    # A real arrival: learned at 11 m/s, came home at 20 m/s; the fix at 421 m waited, the next was at 123 m, inside
    m = model()
    for phase in range(0, 45, 5):
        events, started = run(garageopen.Approach(model=m), dense(metres_per_s=20.0, start=4000), phase=phase)
        assert "schedule" in events and "missed" not in events, (phase, events)


def camera(m):
    a = garageopen.Approach(model=m)
    a.camera = True
    return a


def test_with_the_camera_a_stop_at_the_light_on_the_way_in_keeps_it_armed():
    # Waiting at a light to turn into the neighbourhood: the camera decides, so a stop calls nothing off
    for phase in range(0, 45, 5):
        events, _ = run(camera(model()), dense(start=4000, stop_at=400, stop_s=90), phase=phase)
        assert "schedule" in events and "cancel" not in events and "arrived" in events and "missed" not in events, (phase, events)


def test_with_the_camera_a_fix_that_says_0_mph_mid_drive_calls_nothing_off():
    # A real arrival: 51 mph at 579 m, then 0 mph at 364 m though it was at 149 m 45 s later
    for phase in range(0, 45, 5):
        track = [{**f, "mph": 0} if 250 <= (HOME["lat"] - f["lat"]) * M_PER_DEG <= 500 else f for f in dense(metres_per_s=15.0, start=4000)]
        events, _ = run(camera(model()), track, phase=phase)
        assert "schedule" in events and "cancel" not in events and "missed" not in events, (phase, events)


def test_with_the_camera_an_approach_off_the_learned_roads_arms_and_driving_away_calls_it_off():
    east = [{"t": T0 + timedelta(seconds=45 * i), "lat": HOME["lat"], "lon": HOME["lon"] + d / 88_800, "mph": 25, "gear": "D"} for i, d in enumerate([2000, 900, 400, 100])]
    events, _ = run(camera(model()), east + [{**east[-1], "t": east[-1]["t"] + timedelta(seconds=45), "lon": HOME["lon"], "mph": 0}], every=1)
    assert "schedule" in events and "arrived" in events
    gone = {**east[2], "t": east[2]["t"] + timedelta(seconds=45), "lon": HOME["lon"] + 900 / 88_800}
    away = east[:3] + [gone, {**gone, "t": gone["t"] + timedelta(seconds=45)}]
    events, _ = run(camera(model()), away, every=1)
    assert events[0] == "schedule" and events[1:] in (["cancel"], ["short"])  # short: the harness's timer had started it


def test_with_the_camera_back_from_a_neighbours_inside_the_bubble_arms():
    # A stop at a neighbour's and back
    out = [{"t": T0 + timedelta(seconds=45 * i), "lat": south(d), "lon": HOME["lon"], "mph": 15, "gear": "D"} for i, d in enumerate([0, 300, 600, 600, 600])]
    back = [{"t": out[-1]["t"] + timedelta(seconds=45 * (i + 1)), "lat": south(d), "lon": HOME["lon"], "mph": 15, "gear": "D"} for i, d in enumerate([350, 60, 0, 0])]
    events, _ = run(camera(model()), out + back, every=1)
    assert events[:1] == ["schedule"] and "arrived" in events, events
    events, _ = run(garageopen.Approach(model=model()), out + back, every=1)
    assert events == []  # without the camera, only a trip from ARM_M out counts


def test_with_the_camera_a_fix_inside_home_leaves_the_door_to_the_camera_still_looking():
    # A real arrival: the car's fix landed inside home's radius at 11:30:42 and the camera saw it turn in at 11:30:43; the trip had ended, so nothing opened
    a = camera(model())
    track = [{"t": T0 + timedelta(seconds=45 * i), "lat": south(d), "lon": HOME["lon"], "mph": 25, "gear": "D"} for i, d in enumerate([2000, 1100, 400, 120, 0])]
    events = [k for f in track for k, _ in a.step("V", f, f["t"])]
    assert "schedule" in events and "arrived" in events
    trip = a.trips["V"]
    assert trip.go is not None and trip.started is None  # what the camera checks before it starts the door
    leave = {"t": track[-1]["t"] + timedelta(hours=2), "lat": south(2000), "lon": HOME["lon"], "mph": 30, "gear": "D"}
    a.step("V", leave, leave["t"])
    assert trip.go is None  # the next trip starts clean


def test_without_the_camera_a_fix_inside_home_ends_the_trip():
    a = garageopen.Approach(model=model())
    track = [{"t": T0 + timedelta(seconds=45 * i), "lat": south(d), "lon": HOME["lon"], "mph": 25, "gear": "D"} for i, d in enumerate([2000, 1100, 400, 120, 0])]
    for f in track:
        a.step("V", f, f["t"])
    assert a.trips["V"].go is None
