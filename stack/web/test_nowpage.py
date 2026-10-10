"""What the Now page says, from plain data."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from nowpage import feed_status, gauge, state_word, strip_ticks, today_line

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


def test_strip_ticks_sit_on_whole_hours():
    end = datetime(2026, 9, 30, 5, 25, tzinfo=timezone.utc)  # 10:25 PM Pacific
    start = end - timedelta(hours=24)
    ticks = strip_ticks(start, end, PT)
    assert [t["label"] for t in ticks] == ["11 PM", "5 AM", "11 AM", "5 PM", "now"]
    first = datetime(2026, 9, 29, 6, 0, tzinfo=timezone.utc)  # 11 PM Pacific on the 28th
    assert abs(ticks[0]["left"] - (first - start) / (end - start) * 100) < 0.01 and ticks[-1]["left"] == 100.0


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
    assert feed_status(row("Driving", 20), now - timedelta(hours=2), now) is None, "a row over 15 minutes old is not now"
    assert feed_status(None, stored, now) is None and feed_status(row("Driving", 1), None, now) == "Driving"


def test_a_car_out_now_is_part_of_today():
    cars = [{"vin": "A", "name": "Alpha", "place": "Work"}, {"vin": "B", "name": "Bravo", "place": "Home"}]
    got = {r["vin"]: r for r in today_line(cars, {"B": {"drives": 2, "miles": 12.0, "kwh": 0.0}}, active={"A": "driving", "B": "charging"})}
    assert got["A"]["text"] == "is driving now" and got["A"]["moved"] is True, "not 'stayed put' under its own banner"
    assert got["B"]["text"] == "drove 12 mi in 2 drives and is charging now"
