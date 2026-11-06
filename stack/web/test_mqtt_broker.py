"""The publisher against a real Mosquitto broker with real paho, in a throwaway container. Skipped
when docker is not running. Checks that discovery configs and state arrive retained, that the
last will says offline when the connection drops, a clean stop says offline, and that the
publisher reconnects and republishes after the broker restarts."""

import json
import shutil
import socket
import subprocess
import threading
import time
import uuid

import paho.mqtt.client as paho
import pytest

import mqtt_pub as mp

VIN = "ALPHA0000000000001"
CAR = {"name": "Alpha", "status": "Parked", "soc": 55.0, "range_mi": 180.0, "plugged": False, "locked": True, "updated": "2026-11-03T12:00:00+00:00"}


def docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def wait_for(check, seconds=30.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        got = check()
        if got:
            return got
        time.sleep(0.2)
    return None


def collect(port: int, topic_filter: str = "#", seconds: float = 1.5) -> dict[str, str]:
    """What a fresh subscriber is handed (the retained messages) within a moment."""
    got: dict[str, str] = {}
    c = paho.Client(paho.CallbackAPIVersion.VERSION2)
    c.on_message = lambda cl, ud, m: got.__setitem__(m.topic, m.payload.decode())
    c.on_connect = lambda cl, ud, flags, rc, props=None: cl.subscribe(topic_filter, qos=1)  # paho drops a subscribe made before the CONNACK
    c.connect("127.0.0.1", port)
    c.loop_start()
    time.sleep(seconds)
    c.loop_stop()
    c.disconnect()
    return got


def watcher(port: int, topic: str) -> list[str]:
    """A subscriber that keeps listening; the list it fills is every payload seen on topic."""
    seen: list[str] = []
    c = paho.Client(paho.CallbackAPIVersion.VERSION2)
    c.on_message = lambda cl, ud, m: seen.append(m.payload.decode())
    subscribed = threading.Event()
    c.on_connect = lambda cl, ud, flags, rc, props=None: cl.subscribe(topic, qos=1)  # paho drops a subscribe made before the CONNACK
    c.on_subscribe = lambda cl, ud, mid, rcs, props=None: subscribed.set()
    c.connect("127.0.0.1", port)
    c.loop_start()
    watcher.clients.append(c)
    assert subscribed.wait(10), "the watcher never subscribed"
    return seen


watcher.clients = []


def publisher(port: int) -> mp.Publisher:
    pub = mp.Publisher(url=f"mqtt://127.0.0.1:{port}", states=lambda: {VIN: dict(CAR)})
    pub.connect()
    assert wait_for(lambda: pub.connected), "never connected"
    return pub


def scenario(port: int, restart) -> None:
    cid = mp.car_id(VIN)
    status = watcher(port, "carcommand/status")
    pub = publisher(port)
    assert pub.step() > 10

    got = collect(port)
    assert got["carcommand/status"] == "online"
    state = json.loads(got[f"carcommand/{cid}/state"])
    assert state["state"] == "parked" and state["battery"] == 55.0 and state["lock"] == "locked"
    conf = json.loads(got[f"homeassistant/sensor/carcommand_{cid}/battery/config"])
    assert conf["state_topic"] == f"carcommand/{cid}/state" and conf["device"]["name"] == "Alpha"
    assert f"homeassistant/binary_sensor/carcommand_{cid}/lock/config" in got
    assert not any(VIN in t or VIN in p for t, p in got.items()), "the VIN never reaches the broker"

    # The connection drops without a goodbye: the broker publishes the last will.
    pub.client.loop_stop()
    pub.client.socket().close()
    assert wait_for(lambda: status and status[-1] == "offline"), f"no last will: {status}"
    assert collect(port, "carcommand/status")["carcommand/status"] == "offline", "the will is retained"

    # A restart of the broker: the publisher reconnects by itself and publishes everything again.
    pub = publisher(port)
    pub.step()
    restart()
    assert wait_for(lambda: pub.connected and pub.sent == {}, 60), "did not reconnect"
    assert pub.step() > 10
    again = collect(port)
    assert again["carcommand/status"] == "online" and f"carcommand/{cid}/state" in again

    # A clean stop says offline itself.
    pub.stop()
    assert collect(port, "carcommand/status")["carcommand/status"] == "offline"
    for c in watcher.clients:
        c.loop_stop()
        c.disconnect()
    watcher.clients.clear()


@pytest.mark.skipif(not docker_ready(), reason="docker is not running")
def test_against_a_real_mosquitto(tmp_path):
    name = "cc-mqtt-test-" + uuid.uuid4().hex[:8]
    conf = tmp_path / "mosquitto.conf"
    conf.write_text("listener 1883\nallow_anonymous true\npersistence false\n")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    run = ["docker", "run", "--rm", "-d", "--name", name, "-p", f"127.0.0.1:{port}:1883", "-v", f"{conf}:/mosquitto/config/mosquitto.conf:ro", "eclipse-mosquitto:2"]
    subprocess.run(run, check=True, capture_output=True, timeout=300)
    try:
        assert wait_for(lambda: listening(port), 30), "broker did not start"
        scenario(port, lambda: subprocess.run(["docker", "restart", name], check=True, capture_output=True, timeout=60))
    finally:
        subprocess.run(["docker", "stop", "-t", "1", name], capture_output=True, timeout=60)
