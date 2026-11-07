"""Each car's state published to an MQTT broker, with Home Assistant discovery, so a self-hoster's
Home Assistant shows the cars without any setup of its own.

Off unless MQTT_URL is set (mqtt://user:pass@host:1883, or mqtts:// for TLS). A background thread
reads the cars' shared state every EVERY_S and publishes what changed, retained, plus everything
again every HEARTBEAT_S so a restarted broker or a late subscriber is never stale for long. Nothing
here runs on a page load, and a broker that is down only shows as an error in Settings: paho
reconnects with backoff on its own thread, and every call into it is guarded.

Topics (PREFIX is MQTT_PREFIX, default carcommand; <id> is a short hash of the VIN, so the VIN itself
never reaches the broker):
  PREFIX/status            online / offline, the last will; every entity hangs on it
  PREFIX/<id>/state        one JSON object of the car's readings
  PREFIX/<id>/location     latitude, longitude and heading; only with MQTT_LOCATION=1
Discovery configs go to MQTT_DISCOVERY_PREFIX (default homeassistant), one per entity.

Location is the one thing that must stay on the owner's own network: it is published only when
asked for, and when it is off any location entity an earlier run left behind is cleared.
"""

from __future__ import annotations

import json
import logging
import os
import hashlib
import threading
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

from units import Units

LOG = logging.getLogger("carcommand.mqtt")

EVERY_S = 15.0  # how often the cars are looked at
HEARTBEAT_S = 600.0  # everything is published again this often, changed or not
QOS = 1

# (key in the state JSON, entity name, extra discovery fields). {dist} and {temp} are filled in from the
# site's units setting (km and °C when metric).
SENSORS = [
    ("state", "State", {"device_class": "enum", "options": ["driving", "charging", "parked", "asleep"], "icon": "mdi:car"}),
    ("battery", "Battery", {"device_class": "battery", "unit_of_measurement": "%", "state_class": "measurement"}),
    ("range", "Range", {"device_class": "distance", "unit_of_measurement": "{dist}", "state_class": "measurement"}),
    ("charging_kw", "Charging power", {"device_class": "power", "unit_of_measurement": "kW", "state_class": "measurement"}),
    ("charge_limit", "Charge limit", {"unit_of_measurement": "%", "icon": "mdi:battery-charging-high"}),
    ("inside_temp", "Inside temperature", {"device_class": "temperature", "unit_of_measurement": "{temp}", "state_class": "measurement"}),
    ("odometer", "Odometer", {"device_class": "distance", "unit_of_measurement": "{dist}", "state_class": "total_increasing"}),
    ("last_updated", "Last updated", {"device_class": "timestamp", "entity_category": "diagnostic"}),
]
# Home Assistant's lock and plug classes read ON as unlocked / plugged in, so the payloads say that.
BINARY = [
    ("plugged", "Plugged in", {"device_class": "plug", "payload_on": "plugged", "payload_off": "unplugged"}),
    ("lock", "Lock", {"device_class": "lock", "payload_on": "unlocked", "payload_off": "locked"}),
]


def parse_url(text: str | None) -> dict | None:
    """MQTT_URL as {host, port, user, password, tls}, or None when it is blank or not an mqtt:// or mqtts:// address."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        u = urlsplit(text)
        port = u.port
    except ValueError:
        return None
    if u.scheme not in ("mqtt", "mqtts") or not u.hostname:
        return None
    tls = u.scheme == "mqtts"
    return {"host": u.hostname, "port": port or (8883 if tls else 1883), "user": unquote(u.username) if u.username else None, "password": unquote(u.password) if u.password else None, "tls": tls}


def car_state(car: dict) -> str:
    """The car's status as one of driving, charging, parked or asleep."""
    s = str(car.get("status") or "").lower()
    if s in ("driving", "charging"):
        return s
    return "asleep" if s in ("sleeping", "asleep", "offline") else "parked"


def _iso(v) -> str | None:
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    return str(v) if v else None


def _num(v):
    return round(v, 1) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _flag(v, yes: str, no: str) -> str | None:
    return None if v is None else (yes if v else no)


def reading(car: dict, units: Units | None = None) -> dict:
    """The car's published JSON object: its state and readings in the site's units (miles and °F stored, km and °C when metric), None for anything it has not said."""
    u = units or Units()
    state = car_state(car)
    kw = _num(car.get("charging_power_kw"))
    return {
        "state": state,
        "battery": _num(car.get("soc")),
        "range": _num(u.distance(car.get("range_mi"))),
        "charging_kw": (kw or 0.0) if state == "charging" else 0.0,
        "charge_limit": _num(car.get("charge_target")),
        "plugged": _flag(car.get("plugged"), "plugged", "unplugged"),
        "lock": _flag(car.get("locked"), "locked", "unlocked"),
        "inside_temp": _num(u.temperature(car.get("inside_f"))),
        "odometer": _num(u.distance(car.get("odometer_mi"))),
        "last_updated": _iso(car.get("updated")),
    }


def car_id(vin: str) -> str:
    """A stable ten-character id for a car in topics and unique ids: the VIN's hash, never the VIN."""
    return hashlib.sha256(vin.encode()).hexdigest()[:10]


def messages(prefix: str, disc: str, cars: dict[str, dict], location: bool, units: Units | None = None) -> dict[str, str]:
    """Every retained message the cars call for, as {topic: payload}: discovery configs, state and, when asked, location."""
    out: dict[str, str] = {}
    avail = f"{prefix}/status"
    labels = (units or Units()).labels
    for vin, car in cars.items():
        if not isinstance(car, dict):
            continue
        cid = car_id(vin)
        base = f"{prefix}/{cid}"
        out[f"{base}/state"] = json.dumps(reading(car, units), sort_keys=True)
        device = {
            "identifiers": [f"carcommand_{cid}"],
            "name": str(car.get("name") or "Car"),
            "manufacturer": car.get("make") or None,
            "model": car.get("model") or None,
            "sw_version": car.get("software") or None,
        }
        device = {k: v for k, v in device.items() if v}

        def config(component: str, key: str, name: str, extra: dict, topic_key: str = "state") -> None:
            conf = {
                "name": name,
                "has_entity_name": True,
                "unique_id": f"carcommand_{cid}_{key}",
                "state_topic": f"{base}/{topic_key}",
                "value_template": "{{ value_json.%s }}" % key,
                "availability_topic": avail,
                "device": device,
                **{k: v.format(**labels) if isinstance(v, str) else v for k, v in extra.items()},
            }
            out[f"{disc}/{component}/carcommand_{cid}/{key}/config"] = json.dumps(conf, sort_keys=True)

        for key, name, extra in SENSORS:
            config("sensor", key, name, extra)
        for key, name, extra in BINARY:
            config("binary_sensor", key, name, extra)
        tracker = f"{disc}/device_tracker/carcommand_{cid}/location/config"
        if location and isinstance(car.get("lat"), (int, float)) and isinstance(car.get("lon"), (int, float)):
            out[f"{base}/location"] = json.dumps({"latitude": car["lat"], "longitude": car["lon"], "heading": car.get("heading")}, sort_keys=True)
            conf = {
                "name": "Location",
                "has_entity_name": True,
                "unique_id": f"carcommand_{cid}_location",
                "json_attributes_topic": f"{base}/location",
                "availability_topic": avail,
                "source_type": "gps",
                "device": device,
            }
            out[tracker] = json.dumps(conf, sort_keys=True)
        elif not location:
            out[tracker] = ""  # an empty retained message deletes what an earlier run left
            out[f"{base}/location"] = ""
    return out


def new_client(cfg: dict, will_topic: str):
    """A paho client for cfg, with the last will set and reconnect backoff of 1 s up to 2 min. Imported here so the site runs without the package when MQTT is off."""
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="", clean_session=True)
    if cfg["user"]:
        client.username_pw_set(cfg["user"], cfg["password"])
    if cfg["tls"]:
        client.tls_set()
    client.will_set(will_topic, "offline", qos=QOS, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    return client


class Publisher(threading.Thread):
    def __init__(self, *, url, states, units=Units, prefix="carcommand", discovery="homeassistant", location=False, client_factory=new_client, now=time.monotonic):
        super().__init__(daemon=True, name="mqtt")
        self.cfg = parse_url(url)
        self.url_given = bool((url or "").strip())
        self.states, self.units, self.location, self.factory, self.now = states, units, bool(location), client_factory, now
        self.prefix = (prefix or "carcommand").strip("/") or "carcommand"
        self.discovery = (discovery or "homeassistant").strip("/") or "homeassistant"
        self.client = None
        self.connected = False
        self.error: str | None = None
        self.sent: dict[str, str] = {}
        self.sent_at = 0.0
        self._stop_event = threading.Event()

    @property
    def avail(self) -> str:
        return f"{self.prefix}/status"

    @classmethod
    def from_env(cls, states, env=None, units=Units):
        env = os.environ if env is None else env
        return cls(
            url=env.get("MQTT_URL", ""),
            states=states,
            prefix=env.get("MQTT_PREFIX", ""),
            discovery=env.get("MQTT_DISCOVERY_PREFIX", ""),
            location=env.get("MQTT_LOCATION", "").strip() == "1",
            units=units,
        )

    def status(self) -> dict:
        """What Settings shows: state is off, ok, wait or bad, with a short text."""
        if not self.url_given:
            return {"state": "off", "text": "Off", "detail": ""}
        if self.cfg is None:
            return {"state": "bad", "text": "MQTT_URL is not an mqtt:// or mqtts:// address", "detail": ""}
        where = f"{self.cfg['host']}:{self.cfg['port']}"
        if self.connected and not self.error:
            return {"state": "ok", "text": "Connected", "detail": where}
        if self.error:
            return {"state": "bad", "text": self.error, "detail": where}
        return {"state": "wait", "text": "Connecting", "detail": where}

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        failed = getattr(reason_code, "is_failure", False)
        if failed:
            self.connected, self.error = False, f"Refused: {reason_code}"
            return
        self.connected, self.error = True, None
        self.sent = {}  # a fresh session: everything is published again on the next pass
        self._send(self.avail, "online")

    def _on_disconnect(self, client, userdata, *args) -> None:
        self.connected = False
        if self.error is None:
            self.error = "Disconnected, retrying"

    def _send(self, topic: str, payload: str) -> bool:
        try:
            self.client.publish(topic, payload, qos=QOS, retain=True)
            if self.connected:
                self.error = None
            return True
        except Exception as err:  # noqa: BLE001 - a broker that is down must not stop the site
            self.error = f"Publish failed ({type(err).__name__})"
            return False

    def step(self) -> int:
        """Publish whatever changed since the last pass, or everything on the heartbeat. Returns how many messages went."""
        if not self.connected:
            return 0
        beat = self.now() - self.sent_at >= HEARTBEAT_S
        if beat:
            self.sent = {}
        try:
            want = messages(self.prefix, self.discovery, self.states(), self.location, self.units())
        except Exception as err:  # noqa: BLE001 - the cars unreadable this pass; try again next
            LOG.warning("mqtt: could not read the cars (%s)", type(err).__name__)
            return 0
        n = 0
        for topic, payload in want.items():
            if self.sent.get(topic) != payload and self._send(topic, payload):
                self.sent[topic] = payload
                n += 1
        if beat and n:
            self.sent_at = self.now()
        return n

    def connect(self) -> None:
        self.client = self.factory(self.cfg, self.avail)
        self.client.on_connect, self.client.on_disconnect = self._on_connect, self._on_disconnect
        self.client.connect_async(self.cfg["host"], self.cfg["port"], keepalive=60)
        self.client.loop_start()

    def run(self) -> None:
        if self.cfg is None:
            if self.url_given:
                LOG.warning("mqtt: MQTT_URL is not an mqtt:// or mqtts:// address; not publishing")
            return
        try:
            self.connect()
        except Exception as err:  # noqa: BLE001 - no paho, or a bad setting: the site runs on
            self.error = f"Could not start ({type(err).__name__})"
            LOG.warning("mqtt: %s", self.error)
            return
        LOG.info("mqtt: publishing to %s:%s as %s/ (location %s)", self.cfg["host"], self.cfg["port"], self.prefix, "on" if self.location else "off")
        while not self._stop_event.wait(EVERY_S if self.sent else 1.0):
            try:
                self.step()
            except Exception as err:  # noqa: BLE001
                LOG.warning("mqtt: %s", type(err).__name__)

    def stop(self) -> None:
        self._stop_event.set()
        if self.client is not None:
            for leave in (self._say_offline, self.client.disconnect, self.client.loop_stop):
                try:
                    leave()
                except Exception:  # noqa: BLE001, S110 - leaving anyway
                    pass

    def _say_offline(self) -> None:
        """A clean exit sends no last will, so the offline is sent by hand, waited on for up to two seconds."""
        if self.connected:
            self.client.publish(self.avail, "offline", qos=QOS, retain=True).wait_for_publish(2)
