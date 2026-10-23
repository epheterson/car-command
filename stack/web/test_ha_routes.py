"""Settings › Alerts links Home Assistant with its own sign-in: the state comes back once, the
refresh token is sealed, the first garage door is picked, and unlinking revokes and forgets."""

import json
from urllib.parse import parse_qs, urlparse

import pytest

import garagedoor
import sources as sources_mod
from test_empty import bare, client  # noqa: F401 - fixtures
from test_settings_routes import form
from test_urdb import Rows

HA_STATE = "ha_oauth_state"
STATES = [{"entity_id": "cover.garage_door", "state": "closed", "attributes": {"device_class": "garage", "friendly_name": "Garage"}}]


@pytest.fixture
def ha(bare, monkeypatch):  # noqa: F811
    store, posted = Rows(), []
    monkeypatch.setattr(bare, "db", lambda: store)
    monkeypatch.setattr(bare, "VAULT", sources_mod.Vault(sources_mod.new_key()))
    monkeypatch.setattr(garagedoor, "LINK", garagedoor.Link())
    monkeypatch.setattr(garagedoor, "READER", garagedoor.Reader(get=lambda: STATES[0]))

    class Answer:
        status_code = 200

        def json(self):
            return {"access_token": "acc-1", "refresh_token": "ref-secret-1", "expires_in": 1800}

    monkeypatch.setattr(garagedoor, "_post", lambda url, **kw: posted.append((url, kw.get("data"))) or Answer())
    monkeypatch.setattr(garagedoor, "get_json", lambda path, url=None, bearer=None: STATES)
    monkeypatch.setattr(garagedoor, "_ping", lambda url: type("A", (), {"status_code": 401})())  # Home Assistant, asked with no token
    return store, posted


def start(bare, client):  # noqa: F811
    r = client.post("/settings/alerts/ha/start", data=form(bare, {"url": "ha.springfield.test:8123"}), headers={"host": "cc.springfield.test", "x-forwarded-proto": "https"})
    # a page that moves on by itself, not a redirect: form-action 'self' would hold a form's redirect
    assert r.status_code == 200 and 'http-equiv="refresh"' in r.text
    import html as html_mod
    import re

    location = html_mod.unescape(re.search(r'href="([^"]+)"', r.text).group(1))
    q = parse_qs(urlparse(location).query)
    assert location.startswith("http://ha.springfield.test:8123/auth/authorize?")
    assert q["client_id"] == ["https://cc.springfield.test/"] and q["redirect_uri"] == ["https://cc.springfield.test/settings/alerts/ha/callback"]
    return q["state"][0]


def test_linking_seals_the_refresh_token_and_picks_the_garage_door(bare, ha, client):  # noqa: F811
    store, posted = ha
    state = start(bare, client)
    back = client.get(f"/settings/alerts/ha/callback?code=c1&state={state}", headers={"host": "cc.springfield.test", "x-forwarded-proto": "https"})
    assert back.status_code == 303 and "saved=" in back.headers["location"]
    assert posted == [("http://ha.springfield.test:8123/auth/token", {"grant_type": "authorization_code", "code": "c1", "client_id": "https://cc.springfield.test/"})]
    kept = json.loads(store.state[garagedoor.STATE_KEY])
    assert kept["entity"] == "cover.garage_door" and "ref-secret-1" not in store.state[garagedoor.STATE_KEY]
    assert garagedoor.LINK.ready() and garagedoor.LINK.refresh == "ref-secret-1"
    page = client.get("/settings/alerts").text
    assert "http://ha.springfield.test:8123" in page and '<option value="cover.garage_door" selected>Garage</option>' in page and "ref-secret-1" not in page


def test_a_state_is_good_once(bare, ha, client):  # noqa: F811
    state = start(bare, client)
    client.get(f"/settings/alerts/ha/callback?code=c1&state={state}")
    again = client.get(f"/settings/alerts/ha/callback?code=c1&state={state}")
    assert "spent" in parse_qs(urlparse(again.headers["location"]).query)["ha_error"][0]
    forged = client.get("/settings/alerts/ha/callback?code=c1&state=nope")
    assert "ha_error" in forged.headers["location"]


def test_a_bad_address_is_said_and_nothing_leaves(bare, ha, client):  # noqa: F811
    r = client.post("/settings/alerts/ha/start", data=form(bare, {"url": "ftp://ha"}))
    assert r.status_code == 303 and "ha_error" in r.headers["location"] and not ha[1]


def test_unlinking_revokes_in_home_assistant_and_forgets_here(bare, ha, client):  # noqa: F811
    store, posted = ha
    state = start(bare, client)
    client.get(f"/settings/alerts/ha/callback?code=c1&state={state}")
    r = client.post("/settings/alerts/ha/forget", data=form(bare, {}))
    assert r.status_code == 303 and garagedoor.STATE_KEY not in store.state and not garagedoor.LINK.ready()
    assert posted[-1] == ("http://ha.springfield.test:8123/auth/revoke", {"token": "ref-secret-1"})


def test_without_a_sealing_key_nothing_is_kept(bare, ha, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare, "VAULT", sources_mod.Vault(None))
    state = start(bare, client)
    back = client.get(f"/settings/alerts/ha/callback?code=c1&state={state}")
    assert "cannot+be+kept" in back.headers["location"] or "cannot%20be%20kept" in back.headers["location"]
    assert garagedoor.STATE_KEY not in ha[0].state and not garagedoor.LINK.ready()


def setup_opener(bare, monkeypatch, door_state="closed"):  # noqa: F811
    import garageopen
    from test_garageopen import HOME, model

    monkeypatch.setattr(bare, "home_places", lambda: [{"id": 7, "name": "Home", "latitude": HOME["lat"], "longitude": HOME["lon"], "radius_m": 150}])
    monkeypatch.setattr(bare, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}])
    monkeypatch.setattr(garagedoor, "configured", lambda: True)
    monkeypatch.setenv("HA_URL", "http://ha.springfield.test:8123")
    monkeypatch.setenv("HA_TOKEN", "env-token")
    monkeypatch.setattr(garagedoor, "READER", garagedoor.Reader(get=lambda: {"state": door_state, "last_changed": "2026-10-18T00:00:00+00:00"}))
    monkeypatch.setattr(bare, "APPROACH", garageopen.Approach(model=model()))
    monkeypatch.setattr(bare, "GARAGE_TIMERS", {})
    monkeypatch.setattr(bare, "GARAGE_DOOR_SEEN", {})
    monkeypatch.setattr(bare, "GARAGE_RECENT", {})
    monkeypatch.setattr(bare, "learn_garage_model", lambda: None)
    opened, sent = [], []
    monkeypatch.setattr(garagedoor, "open_cover", lambda entity=None: opened.append(1))
    monkeypatch.setattr(bare.recorder, "notify", lambda text, kind="other", vin=None: sent.append((kind, text)))
    return opened, sent


def drive_home(bare, monkeypatch, vin="V1"):  # noqa: F811
    """The test model's car (north to home at 11 m/s) as the recorder sees a Lucid: a new fix every
    45 s, a tick every 10 s; the timer is run by the tick at its time."""
    from datetime import timedelta

    from test_garageopen import dense, fixes

    track = dense()
    fx, i, t = fixes(track), 0, track[0]["t"]
    monkeypatch.setattr(bare.threading, "Timer", lambda delay, fn, args: type("T", (), {"start": lambda self: None, "cancel": lambda self: None, "daemon": True})())
    while t <= track[-1]["t"]:
        while i + 1 < len(fx) and fx[i + 1]["t"] <= t:
            i += 1
        f = fx[i]
        bare.garage_arrival({"vin": vin, "name": "Alpha", "lat": f["lat"], "lon": f["lon"], "speed_mph": f["mph"]}, t)
        t += timedelta(seconds=10)


def test_the_chosen_car_coming_home_starts_a_closed_door_once_and_watch_only_notes_it(bare, ha, client, monkeypatch):  # noqa: F811
    opened, sent = setup_opener(bare, monkeypatch)
    assert client.post("/settings/alerts/ha/arrival", data=form(bare, {"mode": "watch", "vin": "V1", "place": "7", "aim": "8"})).status_code == 303
    drive_home(bare, monkeypatch)
    kinds = [e["kind"] for e in bare.garage_log()]
    assert not opened and kinds[-1] == "schedule" and "start" in kinds and kinds[0] == "arrived"
    assert next(e for e in bare.garage_log() if e["kind"] == "start")["did"] == "would start the door"
    client.post("/settings/alerts/ha/arrival", data=form(bare, {"mode": "open", "vin": "V1", "place": "7", "aim": "8"}))
    drive_home(bare, monkeypatch, vin="V2")  # not the chosen car
    assert not opened
    drive_home(bare, monkeypatch)
    assert opened == [1] and sent == [("garage_open", "Opening the garage for Alpha.")]
    page = client.get("/settings/alerts").text
    assert '<option value="open" selected>Open</option>' in page and '<option value="8" selected>open 8 s before</option>' in page and "started the door" in page


def test_an_open_door_is_left_alone(bare, ha, monkeypatch):  # noqa: F811
    opened, _ = setup_opener(bare, monkeypatch, door_state="open")
    bare.state_set(bare.GARAGE_OPEN_KEY, json.dumps({"mode": "open", "vin": "V1"}))
    drive_home(bare, monkeypatch)
    assert not opened and next(e for e in bare.garage_log() if e["kind"] == "start")["did"] == "left alone: the door reads open"


def test_watching_times_the_owners_own_opening_against_the_model(bare, ha, monkeypatch):  # noqa: F811
    from datetime import timedelta

    from test_garageopen import T0

    setup_opener(bare, monkeypatch)
    bare.state_set(bare.GARAGE_OPEN_KEY, json.dumps({"mode": "watch", "vin": "V1"}))
    state = {"s": "closed", "since": T0}
    monkeypatch.setattr(garagedoor, "READER", type("R", (), {"read": lambda self: {"state": state["s"], "open": state["s"] != "closed", "since": state["since"]}, "at": 0})())
    import test_garageopen as tg

    track, fx = tg.dense(), tg.fixes(tg.dense())
    t, i = track[0]["t"], 0
    while t <= track[-1]["t"]:
        while i + 1 < len(fx) and fx[i + 1]["t"] <= t:
            i += 1
        trip = bare.APPROACH.trips.get("V1")
        if trip and trip.go and state["s"] == "closed":
            state["s"], state["since"] = "opening", trip.go + timedelta(seconds=6)  # the owner taps 6 s after the model would have
        bare.garage_arrival({"vin": "V1", "name": "Alpha", "lat": fx[i]["lat"], "lon": fx[i]["lon"], "speed_mph": fx[i]["mph"]}, t)
        t += timedelta(seconds=10)
    assert next(e for e in bare.garage_log() if e["kind"] == "you_opened")["off_s"] == -6


def camera_drive(bare, monkeypatch, answer, vin="V1"):  # noqa: F811
    """drive_home with the driveway camera on: the watch runs at once, in line, with this answer."""
    calls = []
    monkeypatch.setattr(bare, "DRIVEWAY_URL", "http://driveway:8090")

    class Now:
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            self.target, self.args, self.kwargs = target, args, kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(bare.threading, "Thread", Now)
    monkeypatch.setattr(bare.httpx, "post", lambda url, json=None, timeout=None: calls.append((url, json)) or type("A", (), {"json": lambda self: answer})())
    drive_home(bare, monkeypatch, vin=vin)
    return calls


def test_with_the_camera_the_door_starts_when_it_sees_the_car_and_never_on_the_guess_alone(bare, ha, monkeypatch):  # noqa: F811
    opened, sent = setup_opener(bare, monkeypatch)
    bare.state_set(bare.GARAGE_OPEN_KEY, json.dumps({"mode": "open", "vin": "V1", "aim": 13}))
    calls = camera_drive(bare, monkeypatch, {"seen": True, "frames": 40, "fps": 4.8, "parked": 4})
    assert calls[0][0] == "http://driveway:8090/watch" and 30 < calls[0][1]["seconds"] <= 240
    assert opened == [1] and sent == [("garage_open", "Opening the garage for Alpha.")]
    start = next(e for e in bare.garage_log() if e["kind"] == "start")
    assert start["how"] == "camera" and next(e for e in bare.garage_log() if e["kind"] == "camera")["seen"] is True


def test_the_camera_seeing_nothing_or_unable_to_look_opens_nothing(bare, ha, monkeypatch):  # noqa: F811
    opened, _ = setup_opener(bare, monkeypatch)
    bare.state_set(bare.GARAGE_OPEN_KEY, json.dumps({"mode": "open", "vin": "V1", "aim": 13}))
    camera_drive(bare, monkeypatch, {"seen": False, "why": "timed out", "frames": 400})
    assert not opened and not any(e["kind"] == "start" for e in bare.garage_log())
    opened, _ = setup_opener(bare, monkeypatch)
    bare.state_set(bare.GARAGE_OPEN_KEY, json.dumps({"mode": "open", "vin": "V1", "aim": 13}))
    camera_drive(bare, monkeypatch, {"seen": False, "why": "no frames", "frames": 0})
    assert not opened and not any(e["kind"] == "start" for e in bare.garage_log())  # the camera down: skip


def test_watching_needs_no_home_assistant_but_open_does(bare, ha, client, monkeypatch):  # noqa: F811
    setup_opener(bare, monkeypatch)
    monkeypatch.setattr(garagedoor, "configured", lambda: False)
    r = client.post("/settings/alerts/ha/arrival", data=form(bare, {"mode": "open", "vin": "V1", "aim": "13"}))
    assert "Connect+Home+Assistant+first" in r.headers["location"] and bare.garage_open_config()["mode"] != "open"
    client.post("/settings/alerts/ha/arrival", data=form(bare, {"mode": "watch", "vin": "V1", "aim": "13"}))
    camera_drive(bare, monkeypatch, {"seen": True, "frames": 40})
    assert next(e for e in bare.garage_log() if e["kind"] == "start")["did"] == "would start the door"


def test_another_car_coming_home_is_watched_for_its_frames_and_never_opens_the_door(bare, ha, monkeypatch):  # noqa: F811
    """Training data: the other cars' arrivals keep frames under their own
    names, so the opener's car can be told from them; the door is the opener's car's alone."""
    opened, sent = setup_opener(bare, monkeypatch)
    bare.state_set(bare.GARAGE_OPEN_KEY, json.dumps({"mode": "open", "vin": "V1", "aim": 13}))
    calls = camera_drive(bare, monkeypatch, {"seen": True, "frames": 40, "fps": 4.8, "parked": 4}, vin="V2")
    assert len(calls) == 1 and calls[0][1]["car"] == "Alpha" and not opened and not sent
    log = bare.garage_log()
    assert [e["kind"] for e in log] == ["camera"] and log[0]["collect"] and log[0]["seen"]


def depart(bare, monkeypatch, answer, door_after="open", status=("Sleeping", "Ready"), moves=True):  # noqa: F811
    """The garage's car leaving: the door read closed then open, the departure tick, and the
    driveway watch answering at once, in line, then the car reporting Driving (`moves`) or staying parked."""
    import garageclose
    from datetime import timedelta

    from test_garageopen import T0

    monkeypatch.setattr(bare, "DEPARTURE", garageclose.Departure())
    monkeypatch.setattr(bare, "DRIVEWAY_URL", "http://driveway:8090")
    door, car, closed, calls, clock = {"s": "closed"}, {"status": status[0]}, [], [], {"t": T0}
    monkeypatch.setattr(bare, "garage_now", lambda: clock["t"])
    monkeypatch.setattr(garagedoor, "READER", type("R", (), {"read": lambda self: {"state": door["s"], "open": door["s"] != "closed", "since": None}, "at": 0})())
    monkeypatch.setattr(garagedoor, "close_cover", lambda entity=None: closed.append(1))
    monkeypatch.setattr(bare, "shared_states", lambda vin, hide, share=False: {"V1": {"name": "Alpha", "status": car["status"], "at_home": True, "speed_mph": car.get("speed", 0)}})

    class Now:
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            self.target, self.args, self.kwargs = target, args, kwargs or {}

        def start(self):
            door["s"] = door_after  # the car goes while the camera watches
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(bare.threading, "Thread", Now)

    def post(url, json=None, timeout=None):
        calls.append((url, json))
        clock["t"] = T0 + timedelta(seconds=60 if answer.get("seen") else 700)  # a sighting is quick; nothing seen runs out the ten-minute watch
        return type("A", (), {"json": lambda self: answer})()

    monkeypatch.setattr(bare.httpx, "post", post)
    bare.garage_departure_tick(T0)
    door["s"], car["status"] = "open", status[1]
    bare.garage_departure_tick(T0 + timedelta(seconds=5))
    if moves:
        car["status"], car["speed"] = "Driving", 14
    bare.garage_departure_tick(T0 + timedelta(seconds=100))
    bare.garage_departure_tick(T0 + timedelta(seconds=200))  # two minutes on: a car that never moved is settled
    door["s"] = "closed"
    bare.garage_departure_tick(T0 + timedelta(seconds=300))
    return closed, calls


def test_the_car_going_out_closes_the_door_behind_it_and_watch_only_notes_it(bare, ha, client, monkeypatch):  # noqa: F811
    _, sent = setup_opener(bare, monkeypatch)
    bare.state_set(bare.GARAGE_OPEN_KEY, json.dumps({"mode": "open", "vin": "V1"}))
    assert client.post("/settings/alerts/ha/close", data=form(bare, {"mode": "watch"})).status_code == 303
    closed, calls = depart(bare, monkeypatch, {"seen": True, "leaving": True, "people": 0, "frames": 900})
    assert calls[0][1] == {"seconds": 600.0, "car": "Alpha", "leaving": True} and not closed
    log = bare.garage_log()
    assert log[0]["kind"] == "departure" and log[0]["closed_s"] == 295 and log[0]["did"] == "would close the door"
    assert next(e for e in log if e["kind"] == "close")["did"] == "would close the door"
    client.post("/settings/alerts/ha/close", data=form(bare, {"mode": "close"}))
    closed, _ = depart(bare, monkeypatch, {"seen": True, "leaving": True, "people": 0})
    assert closed == [1] and ("garage_open", "Closing the garage behind Alpha.") in sent
    page = client.get("/settings/alerts").text
    assert '<option value="close" selected>Close</option>' in page and "closed the door" in page and "Comings and goings" in page


def test_someone_in_the_driveway_or_a_camera_that_saw_nothing_closes_nothing(bare, ha, monkeypatch):  # noqa: F811
    setup_opener(bare, monkeypatch)
    bare.state_set(bare.GARAGE_OPEN_KEY, json.dumps({"mode": "open", "vin": "V1"}))
    bare.state_set(bare.GARAGE_CLOSE_KEY, json.dumps({"mode": "close"}))
    closed, _ = depart(bare, monkeypatch, {"seen": True, "leaving": True, "people": 1})
    assert not closed and next(e for e in bare.garage_log() if e["kind"] == "close")["did"] == "left open: 1 person in the driveway"
    closed, _ = depart(bare, monkeypatch, {"seen": False, "why": "timed out", "frames": 3000})
    assert not closed and not any(e["kind"] == "close" for e in bare.garage_log()[:3])
    closed, _ = depart(bare, monkeypatch, {"seen": True, "leaving": True, "people": 0}, door_after="closed")
    assert not closed  # closed by hand before the camera answered: left alone


def test_closing_needs_home_assistant(bare, ha, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(garagedoor, "configured", lambda: False)
    r = client.post("/settings/alerts/ha/close", data=form(bare, {"mode": "close"}))
    assert "Connect+Home+Assistant+first" in r.headers["location"] and bare.garage_close_mode() == "off"
    assert client.post("/settings/alerts/ha/close", data=form(bare, {"mode": "slam"})).status_code == 400


def test_an_address_that_does_not_answer_is_said_before_the_browser_leaves(bare, ha, client, monkeypatch):  # noqa: F811
    def down(url):
        raise OSError("name not known")

    monkeypatch.setattr(garagedoor, "_ping", down)
    r = client.post("/settings/alerts/ha/start", data=form(bare, {"url": "ha.springfield.test:8123"}))
    msg = parse_qs(urlparse(r.headers["location"]).query)["ha_error"][0]
    assert r.status_code == 303 and "Nothing answers at http://ha.springfield.test:8123" in msg and "OSError" not in msg and not ha[1]
    assert HA_STATE not in ha[0].state, "no sign-in was started"


def test_something_that_is_not_home_assistant_is_said_too(bare, ha, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(garagedoor, "_ping", lambda url: type("A", (), {"status_code": 404})())
    r = client.post("/settings/alerts/ha/start", data=form(bare, {"url": "http://printer.springfield.test"}))
    assert "answered 404" in parse_qs(urlparse(r.headers["location"]).query)["ha_error"][0]


def test_a_vehicle_out_while_the_car_stays_parked_still_closes_and_says_a_car(bare, ha, monkeypatch):  # noqa: F811
    setup_opener(bare, monkeypatch)
    bare.state_set(bare.GARAGE_OPEN_KEY, json.dumps({"mode": "open", "vin": "V1"}))
    bare.state_set(bare.GARAGE_CLOSE_KEY, json.dumps({"mode": "close"}))
    sent = []
    monkeypatch.setattr(bare.recorder, "notify", lambda text, *a, **k: sent.append(text))
    closed, _ = depart(bare, monkeypatch, {"seen": True, "leaving": True, "people": 0}, moves=False, status=("Sleeping", "Sleeping"))
    assert closed and any(t == "Closing the garage behind a car." for t in sent), sent
