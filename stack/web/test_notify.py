from notify import parse_smtp_url, send_email, send_pushover, connector_down, events

BASE = {
    "name": "Alpha",
    "status": "Sleeping",
    "soc": 74.0,
    "range_mi": 286.0,
    "odometer_mi": 1015.0,
    "place": "Home",
    "address": "Elm Street, Springfield",
    "locked": True,
    "doors_open": False,
    "charge_target": 90.0,
    "charging_power_kw": 0.0,
}


def test_first_snapshot_is_silent():
    assert events(None, BASE) == []


def test_nothing_changed_is_silent():
    assert events(BASE, dict(BASE)) == []


def test_charging_start_and_finish():
    started = events(BASE, {**BASE, "status": "Charging", "charging_power_kw": 11.4, "soc": 52.0})
    assert started == ["Alpha started charging at Home: 52% now, limit 90%, 11.4 kW."]
    finished = events({**BASE, "status": "Charging"}, {**BASE, "status": "Idle", "soc": 90.0, "range_mi": 350.0})
    assert finished == ["Alpha finished charging at Home: 90%, 350 mi of range."]


def test_drive_start_and_park_with_distance_and_arrival():
    left = events(BASE, {**BASE, "status": "Driving", "place": None, "address": "Elm Street, Springfield"})
    assert left == ["Alpha is driving, leaving Home at 74%."]
    parked = events(
        {**BASE, "status": "Driving", "place": None, "odometer_mi": 1015.0},
        {**BASE, "status": "Idle", "place": "Work", "address": "Main Street, Shelbyville", "odometer_mi": 1057.4, "soc": 61.0, "range_mi": 235.0},
    )
    assert parked == ["Alpha parked at Work: 61%, 235 mi left, 42.4 mi since it last woke.", "Alpha arrived at Work."]


def test_low_battery_crossing_and_security():
    low = events({**BASE, "soc": 21.0}, {**BASE, "soc": 19.5, "range_mi": 74.0})
    assert low == ["Alpha is down to 20% (74 mi)."]
    assert events({**BASE, "soc": 19.0}, {**BASE, "soc": 18.0}) == []  # already below; no repeat
    unlocked = events(BASE, {**BASE, "locked": False})
    assert unlocked == ["Alpha is unlocked at Home."]
    door = events(BASE, {**BASE, "doors_open": True})
    assert door == ["A door on Alpha is open at Home."]


def test_connector_down_fires_once_at_threshold():
    assert [connector_down(n) for n in (9, 10, 11)] == [False, True, False]


def test_telegram_sender_is_off_without_credentials():
    from notify import send_telegram

    assert send_telegram(None, None, "x") is False and send_telegram("t", None, "x") is False


def test_a_car_that_stops_saying_whether_it_is_locked_is_not_reported_unlocked():
    """The alert someone actually gets at night, for nothing.

    Both the Lucid API and TeslaFi have an explicit unknown for lock state, and it turns
    up whenever a car is between states — waking, sleeping, mid-poll. Treating it as
    unlocked turns every one of those into "your car is open".
    """
    assert events({**BASE, "locked": True}, {**BASE, "locked": None}) == []
    assert events({**BASE, "locked": None}, {**BASE, "locked": False}) == [], "unknown to unlocked is not a transition anyone saw"
    assert events({**BASE, "locked": True}, {**BASE, "locked": False}) == ["Alpha is unlocked at Home."], "the real one still fires"


def test_a_door_of_unknown_state_is_not_reported_open():
    assert events({**BASE, "doors_open": None}, {**BASE, "doors_open": None}) == []
    assert events({**BASE, "doors_open": False}, {**BASE, "doors_open": None}) == []
    assert events({**BASE, "doors_open": False}, {**BASE, "doors_open": True}) == ["A door on Alpha is open at Home."]


def test_plug_in_reminder_fires_once_after_the_hour_for_a_car_home_and_unplugged():
    from datetime import date, datetime

    from notify import plug_in_reminder

    car = {"name": "Alpha", "plugged": False, "place": "Home", "status": "Idle", "soc": 32.0}
    assert plug_in_reminder(car, datetime(2026, 9, 17, 20, 59), None) is None, "not before the hour"
    assert plug_in_reminder(car, datetime(2026, 9, 17, 21, 0), None) == "Alpha is home at 32% and not plugged in."
    assert plug_in_reminder(car, datetime(2026, 9, 17, 22, 30), date(2026, 9, 17)) is None, "once a day"
    assert plug_in_reminder(car, datetime(2026, 9, 18, 21, 5), date(2026, 9, 17)) is not None, "and again the next day"


def test_plug_in_reminder_stays_quiet_unless_every_condition_is_known_and_met():
    """A car that has not said whether it is plugged in is not a car left unplugged."""
    from datetime import datetime

    from notify import plug_in_reminder

    base = {"name": "Alpha", "plugged": False, "place": "Home", "status": "Sleeping", "soc": 32.0}
    at = datetime(2026, 9, 17, 21, 30)
    assert plug_in_reminder({**base, "plugged": None}, at, None) is None, "unknown is not unplugged"
    assert plug_in_reminder({**base, "plugged": True}, at, None) is None
    assert plug_in_reminder({**base, "place": "Work"}, at, None) is None
    assert plug_in_reminder({**base, "place": None}, at, None) is None
    assert plug_in_reminder({**base, "status": "Driving"}, at, None) is None
    assert plug_in_reminder({**base, "soc": 80.0}, at, None) is None, "plenty of charge, nothing to say"
    assert plug_in_reminder({**base, "soc": None}, at, None) is None


def test_a_window_left_open_on_a_parked_car_is_news_once():
    from notify import events

    prev = {"name": "Alpha", "status": "Idle", "place": "Home", "windows_open": False}
    now = {**prev, "windows_open": True}
    assert events(prev, now) == ["A window on Alpha is open at Home."]
    assert events(now, now) == [], "not every tick"
    assert not [m for m in events({**prev, "windows_open": None}, {**now, "status": "Driving"}) if "window" in m], "not while driving"
    assert events(prev, {**prev, "windows_open": None}) == [], "unknown is not open"


def test_the_cars_own_tire_warning_is_reported_once_with_the_lowest_pressure():
    from notify import events

    prev = {"name": "Alpha", "status": "Idle", "place": "Home", "tire_warning": False, "tires_psi": {"front_left": 41.3, "front_right": 41.3, "rear_left": 41.3, "rear_right": 33.9}}
    now = {**prev, "tire_warning": True}
    assert events(prev, now) == ["Alpha is warning about a tire (33.9 psi at the lowest)."]
    assert events(now, now) == []
    assert events(prev, {**now, "tires_psi": None}) == ["Alpha is warning about a tire."]
    assert events(prev, {**prev, "tire_warning": None}) == [], "unknown is not a warning"


def test_sentry_reacting_a_battery_flag_and_an_update_are_each_news_once():
    from notify import events

    prev = {"name": "Alpha", "status": "Idle", "place": "Home", "sentry_threat": False, "battery_warning": False, "update_available": False}
    assert events(prev, {**prev, "sentry_threat": True, "make": "Tesla"}) == ["Sentry on Alpha is reacting to something at Home."]
    assert events(prev, {**prev, "sentry_threat": True, "make": "Lucid"}) == ["Halo Secure on Alpha is reacting to something at Home."]
    assert events(prev, {**prev, "battery_warning": True}) == ["Alpha is flagging a battery health warning."]
    assert events(prev, {**prev, "update_available": True}) == ["A software update is waiting on Alpha."]
    on = {**prev, "sentry_threat": True}
    assert events(on, on) == [], "not every tick"
    assert events(prev, {**prev, "sentry_threat": None}) == [], "unknown is not a threat"


def test_an_smtp_url_says_host_port_tls_and_who_and_a_bad_one_says_nothing():
    c = parse_smtp_url("smtps://me%40example.com:s%23cret@smtp.example.com")
    assert c == {"tls": True, "host": "smtp.example.com", "port": 465, "user": "me@example.com", "password": "s#cret"}
    c = parse_smtp_url("smtp://relay.lan:2525")
    assert c["tls"] is False and c["port"] == 2525 and c["user"] == ""
    assert parse_smtp_url("") is None and parse_smtp_url("http://x") is None and parse_smtp_url(None) is None


def test_no_url_or_no_recipient_sends_nothing():
    assert send_email(None, "a@b.c", None, "s", "t") is False
    assert send_email("smtps://u:p@h", "", None, "s", "t") is False


def test_pushover_needs_both_keys():
    assert send_pushover(None, "u", "t") is False and send_pushover("a", "", "t") is False


def test_every_event_carries_a_kind_the_settings_page_knows():
    from notify import KINDS, event_kinds

    prev = {"name": "Alpha", "status": "Driving", "soc": 50, "locked": True, "place": "Home"}
    now = {"name": "Alpha", "status": "Idle", "soc": 49, "locked": False, "place": "Work", "range_mi": 150, "windows_open": True, "sentry_threat": True}
    kinds = [k for k, _ in event_kinds(prev, now)]
    assert kinds and all(k in KINDS for k in kinds)
    assert {"parked", "unlocked", "window", "sentry"} <= set(kinds)


def test_routes_from_settings():
    from notify import CHANNELS, KINDS, routes_from

    assert routes_from({}) == {}, "nothing set: every kind goes everywhere"
    old = routes_from({"muted_alerts": ["parked", "nope"]})
    assert old == {"parked": set()}, "the old on/off list still reads: off means no channel"
    new = routes_from({"alert_routes": {"parked": ["discord", "fax"], "login": [], "nope": ["discord"], "tire": "discord"}})
    assert new == {"parked": {"discord"}, "login": set()}, "unknown kinds, channels and shapes are dropped"
    assert set(CHANNELS) == {"discord", "telegram", "email", "pushover"} and "data" in KINDS


def test_your_own_tire_limits_fire_once_per_crossing():
    from notify import tire_limits

    a = {"name": "Alpha", "tires_psi": {"front_left": 41.0, "front_right": 41.0, "rear_left": 41.0, "rear_right": 41.0}}
    b = {"name": "Alpha", "tires_psi": {"front_left": 41.0, "front_right": 41.0, "rear_left": 41.0, "rear_right": 33.5}}
    assert tire_limits(a, b, None, None) == [], "no limit set, nothing said"
    assert tire_limits(a, b, 35.0, None) == ["Alpha's rear right tire is at 33.5 psi, below your 35.0 psi limit."]
    assert tire_limits(b, b, 35.0, None) == [], "still low: said once"
    assert tire_limits({}, b, 35.0, 50.0) == ["Alpha's rear right tire is at 33.5 psi, below your 35.0 psi limit."], "first reading after a restart counts as a crossing"
    hot = {"name": "Alpha", "tires_psi": {"front_left": 52.5, "front_right": 41.0}}
    assert tire_limits(a, hot, None, 50.0) == ["Alpha's front left tire is at 52.5 psi, above your 50.0 psi limit."]
    assert tire_limits(a, {"name": "Alpha", "tires_psi": None}, 35.0, 50.0) == []


def test_the_guard_alert_is_named_for_the_cars_in_the_garage():
    from notify import kinds_for

    assert kinds_for(["Tesla"])["sentry"] == "Sentry reacting"
    assert kinds_for(["Lucid"])["sentry"] == "Halo Secure reacting"
    assert kinds_for(["Tesla", "Lucid", "Tesla"])["sentry"] == "Sentry / Halo Secure reacting"
