from datetime import timedelta

from recorder import should_record


def test_first_fix_is_recorded_and_missing_position_is_not():
    assert should_record(None, {"latitude": 37.0, "longitude": -122.0, "state": "Idle"})
    assert not should_record(None, {"latitude": None, "longitude": -122.0})


def test_records_on_movement_odometer_or_state_change_only():
    last = {"latitude": 37.0, "longitude": -122.0, "odometer": 1000.0, "state": "Sleeping"}
    assert not should_record(last, {**last})
    assert not should_record(last, {**last, "latitude": 37.000001})  # GPS jitter below 1e-5 deg
    assert should_record(last, {**last, "latitude": 37.001})
    assert should_record(last, {**last, "odometer": 1000.2})
    assert should_record(last, {**last, "state": "Driving"})


def test_tick_inserts_only_when_needed():
    from recorder import Recorder

    inserted = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            inserted.append(params) if params else None

        def commit(self):
            pass

    car = {"vin": "V", "lat": 37.0, "lon": -122.0, "heading": 90.0, "soc": 74.0, "odometer_mi": 1015.0, "status": "Sleeping"}
    r = Recorder(fetch_live=lambda: car, connect=lambda: Conn(), webhook_url="")
    assert r.tick() is True and len(inserted) == 1
    assert r.tick() is False and len(inserted) == 1
    car = {**car, "status": "Driving", "lat": 37.01}
    assert r.tick() is True and len(inserted) == 2 and inserted[-1][7] == "Driving"
    stored = Recorder(fetch_live=lambda: {**car, "live": False, "lat": 38.0}, connect=lambda: Conn(), webhook_url="")
    assert stored.tick() is False and len(inserted) == 2, "a car read from history is not a new fix"


def test_tick_emits_events_and_survives_connector_failures():
    from recorder import Recorder

    sent = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            pass

        def commit(self):
            pass

    from notify import Watcher

    snapshots = [{"vin": "V", "lat": 37.0, "lon": -122.0, "soc": 74.0, "odometer_mi": 1015.0, "status": "Sleeping", "name": "Alpha", "locked": True, "place": "Home", "live": True}]
    r = Recorder(fetch_live=lambda: snapshots[-1], connect=lambda: Conn(), webhook_url="")
    r.watcher = Watcher(load=lambda vin: None, save=lambda vin, text: None)
    r.notify = lambda text, *kind: sent.append(text)
    r.tick()
    snapshots.append({**snapshots[-1], "status": "Charging", "charging_power_kw": 11.0, "charge_target": 90.0})
    r.tick()
    assert sent == ["Alpha started charging at Home: 74% now, limit 90%, 11.0 kW."]

    def boom():
        raise RuntimeError("connector away")

    r.fetch_live = boom
    t = [r.clock()]
    r.clock = lambda: t[0]
    for _ in range(11):
        t[0] += timedelta(minutes=1)
        try:
            r.tick()
        except RuntimeError:
            pass
    assert sent[-1] == "Car Command has not heard from CarConnectivity for 10 minutes."
    r.fetch_live = lambda: snapshots[-1]
    r.tick()
    assert sent[-1] == "CarConnectivity is back."


def test_a_moving_car_is_sampled_more_often():
    from recorder import next_interval

    assert next_interval("Driving", 60, 20) == 20
    assert next_interval("Ready", 60, 20) == 20, "ignition on counts; the first fix of a drive cannot be recovered"


def test_a_car_going_nowhere_is_left_alone():
    from recorder import next_interval

    for status in ("Sleeping", "Idle", "Charging", "Unknown", None):
        assert next_interval(status, 60, 20) == 60, status


def test_a_muted_kind_sends_nothing():
    from recorder import Recorder

    sent = []
    r = Recorder(fetch_live=lambda: {}, connect=lambda: None, webhook_url="")
    r.telegram = (None, None)
    r.email = (None, None, None)
    r.pushover = (None, None)
    r.notify_raw = r.notify
    import recorder as mod

    mod.send_discord = lambda url, text: sent.append(text)
    r.webhook_url = "http://example.invalid/hook"
    r.muted = {"login"}
    r.notify("hello", "login")
    r.notify("world", "parked")
    assert sent == ["world"]


def test_notify_routes_each_kind_to_its_channels(monkeypatch):
    import recorder as rec

    sent = []
    monkeypatch.setattr(rec, "send_discord", lambda url, text: sent.append(("discord", text)))
    monkeypatch.setattr(rec, "send_telegram", lambda tok, chat, text: sent.append(("telegram", text)))
    monkeypatch.setattr(rec, "send_email", lambda *a: sent.append(("email", a[-1])))
    monkeypatch.setattr(rec, "send_pushover", lambda tok, user, text, priority=0: sent.append(("pushover", text)))
    r = rec.Recorder(fetch_live=lambda: {}, connect=None, webhook_url="https://example.invalid/hook")
    r.telegram = ("t", "c")
    r.email = ("smtp://x", "to@example.com", None)
    r.pushover = ("a", "u")
    assert r.channels() == ["discord", "telegram", "email", "pushover"]
    r.routes = {"parked": {"pushover"}, "login": set()}
    r.notify("parked", "parked")
    r.notify("who", "login")
    r.notify("set off", "driving")
    assert [c for c, t in sent if t == "parked"] == ["pushover"], "a routed kind goes only where it is sent"
    assert not [t for c, t in sent if t == "who"], "an empty route is off"
    assert [c for c, t in sent if t == "set off"] == ["discord", "telegram", "email", "pushover"], "an unrouted kind goes everywhere"
    r.pushover = (None, None)
    assert r.channels() == ["discord", "telegram", "email"], "a channel without its keys is not offered"
    monkeypatch.setattr(rec, "send_webhook", lambda url, tok, text, kind: sent.append(("webhook", f"{kind}:{text}")))
    r.hook = ("https://hub.example.test/alert", None)
    assert r.channels()[-1] == "webhook"
    r.routes = {"charge_hold": {"webhook"}}
    r.notify("Paused", "charge_hold")
    assert [t for c, t in sent if c == "webhook"] == ["charge_hold:Paused"], "the kind travels with the text"


def test_a_silent_connector_is_down_though_the_fetch_worked():
    """The site reads history when the connector stops answering, so fetch_live never fails;
    the connector check is what counts the minutes."""
    from recorder import Recorder

    sent = []
    down = [False]
    r = Recorder(fetch_live=lambda: {"vin": "V", "live": False}, connect=None, webhook_url="")
    r.connector_lost = lambda: down[0]
    r.notify = lambda text, *kind: sent.append(text)
    t = [r.clock()]
    r.clock = lambda: t[0]
    down[0] = True
    for _ in range(12):
        t[0] += timedelta(minutes=1)
        r.tick()
    assert sent == ["Car Command has not heard from CarConnectivity for 10 minutes."]
    down[0] = False
    r.tick()
    assert sent[-1] == "CarConnectivity is back."


def test_connector_down_is_ten_minutes_by_the_clock_not_ten_ticks():
    """A driving car ticks every ten seconds: ten failed ticks were under two minutes."""
    from recorder import Recorder

    sent = []
    down = [True]
    r = Recorder(fetch_live=lambda: {"vin": "V", "live": False}, connect=None, webhook_url="")
    t = [r.clock()]
    r.clock = lambda: t[0]
    r.connector_lost = lambda: down[0]
    r.notify = lambda text, *kind: sent.append(text)
    for _ in range(30):
        t[0] += timedelta(seconds=10)
        r.tick()
    assert sent == [], "five minutes is not ten"
    down[0] = False
    r.tick()
    assert sent == [], "back from an outage nobody was told of is no news"
    down[0] = True
    for _ in range(61):
        t[0] += timedelta(seconds=10)
        r.tick()
    assert sent == ["Car Command has not heard from CarConnectivity for 10 minutes."]


def test_a_car_read_from_history_is_not_compared_with_the_connector_car():
    """With the connector away, fetch_live hands back another car from history. Diffing it
    against the connector car's last snapshot compared two cars: the timer watches it instead."""
    from notify import Watcher
    from recorder import Recorder

    sent = []
    car = [{"vin": "A", "name": "Alpha", "status": "Charging", "soc": 60.0, "live": True}]
    r = Recorder(fetch_live=lambda: car[0], connect=None, webhook_url="")
    r.watcher = Watcher(load=lambda vin: None, save=lambda vin, text: None)
    r.notify = lambda text, *kind: sent.append(text)
    r.tick()
    car[0] = {"vin": "B", "name": "Bravo", "status": "Idle", "soc": 40.0, "live": False, "source": "feed"}
    r.tick()
    car[0] = {"vin": "A", "name": "Alpha", "status": "Charging", "soc": 61.0, "live": True}
    r.tick()
    assert sent == [], "no car finished or started charging"


def test_every_alert_is_logged_with_what_became_of_it(monkeypatch):
    import recorder as rec

    rows = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if "cc_alert_log" in sql and params:
                rows.append(params[1:])

        def commit(self):
            pass

    monkeypatch.setattr(rec, "send_discord", lambda url, text: True)
    monkeypatch.setattr(rec, "send_pushover", lambda tok, user, text, priority=0: False)
    r = rec.Recorder(fetch_live=lambda: {}, connect=lambda: Conn(), webhook_url="https://example.invalid/hook")
    r.telegram, r.email, r.hook = (None, None), (None, None, None), (None, None)
    r.pushover = ("a", "u")
    r.notify("Alpha started charging.", "charge_start", "VIN-A")
    r.routes = {"parked": set(), "driving": {"pushover"}}
    r.notify("Alpha parked.", "parked", "VIN-A")
    r.notify("Alpha is driving.", "driving", "VIN-A")
    r.pushover = (None, None)
    r.webhook_url = ""
    r.notify("Signed in.", "login")
    assert rows == [
        ("charge_start", "VIN-A", "Alpha started charging.", "partly sent", "discord, pushover failed"),
        ("parked", "VIN-A", "Alpha parked.", "muted", ""),
        ("driving", "VIN-A", "Alpha is driving.", "failed", "pushover failed"),
        ("login", None, "Signed in.", "no channel", ""),
    ]


def test_a_log_that_cannot_be_written_does_not_stop_the_message(monkeypatch):
    import recorder as rec

    sent = []
    monkeypatch.setattr(rec, "send_discord", lambda url, text: sent.append(text) or True)

    def broken():
        raise RuntimeError("database away")

    r = rec.Recorder(fetch_live=lambda: {}, connect=broken, webhook_url="https://example.invalid/hook")
    r.telegram, r.email, r.pushover, r.hook = (None, None), (None, None, None), (None, None), (None, None)
    r.notify("Alpha parked.", "parked")
    assert sent == ["Alpha parked."]


def test_someone_left_inside_goes_to_pushover_at_high_priority_and_nothing_else_does(monkeypatch):
    import recorder as rec

    sent = []
    monkeypatch.setattr(rec, "send_pushover", lambda tok, user, text, priority=0: sent.append((text, priority)) or True)
    r = rec.Recorder(fetch_live=lambda: {}, connect=None, webhook_url="")
    r.telegram, r.email, r.hook = (None, None), (None, None, None), (None, None)
    r.pushover = ("a", "u")
    r.notify("Alpha senses someone left inside at Home.", "occupant")
    r.notify("The alarm on Alpha is going off at Home: an intrusion.", "alarm")
    assert [p for _t, p in sent] == [1, 0]


def test_a_live_position_keeps_the_raw_gps_height_beside_it():
    import recorder as rec

    sql = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, q, params=None):
            sql.append((q, params))

        def commit(self):
            pass

    car = {"vin": "V1", "live": True, "lat": 39.78, "lon": -89.65, "status": "Idle", "elevation_raw": 8932.0}
    r = rec.Recorder(fetch_live=lambda: car, connect=Conn, webhook_url="")
    assert r.tick()
    q, params = [x for x in sql if "cc_positions" in x[0]][-1]
    assert "elevation_raw) VALUES" in q and params[-1] == 8932.0 and "elevation," not in q, "never the metres column"
    sql.clear()
    r.last = None
    r.fetch_live = lambda: {**car, "elevation_raw": None}
    assert r.tick() and [x for x in sql if "cc_positions" in x[0]][-1][1][-1] is None, "a Tesla, or connector 0.3.1"
