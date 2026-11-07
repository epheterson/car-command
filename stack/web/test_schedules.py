"""Scheduled departure and what the car says of its schedule: the bodies sent to Tesla's proxy,
the argument rules, reading the stream's schedule fields, and the Now card's tile. Fakes only."""

from datetime import datetime, timezone
from pathlib import Path

import jinja2

import activity
import commands as cm
import filters
import schedules as sc
import tesla_cmd as tc
import tesla_stream as ts

VIN = "5YJ3E1EA0JF000001"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def sent(key, arg=None):
    name, body = tc.ENDPOINTS[key]
    return name, body(arg)


def test_departure_sends_the_proxys_parameters_for_preconditioning():
    name, body = sent("depart", 450)
    assert name == "set_scheduled_departure"
    assert body == {
        "enable": True,
        "departure_time": 450,
        "preconditioning_enabled": True,
        "preconditioning_weekdays_only": False,
        "off_peak_charging_enabled": False,
        "off_peak_charging_weekdays_only": False,
        "end_off_peak_time": 0,
    }


def test_off_peak_departure_ends_off_peak_charging_at_the_departure_time():
    _, body = sent("depart_offpeak", 420)
    assert body["off_peak_charging_enabled"] is True and body["end_off_peak_time"] == 420 and body["departure_time"] == 420


def test_departure_off_sends_only_enable():
    assert sent("depart_off") == ("set_scheduled_departure", {"enable": False})


def test_a_departure_posts_through_the_proxy_with_the_token_in_the_header():
    calls = []

    def fake(method, url, *, headers, json=None):
        calls.append((method, url, headers.get("Authorization"), json))
        return 200, {"response": {"result": True, "reason": ""}}

    p = tc.Proxy("https://localhost:4443", call=fake)
    assert p.send("tok", VIN, "depart", 450).ok
    assert calls[0][1].endswith(f"/vehicles/{VIN}/command/set_scheduled_departure") and calls[0][2] == "Bearer tok"


def test_the_departure_commands_are_tier_2_tesla_only_and_take_a_time_of_day():
    for key in ("depart", "depart_offpeak", "depart_off"):
        c = cm.CATALOGUE[key]
        assert c.tier == 2 and c.makes == ("Tesla",)
    assert cm.parse("depart", "450")[1] == 450
    for bad in ("1440", "-1", "x"):
        try:
            cm.parse("depart", bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    # no Lucid route, so a Lucid is refused
    assert "isn't something Lucid" in cm.refusal(cm.CATALOGUE["depart"], "Lucid", "connector", enabled=True, signed=True, allowed_tier=2, stepped_up=False)


def test_the_result_line_says_the_time():
    assert cm.said(cm.CATALOGUE["depart"], 450, True, None, "9 PM") == "Scheduled departure set to 7:30 AM 9 PM"


def test_departure_counts_against_the_limiter_like_any_command():
    lim = cm.Limiter(per_minute=1)
    assert lim.admit(VIN, 2, "depart") is None
    assert lim.admit(VIN, 2, "depart_off")


def test_the_stream_reads_a_time_and_a_mode():
    assert ts.value_of({"timeValue": {"hour": 7, "minute": 30}}) == 450
    assert ts.value_of({"time_value": {"hour": 23}}) == 1380
    assert ts.value_of({"scheduledChargingModeValue": "ScheduledChargingModeDepartBy"}) == "DepartBy"


def test_the_schedule_is_kept_while_the_car_sleeps_and_empty_when_unsaid():
    old = datetime(2026, 11, 1, tzinfo=timezone.utc)
    c = {"_at": old, "ScheduledChargingMode": "DepartBy", "ScheduledDepartureTime": 450, "ScheduledChargingPending": True}
    st = activity.stream_now(c, NOW)
    assert st["fresh"] is False and (st["sched_mode"], st["sched_depart_min"], st["sched_pending"]) == ("DepartBy", 450, True)
    assert "sched_mode" not in activity.stream_now({"_at": old}, NOW)


def test_a_start_streamed_as_a_unix_time_reads_as_its_local_clock():
    from zoneinfo import ZoneInfo

    c = {"_at": NOW, "ScheduledChargingMode": "StartAt", "ScheduledChargingStartTime": 1793865600}  # midnight in Springfield
    st = activity.stream_now(c, NOW, tz=ZoneInfo("America/Los_Angeles"))
    assert st["sched_charge_min"] == 0 and sc.lines(None, st) == ["charges at 12:00 AM"]
    assert "sched_charge_min" not in activity.stream_now(dict(c, ScheduledChargingStartTime=99999), NOW), "neither a minute nor a time: unsaid"


def _tiles(car):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    env.globals.update(guard_name=lambda make: "Sentry", state_word=lambda s: ("Parked", "parked"))
    return env.get_template("_now_tiles.html").render(car=car, cmd=None, root="", q="", vq="?", a=[], tread_last=None, u={"dist": "mi"})


def test_the_card_shows_the_schedule_with_commands_off_and_hides_it_when_none():
    base = {"vin": "V1", "name": "Alpha", "status": "Idle", "make": "Tesla"}
    html = _tiles(dict(base, sched_lines=["leave by 7:30 AM", "warms up"]))
    assert "Schedule" in html and '<span class="ft-b">7:30 AM</span>' in html and "leave by · warms up" in html
    assert "Schedule" not in _tiles(dict(base, sched_lines=[]))
    assert "Schedule" not in _tiles(base)


def test_firmware_before_2024_26_takes_the_legacy_pair_and_unreadable_counts_as_current():
    assert sc.modern("2024.26.3 abc") and sc.modern("2025.2.6") and sc.modern("2024.44.25.1 x")
    assert not sc.modern("2024.20.9") and not sc.modern("2023.44.30.4")
    assert sc.modern(None) and sc.modern("")


ANSWER = {
    "response": {
        "vehicle_state": {"car_version": "2025.2.6 abc123"},
        "charge_state": {"home_location": {"latitude": 37.5, "longitude": -122.25}, "work_location": {"latitude": 37.6, "longitude": -122.4}},
        "charge_schedule_data": {
            "charge_schedules": [
                {"id": 1, "enabled": True, "start_enabled": True, "start_time": 1380, "end_enabled": False, "end_time": 0, "latitude": 37.5, "longitude": -122.25},
                {"id": 2, "enabled": False, "start_enabled": True, "start_time": 60},
                {"id": 3, "enabled": True, "start_enabled": False, "end_enabled": True, "end_time": 420, "latitude": 37.6, "longitude": -122.4},
            ]
        },
        "preconditioning_schedule_data": {
            "precondition_schedules": [{"id": 4, "enabled": True, "precondition_time": 450, "latitude": 39.0, "longitude": -121.0}, {"id": 5, "enabled": False, "precondition_time": 100}]
        },
    }
}


def test_the_car_s_schedules_are_read_with_the_name_of_their_places_and_no_positions():
    got = sc.parse(ANSWER, locate=lambda lat, lon: "Cabin" if lat == 39.0 else None)
    assert got["firmware"] == "2025.2.6"
    assert got["charge"] == [{"start": 1380, "end": None, "at": "Home"}, {"start": None, "end": 420, "at": "Work"}]
    assert got["precondition"] == [{"time": 450, "at": "Cabin"}]
    assert got["tesla"] == {"Home": (37.5, -122.25), "Work": (37.6, -122.4)}
    assert "39.0" not in repr(got) and "-121" not in repr(got), "a schedule's own position is dropped; only the car's Home and Work are held"
    assert sc.parse(ANSWER)["precondition"][0]["at"] == "elsewhere"
    assert sc.parse(None) == {"firmware": None, "tesla": {}, "charge": [], "precondition": []}
    assert sc.parse({"response": {"charge_schedule_data": {"charge_schedules": ["x", {"enabled": True, "start_enabled": True, "start_time": "no"}]}}})["charge"] == []
    assert sc.parse({"response": {"charge_state": {"home_location": {"latitude": 0, "longitude": 0}}}})["tesla"] == {}


def test_the_lines_name_the_place_and_fall_back_to_the_stream():
    assert sc.lines(sc.parse(ANSWER, lambda a, b: "Cabin"), {"sched_mode": "DepartBy", "sched_depart_min": 1}) == ["charges at 11:00 PM (Home)", "charged by 7:00 AM (Work)", "warm by 7:30 AM (Cabin)"]
    assert sc.lines(None, {"sched_mode": "DepartBy", "sched_depart_min": 450, "preconditioning": True}) == ["leave by 7:30 AM", "warms up"]
    assert sc.lines(sc.parse(None), {"sched_mode": "StartAt", "sched_charge_min": 1380}) == ["charges at 11:00 PM"]
    assert sc.lines(None, {"sched_mode": "Off"}) == []


ROWS = [
    {"id": 1, "name": "Alpha House", "latitude": 37.5, "longitude": -122.25, "icon": "home", "auto": False},  # the spot Tesla calls Home
    {"id": 2, "name": "Bravo Home", "latitude": 38.0, "longitude": -121.0, "icon": "home", "auto": False},
    {"id": 3, "name": "Charlie Office", "latitude": 38.1, "longitude": -121.1, "icon": "work", "auto": False},
    {"id": 4, "name": "Delta Gym", "latitude": 38.2, "longitude": -121.2, "icon": "gym", "auto": False},
    {"id": 5, "name": "12 Elm St", "latitude": 38.3, "longitude": -121.3, "icon": None, "auto": True},
    {"id": 6, "name": "Merged", "latitude": 38.4, "longitude": -121.4, "icon": None, "auto": False, "merged_into": 4},
]
HERE = (40.0, -120.0)


def test_the_places_are_offered_in_order_tesla_first_and_never_with_a_position():
    options = sc.choices(sc.parse(ANSWER), ROWS, HERE)
    assert [(o["id"], o["name"]) for o in options] == [
        ("tesla:home", "Home"),
        ("tesla:work", "Work"),
        ("place:2", "Bravo Home"),
        ("place:3", "Charlie Office"),
        ("car", "Where the car is now"),
        ("place:4", "Delta Gym"),
    ], "our place at Tesla's Home is the same choice; an auto-named or merged place is not offered"
    assert "37.5" not in repr(options) and "40.0" not in repr(options)
    assert sc.default(options) == "tesla:home"


def test_without_tesla_s_home_the_default_is_where_the_car_is_and_with_neither_nothing():
    options = sc.choices(sc.parse(None), ROWS, HERE)
    assert [o["id"] for o in options][:3] == ["place:1", "place:2", "place:3"] and sc.default(options) == "car"
    assert sc.default(sc.choices(None, [], None)) is None


def test_a_choice_is_looked_up_here_never_taken_from_the_request():
    read = sc.parse(ANSWER)
    assert sc.resolve("tesla:home", read, ROWS, HERE) == (37.5, -122.25) and sc.resolve("tesla:work", read, ROWS, HERE) == (37.6, -122.4)
    assert sc.resolve("place:3", read, ROWS, HERE) == (38.1, -121.1) and sc.resolve("car", read, ROWS, HERE) == HERE
    for bad in ("place:6", "place:99", "tesla:other", "37.5,-122.2", "", None, "car"):
        assert sc.resolve(bad, read, ROWS, None) is None, bad


AT = dict(lat=37.5, lon=-122.25)


def test_the_current_api_sends_a_schedule_keyed_to_the_cars_place_with_our_ids():
    s = tc.Schedule(450, **AT)
    (name, body), (gone, which) = tc.plan("depart", s)
    assert name == "add_precondition_schedule" and (gone, which) == ("remove_charge_schedule", {"id": tc.DONE_ID}), "an off-peak charge left from before ends"
    assert body == {"days_of_week": "ALL", "enabled": True, "one_time": False, "precondition_time": 450, "id": tc.PRE_ID, "lat": 37.5, "lon": -122.25}
    ((name, body),) = tc.plan("sched_charge", tc.Schedule(1380, **AT))
    assert name == "add_charge_schedule" and body["start_enabled"] is True and body["start_time"] == 1380 and body["end_enabled"] is False and body["id"] == tc.CHARGE_ID


def test_off_peak_departure_is_a_precondition_schedule_and_a_charge_that_must_finish_by_then():
    steps = tc.plan("depart_offpeak", tc.Schedule(420, **AT))
    assert [n for n, _ in steps] == ["add_precondition_schedule", "add_charge_schedule"]
    done = steps[1][1]
    assert done["end_enabled"] is True and done["end_time"] == 420 and done["start_enabled"] is False and done["id"] == tc.DONE_ID


def test_off_removes_our_schedules_by_id_and_needs_no_place():
    assert tc.plan("depart_off", tc.Schedule()) == [("remove_precondition_schedule", {"id": tc.PRE_ID}), ("remove_charge_schedule", {"id": tc.DONE_ID})]
    assert tc.plan("sched_charge_off", tc.Schedule()) == [("remove_charge_schedule", {"id": tc.CHARGE_ID})]


class Fake:
    def __init__(self, *answers):
        self.answers, self.sent = list(answers), []

    def __call__(self, method, url, *, headers, json=None):
        self.sent.append((url.rsplit("/", 1)[1], json))
        return self.answers.pop(0) if self.answers else (200, {"response": {"result": True, "reason": ""}})


def test_a_schedule_goes_by_the_current_api_in_order_and_stops_at_the_first_no():
    fake = Fake((200, {"response": {"result": False, "reason": "unsupported"}}))
    r = tc.Proxy("https://localhost:4443", call=fake).send("t", VIN, "depart_offpeak", tc.Schedule(420, **AT))
    assert not r.ok and [n for n, _ in fake.sent] == ["add_precondition_schedule"]
    fake = Fake()
    assert tc.Proxy("https://localhost:4443", call=fake).send("t", VIN, "depart_offpeak", tc.Schedule(420, **AT)).ok
    assert [n for n, _ in fake.sent] == ["add_precondition_schedule", "add_charge_schedule"]


def test_removing_a_schedule_the_car_does_not_hold_is_done():
    fake = Fake((200, {"response": {"result": False, "reason": "not_found"}}), (200, {"response": {"result": False, "reason": "not_found"}}))
    assert tc.Proxy("https://localhost:4443", call=fake).send("t", VIN, "depart_off", tc.Schedule()).ok
    assert [n for n, _ in fake.sent] == ["remove_precondition_schedule", "remove_charge_schedule"]


def test_older_firmware_gets_the_legacy_command_with_no_place():
    fake = Fake()
    assert tc.Proxy("https://localhost:4443", call=fake).send("t", VIN, "depart", tc.Schedule(450, modern=False)).ok
    assert fake.sent[0][0] == "set_scheduled_departure" and "lat" not in fake.sent[0][1] and fake.sent[0][1]["departure_time"] == 450
    fake = Fake()
    tc.Proxy("https://localhost:4443", call=fake).send("t", VIN, "sched_charge_off", tc.Schedule(modern=False))
    assert fake.sent == [("set_scheduled_charging", {"enable": False, "time": 0})]


def test_the_charge_tab_offers_the_departure_rows():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    from markupsafe import Markup  # noqa: F401

    env.globals.update(mc=lambda n: "", guard_name=lambda m: "Sentry")
    cmd = {"reason": None, "allowed": set(), "more": [cm.CATALOGUE[k] for k in ("depart", "depart_offpeak", "depart_off")]}
    html = env.from_string('{% import "_commands.html" as c %}{{ c.dock(cmd, car, root, u) }}').render(cmd=cmd, car={"vin": "V1", "make": "Tesla"}, root="", u={"metric": False})
    assert 'data-send="depart"' in html and 'data-send="depart_offpeak"' in html and 'data-send="depart_off"' in html and "Charge</button>" in html


def test_the_identity_round_reads_an_awake_cars_schedules_and_only_then(monkeypatch):
    import test_identity as ti

    class Fleet(ti.FakeFleet):
        def schedules(self, token, base, vin):
            return ANSWER

    fleet = Fleet([{"vin": ti.T1, "state": "asleep"}, {"vin": ti.T3, "state": "online"}])
    store, kept = ti.Store(), {}
    w, _saved, _after = ti.worker(fleet, store, monkeypatch)
    w.schedules = lambda vin, body: kept.__setitem__(vin, sc.parse(body))
    w.round(ti.NOW)
    assert list(kept) == [ti.T3] and kept[ti.T3]["precondition"][0]["time"] == 450


def test_the_charge_tab_has_a_compact_picker_of_places_with_the_default_selected():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    env.globals.update(mc=lambda n: "", guard_name=lambda m: "Sentry")
    options = sc.choices(sc.parse(ANSWER), ROWS, HERE)
    cmd = {"reason": None, "allowed": set(), "more": [cm.CATALOGUE[k] for k in ("depart", "sched_charge")], "where": {"options": options, "default": sc.default(options)}}
    html = env.from_string('{% import "_commands.html" as c %}{{ c.dock(cmd, car, root, u) }}').render(cmd=cmd, car={"vin": "V1", "make": "Tesla"}, root="", u={"metric": False})
    assert html.count("data-where") == 1 and '<optgroup label="From the car">' in html and '<option value="tesla:home" selected>Home</option>' in html
    assert html.index("data-where") < html.index('data-send="sched_charge"'), "the picker comes before the rows it governs"
    assert "37.5" not in html and "-122" not in html


def test_a_lucid_schedule_is_one_window_with_no_place():
    assert sc.lucid_lines(True, "23:00", "06:00") == ["charges 11:00 PM to 6:00 AM"]
    assert sc.lucid_lines(True, "23:00", "06:00", "scheduled") == ["charges 11:00 PM to 6:00 AM", "waiting for it"]
    assert sc.lucid_lines(True, "23:00", None) == ["charges from 11:00 PM"] and sc.lucid_lines(True, None, "06:00") == ["charged by 6:00 AM"]
    assert sc.lucid_lines(False, "23:00", "06:00") == ["off"]
    assert sc.lucid_lines(None, "23:00", "06:00") == [], "a connector that has not said"
    assert sc.lucid_lines(True, None, None) == [] and sc.lucid_lines(False, None, None) == [], "a car that never held one"


def test_the_card_shows_a_lucids_schedule_read_only():
    base = {"vin": "V1", "name": "Bravo", "status": "Idle", "make": "Lucid"}
    html = _tiles(dict(base, sched_lines=sc.lucid_lines(True, "23:00", "06:00")))
    assert "Schedule" in html and '<span class="ft-b">11:00 PM</span>' in html and "charges until 6:00 AM" in html
    assert "Schedule" not in _tiles(dict(base, sched_lines=sc.lucid_lines(None, None, None)))


def test_the_tile_says_the_time_in_bold_and_what_happens_under_it():
    assert sc.head(["charges at 12:00 AM"]) == ("12:00 AM", "starts charging")
    assert sc.head(["charges at 11:00 PM (Home)", "charged by 7:00 AM (Work)"]) == ("11:00 PM", "starts charging at Home · charged by 7:00 AM (Work)")
    assert sc.head(["charges 11:00 PM to 6:00 AM", "waiting for it"]) == ("11:00 PM", "charges until 6:00 AM · waiting for it")
    assert sc.head(["leave by 7:30 AM", "warms up"]) == ("7:30 AM", "leave by · warms up")
    assert sc.head(["off"]) == ("Off", "") and sc.head([]) == ("", "")
