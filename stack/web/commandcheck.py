"""The command check: each Lucid command sent one at a time from a page, with someone at the car.

A step is sent, the car's own report is read back beside it, the person at the car says whether
it did what it said, and the step is put back. Only commands that neither move anything nor make
a sound: no windows, trunk, frunk, alarm, horn or flash. What the person said is kept per car.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Step:
    key: str
    label: str
    path: str  # under garage/<VIN>/ on the connector's REST plugin
    do: object | None  # the value that tries it; None: the other way from what the car reads
    undo: object | None  # the value that puts it back; None: whatever the car read before
    read: str | None  # where the car reports it; None: it does not, so eyes only
    note: str = ""  # what to look for at the car
    settle: int = 0  # seconds a motion takes: Put back waits this long after Try
    each: tuple[tuple[str, str], ...] = ()  # (short name, path) of parts the car reports one by one, shown beside the whole

    @property
    def method(self) -> str:
        return "POST" if "/commands/" in f"/{self.path}" else "PUT"  # a setting is an attribute


STEPS = (
    Step("lights", "Headlights", "commands/lights", "on", "off", "lights/light_state", "headlights come on; the car may not report them"),
    Step("wheel", "Steering wheel heat", "climatization/commands/steering-wheel-heater", "low", "off", "steering_wheel_heater", "wheel heat icon on the screen"),
    Step("seat_heat", "Driver seat heat", "climatization/commands/seat-climate", "driver heat low", "driver heat off", "seat_heat_driver", "driver seat heat, low"),
    Step("seat_vent", "Passenger seat vent", "climatization/commands/seat-climate", "front-passenger vent low", "front-passenger vent off", "seat_vent_front_passenger", "passenger seat fan, low"),
    Step("max_ac", "Max A/C", "climatization/commands/max-ac", "on", "off", "max_ac", "fans run"),
    Step("comfort", "Creature Comfort", "climatization/commands/creature-comfort", "on", "off", "creature_comfort", "fans run, the screen says so"),
    Step("precondition", "Battery preconditioning", "charging/commands/battery-preconditioning", "on", "off", "battery_preconditioning", "the screen shows it; pumps may hum"),
    Step("halo", "Halo Secure", "commands/sentry", None, None, "sentry", "the other way, then back"),
    Step("deterrence", "Enhanced deterrence", "commands/enhanced-deterrence", None, None, "enhanced_deterrence", "the other way, then back"),
    Step("flash", "Flash the lights", "commands/honk-flash", "flash", "", None, "lights flash once; no horn; nothing to put back"),
    Step(
        "windows",
        "Vent the windows",
        "commands/windows",
        "vent",
        "close",
        "windows/open_state",
        "all windows drop a little; Put back closes them",
        settle=20,
        each=(("FL", "windows/front_left/open_state"), ("FR", "windows/front_right/open_state"), ("RL", "windows/rear_left/open_state"), ("RR", "windows/rear_right/open_state")),
    ),
    Step("charge_stop", "Stop charging", "charging/commands/start-stop", "stop", "start", "charging/state", "charging stops; Put back starts it again"),
    Step("charge_limit", "Charge limit", "charging/settings/target_level", None, None, "charging/settings/target_level", "the limit moves 5 points down, then back"),
    Step("climate", "Climate on", "climatization/commands/start-stop", "start", "stop", "climatization/state", "cabin climate runs; fans"),
    Step("defrost", "Defrost", "window_heating/commands/start-stop", "start", "stop", "window_heating/heating_state", "defrost and rear window heat; fans"),
    Step("wake", "Wake", "commands/wake-sleep", "wake", "", "state", "the car wakes; nothing to put back"),
    Step("keyless", "Keyless driving", "commands/keyless-driving", "on", "off", "keyless_driving", "the car drives with no key in it; Put back turns that off"),
    Step("amps", "Charging current", "charging/settings/maximum_current", 32, None, "charging/settings/maximum_current", "32 A in the car's charging screen"),
)
BY_KEY = {s.key: s for s in STEPS}
LEFT_OUT = "Trunk, frunk, alarm and horn."
VERDICTS = ("worked", "failed", "skipped")


def read(tree: dict | None, path: str | None):
    node = tree
    for part in (path or "").split("/") if path else ():
        node = node.get(part) if isinstance(node, dict) else None
    return node.get("val") if isinstance(node, dict) else None


def readings(tree: dict | None) -> dict:
    """Each step's value as the car reports it now, by step key; also speed and state."""
    out = {s.key: read(tree, s.read) for s in STEPS}
    for s in STEPS:
        if s.each:  # "closed · FL closed, FR unknown…": the whole can say closed while a part cannot say
            out[s.key] = f"{out[s.key]} · " + ", ".join(f"{name} {v if (v := read(tree, path)) is not None else '—'}" for name, path in s.each)
    out["_state"], out["_speed"] = read(tree, "state"), read(tree, "speed")
    return out


def moving(tree: dict | None) -> bool:
    speed, state = read(tree, "speed"), read(tree, "state")
    return state == "driving" or speed not in (None, 0, 0.0)


def back_value(step: Step, before) -> object:
    """What undo sends: the step's own, or what the car read before it was tried."""
    if step.undo is not None:
        return step.undo
    if before is True:
        return "on"
    if before is False:
        return "off"
    if isinstance(before, float):  # the car reports 90.00000134; the connector takes whole numbers
        return round(before)
    return before


def value_for(step: Step, which: str, before) -> object:
    if which == "do":
        if step.do is not None:
            return step.do
        if before is True:
            return "off"
        if before is False:
            return "on"
        if isinstance(before, (int, float)):  # a level: five points down, never under 50
            return max(50, round(before) - 5)
        raise ValueError("the car has not said which way it is")
    if which == "undo":
        if step.undo == "":
            raise ValueError("nothing to put back")
        return back_value(step, before)
    raise ValueError(which)
