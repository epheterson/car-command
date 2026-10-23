"""Try every Lucid command once on a parked car, read the car's answer, and put it back.

Run on the host, beside someone at the car:
    docker compose exec -T web python3 - VIN --parked [only ...] < scripts/lucid-command-check.py

Each step sends a command through the connector's REST plugin, waits for the car's own
report to agree (the connector polls a parked car once a minute), then sends the reverse
and waits again. The car is left as it was found. Refuses a car that is moving. Steps can
be picked by name: `... VIN lights max-ac`. Halo at home and at work run only when named
with the value they have now (`sentry-at-home=on`), since the car can't be asked.
"""

import os
import sys
import time

import httpx

REST = os.environ.get("CC_REST_URL", "http://carcommand-cc:40000")
AUTH = (os.environ["REST_USER"], os.environ["REST_PASSWORD"])
WAIT_S = 150

# name: (command path, value to send, state path, state it should read, value that undoes it)
# The undo is None where the step returns the car to how it was by reading it first.
STEPS = {
    "lights": ("commands/lights", "on", "lights/light_state", "on", "off"),
    "windows": ("commands/windows", "vent", "windows/open_state", "open", "close"),
    "trunk": ("doors/commands/trunk", "open", "doors/trunk/open_state", "open", "close"),
    "frunk": ("doors/commands/frunk", "open", "doors/frunk/open_state", "open", "close"),
    "max-ac": ("climatization/commands/max-ac", "on", "max_ac", True, "off"),
    "creature-comfort": ("climatization/commands/creature-comfort", "on", "creature_comfort", True, "off"),
    "steering-wheel": ("climatization/commands/steering-wheel-heater", "low", "steering_wheel_heater", "low", "off"),
    "seat-heat": (
        "climatization/commands/seat-climate",
        "driver heat low",
        "seat_heat_driver",
        "low",
        "driver heat off",
    ),
    "seat-vent": (
        "climatization/commands/seat-climate",
        "front-passenger vent low",
        "seat_vent_front_passenger",
        "low",
        "front-passenger vent off",
    ),
    "preconditioning": ("charging/commands/battery-preconditioning", "on", "battery_preconditioning", True, "off"),
    "sentry": ("commands/sentry", None, "sentry", None, None),
    "enhanced-deterrence": ("commands/enhanced-deterrence", None, "enhanced_deterrence", None, None),
    "alarm": ("commands/alarm", None, "alarm", None, None),
    "charge-current": ("charging/settings/maximum_current", 32, "charging/settings/maximum_current", 32.0, None),
}
# The car reports nothing to read back for these, so they run only when named with the
# value the car has now (`sentry-at-home=on`), and send just that: accepted or not.
SENT_ONLY = {
    "sentry-at-home": "commands/sentry-at-home",
    "sentry-at-work": "commands/sentry-at-work",
}


def tree(vin: str) -> dict:
    return httpx.get(f"{REST}/garage/{vin}", auth=AUTH, timeout=20).json()


def read(vin: str, path: str):
    node = tree(vin)
    for part in path.split("/"):
        node = node.get(part) if isinstance(node, dict) else None
    return node.get("val") if isinstance(node, dict) else None


def send(vin: str, path: str, value) -> str | None:
    method = "POST" if "/commands/" in f"/{path}" else "PUT"  # a setting is an attribute
    r = httpx.request(method, f"{REST}/garage/{vin}/{path}", auth=AUTH, json={"value": value}, timeout=90)
    return None if r.status_code < 300 else f"HTTP {r.status_code}: {r.text[:160]}"


def agree(got, want) -> bool:
    if want == "open":  # a vent reads open or ajar
        return str(got).lower() not in ("closed", "none", "unknown open state")
    if isinstance(want, float) and isinstance(got, (int, float)):
        return round(got) == round(want)
    return str(got).lower() == str(want).lower()


def wait_for(vin: str, path: str, want) -> tuple[bool, object, float]:
    t0 = time.monotonic()
    got = None
    while time.monotonic() - t0 < WAIT_S:
        got = read(vin, path)
        if agree(got, want):
            return True, got, time.monotonic() - t0
        time.sleep(10)
    return False, got, time.monotonic() - t0


def flip(value):
    if value is True:
        return "off", False
    if value is False:
        return "on", True
    if value in ("on", "off", "silent"):  # the alarm: to silent, or from it to on
        return ("on", "on") if value == "silent" else ("silent", "silent")
    return None, None


def run(vin: str, name: str) -> str:
    path, value, state, want, undo = STEPS[name]
    before = read(vin, state)
    if value is None:  # a two-way switch: send the other way first
        value, want = flip(before)
        if value is None:
            return f"skipped: reads {before!r}"
    if undo is None:  # put back exactly what was there
        undo = "on" if before is True else "off" if before is False else before
    err = send(vin, path, value)
    if err:
        return f"FAIL sending {value!r}: {err}"
    ok, got, took = wait_for(vin, state, want)
    there = f"{value!r} -> reads {got!r} in {took:.0f}s" if ok else f"{value!r} accepted, still reads {got!r} after {took:.0f}s"
    err = send(vin, path, undo)
    if err:
        return f"{'ok' if ok else 'FAIL'}: {there}; UNDO FAILED ({undo!r}): {err}"
    # a closure the car never reported should read closed once closed
    back, got, took = wait_for(vin, state, "closed" if before in (None, "unknown open state") else before)
    home = f"back to {got!r} in {took:.0f}s" if back else f"undo accepted, reads {got!r} not {before!r}"
    return f"{'ok' if ok and back else 'FAIL'}: {there}; {home}"


def main() -> None:
    if "--parked" not in sys.argv:
        sys.exit("add --parked once someone at the car has said it is parked: a car that is asleep or out of reach reads speed 0 too")
    args = [a for a in sys.argv[1:] if a != "--parked"]
    vin = args[0]
    named = dict(a.split("=", 1) if "=" in a else (a, "") for a in args[1:])
    only = set(named)
    speed, state = read(vin, "speed"), read(vin, "state")
    if speed not in (None, 0, 0.0) or state == "driving":
        sys.exit(f"the car reads {state}, {speed} km/h; this runs on a parked car only")
    for name in STEPS:
        if not only or name in only:
            print(f"{name:20} {run(vin, name)}", flush=True)
    for name, path in SENT_ONLY.items():
        if named.get(name) in ("on", "off"):
            err = send(vin, path, named[name])
            print(
                f"{name:20} {'FAIL: ' + err if err else 'ok: ' + named[name] + ' accepted (nothing to read back)'}",
                flush=True,
            )


if __name__ == "__main__":
    main()
