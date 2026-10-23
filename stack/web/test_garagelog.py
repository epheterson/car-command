from datetime import datetime, timedelta, timezone

import garagelog

T = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)


def drive(i, start, end, vin="V1"):
    return {"id": i, "vin": vin, "start_date": start, "destination_date": end}


def ev(kind, at, did, vin="V1"):
    return {"vin": vin, "at": at, "kind": kind, "detail": {"did": did}}


def test_the_labels_say_did_or_would_have_and_skip_what_the_car_did_not_do():
    assert garagelog.label("close", {"did": "closed the door"}) == ("out", "Garage closed")
    assert garagelog.label("close", {"did": "would close the door"})[1] == "Garage would have closed"
    assert garagelog.label("start", {"did": "started the door"}) == ("in", "Garage opened")
    assert garagelog.label("start", {"did": "would start the door"})[1] == "Garage would have opened"
    for did in ("left alone: the door reads open", "left open: 1 person in the driveway", "failed: OSError", None):
        assert garagelog.label("close", {"did": did}) is None
    assert garagelog.label("you_opened", {"did": "started the door"}) is None, "a door opened by hand is not shown"


def test_a_close_goes_to_the_drive_that_left_and_an_open_to_the_one_that_arrived():
    out = drive(1, T, T + timedelta(minutes=20))
    back = drive(2, T + timedelta(hours=3), T + timedelta(hours=3, minutes=20))
    events = [ev("close", T + timedelta(seconds=40), "closed the door"), ev("start", back["destination_date"] - timedelta(minutes=2), "started the door")]
    assert garagelog.attach([out, back], events) == {1: ["Garage closed"], 2: ["Garage opened"]}


def test_an_action_far_from_every_drive_or_on_another_car_goes_nowhere():
    d = drive(1, T, T + timedelta(minutes=20))
    assert garagelog.attach([d], [ev("close", T + timedelta(minutes=30), "closed the door")]) == {}
    assert garagelog.attach([d], [ev("close", T, "closed the door", vin="V2")]) == {}
    assert garagelog.attach([d], [ev("close", T - timedelta(minutes=10), "closed the door")]) == {}
    assert garagelog.attach([drive(3, T, None)], [ev("start", T, "started the door")]) == {}, "an open drive has no arrival"


def test_two_closes_near_two_drives_each_take_the_nearest_once():
    a, b = drive(1, T, T + timedelta(minutes=2)), drive(2, T + timedelta(minutes=4), T + timedelta(minutes=30))
    got = garagelog.attach([a, b], [ev("close", T + timedelta(seconds=30), "closed the door"), ev("close", T + timedelta(minutes=4, seconds=20), "would close the door")])
    assert got == {1: ["Garage closed"], 2: ["Garage would have closed"]}


def test_a_log_entry_becomes_a_row_with_its_detail_and_without_a_time_it_does_not():
    r = garagelog.row_of({"at": T.isoformat(), "kind": "close", "mode": "close", "did": "closed the door"}, "V1")
    assert r == ("V1", T, "close", "close", {"did": "closed the door"})
    assert garagelog.row_of({"kind": "close"}, "V1") is None and garagelog.row_of({"at": T.isoformat(), "kind": "close"}, None) is None
