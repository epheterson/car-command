"""What the Now page says, from plain data."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from nowpage import TICKS, WINDOWS, day_path, feed_status, gauge, state_word, strip_marks, strip_ticks, today_line, window_of

PT = ZoneInfo("America/Los_Angeles")


def test_state_words():
    assert state_word("Sleeping") == ("Asleep", "asleep")
    assert state_word("Idle") == ("Parked", "parked")
    assert state_word("Ready") == ("Idling", "idling")
    assert state_word("Driving") == ("Driving", "driving")
    assert state_word("Charging") == ("Charging", "charging")
    assert state_word("Offline") == ("Offline", "unknown") and state_word(None) == ("Unknown", "unknown") and state_word("") == ("Unknown", "unknown")


def test_today_line_names_what_each_car_did():
    cars = [{"vin": "A", "name": "Alpha", "place": "Home"}, {"vin": "B", "name": "Bravo", "place": "Work"}, {"vin": "C", "name": "Charlie", "place": "Home"}]
    today = {"A": {"drives": 3, "miles": 98.2, "kwh": 9.2}, "B": {"drives": 5, "miles": 11.2, "kwh": 0.0}}
    got = {r["vin"]: r for r in today_line(cars, today)}
    assert got["A"]["text"] == "drove 98 mi and charged 9 kWh" and got["A"]["moved"]
    assert got["B"]["text"] == "drove 11 mi in 5 drives"
    assert got["C"]["text"] == "stayed home" and not got["C"]["moved"]
    one = today_line([{"vin": "B", "name": "Bravo", "place": "Work"}], {})
    assert one[0]["text"] == "stayed put", "not at home: it stayed where it is"
    assert today_line(cars, {"C": {"drives": 0, "miles": 0.0, "kwh": 21.4}})[2]["text"] == "charged 21 kWh"
    assert today_line(cars, {"A": {"drives": 1, "miles": 3.04, "kwh": 0.4}})[0]["text"] == "drove 3 mi", "under a kWh is not a charge worth naming"


def test_gauge_clamps_and_only_marks_a_real_limit():
    assert gauge(58, 90) == {"fill": 58.0, "limit": 90.0}
    assert gauge(104.2, 100) == {"fill": 100.0, "limit": 100.0}
    assert gauge(-3, None) == {"fill": 0.0, "limit": None}
    assert gauge(None, 150) == {"fill": 0.0, "limit": None}
    assert gauge(40, 0) == {"fill": 40.0, "limit": None}


class Metric:
    labels = {"dist": "km"}

    def distance(self, mi):
        return mi * 1.609344


def test_today_line_speaks_the_active_units_and_a_charge_counts_as_doing_something():
    cars = [{"vin": "A", "name": "Alpha", "place": "Home"}, {"vin": "C", "name": "Charlie", "place": "Home"}]
    got = {r["vin"]: r for r in today_line(cars, {"A": {"drives": 3, "miles": 98.2, "kwh": 9.2}, "C": {"drives": 0, "miles": 0.0, "kwh": 21.4}}, Metric())}
    assert got["A"]["text"] == "drove 158 km and charged 9 kWh"
    assert got["C"]["text"] == "charged 21 kWh" and got["C"]["moved"], "a car that only charged still did something today"


def test_the_day_strip_marks_midnight_with_its_weekday_and_labels_6_noon_6():
    end = datetime(2026, 9, 30, 5, 25, tzinfo=timezone.utc)  # 10:25 PM Pacific, Tuesday the 29th
    start = end - timedelta(hours=24)
    ticks = strip_ticks(start, end, PT)
    assert [(t["label"], t["kind"]) for t in ticks] == [("Tue", "day"), ("6 AM", "hour"), ("noon", "hour"), ("6 PM", "hour"), ("now", "now")]
    midnight = datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc)
    assert abs(ticks[0]["left"] - (midnight - start) / (end - start) * 100) < 0.01 and ticks[-1]["left"] == 100.0
    assert [t["left"] for t in ticks] == sorted(t["left"] for t in ticks)


def test_an_hour_too_close_to_an_edge_is_left_out_and_a_midnight_at_the_edge_keeps_its_line():
    end = datetime(2026, 9, 30, 1, 10, tzinfo=timezone.utc)  # 6:10 PM Pacific: the 6 PM label would sit on "now"
    ticks = strip_ticks(end - timedelta(hours=24), end, PT)
    assert "6 PM" not in [t["label"] for t in ticks]
    edge = datetime(2026, 9, 30, 7, 30, tzinfo=timezone.utc)  # 12:30 AM Pacific: midnight was half an hour ago
    ticks = strip_ticks(edge - timedelta(hours=24), edge, PT)
    late = [t for t in ticks if t["left"] > 94]
    assert [(t["kind"], t["label"]) for t in late] == [("day", ""), ("now", "now")], "the line is drawn, the name only where it fits"


def test_a_fresh_feed_row_says_what_the_car_is_doing_before_the_hourly_rebuild():
    now = datetime(2026, 10, 6, 23, 55, tzinfo=timezone.utc)
    stored = now - timedelta(minutes=32)  # the history ends at the last rebuild
    row = lambda state, ago: {"at": now - timedelta(minutes=ago), "car_state": state}
    assert feed_status(row("Driving", 1), stored, now) == "Driving"
    assert feed_status(row("Charging", 1), stored, now) == "Charging"
    assert feed_status(row("Idling", 1), stored, now) == "Idle" and feed_status(row("Sentry", 1), stored, now) == "Idle"
    assert feed_status(row("Sleeping", 1), stored, now) == "Sleeping"
    assert feed_status(row("Unknown", 1), stored, now) is None, "an unknown row says nothing"
    assert feed_status(row("Driving", 40), stored, now) is None, "older than the history: the history wins"
    assert feed_status(row("Charging", 1), now - timedelta(minutes=1), now) == "Charging", "the position this row wrote is the row, not newer history"
    assert feed_status(row("Driving", 20), now - timedelta(hours=2), now) is None, "a row over 15 minutes old is not now"
    assert feed_status(None, stored, now) is None and feed_status(row("Driving", 1), None, now) == "Driving"


def test_a_car_out_now_is_part_of_today():
    cars = [{"vin": "A", "name": "Alpha", "place": "Work"}, {"vin": "B", "name": "Bravo", "place": "Home"}]
    got = {r["vin"]: r for r in today_line(cars, {"B": {"drives": 2, "miles": 12.0, "kwh": 0.0}}, active={"A": "driving", "B": "charging"})}
    assert got["A"]["text"] == "is driving now" and got["A"]["moved"] is True, "not 'stayed put' under its own banner"
    assert got["B"]["text"] == "drove 12 mi in 2 drives", "the banner above says it is charging; the day says what it did"


def test_the_day_path_is_todays_drives_in_order():
    t0 = datetime(2026, 10, 12, 7, tzinfo=PT)
    drives = [
        {"id": 3, "start_date": t0 + timedelta(hours=9)},
        {"id": 1, "start_date": t0 - timedelta(hours=10)},  # yesterday's: not today's path
        {"id": 2, "start_date": t0 + timedelta(hours=1)},
        {"id": 4, "start_date": t0 + timedelta(hours=2)},  # no route drawn: left out
    ]
    points = {1: [[1, 1], [1, 2]], 2: [[2, 1], [2, 2]], 3: [[3, 1], [3, 2]]}
    assert day_path(drives, points, t0 - timedelta(hours=7)) == [[[2, 1], [2, 2]], [[3, 1], [3, 2]]]


def test_no_drives_today_is_no_path():
    assert day_path([], {}, datetime(2026, 10, 12, tzinfo=PT)) == []


def test_each_stretch_of_the_strip_names_what_it_was():
    t0 = datetime(2026, 10, 12, 8, tzinfo=PT)
    h = lambda n: t0 + timedelta(hours=n)  # noqa: E731
    segs = [
        {"label": "sleeping", "start": h(0), "end": h(1)},
        {"label": "driving", "start": h(1), "end": h(1.5)},
        {"label": "charging", "start": h(2), "end": h(4)},
        {"label": "driving", "start": h(5), "end": h(5.2)},  # a drive the lists don't hold
    ]
    drives = [{"id": 7, "start_date": h(1.02), "destination_date": h(1.48)}, {"id": 3, "start_date": h(-5), "destination_date": h(-4)}]
    charges = [{"id": 9, "session_start_date": h(2.1), "session_end_date": h(3.9)}]
    marks = strip_marks(segs, drives, charges)
    assert [m.get("drive", {}).get("id") for m in marks] == [None, 7, None, None]
    assert [m.get("charge", {}).get("id") for m in marks] == [None, None, 9, None]
    assert marks[3] == {}, "no match: the strip links to the day instead"


def test_a_paused_guard_mode_says_why_in_a_tiles_words():
    from nowpage import paused_word

    assert paused_word("usb-full") == "USB full" and paused_word("creature-comfort") == "creature comfort on"
    assert paused_word("a-new-reason") == "a new reason", "a reason a later connector adds still reads"
    assert paused_word(None) is None and paused_word("") is None


def test_the_alarm_reads_only_while_it_is_going_off():
    from nowpage import alarm_word

    assert alarm_word("intrusion") == "alarm: intrusion" and alarm_word("pre-alarm") == "alarm warning"
    assert [alarm_word(x) for x in ("armed", "disarmed", None)] == [None, None, None]


def test_the_safety_notes_come_most_pressing_first_and_only_what_is_wrong():
    from nowpage import security_notes

    car = {"sentry": True, "living_object": "level-2", "alarm_status": "shock", "sentry_usb": False, "keyfob_low": True}
    assert security_notes(car, "Halo Secure") == ["Someone left inside", "Alarm: shock", "Halo Secure: no USB drive", "Key fob battery low"]
    assert security_notes({"sentry": True, "sentry_paused": "camera-error"}, "Halo Secure") == ["Halo Secure paused: camera error"]
    for chosen in ("at-home", "at-work", "occupied", "updating"):
        assert security_notes({"sentry": True, "sentry_paused": chosen}, "Halo Secure") == []
    assert security_notes({"sentry": False, "sentry_usb": False, "sentry_paused": "usb-full"}, "Halo Secure") == [], "off: nothing to record anyway"
    assert security_notes({"name": "Alpha", "make": "Tesla", "sentry": True}, "Sentry") == [], "a Tesla has none of these keys"


def test_the_window_is_the_one_asked_for_then_the_one_kept_then_a_day():
    assert window_of("7d", "24h") == "7d" and window_of(None, "7d") == "7d" and window_of("90d", "7d") == "7d" and window_of(None, None) == "24h"
    assert WINDOWS == {"24h": 24, "7d": 168} and set(TICKS) == set(WINDOWS)


def test_a_weeks_ticks_are_its_midnights():
    end = datetime(2026, 9, 30, 5, 25, tzinfo=timezone.utc)  # Tuesday 10:25 PM Pacific
    ticks = strip_ticks(end - timedelta(hours=168), end, PT, **TICKS["7d"])
    assert [t["label"] for t in ticks] == ["Wed", "Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "now"]
    assert {t["kind"] for t in ticks[:-1]} == {"day"}, "a week names its days and nothing in between"
    span = 168 * 3600
    midnight = datetime(2026, 9, 23, 7, 0, tzinfo=timezone.utc)  # 12 AM Pacific, the Wednesday
    assert abs(ticks[0]["left"] - (midnight - (end - timedelta(hours=168))).total_seconds() / span * 100) < 0.01


def test_feed_charging_lag_after_the_car_stopped():
    """A Tesla at 07:42: carState still said Charging a minute after charging_state said Stopped."""
    now = datetime(2026, 10, 19, 7, 43, 8, tzinfo=timezone.utc)
    lagging = {"at": now - timedelta(seconds=68), "car_state": "Charging", "charging_state": "Stopped"}
    assert feed_status(lagging, None, now) == "Idle"
    assert feed_status(dict(lagging, charging_state="Charging"), None, now) == "Charging"
