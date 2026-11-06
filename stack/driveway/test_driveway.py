import threading
import time

import driveway

PARKED = [100, 0, 300, 40, 0.9, 2]


def box(x, y=200, w=200, h=120, score=0.8):
    return [x, y, x + w, y + h, score, 2]


def test_boxes_overlap_and_the_weaker_twin_is_dropped():
    assert driveway.iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0 and driveway.iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    assert driveway.nms([box(0, score=0.5), box(5, score=0.9), box(500)]) == [box(5, score=0.9), box(500)]


def test_what_is_there_in_the_first_second_is_parked_and_never_counts():
    t = driveway.Tracker()
    assert t.step(0.0, [PARKED]) is None and t.step(0.5, [PARKED]) is None
    for i in range(10):
        assert t.step(1.0 + i * 0.2, [PARKED]) is None
    assert t.parked == [PARKED]


def test_a_new_car_moving_into_view_counts_once_seen_twice_and_moved():
    t = driveway.Tracker()
    t.step(0.0, [PARKED])
    assert t.step(1.2, [PARKED, box(600)]) is None  # first sight
    assert t.step(1.4, [PARKED, box(602)]) is None  # seen again but barely moved
    hit = t.step(1.6, [PARKED, box(560)])  # 40 px of 960: moved
    assert hit == box(560)


def test_a_new_car_that_does_not_move_is_not_an_arrival():
    t = driveway.Tracker()
    t.step(0.0, [])
    for i in range(10):
        assert t.step(1.2 + i * 0.2, [box(600)]) is None


def test_a_car_moving_on_the_street_counts_only_once_it_turns_into_the_driveway():
    t = driveway.Tracker(driveway.zone("0.5,0.5 1,0.5 1,1 0.5,1"))  # the lower right quarter
    t.step(0.0, [])
    path = [(100, 100), (160, 110), (220, 150), (290, 190), (360, 230)]  # along the street, then the mouth of the drive
    for i, (x, y) in enumerate(path):
        assert t.step(1.2 + i * 0.2, [box(x, y=y)]) is None  # moving, but its wheels not yet in
    assert t.step(2.2, [box(430, y=260)]) == box(430, y=260)  # bottom middle (530, 380): in


DRIVE_Q = "0.5,0.5 1,0.5 1,1 0.5,1"  # the driveway: the lower right quarter
STREET_L = "0,0 0.5,0 0.5,0.5 0,0.5"  # the street it is reached by: the upper left quarter


def test_a_car_coming_along_the_street_toward_the_driveway_counts_there():
    t = driveway.Tracker(driveway.zone(DRIVE_Q), driveway.zone(STREET_L))
    t.step(0.0, [])
    assert t.step(1.2, [box(0, y=40, w=100, h=60)]) is None  # first sight, ground point (50, 100)
    assert t.step(1.4, [box(30, y=60, w=100, h=60)]) == box(30, y=60, w=100, h=60)  # (80, 120): nearer the drive
    assert t.where == "street"


def test_a_car_on_the_street_going_away_from_the_driveway_or_standing_does_not_count():
    t = driveway.Tracker(driveway.zone(DRIVE_Q), driveway.zone(STREET_L))
    t.step(0.0, [])
    for i, x in enumerate((300, 260, 220, 180, 140)):  # past the drive, heading off: farther each frame
        assert t.step(1.2 + i * 0.2, [box(x, y=60, w=100, h=60)]) is None
    t = driveway.Tracker(driveway.zone(DRIVE_Q), driveway.zone(STREET_L))
    t.step(0.0, [])
    for i in range(6):  # parked in the street, the box jittering a pixel
        assert t.step(1.2 + i * 0.2, [box(100 + i % 2, y=60, w=100, h=60)]) is None


def test_the_street_counts_only_from_when_the_car_is_expected_and_the_driveway_always():
    t = driveway.Tracker(driveway.zone(DRIVE_Q), driveway.zone(STREET_L))
    t.street_from = 5.0
    t.step(0.0, [])
    path = [(0, 40), (30, 60), (60, 80), (90, 100)]
    assert all(t.step(1.2 + i * 0.2, [box(x, y=y, w=100, h=60)]) is None for i, (x, y) in enumerate(path))  # too early
    assert t.step(5.0, [box(120, y=110, w=100, h=60)]) is not None and t.where == "street"
    t = driveway.Tracker(driveway.zone(DRIVE_Q), driveway.zone(STREET_L))
    t.street_from = 1e9
    t.step(0.0, [])
    t.step(1.2, [box(400, y=200)])
    assert t.step(1.4, [box(430, y=260)]) is not None and t.where == "driveway"


def test_a_street_without_a_driveway_is_ignored():
    t = driveway.Tracker(None, driveway.zone(STREET_L))
    assert t.street is None


def test_a_watch_says_where_it_saw_the_car_and_takes_street_after(monkeypatch):
    monkeypatch.setenv("DRIVEWAY_ZONE", DRIVE_Q)
    monkeypatch.setenv("DRIVEWAY_STREET", STREET_L)
    now = time.time()
    seq = [(now, []), (now + 1.2, [box(0, y=40, w=100, h=60)]), (now + 1.4, [box(30, y=60, w=100, h=60)])]
    got = driveway.Watch(detector_factory=Fake, frame_source=lambda stop: iter(seq)).run(10, store=False)
    assert got["seen"] and got["on"] == "street"
    got = driveway.Watch(detector_factory=Fake, frame_source=lambda stop: iter(seq)).run(10, store=False, street_after=60)
    assert not got["seen"]


def test_a_zone_that_does_not_parse_is_the_whole_frame():
    assert driveway.zone(None) is None and driveway.zone("") is None and driveway.zone("0,0 1,1") is None and driveway.zone("0,0 2,0 1,1") is None
    assert driveway.inside(None, 5, 5) and driveway.inside(driveway.zone("0,0 1,0 1,1"), 900, 100) and not driveway.inside(driveway.zone("0,0 1,0 1,1"), 100, 500)


class Fake:
    def boxes(self, frame):
        return frame


def test_a_watch_answers_with_the_arriving_car_and_says_why_when_it_saw_none():
    w = driveway.Watch(detector_factory=Fake, frame_source=lambda stop: iter([(0.0, [PARKED]), (1.2, [PARKED, box(600)]), (1.4, [PARKED, box(550)])]))
    got = w.run(10, store=False)
    assert got["seen"] and got["box"] == [550, 200, 750, 320] and got["parked"] == 1
    w = driveway.Watch(detector_factory=Fake, frame_source=lambda stop: iter([(0.0, [PARKED]), (1.2, [PARKED])]))
    got = w.run(10, store=False)
    assert not got["seen"] and got["why"] == "stream ended" and got["parked"] == 1
    w = driveway.Watch(detector_factory=Fake, frame_source=lambda stop: iter([]))
    assert w.run(10, store=False)["why"] == "no frames"


def test_a_stream_error_never_says_the_streams_address(monkeypatch):
    monkeypatch.setenv("DRIVEWAY_RTSP", "rtsp://user:secret@10.0.0.9/live0")

    def boom(stop):
        raise OSError("cannot open rtsp://user:secret@10.0.0.9/live0")
        yield  # pragma: no cover

    got = driveway.Watch(detector_factory=Fake, frame_source=boom).run(10, store=False)
    assert "secret" not in got["why"] and "rtsp://…" in got["why"]


def test_two_askers_share_one_watch():
    gate = threading.Event()

    def frames(stop):
        gate.wait(2)
        yield 0.0, [PARKED]
        yield 1.2, [PARKED, box(600)]
        yield 1.4, [PARKED, box(550)]

    w, out = driveway.Watch(detector_factory=Fake, frame_source=frames), []
    threads = [threading.Thread(target=lambda: out.append(w.run(10, store=False))) for _ in range(2)]
    [t.start() for t in threads]
    import time

    time.sleep(0.2)  # both are waiting on the watch before it sees anything
    gate.set()
    [t.join(5) for t in threads]
    assert len(out) == 2 and out[0] is out[1] and out[0]["seen"]


def test_old_watches_are_dropped_after_keep_days_and_kept_when_it_is_0(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(driveway, "DATA", tmp_path)
    old = tmp_path / (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0).isoformat()
    old.mkdir()
    (old / "box.json").write_text("[]")
    monkeypatch.setattr(driveway, "KEEP_DAYS", 0.0)
    driveway.keep([], [1, 2, 3, 4])
    assert old.exists()
    monkeypatch.setattr(driveway, "KEEP_DAYS", 7.0)
    driveway.keep([], [1, 2, 3, 4])
    assert not old.exists() and len(list(tmp_path.iterdir())) == 1


def test_kept_frames_say_which_car_the_watch_was_for(tmp_path, monkeypatch):
    monkeypatch.setattr(driveway, "DATA", tmp_path)
    monkeypatch.setattr(driveway, "jpeg", lambda frame: b"jpg")
    w = driveway.Watch(detector_factory=Fake, frame_source=lambda stop: iter([(0.0, [PARKED]), (1.2, [PARKED, box(600)]), (1.4, [PARKED, box(550)])]))
    got = w.run(10, car="Bravo")
    assert got["seen"] and (tmp_path / got["kept"] / "car.txt").read_text() == "Bravo"


class Burst:
    """A camera stream that has already sent n frames when it is first read: what a slow detector meets."""

    def __init__(self, n: int, size: int) -> None:
        self.left, self.size, self.gate = n, size, threading.Event()

    def read(self, size: int) -> bytes:
        if self.left == 0:
            self.gate.wait(2)
            return b""
        self.left -= 1
        return bytes([self.left]) * size


def test_a_slow_detector_is_handed_the_newest_frame_never_a_backlog():
    stream = Burst(10, 4)
    newest = driveway.Newest(stream, 4)
    time.sleep(0.2)  # the detector was busy while ten frames came in
    t, raw = newest.take(threading.Event())
    assert raw == bytes([0]) * 4 and newest.dropped == 9
    stream.gate.set()
    assert newest.take(threading.Event()) is None  # the stream ended


def test_waiting_for_a_frame_ends_when_the_watch_is_stopped():
    stop = threading.Event()
    stream = Burst(0, 4)
    newest = driveway.Newest(stream, 4)
    stop.set()
    assert newest.take(stop) is None
    stream.gate.set()


DRIVE = "0.3,0.3 0.7,0.3 0.7,1 0.3,1"  # the drive runs up the middle of the frame to the street at y 0.3 (162 px)


def out(bottom, x=380, w=200, h=150):
    """A car going out: its ground point at `bottom`, coming up from the garage under the camera."""
    return [x, bottom - h, x + w, bottom, 0.8, 2]


def person(x, bottom, h=120):
    return [x, bottom - h, x + 40, bottom, 0.8, driveway.PERSON]


def leave(path, extra=lambda i: []):
    t = driveway.Leaver(driveway.zone(DRIVE))
    t.step(0.0, [PARKED])
    for i, b in enumerate(path):
        hit = t.step(1.2 + i * 0.2, [PARKED, b] + extra(i))
        if hit:
            return i, hit
    return None


def test_a_car_out_of_the_garage_answers_once_it_is_all_out_moving_or_stopped():
    path = [out(y) for y in range(540, 119, -40)]  # 40 px a frame: 5 fps at a walking pace
    _, (b, people) = leave(path)
    assert b == out(460) and people == []  # its near edge at 460 is above 486: the front is out from under the camera
    i, (b, _) = leave([out(540), out(500), out(470)] + [out(470)] * 10)
    assert b == out(470) and i == 2  # stopped in the drive, all out: the button in the car stops it if need be


def test_a_car_still_under_the_door_or_backing_in_is_not_leaving():
    assert leave([out(540), out(520), out(500)] + [out(500)] * 10) is None  # half out, sitting
    assert leave([out(y) for y in range(500, 541, 10)]) is None  # back into the garage


def test_the_driver_through_the_glass_is_not_someone_in_the_drive():
    _, (_, people) = leave([out(y) for y in range(540, 119, -40)], lambda i: [person(450, 520 - 40 * i)])
    assert people == []


def test_a_car_parked_in_the_drive_and_half_hidden_by_the_one_leaving_is_not_taken_for_it():
    # 2026-10-24 13:01, the boxes as the detector drew them: a car parked up the drive on the left; the
    # one leaving came up under it, and the parked car's uncovered top was answered as "all out" while
    # the leaving car was still under the door (the opener's eye refused the close)
    parked = [44, 72, 428, 328, 0.91, 2]
    frames = [
        [parked, [1, 269, 222, 539, 0.47, 2]],
        [parked, [1, 252, 310, 537, 0.83, 2]],
        [[41, 74, 421, 315, 0.9, 2], [0, 216, 371, 534, 0.85, 2]],
        [[0, 212, 424, 532, 0.92, 2], [5, 76, 433, 257, 0.86, 2]],
        [[2, 165, 464, 534, 0.87, 2], [3, 75, 434, 232, 0.81, 2]],
        [[0, 84, 496, 533, 0.94, 2], [4, 78, 434, 225, 0.49, 2]],
        [[-2, 77, 504, 532, 0.96, 2], [2, 80, 435, 212, 0.46, 2]],
    ]
    t = driveway.Leaver(driveway.zone(DRIVE))
    t.step(0.0, [parked])
    assert all(t.step(1.2 + i * 0.2, boxes) is None for i, boxes in enumerate(frames))
    hit = None
    for i, bottom in enumerate(range(500, 300, -40)):  # then it comes on up the drive, all out
        hit = hit or t.step(2.8 + i * 0.2, [[2, 80, 435, 212, 0.46, 2], [0, bottom - 400, 500, bottom, 0.95, 2]])
    assert hit and hit[0][3] < driveway.CLEAR_Y * driveway.H and hit[0][2] == 500


def test_a_car_coming_in_or_passing_on_the_street_is_not_leaving():
    assert leave([out(y) for y in range(140, 541, 40)]) is None  # pulling in: first seen up the drive
    assert leave([out(100, x=x) for x in (0, 100, 200, 300, 400, 500)]) is None  # along the street
    assert leave([out(540)] * 6) is None  # out of the garage but not moving


def test_someone_in_the_drive_goes_with_the_answer_and_someone_on_the_sidewalk_does_not():
    path = [out(y) for y in range(540, 119, -40)]
    _, (_, people) = leave(path, lambda i: [person(600, 400), person(800, 150)])  # in the drive at x 620; past it on the right
    assert len(people) == 1
    _, (_, people) = leave(path, lambda i: [person(100, 120)])  # the kid on the sidewalk
    assert people == []


def test_the_arrival_tracker_never_takes_a_person_for_a_car():
    t = driveway.Tracker()
    t.step(0.0, [])
    assert all(t.step(1.2 + i * 0.2, [person(600 - 40 * i, 300)]) is None for i in range(6))


def test_a_leaving_watch_says_people_and_a_watch_the_other_way_is_busy(monkeypatch):
    monkeypatch.setenv("DRIVEWAY_ZONE", DRIVE)
    frames = [(0.0, [PARKED])] + [(1.2 + i * 0.2, [PARKED, b]) for i, b in enumerate([out(y) for y in range(540, 119, -40)])]
    got = driveway.Watch(detector_factory=Fake, frame_source=lambda stop: iter(frames)).run(10, store=False, leaving=True)
    assert got["seen"] and got["leaving"] and got["people"] == 0
    gate = threading.Event()

    def slow(stop):
        gate.wait(2)
        yield 0.0, []

    w = driveway.Watch(detector_factory=Fake, frame_source=slow)
    threading.Thread(target=lambda: w.run(10, store=False), daemon=True).start()
    time.sleep(0.1)
    assert w.run(10, store=False, leaving=True) == {"seen": False, "why": "busy", "frames": 0}
    gate.set()
    monkeypatch.delenv("DRIVEWAY_ZONE")
    got = driveway.Watch(detector_factory=Fake, frame_source=lambda stop: iter(frames)).run(10, store=False, leaving=True)
    assert not got["seen"] and "DRIVEWAY_ZONE" in got["why"]


def test_a_car_coming_in_keeps_its_way_down_the_street_and_one_leaving_does_not(tmp_path, monkeypatch):
    monkeypatch.setattr(driveway, "DATA", tmp_path)
    monkeypatch.setattr(driveway, "jpeg", lambda frame: b"jpg")
    feed = [(0.0, [PARKED])] + [(0.25 * i, [PARKED]) for i in range(1, 9)] + [(2.2, [PARKED, box(600)]), (2.4, [PARKED, box(550)])]
    got = driveway.Watch(detector_factory=Fake, frame_source=lambda stop: iter(feed)).run(10, car="Bravo")
    street = sorted(p.name for p in (tmp_path / got["kept"] / "street").iterdir())
    assert got["seen"] and street == ["0000.4.jpg", "0000.9.jpg", "0001.4.jpg", "0001.9.jpg", "0002.4.jpg"]


def test_a_watch_that_sees_nothing_keeps_its_frames_with_no_box(tmp_path, monkeypatch):
    monkeypatch.setattr(driveway, "DATA", tmp_path)
    monkeypatch.setattr(driveway, "jpeg", lambda frame: b"jpg")
    feed = [(0.0, [PARKED]), (0.6, [PARKED]), (1.2, [PARKED])]
    got = driveway.Watch(detector_factory=Fake, frame_source=lambda stop: iter(feed)).run(10, car="Bravo")
    (kept,) = list(tmp_path.iterdir())
    assert not got["seen"] and (kept / "box.json").read_text() == "null" and (kept / "car.txt").read_text() == "Bravo"
    assert len(list((kept / "street").iterdir())) == 3 and len(list(kept.glob("*.jpg"))) == 3


def test_random_frames_are_taken_only_with_no_watch_running(tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(driveway, "DATA", tmp_path)

    def run(times: int) -> list:
        stop, waits, shots = threading.Event(), [], []

        def wait(a, b):
            waits.append(1)
            if len(waits) > times:
                stop.set()
            return 0

        driveway.random_frames(stop, wait=wait, grab=lambda: shots.append(1) or b"jpg")
        return shots

    assert len(run(2)) == 2 and len(list((tmp_path / "random").iterdir())) >= 1
    monkeypatch.setattr(driveway.WATCH, "running", {"leaving": False})
    assert run(2) == []
