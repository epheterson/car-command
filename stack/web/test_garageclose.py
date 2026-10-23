from datetime import datetime, timedelta, timezone

import garageclose

T0 = datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc)


def at(s):
    return T0 + timedelta(seconds=s)


def run(d, steps):
    """steps: (seconds, door, status, at_home); the events, as (second, kind, info)."""
    return [(s, k, i) for s, door, status, home in steps for k, i in d.step(door, status, home, at(s))]


def play(steps, sights=(), people=0):
    """The pure part end to end, with the camera and its loop stood in for. steps: (second, door,
    status, locked, speed) at the tick; sights: seconds at which a vehicle goes out of the garage in
    front of the camera, seen only if a watch is running then. Returns (events, the Departure)."""
    d, events, end, pending = garageclose.Departure(), [], None, sorted(sights)
    for s, door, status, locked, speed in steps:
        now = at(s)
        while pending and pending[0] <= s:
            t = pending.pop(0)
            if d.watching and (end is None or t <= end):
                d.sighted(at(t), people)
                d.watching, end = False, None
        if d.watching and end is not None and now >= at(end):  # the watch timed out: the loop asks again, or stops
            more = d.wanted(now)
            end = end + more if more else None
            d.watching = bool(more)
        for k, i in d.step(door, status, True, now, locked=locked, speed=speed):
            events.append((s, k, i))
            if k == "watch":
                end = s + i["seconds"]
    return events, d


def kinds(events):
    return [(s, k) for s, k, _ in events]


def test_the_door_opening_starts_the_camera_for_ten_minutes_and_the_close_logs_the_timings():
    # 10-19 16:55 as it happened: Ready +112 s, camera +205 s, Driving +248 s, the door closed by hand +218 s
    d = garageclose.Departure()
    ev = run(d, [(0, "closed", "Sleeping", True), (5, "open", "Sleeping", True)])
    assert ev == [(5, "watch", {"seconds": garageclose.DOOR_WATCH_S})] and garageclose.DOOR_WATCH_S == 600.0
    ev = run(d, [(117, "open", "Ready", True), (200, "open", "Ready", True)])
    assert ev == []  # one watch at a time; the wake extended it
    d.watching, d.seen, d.did = False, at(210), "would close"
    ev = run(d, [(223, "closed", "Ready", True), (253, "closed", "Driving", False)])
    assert [(s, k) for s, k, _ in ev] == [(223, "departure")]
    t = ev[0][2]
    assert t["opened"] == at(5) and (t["wake_s"], t["ignition_s"], t["seen_s"], t["closed_s"], t["did"]) == (112, 112, 205, 218, "would close")
    assert t["unlock_s"] is None and t["driving_s"] is None


def test_a_door_left_open_rearms_on_the_car_waking_for_a_while_not_for_ever():
    d = garageclose.Departure()
    run(d, [(0, "closed", "Sleeping", True), (5, "open", "Sleeping", True)])
    d.watching = False  # that watch saw nothing
    assert run(d, [(1000, "open", "Sleeping", True), (3000, "open", "Sleeping", True)]) == []  # past the door's window: quiet
    assert run(d, [(3010, "open", "Ready", True)]) == [(3010, "watch", {"seconds": garageclose.SIGNAL_WATCH_S["wake"]})]
    d.watching = False
    assert run(d, [(3300, "open", "Ready", True)]) == [(3300, "watch", {"seconds": 10.0})]  # 10 s of that window left
    d.watching = False
    assert run(d, [(3400, "open", "Ready", True)]) == []  # run out


def test_a_door_open_when_first_read_arms_on_the_car_waking_not_before():
    d = garageclose.Departure()
    assert run(d, [(0, "open", "Sleeping", True), (60, "open", "Sleeping", True)]) == []
    assert [k for _, k, _ in run(d, [(70, "open", "Driving", True)])] == ["watch"]


def test_the_door_opening_while_the_car_is_away_is_someone_else():
    d = garageclose.Departure()
    assert run(d, [(0, "closed", "Driving", False), (5, "open", "Driving", False)]) == []


def test_ha_unread_changes_nothing_and_a_close_with_no_opening_seen_logs_nothing():
    d = garageclose.Departure()
    assert run(d, [(0, "closed", "Sleeping", True), (5, None, "Sleeping", True)]) == []
    assert d.door == "closed"
    d = garageclose.Departure()
    assert run(d, [(0, "open", "Sleeping", True), (5, "closed", "Sleeping", True)]) == []


def test_the_car_woken_just_before_the_door_counts_for_this_departure():
    d = garageclose.Departure()
    run(d, [(0, "closed", "Sleeping", True), (10, "closed", "Ready", True), (40, "open", "Ready", True)])
    d.watching = False
    ev = run(d, [(300, "closed", "Driving", True)])
    assert ev[0][1] == "departure" and ev[0][2]["wake_s"] == -30


def test_a_door_opened_for_the_car_driving_in_is_no_departure_until_it_shows_life_again():
    # A real departure: the door opened for the car coming home, stood open 212 s, closed by hand
    d = garageclose.Departure()
    assert run(d, [(-60, "closed", "Driving", False), (0, "closed", "Driving", True), (5, "open", "Driving", True), (30, "open", "Idle", True), (217, "closed", "Idle", True)]) == []
    run(d, [(240, "closed", "Driving", False), (300, "open", "Driving", True), (320, "open", "Idle", True)])  # home again, the door left open
    assert [(s, k) for s, k, _ in run(d, [(2000, "open", "Ready", True)])] == [(2000, "watch")]  # later it wakes to go
    d.watching = False
    assert [k for _, k, _ in run(d, [(2300, "closed", "Driving", False)])] == ["departure"]


def test_a_car_in_gear_in_the_garage_when_the_door_opens_is_leaving():
    d = garageclose.Departure()
    assert run(d, [(0, "closed", "Driving", True), (5, "open", "Driving", True)]) == [(5, "watch", {"seconds": garageclose.DOOR_WATCH_S})]


def test_the_owner_getting_out_of_the_car_that_just_came_in_arms_nothing():
    d = garageclose.Departure()
    steps = [(-60, "closed", "Driving", False), (0, "closed", "Driving", True), (5, "open", "Driving", True), (30, "open", "Idle", True)]
    run(d, steps)
    d.step("open", "Idle", True, at(60), locked=True)
    assert d.step("open", "Idle", True, at(70), locked=False) == []  # unlocked a minute after it came in
    assert d.step("open", "Idle", True, at(80), locked=True) == []


def test_the_doors_unlocking_or_opening_arms_a_long_watch_and_each_new_sign_extends_it():
    d = garageclose.Departure()
    run(d, [(0, "closed", "Sleeping", True), (5, "open", "Sleeping", True)])
    d.watching = False
    d.step("open", "Sleeping", True, at(1000), locked=True, doors_open=False)
    ev = d.step("open", "Sleeping", True, at(1010), locked=False, doors_open=False)  # unlocked
    assert ev == [("watch", {"seconds": garageclose.MAX_WATCH_S})] and d.until == at(1010 + 720)
    d.step("open", "Sleeping", True, at(1100), locked=False, doors_open=False)
    d.step("open", "Sleeping", True, at(1300), locked=False, doors_open=True)  # a door opened: extends
    assert d.until == at(1300 + 720)
    assert d.signals["unlock"] == at(1010) and d.signals["doors"] == at(1300)


# ---- the camera and the car must agree


def test_a_pull_out_closes_at_once_without_waiting_for_the_car_to_report():
    """The car's Driving report trails the camera by a poll: the door closes on the sighting itself."""
    d = garageclose.Departure()
    run(d, [(0, "closed", "Sleeping", True), (5, "open", "Sleeping", True)])  # no word from the car yet
    d.sighted(at(100))
    ev = d.step("open", "Sleeping", True, at(103), speed=0)
    assert [k for k, _ in ev] == ["close"] and ev[0][1]["people"] == 0 and ev[0][1]["car"] is False


def test_another_car_pulling_out_closes_the_door_too_and_says_it_was_not_this_car():
    d = garageclose.Departure()
    run(d, [(0, "closed", "Sleeping", True), (5, "open", "Sleeping", True)])
    d.watching = False
    d.sighted(at(50))
    ev = d.step("open", "Sleeping", True, at(55), speed=0)
    assert [k for k, _ in ev] == ["close"] and ev[0][1]["car"] is False


def test_a_fresh_sign_of_life_closes_on_the_sighting_without_waiting_for_the_slower_report():
    """The car woke and went Ready a minute before the camera saw a vehicle go: that is the car
    going, and its Driving report, a poll behind, is not waited for."""
    d = garageclose.Departure()
    run(d, [(0, "closed", "Sleeping", True), (5, "open", "Sleeping", True), (40, "open", "Ready", True)])
    d.sighted(at(100))
    ev = d.step("open", "Ready", True, at(103), speed=0)
    assert [k for k, _ in ev] == ["close"] and ev[0][1]["moved"] is None


def test_the_car_that_showed_life_is_named_as_the_one_that_went():
    d = garageclose.Departure()
    run(d, [(0, "closed", "Sleeping", True), (5, "open", "Sleeping", True), (40, "open", "Ready", True)])
    d.sighted(at(100))
    ev = d.step("open", "Ready", True, at(103), speed=0)
    assert [k for k, _ in ev] == ["close"] and ev[0][1]["car"] is True


def test_the_door_closing_by_hand_before_the_car_confirms_is_a_departure_with_no_close():
    d = garageclose.Departure()
    run(d, [(0, "closed", "Ready", True), (5, "open", "Ready", True)])
    d.sighted(at(100))
    d.watching = False
    ev = d.step("closed", "Ready", True, at(110))
    assert [k for k, _ in ev] == ["departure"] and ev[0][1]["did"] == "door closed before the car confirmed" and ev[0][1]["seen_s"] == 95


def test_the_door_closing_ends_a_running_watch():
    d = garageclose.Departure()
    run(d, [(0, "closed", "Sleeping", True)])
    d.step("open", "Sleeping", True, at(5))
    assert d.watching
    assert [k for k, _ in d.step("closed", "Sleeping", True, at(60))] == ["departure", "stop"]


# ---- replays of real days. Times are seconds from the door opening (T0), the car is Alpha.


def test_replay_2026_10_22_morning_both_pull_outs_close_as_they_happen():
    # 08:47:18 door opens (+0) and a Tesla pulls out at 08:48 (+45): under this rule the door closes
    # behind it at once. The Lucid, asleep, is said not to be the car that went.
    Z, Y = "Sleeping", "Driving"
    steps = [(-5, "closed", Z, True, 0), (0, "open", Z, True, 0)] + [(s, "open", Z, True, 0) for s in range(10, 120, 10)]
    ev, _ = play(steps, sights=[45])
    closes = [(s, i) for s, k, i in ev if k == "close"]
    assert len(closes) == 1 and closes[0][0] == 50 and closes[0][1]["car"] is False
    # 09:26: the door opened again for the Lucid (unlocked at 09:16, asleep since); the camera sees it
    # out at +12 s and the door closes then, before its first report (Driving) comes 28 s later.
    later = [(-5, "closed", Z, True, 0), (0, "open", Z, True, 0), (5, "open", Z, True, 0), (10, "open", Z, True, 0), (15, "open", Z, True, 0), (40, "open", Y, True, 23 / 1.609)]
    ev2, _ = play(later, sights=[12])
    assert [(s, k) for s, k, _ in ev2 if k == "close"] == [(15, "close")]


def test_replay_2026_10_21_the_three_closes_still_close():
    # The camera saw the car out 146, 170 and 191 s after the door opened; the Lucid (here as on 10-19:
    # Ready about +112 s, Driving reported 43 s after the camera) confirms inside the window.
    for seen in (146, 170, 191):
        steps = [(-5, "closed", "Sleeping", True, 0), (0, "open", "Sleeping", True, 0)]
        steps += [(s, "open", "Ready" if s >= 112 else "Sleeping", True, 0) for s in range(10, seen + 40, 10)]
        steps += [(s, "open", "Driving", True, 14) for s in range(seen + 40, seen + 60, 5)]
        ev, _ = play(sorted(steps, key=lambda x: x[0]), sights=[seen])
        closes = [s for s, k in kinds(ev) if k == "close"]
        assert len(closes) == 1 and 0 <= closes[0] - seen <= 60, (seen, kinds(ev))
    # and with no sign from the car but the door, the 210 s door watch still reaches the slowest of them
    steps = (
        [(-5, "closed", "Idle", True, 0), (0, "open", "Idle", True, 0)] + [(s, "open", "Idle", True, 0) for s in range(10, 230, 10)] + [(s, "open", "Driving", True, 14) for s in range(235, 250, 5)]
    )
    ev, _ = play(sorted(steps, key=lambda x: x[0]), sights=[191])
    assert [k for _, k in kinds(ev)].count("close") == 1


def test_another_car_leaving_while_ours_stays_parked_closes_behind_it():
    steps = [(-5, "closed", "Sleeping", True, 0), (0, "open", "Sleeping", True, 0)] + [(s, "open", "Sleeping", True, 0) for s in range(10, 400, 10)]
    ev, d = play(steps, sights=[45])
    closes = [(s, i) for s, k, i in ev if k == "close"]
    assert len(closes) == 1 and closes[0][0] == 50 and closes[0][1]["car"] is False


