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
    ramping = events(BASE, {**BASE, "status": "Charging", "charging_power_kw": 0.0, "soc": 52.0})
    assert ramping == ["Alpha started charging at Home: 52% now, limit 90%."], "0.0 kW before the ramp says nothing true"
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


def test_connector_down_is_by_the_clock():
    from datetime import datetime, timedelta, timezone

    t = datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert [connector_down(t, t + timedelta(minutes=m)) for m in (9, 10, 11)] == [False, True, True]


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
    assert set(CHANNELS) == {"discord", "telegram", "email", "pushover", "webhook"} and "data" in KINDS


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


def test_the_webhook_posts_json_with_the_token_as_a_bearer_header(monkeypatch):
    import json

    import notify

    seen = []

    class Answer:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout):
        seen.append((req.full_url, dict(req.header_items()), json.loads(req.data)))
        return Answer()

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    assert notify.send_webhook(None, "t", "x") is False and seen == []
    assert notify.send_webhook("https://hub.example.test/alert", "s3cret", "Paused Alpha charging", "charge_hold")
    url, headers, body = seen[0]
    assert url == "https://hub.example.test/alert" and headers["Authorization"] == "Bearer s3cret"
    assert body == {"source": "carcommand", "kind": "charge_hold", "summary": "Paused Alpha charging"}
    assert notify.send_webhook("https://hub.example.test/alert", None, "x")
    assert "Authorization" not in dict(seen[1][1])


def test_the_command_kinds_have_a_group():
    from notify import GROUPS, KINDS

    grouped = [k for _, kinds in GROUPS for k in kinds]
    assert "charge_hold" in grouped and "command" in grouped and "command" in KINDS


def test_a_charge_port_left_open_is_said_once_the_cable_is_out_and_the_car_is_locked():
    unplugged = {**BASE, "status": "Idle", "locked": False, "port_open": True, "plugged": False}
    left = "The charge port door on Alpha is open at Home, with no cable in."
    assert events({**unplugged, "plugged": True}, unplugged) == [], "the cable just came out; the door is still in the hand"
    assert events(unplugged, {**unplugged, "locked": True}) == [left], "walked away with it open"
    assert events({**unplugged, "locked": True}, {**unplugged, "locked": True}) == [], "once, not every minute"
    assert events(unplugged, {**unplugged, "status": "Sleeping"}) == [left], "or the car fell asleep with it open"
    assert events({**BASE, "port_open": True, "plugged": True}, {**BASE, "port_open": True, "plugged": True}) == [], "open while charging is normal"
    assert events({**BASE, "port_open": None, "plugged": False}, {**BASE, "port_open": None, "plugged": False}) == [], "a car that has not said"
    assert events({**unplugged, "locked": True}, {**unplugged, "locked": True, "port_open": False}) == []


# ---- the Watcher: the same events from every source, once each, across a restart

from datetime import datetime as _dt, timedelta as _td, timezone as _tz  # noqa: E402


class _Clock:
    def __init__(self):
        self.t = _dt(2026, 3, 1, 20, 0, tzinfo=_tz.utc)

    def __call__(self):
        return self.t

    def tick(self, minutes=1):
        self.t += _td(minutes=minutes)


def _watcher(store=None, facts=None, clock=None):
    from notify import Watcher

    store = {} if store is None else store
    return Watcher(load=store.get, save=store.__setitem__, facts=facts, clock=clock or _Clock()), store


FEED = {**BASE, "vin": "VIN-B", "name": "Bravo", "status": "Idle", "soc": 41.0, "plugged": True, "live": False, "source": "feed"}
STREAM = {**FEED, "source": "stream"}
LUCID = {**BASE, "vin": "VIN-A", "status": "Idle", "soc": 47.0, "live": True, "source": "connector"}


def test_every_source_raises_the_same_charging_events():
    for car in (LUCID, FEED, STREAM):
        w, _ = _watcher()
        assert w.step(car) == []
        started = w.step({**car, "status": "Charging", "charging_power_kw": None})
        assert [k for k, _ in started] == ["charge_start"], car["source"]
        assert started[0][1].endswith("% now, limit 90%."), "a power the source does not carry is left out, not a '?'"
        done = w.step({**car, "status": "Idle", "soc": 90.0, "charge_kwh_added": 31.2, "charge_kwh_at": w.clock()})
        assert done == [("charge_done", f"{car['name']} finished charging at Home: {car['soc']:.0f}→90%, 31.2 kWh.")], car["source"]


def test_a_reading_from_history_alone_raises_nothing():
    w, store = _watcher()
    old = {**FEED, "source": None}
    assert w.step(old) == [] and w.step({**old, "status": "Charging"}) == [] and store == {}


def test_a_car_on_two_sources_hears_each_event_once():
    """The feed and the stream both read a Tesla; one watcher keyed by VIN sees one change."""
    w, _ = _watcher()
    w.step(FEED)
    out = w.step({**STREAM, "status": "Charging"}) + w.step({**FEED, "status": "Charging"}) + w.step({**STREAM, "status": "Charging", "soc": 42.0})
    assert [k for k, _ in out] == ["charge_start"]


def test_cars_never_compare_with_each_other():
    w, _ = _watcher()
    w.step({**LUCID, "status": "Charging"})
    assert w.step(FEED) == [], "Bravo did not finish a charge Alpha was taking"
    assert w.step({**LUCID, "status": "Charging", "soc": 48.0}) == []


def test_a_restart_neither_repeats_nor_misses():
    clock = _Clock()
    w, store = _watcher(clock=clock)
    w.step(FEED)
    assert [k for k, _ in w.step({**FEED, "status": "Charging"})] == ["charge_start"]
    clock.tick()
    w2, _ = _watcher(store, clock=clock)  # the site restarted: memory gone, the saved snapshot kept
    assert w2.step({**FEED, "status": "Charging", "soc": 43.0}) == [], "the start is not sent again"
    clock.tick(3)
    w3, _ = _watcher(store, clock=clock)
    done = w3.step({**FEED, "status": "Idle", "soc": 88.0, "charge_kwh_added": 30.0, "charge_kwh_at": clock.t})
    assert done == [("charge_done", "Bravo finished charging at Home: 41→88%, 30.0 kWh.")], "the end, while it was down, still goes out, with where it began"


def test_a_snapshot_saved_long_ago_is_no_before():
    clock = _Clock()
    w, store = _watcher(clock=clock)
    w.step({**FEED, "status": "Charging"})
    clock.tick(120)
    w2, _ = _watcher(store, clock=clock)
    assert w2.step(FEED) == [], "two hours down: what changed meanwhile is old news"


def test_finished_charging_waits_a_little_for_the_charges_kwh_and_cost():
    clock = _Clock()
    known = {}
    w, _ = _watcher(facts=lambda vin, since: known.get(vin), clock=clock)
    w.step(LUCID)
    w.step({**LUCID, "status": "Charging"})
    clock.tick(60)
    assert w.step({**LUCID, "status": "Sleeping", "soc": 90.0}) == [], "held while the facts are filled"
    clock.tick()
    known["VIN-A"] = {"kwh": 20.1, "cost": 6.05}
    assert w.step({**LUCID, "status": "Sleeping", "soc": 90.0}) == [("charge_done", "Alpha finished charging at Home: 47→90%, 20.1 kWh, $6.05.")]
    assert w.step({**LUCID, "status": "Sleeping", "soc": 90.0}) == [], "once"


def test_finished_charging_goes_out_without_them_after_the_hold():
    clock = _Clock()
    w, _ = _watcher(facts=lambda vin, since: None, clock=clock)
    w.step(LUCID)
    w.step({**LUCID, "status": "Charging"})
    assert w.step({**LUCID, "status": "Sleeping", "soc": 52.0}) == []
    clock.tick(5)
    assert w.step({**LUCID, "status": "Sleeping", "soc": 52.0}) == [("charge_done", "Alpha stopped charging at Home: 47→52%, limit 90%.")], "below its limit it stopped, not finished"


def test_a_charge_that_resumes_flushes_the_held_end_first():
    clock = _Clock()
    w, _ = _watcher(facts=lambda vin, since: None, clock=clock)
    w.step(LUCID)
    w.step({**LUCID, "status": "Charging"})
    w.step({**LUCID, "status": "Sleeping", "soc": 50.0})
    out = w.step({**LUCID, "status": "Charging", "soc": 50.0})
    assert [k for k, _ in out] == ["charge_done", "charge_start"]


def test_a_teslas_alarm_is_its_guard_reacting():
    from activity import stream_now

    now = _dt(2026, 3, 1, 20, 0, tzinfo=_tz.utc)
    assert stream_now({"_at": now, "SentryMode": "Panic"}, now)["sentry_threat"] is True
    assert stream_now({"_at": now, "SentryMode": "Aware"}, now)["sentry_threat"] is False, "someone walking past is not an alarm"
    w, _ = _watcher()
    w.step({**STREAM, "sentry_threat": False, "make": "Tesla"})
    out = w.step({**STREAM, "sentry_threat": True, "make": "Tesla"})
    assert [k for k, _ in out] == ["sentry"]


def test_low_battery_once_while_two_sources_take_turns_around_the_line():
    w, _ = _watcher()
    w.step({**STREAM, "soc": 23.0})
    out = []
    for soc in (20.0, 19.4, 20.0, 19.8, 20.2, 19.6, 21.0, 19.0):
        out += w.step({**STREAM, "source": "feed" if soc >= 20 else "stream", "soc": soc})
    assert [k for k, _ in out] == ["low_battery"], "once, though it crossed whole percents four times"
    assert w.step({**STREAM, "soc": 24.0}) == [] and w.step({**STREAM, "soc": 19.0}) == [], "not re-armed under 25"
    w.step({**STREAM, "soc": 25.0})
    assert [k for k, _ in w.step({**STREAM, "soc": 18.0})] == ["low_battery"], "re-armed after a charge"


def test_left_unlocked_or_a_door_open_means_ten_minutes_of_it_parked():
    for car in (LUCID, STREAM):
        clock = _Clock()
        w, _ = _watcher(clock=clock)
        w.step({**car, "status": "Driving"})
        arrival = [{**car, "locked": False, "doors_open": True}, {**car, "locked": False, "doors_open": False}, {**car, "locked": True}]
        out = []
        for snap in arrival:
            clock.tick(2)
            out += w.step(snap)
        assert [k for k, _ in out if k in ("unlocked", "door")] == [], f"an ordinary arrival is not news ({car['source']})"
        out = []
        for _ in range(12):
            clock.tick()
            out += w.step({**car, "locked": False, "doors_open": True})
        assert sorted(k for k, _ in out) == ["door", "unlocked"], car["source"]
        clock.tick()
        assert w.step({**car, "locked": None, "doors_open": True}) == [], "once; unknown changes nothing"


def test_unlocked_while_driving_is_not_left_unlocked():
    clock = _Clock()
    w, _ = _watcher(clock=clock)
    out = []
    for _ in range(15):
        clock.tick()
        out += w.step({**STREAM, "status": "Driving", "locked": False})
    assert out == []


def test_the_cars_kwh_count_counts_only_from_inside_this_charge():
    clock = _Clock()
    w, _ = _watcher(clock=clock)
    w.step(FEED)
    clock.tick()
    w.step({**FEED, "status": "Charging"})
    started = clock.t
    clock.tick(90)
    before = started - _td(hours=5)
    out = w.step({**FEED, "status": "Idle", "soc": 80.0, "charge_kwh_added": 12.0, "charge_kwh_at": before})
    clock.tick(5)
    out += w.step({**FEED, "status": "Idle", "soc": 80.0, "charge_kwh_added": 12.0, "charge_kwh_at": before})
    assert out == [("charge_done", "Bravo stopped charging at Home: 41→80%, limit 90%.")], "the count of the charge before is not this one's"
    w.step({**FEED, "status": "Charging", "soc": 80.0})
    clock.tick(30)
    out = w.step({**FEED, "status": "Idle", "soc": 90.0, "charge_kwh_added": 7.5, "charge_kwh_at": clock.t})
    assert out == [("charge_done", "Bravo finished charging at Home: 80→90%, 7.5 kWh.")]


def test_the_watcher_remembers_when_a_car_was_last_seen_driving():
    clock = _Clock()
    w, store = _watcher(clock=clock)
    w.step(FEED)
    assert w.last_driving("VIN-B") is None
    w.step({**FEED, "status": "Driving"})
    seen = clock.t
    clock.tick(5)
    w.step({**FEED, "status": "Idle"})
    assert w.last_driving("VIN-B") == seen
    w2, _ = _watcher(store, clock=clock)
    w2.step({**FEED, "status": "Idle"})
    assert w2.last_driving("VIN-B") == seen, "across a restart"


# ---- connector 0.4.0: the alarm, someone left inside, the key fob, Sentry's drive, the waiting update

L40 = {**BASE, "vin": "50EA1TEA0RA000001", "make": "Lucid", "status": "Idle", "sentry": True}


def test_the_alarm_going_off_is_said_once_and_again_when_a_warning_becomes_the_alarm():
    from notify import event_kinds

    armed = {**L40, "alarm_status": "armed"}
    assert events(armed, {**armed, "alarm_status": "intrusion"}) == ["The alarm on Alpha is going off at Home: an intrusion."]
    assert events({**armed, "alarm_status": "intrusion"}, {**armed, "alarm_status": "intrusion"}) == [], "once, not every minute"
    assert events({**armed, "alarm_status": "intrusion"}, {**armed, "alarm_status": "shock"}) == [], "one event, whatever it becomes"
    assert events(armed, {**armed, "alarm_status": "pre-alarm"}) == ["The alarm on Alpha is warning someone off at Home."]
    assert events({**armed, "alarm_status": "pre-alarm"}, {**armed, "alarm_status": "tilt"}) == ["The alarm on Alpha is going off at Home: the car was tilted."]
    assert [k for k, _ in event_kinds(armed, {**armed, "alarm_status": "panic"})] == ["alarm"]
    for quiet in ("armed", "disarmed", None):
        assert events(armed, {**armed, "alarm_status": quiet}) == []


def test_someone_left_inside_comes_first_and_again_as_the_car_warns_harder():
    from notify import event_kinds, occupant_level

    calm = {**L40, "living_object": "not-active", "occupant_warning": "no-request", "locked": True}
    both = event_kinds({**calm, "locked": True}, {**calm, "locked": False, "living_object": "level-1"})
    assert [k for k, _ in both] == ["occupant", "unlocked"], "before anything else in the same tick"
    assert both[0][1] == "Alpha senses someone left inside at Home."
    assert events({**calm, "living_object": "level-1"}, {**calm, "living_object": "level-1"}) == []
    assert events({**calm, "living_object": "level-1"}, {**calm, "living_object": "level-2"}) == ["Alpha senses someone left inside at Home: its second warning."]
    assert events({**calm, "living_object": "level-3"}, {**calm, "living_object": "level-3", "occupant_warning": "stage-2"}) == [], "the higher of the two"
    assert events(calm, {**calm, "occupant_warning": "stage-3"}) == ["Alpha senses someone left inside at Home: its third and highest warning."]
    assert [occupant_level({"living_object": x}) for x in ("disabled", "not-active", None, "level-2")] == [0, 0, 0, 2]
    assert occupant_level({"occupant_warning": "fault"}) == 0, "a fault is the sensor, not a person"


def test_the_key_fob_battery_is_said_once():
    low = {**L40, "keyfob_low": True}
    assert events({**L40, "keyfob_low": False}, low) == ["A key fob for Alpha has a low battery."]
    assert events(low, low) == []
    assert events({**L40, "keyfob_low": None}, {**L40, "keyfob_low": None}) == []


def test_halo_secure_on_with_no_usb_drive_is_said_once_by_the_cars_own_name():
    out = {**L40, "sentry_usb": False}
    assert events({**L40, "sentry_usb": True}, out) == ["Halo Secure on Alpha is on with no USB drive to record to."]
    assert events({**L40, "sentry": False, "sentry_usb": False}, out) == ["Halo Secure on Alpha is on with no USB drive to record to."], "turned on without one"
    assert events(out, out) == []
    assert events({**out, "sentry": None}, out) == [], "a guard that went unknown was not turned off"
    assert events({**L40, "sentry_usb": True}, {**L40, "sentry_usb": None}) == [], "the car's idle drive state is unknown, not out"
    assert events({**L40, "sentry": False, "sentry_usb": True}, {**L40, "sentry": False, "sentry_usb": False}) == [], "off, nothing to record anyway"


def test_the_update_alert_names_the_version_and_install_time_when_the_car_gives_them():
    was = {**L40, "update_available": None}
    assert events(was, {**L40, "update_available": True, "update_version": "3.1.2", "update_install_min": 30}) == ["A software update is waiting on Alpha: 3.1.2, about 30 min to install."]
    assert events(was, {**L40, "update_available": True, "update_version": "3.1.2"}) == ["A software update is waiting on Alpha: 3.1.2."]
    assert events(was, {**L40, "update_available": True}) == ["A software update is waiting on Alpha."], "a connector before 0.4.0, or a Tesla"


def test_a_tesla_and_a_connector_before_0_4_0_raise_none_of_them():
    """None of the new keys exist: a Tesla's reading, or connector 0.3.1's."""
    tesla = {**BASE, "vin": "5YJ3E1EA0JF000001", "make": "Tesla", "sentry": True}
    assert events(tesla, {**tesla, "sentry_threat": False}) == []
    assert events(L40, {**L40}) == []


def test_carried_keeps_what_the_car_stopped_saying_and_nothing_else():
    from notify import carried

    prev = {"keyfob_low": True, "alarm_status": "armed", "locked": True, "sentry_usb": False}
    now = carried({"keyfob_low": None, "locked": None, "sentry_usb": True}, prev)
    assert now == {"keyfob_low": True, "locked": None, "sentry_usb": True, "alarm_status": "armed"}, "the lock is never carried: unknown stays unknown"
    assert carried({"a": 1}, None) == {"a": 1}


def test_the_new_kinds_sit_in_their_groups_with_the_cars_own_guard_name():
    from notify import GROUPS, KINDS, kinds_for

    groups = dict(GROUPS)
    assert groups["Security"][:2] == ("occupant", "alarm") and "sentry_usb" in groups["Security"]
    assert "keyfob" in groups["Car health"]
    assert all(k in KINDS for k in ("occupant", "alarm", "sentry_usb", "keyfob"))
    assert kinds_for(["Lucid"])["sentry_usb"] == "Halo Secure with no USB drive"


def test_pushover_sends_the_priority(monkeypatch):
    import notify

    seen = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout):
        seen["body"] = req.data.decode()
        return Resp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake)
    assert send_pushover("a", "u", "x", priority=1) and "priority=1" in seen["body"]
    assert send_pushover("a", "u", "x") and "priority=0" in seen["body"]


def test_the_watcher_says_a_new_reading_once_though_the_car_stops_saying_it():
    """Alpha's key fob reads low, then the car sleeps (unknown), then wakes still low: one alert.
    The Watcher keeps the 0.4.0 readings, carried over a tick that lacks them."""
    w, _ = _watcher()
    said = []
    for low in (False, True, None, True, True, False, True):
        said += [k for k, _ in w.step({**LUCID, "status": "Sleeping", "keyfob_low": low})]
    assert said == ["keyfob", "keyfob"], "once while it stays low; again only after it read fine"
    for alarm in ("armed", "intrusion", "intrusion", None, "intrusion"):
        said += [k for k, _ in w.step({**LUCID, "alarm_status": alarm})]
    assert said.count("alarm") == 1


def test_alert_kinds_show_only_when_the_install_can_raise_them():
    from notify import GROUPS, KINDS, TESLA_ONLY, hidden_kinds, visible_groups

    nothing = hidden_kinds([], commandable=False, ha=False, connector=False)
    assert set(TESLA_ONLY) | {"sentry", "command", "charge_hold", "garage_open", "connector"} <= nothing
    tesla = hidden_kinds(["Tesla"], commandable=False, ha=False, connector=False)
    assert not (set(TESLA_ONLY) | {"sentry", "command", "charge_hold"}) & tesla, "a Tesla can raise every Tesla kind"
    assert {"garage_open", "connector"} <= tesla, "Home Assistant and the connector are separate facts"
    lucid = hidden_kinds(["Lucid"], commandable=True, ha=False, connector=True)
    assert not {"sentry", "command", "charge_hold", "connector"} & lucid and set(TESLA_ONLY) <= lucid, "a Lucid has a guard mode and takes commands, but has no key fob or Tesla API"
    other = hidden_kinds(["Audi"], commandable=True, ha=True, connector=True)
    assert "sentry" in other and "garage_open" not in other
    everything = hidden_kinds(["Tesla", "Lucid"], commandable=True, ha=True, connector=True)
    assert everything == set(), "a house with a Tesla, a Lucid, Home Assistant and the connector sees every kind"
    groups = visible_groups(nothing)
    assert [k for _, keys in groups for k in keys if k in nothing] == [] and all(keys for _, keys in groups)
    assert [k for _, keys in visible_groups(set()) for k in keys] == [k for _, keys in GROUPS for k in keys] and set(KINDS) >= nothing
