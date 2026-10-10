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

    snapshots = [{"vin": "V", "lat": 37.0, "lon": -122.0, "soc": 74.0, "odometer_mi": 1015.0, "status": "Sleeping", "name": "Alpha", "locked": True, "place": "Home"}]
    r = Recorder(fetch_live=lambda: snapshots[-1], connect=lambda: Conn(), webhook_url="")
    r.notify = lambda text, *kind: sent.append(text)
    r.tick()
    snapshots.append({**snapshots[-1], "status": "Charging", "charging_power_kw": 11.0, "charge_target": 90.0})
    r.tick()
    assert sent == ["Alpha started charging at Home: 74% now, limit 90%, 11.0 kW."]

    def boom():
        raise RuntimeError("connector away")

    r.fetch_live = boom
    for _ in range(10):
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
    monkeypatch.setattr(rec, "send_pushover", lambda tok, user, text: sent.append(("pushover", text)))
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
