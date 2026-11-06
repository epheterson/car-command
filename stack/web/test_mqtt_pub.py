"""The MQTT publisher: the address, the car's readings, the discovery messages, what is sent on a change
and on the heartbeat, and a broker that fails. A fake client stands in for paho; nothing touches a network."""

import json

import mqtt_pub as mp
from test_empty import bare, client  # noqa: F401 - fixtures

ALPHA = {
    "name": "Alpha",
    "make": "Lucid",
    "model": "Air",
    "status": "Charging",
    "soc": 61.04,
    "range_mi": 201.26,
    "charging_power_kw": 42.0,
    "charge_target": 80,
    "plugged": True,
    "locked": True,
    "inside_f": 70.0,
    "odometer_mi": 12345.6,
    "updated": "2026-11-03T12:00:00+00:00",
    "lat": 39.78,
    "lon": -89.65,
    "heading": 90,
}
VIN = "ALPHA0000000000001"


class Fake:
    def __init__(self, fail=False):
        self.sent, self.fail, self.calls = [], fail, []

    def publish(self, topic, payload, qos=0, retain=False):
        if self.fail:
            raise OSError("broker gone")
        self.sent.append((topic, payload, retain))
        return self

    def wait_for_publish(self, timeout=None):
        self.calls.append("wait_for_publish")

    def __getattr__(self, name):  # connect_async, loop_start, disconnect and the rest
        return lambda *a, **k: self.calls.append(name)


def make(cars, **kw):
    fake = Fake()
    t = [0.0]
    pub = mp.Publisher(url="mqtt://u:p@broker.springfield.test", states=lambda: cars, client_factory=lambda cfg, will: fake, now=lambda: t[0], **kw)
    pub.connect()
    pub._on_connect(fake, None, {}, 0)
    return pub, fake, t


def topics(fake):
    return [s[0] for s in fake.sent]


def test_the_address():
    assert mp.parse_url("mqtt://u:p%40ss@broker.springfield.test:1884") == {"host": "broker.springfield.test", "port": 1884, "user": "u", "password": "p@ss", "tls": False}
    assert mp.parse_url("mqtts://broker.springfield.test")["port"] == 8883
    assert mp.parse_url("mqtts://broker.springfield.test")["tls"] is True
    assert mp.parse_url("") is None and mp.parse_url(None) is None
    assert mp.parse_url("http://broker") is None and mp.parse_url("mqtt://") is None and mp.parse_url("mqtt://h:99999999") is None


def test_a_car_is_driving_charging_parked_or_asleep():
    assert [mp.car_state({"status": s}) for s in ("Driving", "Charging", "Idle", "Ready", "Sleeping", None)] == ["driving", "charging", "parked", "parked", "asleep", "parked"]


def test_the_reading():
    r = mp.reading(ALPHA)
    assert r["state"] == "charging" and r["battery"] == 61.0 and r["range"] == 201.3 and r["charging_kw"] == 42.0
    assert (r["charge_limit"], r["plugged"], r["lock"], r["inside_temp"], r["odometer"]) == (80, "plugged", "locked", 70.0, 12345.6)
    r = mp.reading({"status": "Idle", "charging_power_kw": 3.0, "plugged": False, "locked": False})
    assert r["charging_kw"] == 0.0 and r["plugged"] == "unplugged" and r["lock"] == "unlocked" and r["battery"] is None
    assert mp.reading({})["plugged"] is None and mp.reading({})["lock"] is None, "a car that has not said is neither"


def test_the_messages_without_location():
    m = mp.messages("carcommand", "homeassistant", {VIN: ALPHA}, False)
    cid = mp.car_id(VIN)
    assert json.loads(m[f"carcommand/{cid}/state"])["state"] == "charging"
    assert m[f"homeassistant/device_tracker/carcommand_{cid}/location/config"] == "" and m[f"carcommand/{cid}/location"] == ""
    assert "39.78" not in "".join(m.values()), "no position anywhere"
    batt = json.loads(m[f"homeassistant/sensor/carcommand_{cid}/battery/config"])
    assert batt["state_topic"] == f"carcommand/{cid}/state" and batt["availability_topic"] == "carcommand/status" and batt["device"]["name"] == "Alpha"
    assert batt["unique_id"] == f"carcommand_{cid}_battery" and batt["device_class"] == "battery"
    lock = json.loads(m[f"homeassistant/binary_sensor/carcommand_{cid}/lock/config"])
    assert (lock["payload_on"], lock["payload_off"]) == ("unlocked", "locked")
    assert len([t for t in m if t.endswith("/config") and "/sensor/" in t]) == len(mp.SENSORS)


def test_the_messages_with_location():
    cid = mp.car_id(VIN)
    m = mp.messages("cc", "ha", {VIN: ALPHA}, True)
    assert json.loads(m[f"cc/{cid}/location"]) == {"heading": 90, "latitude": 39.78, "longitude": -89.65}
    assert json.loads(m[f"ha/device_tracker/carcommand_{cid}/location/config"])["json_attributes_topic"] == f"cc/{cid}/location"
    nofix = mp.messages("cc", "ha", {VIN: dict(ALPHA, lat=None)}, True)
    assert f"cc/{cid}/location" not in nofix


def test_nothing_goes_before_the_broker_answers():
    fake = Fake()
    pub = mp.Publisher(url="mqtt://broker.springfield.test", states=lambda: {VIN: ALPHA}, client_factory=lambda c, w: fake)
    pub.connect()
    assert pub.step() == 0 and fake.sent == [] and pub.status()["state"] == "wait"


def test_the_first_pass_sends_everything_retained_then_only_changes():
    cars = {VIN: dict(ALPHA)}
    pub, fake, t = make(cars)
    assert fake.sent[0] == ("carcommand/status", "online", True)
    n = pub.step()
    assert n > 10 and all(s[2] for s in fake.sent) and pub.status()["state"] == "ok"
    fake.sent.clear()
    assert pub.step() == 0, "nothing changed"
    cars[VIN]["soc"] = 60.0
    t[0] += 30
    assert pub.step() == 1 and topics(fake) == [f"carcommand/{mp.car_id(VIN)}/state"]


def test_the_heartbeat_sends_it_all_again():
    pub, fake, t = make({VIN: dict(ALPHA)})
    first = pub.step()
    fake.sent.clear()
    t[0] += mp.HEARTBEAT_S - 1
    assert pub.step() == 0
    t[0] += 2
    assert pub.step() == first


def test_a_reconnect_publishes_it_all_again():
    pub, fake, t = make({VIN: dict(ALPHA)})
    first = pub.step()
    pub._on_disconnect(fake, None, None, 0)
    assert pub.status()["state"] == "bad" and pub.step() == 0
    fake.sent.clear()
    pub._on_connect(fake, None, {}, 0)
    assert pub.step() == first and fake.sent[0][:2] == ("carcommand/status", "online")


def test_a_refused_connection_shows_as_an_error():
    class Refused:
        is_failure = True

        def __str__(self):
            return "Not authorized"

    pub, fake, _ = make({})
    pub._on_connect(fake, None, {}, Refused())
    assert pub.status() == {"state": "bad", "text": "Refused: Not authorized", "detail": "broker.springfield.test:1883"}
    assert pub.step() == 0


def test_a_broker_that_fails_or_cars_that_cannot_be_read_do_not_raise():
    pub, fake, _ = make({VIN: dict(ALPHA)})
    fake.fail = True
    assert pub.step() == 0 and pub.sent == {} and "Publish failed" in pub.status()["text"]
    fake.fail = False
    assert pub.step() > 0 and pub.status()["state"] == "ok", "the next good send clears the error"

    def boom():
        raise RuntimeError("db down")

    pub.states = boom
    assert pub.step() == 0


def test_off_and_bad_addresses():
    off = mp.Publisher.from_env(lambda: {}, env={})
    assert off.status() == {"state": "off", "text": "Off", "detail": ""}
    off.run()  # returns at once, creates no client
    assert off.client is None
    bad = mp.Publisher.from_env(lambda: {}, env={"MQTT_URL": "http://nope"})
    assert bad.status()["state"] == "bad"
    bad.run()
    assert bad.client is None


def test_the_environment():
    p = mp.Publisher.from_env(lambda: {}, env={"MQTT_URL": "mqtt://b.springfield.test", "MQTT_PREFIX": "/cars/", "MQTT_DISCOVERY_PREFIX": "ha", "MQTT_LOCATION": "1"})
    assert (p.prefix, p.discovery, p.location, p.avail) == ("cars", "ha", True, "cars/status")
    d = mp.Publisher.from_env(lambda: {}, env={"MQTT_URL": "mqtt://b.springfield.test", "MQTT_LOCATION": "true"})
    assert (d.prefix, d.discovery, d.location) == ("carcommand", "homeassistant", False)


def test_a_client_that_cannot_be_made_leaves_the_site_running():
    def nope(cfg, will):
        raise ImportError("no paho")

    pub = mp.Publisher(url="mqtt://b.springfield.test", states=lambda: {}, client_factory=nope)
    pub.run()
    assert pub.status()["state"] == "bad" and "Could not start" in pub.status()["text"]


def test_stop_says_offline_and_disconnects():
    pub, fake, _ = make({})
    pub.stop()
    assert fake.sent[-1][:2] == ("carcommand/status", "offline") and "disconnect" in fake.calls and "loop_stop" in fake.calls


def test_settings_data_shows_mqtt_off_connected_and_failed(client, bare, monkeypatch):  # noqa: F811
    assert "MQTT" in client.get("/settings/data").text and "Off" in client.get("/settings/data").text
    pub, _, _ = make({})
    monkeypatch.setattr(bare, "mqtt_worker", pub)
    text = client.get("/settings/data").text
    assert "Connected" in text and "broker.springfield.test:1883" in text and "u:p" not in text, "the password is never shown"
    pub.connected, pub.error = False, "Refused: Not authorized"
    assert "Refused: Not authorized" in client.get("/settings/data").text


def test_topics_carry_a_hash_not_the_vin():
    cid = mp.car_id(VIN)
    assert len(cid) == 10 and cid == mp.car_id(VIN) and cid != mp.car_id(VIN + "x") and VIN.lower() not in cid
    m = mp.messages("carcommand", "homeassistant", {VIN: ALPHA}, True)
    assert VIN not in "".join(m) and VIN.lower() not in "".join(m)
    assert json.loads(m[f"homeassistant/sensor/carcommand_{cid}/battery/config"])["device"]["name"] == "Alpha"


def test_metric_readings_and_units():
    from units import Units

    cid = mp.car_id(VIN)
    r = mp.reading(dict(ALPHA, inside_f=68.0), Units(True))
    assert r["range"] == 323.9 and r["odometer"] == 19868.3 and r["inside_temp"] == 20.0
    m = mp.messages("carcommand", "homeassistant", {VIN: ALPHA}, False, Units(True))
    conf = lambda k: json.loads(m[f"homeassistant/sensor/carcommand_{cid}/{k}/config"])  # noqa: E731
    assert (conf("range")["unit_of_measurement"], conf("odometer")["unit_of_measurement"], conf("inside_temp")["unit_of_measurement"]) == ("km", "km", "°C")
    imp = mp.messages("carcommand", "homeassistant", {VIN: ALPHA}, False)
    assert json.loads(imp[f"homeassistant/sensor/carcommand_{cid}/range/config"])["unit_of_measurement"] == "mi"


def test_a_change_of_units_republishes_values_and_configs():
    from units import Units

    metric = [False]
    pub, fake, _ = make({VIN: dict(ALPHA)}, units=lambda: Units(metric[0]))
    pub.step()
    fake.sent.clear()
    metric[0] = True
    assert pub.step() == 4  # state plus the three configs whose unit changed
