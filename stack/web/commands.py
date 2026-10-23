"""Commands sent to a car, the guards in front of them, and the log of every one.

What a command is (CATALOGUE: its tier, its argument, the makes that take it), whether it
may go now (`refusal`: the owner's switch in Settings › Commands, the car's tier, the
step-up a tier 3 command needs, the way to reach the car), how often (`Limiter`: ten a car
a minute, tier 3 three a car an hour), and `cc_command_log`, one row per command asked for,
sent or refused.

Two ways reach a car, and the brand's own credential never enters this process for either:
a Tesla through Tesla's signing proxy (tesla_cmd), a car on the CarConnectivity connector
(the Lucid among them) through the connector's REST plugin (`cc_request` names the path).

Tiers, from the owner's spec: 1 is on once commands are on; 2 waits until it is enabled for
the car; 3 is enabled per car too, and every command in it asks for the password again (or
a fresh Cloudflare Access sign-in) within the last five minutes. Nothing here moves the car,
updates its software, changes a PIN or a speed limit, or touches its keys.

What Home Assistant's and TeslaFi's controls do for each make is here, bar what NEVER holds:
those either move the car, take a PIN, stop it being driven for half an hour (a software
install) or only make a noise.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

import clockfmt

TIERS = {1: "Everyday", 2: "Car settings", 3: "Access"}


@dataclass(frozen=True)
class Command:
    key: str
    label: str  # the button and the log: what was asked
    done: str  # the result line: what happened
    tier: int = 1
    arg: tuple[float, float] | None = None  # an argument's inclusive range
    makes: tuple[str, ...] | None = None  # None: any car with a way to send it
    step: float = 1  # the argument's step: whole numbers unless said


CATALOGUE = {
    c.key: c
    for c in (
        Command("charge_start", "Start charging", "Charging started"),
        Command("charge_stop", "Stop charging", "Charging stopped"),
        Command("charge_limit", "Charge limit", "Charge limit set", arg=(50, 100)),
        # a Tesla takes up to 48 A and refuses more; a Lucid's connector takes 8 to 80
        Command("charge_amps", "Charging current", "Charging current set", arg=(5, 80), makes=("Tesla", "Lucid")),
        Command("charge_port_open", "Open the charge port", "Charge port opened", makes=("Tesla",)),
        Command("charge_port_close", "Close the charge port", "Charge port closed", makes=("Tesla",)),
        Command("precondition_on", "Battery preconditioning on", "Battery preconditioning on", makes=("Lucid",)),
        Command("precondition_off", "Battery preconditioning off", "Battery preconditioning off", makes=("Lucid",)),
        Command("climate_on", "Climate on", "Climate on"),
        Command("climate_off", "Climate off", "Climate off"),
        Command("climate_temp", "Cabin temperature", "Cabin temperature set", arg=(15, 28), makes=("Tesla",), step=0.5),
        Command("defrost_on", "Defrost on", "Defrost on", makes=("Tesla", "Lucid")),
        Command("defrost_off", "Defrost off", "Defrost off", makes=("Tesla", "Lucid")),
        Command("max_ac_on", "Max A/C on", "Max A/C on", makes=("Lucid",)),
        Command("max_ac_off", "Max A/C off", "Max A/C off", makes=("Lucid",)),
        # climate kept on with the car parked: a Tesla's Keep, Dog and Camp; a Lucid's creature comfort is Dog
        Command("keep_on", "Keep climate on", "Climate kept on", makes=("Tesla",)),
        Command("dog_on", "Dog mode", "Dog mode on", makes=("Tesla", "Lucid")),
        Command("camp_on", "Camp mode", "Camp mode on", makes=("Tesla",)),
        Command("keeper_off", "Keep climate off", "Climate no longer kept", makes=("Tesla", "Lucid")),
        Command("overheat_on", "Cabin overheat protection on", "Cabin overheat protection on", tier=2, makes=("Tesla",)),
        Command("overheat_fan", "Cabin overheat protection, fan only", "Cabin overheat protection on, fan only", tier=2, makes=("Tesla",)),
        Command("overheat_off", "Cabin overheat protection off", "Cabin overheat protection off", tier=2, makes=("Tesla",)),
        Command("bioweapon_on", "Bioweapon defense on", "Bioweapon defense on", makes=("Tesla",)),
        Command("bioweapon_off", "Bioweapon defense off", "Bioweapon defense off", makes=("Tesla",)),
        # seat levels 0 (off) to 3; rear seats heat only
        Command("seat_heat", "Driver's seat heat", "Seat heat set", arg=(0, 3), makes=("Tesla", "Lucid")),
        Command("seat_heat_passenger", "Passenger seat heat", "Seat heat set", arg=(0, 3), makes=("Tesla", "Lucid")),
        Command("seat_heat_rear_left", "Rear left seat heat", "Seat heat set", arg=(0, 3), makes=("Tesla", "Lucid")),
        Command("seat_heat_rear_center", "Rear centre seat heat", "Seat heat set", arg=(0, 3), makes=("Tesla", "Lucid")),
        Command("seat_heat_rear_right", "Rear right seat heat", "Seat heat set", arg=(0, 3), makes=("Tesla", "Lucid")),
        Command("seat_vent", "Driver's seat ventilation", "Seat ventilation set", arg=(0, 3), makes=("Tesla", "Lucid")),
        Command("seat_vent_passenger", "Passenger seat ventilation", "Seat ventilation set", arg=(0, 3), makes=("Tesla", "Lucid")),
        # a Tesla's wheel heater is on or off; a Lucid's has three levels
        Command("wheel_heat_on", "Steering wheel heat on", "Steering wheel heat on", makes=("Tesla",)),
        Command("wheel_heat_off", "Steering wheel heat off", "Steering wheel heat off", makes=("Tesla",)),
        Command("wheel_heat", "Steering wheel heat", "Steering wheel heat set", arg=(0, 3), makes=("Lucid",)),
        Command("lock", "Lock", "Locked"),
        Command("flash", "Flash the lights", "Lights flashed"),
        Command("honk", "Honk", "Honked"),
        Command("wake", "Wake", "Awake"),
        Command("lights_on", "Headlights on", "Headlights on", makes=("Lucid",)),
        Command("lights_off", "Headlights off", "Headlights off", makes=("Lucid",)),
        Command("media_toggle", "Play or pause", "Played or paused", makes=("Tesla",)),
        Command("media_next", "Next track", "Next track", makes=("Tesla",)),
        Command("media_prev", "Previous track", "Previous track", makes=("Tesla",)),
        Command("volume", "Volume", "Volume set", arg=(0, 11), makes=("Tesla",), step=0.5),
        Command("sentry_on", "Sentry on", "Sentry on", tier=2, makes=("Tesla", "Lucid")),
        Command("sentry_off", "Sentry off", "Sentry off", tier=2, makes=("Tesla", "Lucid")),
        Command("sentry_home_on", "Sentry at home on", "Sentry at home on", tier=2, makes=("Lucid",)),
        Command("sentry_home_off", "Sentry at home off", "Sentry at home off", tier=2, makes=("Lucid",)),
        Command("sentry_work_on", "Sentry at work on", "Sentry at work on", tier=2, makes=("Lucid",)),
        Command("sentry_work_off", "Sentry at work off", "Sentry at work off", tier=2, makes=("Lucid",)),
        Command("deterrence_on", "Enhanced deterrence on", "Enhanced deterrence on", tier=2, makes=("Lucid",)),
        Command("deterrence_off", "Enhanced deterrence off", "Enhanced deterrence off", tier=2, makes=("Lucid",)),
        Command("alarm_on", "Alarm on", "Alarm on", tier=2, makes=("Lucid",)),
        Command("alarm_silent", "Alarm silent", "Alarm set to notify only", tier=2, makes=("Lucid",)),
        Command("alarm_off", "Alarm off", "Alarm off", tier=2, makes=("Lucid",)),
        Command("alarm_silence", "Silence the alarm", "Alarm silenced", makes=("Lucid",)),
        # minutes after midnight, local to the car
        Command("sched_charge", "Scheduled charging", "Scheduled charging set", tier=2, arg=(0, 1439), makes=("Tesla",)),
        Command("sched_charge_off", "Scheduled charging off", "Scheduled charging off", tier=2, makes=("Tesla",)),
        Command("unlock", "Unlock", "Unlocked", tier=3),
        Command("trunk", "Open the trunk", "Trunk opened", tier=3, makes=("Tesla", "Lucid")),
        Command("frunk", "Open the frunk", "Frunk opened", tier=3, makes=("Tesla", "Lucid")),
        # a Lucid closes its own; a Tesla's trunk button toggles, and its frunk shuts by hand
        Command("trunk_close", "Close the trunk", "Trunk closed", tier=2, makes=("Lucid",)),
        Command("frunk_close", "Close the frunk", "Frunk closed", tier=2, makes=("Lucid",)),
        Command("windows_vent", "Vent the windows", "Windows vented", tier=3, makes=("Tesla", "Lucid")),
        Command("windows_close", "Close the windows", "Windows closed", tier=3, makes=("Tesla", "Lucid")),
        # the garage door the car has learned, sent with where the car is: the car opens it only near it
        Command("homelink", "HomeLink", "HomeLink sent", tier=3, makes=("Tesla",)),
        Command("remote_start", "Keyless driving", "Keyless driving on for two minutes", tier=3, makes=("Tesla", "Lucid")),
    )
}

# Never in the catalogue, whatever a form or an API call names: a test holds it.
NEVER = ("summon", "autopark", "software_update", "speed_limit", "pin_to_drive", "valet", "add_key", "remove_key", "navigation", "boombox")

PER_MINUTE = 10
TIER3_PER_HOUR = 3
STEP_UP_S = 300  # the password, or an Access sign-in, this recent for a tier 3 command

LEVELS = ("off", "low", "medium", "high")  # a seat or wheel level, 0 to 3, as the connector names it

# CarConnectivity paths under garage/<VIN>/: method, path, value. A None value is the
# command's own argument; a function makes the value from it. The connector answers 404 for
# one it does not offer.
CC_PATHS = {
    "charge_start": ("POST", "charging/commands/start-stop", "start"),
    "charge_stop": ("POST", "charging/commands/start-stop", "stop"),
    "charge_limit": ("PUT", "charging/settings/target_level", None),
    "climate_on": ("POST", "climatization/commands/start-stop", "start"),
    "climate_off": ("POST", "climatization/commands/start-stop", "stop"),
    "lock": ("POST", "doors/commands/lock-unlock", "lock"),
    "unlock": ("POST", "doors/commands/lock-unlock", "unlock"),
    "flash": ("POST", "commands/honk-flash", "flash"),
    "honk": ("POST", "commands/honk-flash", "honk-and-flash"),
    "wake": ("POST", "commands/wake-sleep", "wake"),
    "defrost_on": ("POST", "window_heating/commands/start-stop", "start"),
    "defrost_off": ("POST", "window_heating/commands/start-stop", "stop"),
    # the Lucid connector's own, from 0.2.0
    "trunk": ("POST", "doors/commands/trunk", "open"),
    "trunk_close": ("POST", "doors/commands/trunk", "close"),
    "frunk": ("POST", "doors/commands/frunk", "open"),
    "frunk_close": ("POST", "doors/commands/frunk", "close"),
    # from 0.3.0
    "sentry_on": ("POST", "commands/sentry", "on"),
    "sentry_off": ("POST", "commands/sentry", "off"),
    "sentry_home_on": ("POST", "commands/sentry-at-home", "on"),
    "sentry_home_off": ("POST", "commands/sentry-at-home", "off"),
    "sentry_work_on": ("POST", "commands/sentry-at-work", "on"),
    "sentry_work_off": ("POST", "commands/sentry-at-work", "off"),
    "deterrence_on": ("POST", "commands/enhanced-deterrence", "on"),
    "deterrence_off": ("POST", "commands/enhanced-deterrence", "off"),
    "alarm_on": ("POST", "commands/alarm", "on"),
    "alarm_silent": ("POST", "commands/alarm", "silent"),
    "alarm_off": ("POST", "commands/alarm", "off"),
    "alarm_silence": ("POST", "commands/sentry-alarm", "off"),
    "windows_vent": ("POST", "commands/windows", "vent"),
    "windows_close": ("POST", "commands/windows", "close"),
    "lights_on": ("POST", "commands/lights", "on"),
    "lights_off": ("POST", "commands/lights", "off"),
    "max_ac_on": ("POST", "climatization/commands/max-ac", "on"),
    "max_ac_off": ("POST", "climatization/commands/max-ac", "off"),
    "dog_on": ("POST", "climatization/commands/creature-comfort", "on"),
    "keeper_off": ("POST", "climatization/commands/creature-comfort", "off"),
    "seat_heat": ("POST", "climatization/commands/seat-climate", lambda a: f"driver heat {LEVELS[int(a)]}"),
    "seat_heat_passenger": ("POST", "climatization/commands/seat-climate", lambda a: f"front-passenger heat {LEVELS[int(a)]}"),
    "seat_heat_rear_left": ("POST", "climatization/commands/seat-climate", lambda a: f"rear-left heat {LEVELS[int(a)]}"),
    "seat_heat_rear_center": ("POST", "climatization/commands/seat-climate", lambda a: f"rear-center heat {LEVELS[int(a)]}"),
    "seat_heat_rear_right": ("POST", "climatization/commands/seat-climate", lambda a: f"rear-right heat {LEVELS[int(a)]}"),
    "seat_vent": ("POST", "climatization/commands/seat-climate", lambda a: f"driver vent {LEVELS[int(a)]}"),
    "seat_vent_passenger": ("POST", "climatization/commands/seat-climate", lambda a: f"front-passenger vent {LEVELS[int(a)]}"),
    "wheel_heat": ("POST", "climatization/commands/steering-wheel-heater", lambda a: LEVELS[int(a)]),
    "precondition_on": ("POST", "charging/commands/battery-preconditioning", "on"),
    "precondition_off": ("POST", "charging/commands/battery-preconditioning", "off"),
    "charge_amps": ("PUT", "charging/settings/maximum_current", None),
    # from 0.4.0
    "remote_start": ("POST", "commands/keyless-driving", "on"),
}

DDL = """
CREATE TABLE IF NOT EXISTS cc_command_log (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    vin VARCHAR NOT NULL,
    command VARCHAR NOT NULL,
    arg DOUBLE PRECISION,
    tier SMALLINT NOT NULL DEFAULT 1,
    source VARCHAR NOT NULL,
    ip VARCHAR,
    ok BOOLEAN NOT NULL,
    detail TEXT,
    ms INTEGER
);
CREATE INDEX IF NOT EXISTS cc_command_log_vin_at ON cc_command_log (vin, at DESC);
"""
KEEP_DAYS = 365


class Limiter:
    """At most PER_MINUTE commands a car in any sixty seconds, and TIER3_PER_HOUR tier 3
    ones a car in any hour. Only commands admitted count; one refused here does not."""

    def __init__(self, per_minute: int = PER_MINUTE, tier3_per_hour: int = TIER3_PER_HOUR, clock=time.monotonic):
        self.per_minute, self.tier3_per_hour, self.clock = per_minute, tier3_per_hour, clock
        self.sent: dict[str, deque] = defaultdict(deque)
        self.tier3: dict[str, deque] = defaultdict(deque)

    @staticmethod
    def _trim(q: deque, now: float, window: float) -> None:
        while q and now - q[0] >= window:
            q.popleft()

    def admit(self, vin: str, tier: int = 1) -> str | None:
        """None when admitted (and counted), else the sentence that says why not."""
        now, q, q3 = self.clock(), self.sent[vin], self.tier3[vin]
        self._trim(q, now, 60)
        self._trim(q3, now, 3600)
        if len(q) >= self.per_minute:
            return f"That's {self.per_minute} commands to this car in a minute. Wait a moment."
        if tier >= 3 and len(q3) >= self.tier3_per_hour:
            return f"That's {self.tier3_per_hour} of these to this car in an hour. Try again later."
        q.append(now)
        if tier >= 3:
            q3.append(now)
        return None


def route(make: str | None, live: bool, proxy: bool, demo: bool = False) -> str | None:
    """Which way a command reaches this car: 'tesla' (the signing proxy), 'connector' (the
    CarConnectivity REST plugin), or None. A Tesla goes through the proxy when there is one;
    a car the connector reports goes through the connector."""
    if demo:
        return None
    if make == "Tesla" and proxy:
        return "tesla"
    if live:
        return "connector"
    return None


def no_way(make: str | None) -> str:
    if make == "Tesla":
        return "Tesla commands need the signing proxy. See docs/commands.md."
    return "Car Command has no way to send this car commands."


def parse(key: str, raw) -> tuple[Command, float | None]:
    """The command and its argument, or ValueError with a sentence for the page."""
    cmd = CATALOGUE.get(str(key or ""))
    if cmd is None:
        shown = str(key or "")[:40]
        raise ValueError(f"There is no command called {shown!r}. The names are: {', '.join(CATALOGUE)}." if shown else f"No command was named. The names are: {', '.join(CATALOGUE)}.")
    if cmd.arg is None:
        return cmd, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{cmd.label}: a number, please.") from None
    if value != value:  # NaN
        raise ValueError(f"{cmd.label}: a number, please.")
    lo, hi = cmd.arg
    if not lo <= value <= hi:
        raise ValueError(f"{cmd.label}: between {lo:g} and {hi:g}.")
    value = round(value / cmd.step) * cmd.step
    return cmd, (int(value) if cmd.step == 1 else value)


def not_fitted(make: str | None, ident: dict | None) -> set[str]:
    """The controls a Tesla isn't offered because it lacks what they work: the ventilated seats
    and rear seat heaters as its maker says (None: not said, so offered), bioweapon defense,
    which a Model 3 has no HEPA filter for, and the wheel heater a Model 3 before 2021 lacks. `ident` is identity.describe's. A Lucid's connector
    refuses what its car lacks."""
    if make != "Tesla":
        return set()
    fitted, out = (ident or {}).get("fitted") or {}, set()
    if fitted.get("seat_cooling") is False:
        out |= {"seat_vent", "seat_vent_passenger"}
    if fitted.get("rear_seat_heaters") is False:
        out |= {"seat_heat_rear_left", "seat_heat_rear_center", "seat_heat_rear_right"}
    if (ident or {}).get("model") == "Model 3":
        out |= {"bioweapon_on", "bioweapon_off"}
        # the heated wheel came with the 2021 model year; vehicle_config has no flag for it
        if isinstance((ident or {}).get("year"), int) and ident["year"] < 2021:
            out |= {"wheel_heat_on", "wheel_heat_off"}
    return out


def tier_of(tiers: dict | None, vin: str) -> int:
    """The highest tier the owner enabled for this car (1 unless Settings says more)."""
    try:
        return max(1, min(3, int((tiers or {}).get(vin) or 1)))
    except (TypeError, ValueError):
        return 1


TURN_ON = "Turn them on in Settings › Commands."
NO_ROW = "They need a Tesla account or a connector car: add one in Settings › Cars and sources, then turn them on in Settings › Commands."  # Commands is not in Settings until one exists


def refusal(cmd: Command, make: str | None, way: str | None, *, enabled: bool, signed: bool, allowed_tier: int, stepped_up: bool, offered=None, turn_on: str = TURN_ON) -> str | None:
    """Why this command may not go to this car now, or None. In order: the owner's switch,
    a way to reach the car, sign-in in front of the site, the car's tier, the make, what the
    connector offers, the step-up. `offered` is the connector's set of command keys, None
    when it was not asked."""
    if not enabled:
        return f"Commands are off. {turn_on}"
    if way is None:
        return no_way(make)
    if not signed:
        return "Commands need the site's sign-in or Cloudflare Access in front of it."
    if cmd.tier > allowed_tier:
        return f"{cmd.label} is off for this car. Settings › Commands turns it on."
    if cmd.makes is not None and make not in cmd.makes:
        return f"{cmd.label} isn't something {make or 'this car'} takes from here."
    if way == "connector" and (cmd.key not in CC_PATHS or (offered is not None and cmd.key not in offered)):
        return f"This car's connector doesn't offer {cmd.label.lower()}."
    if cmd.tier >= 3 and not stepped_up:
        return "Enter the password again first."
    return None


def cc_request(cmd: Command, vin: str, arg) -> tuple[str, str, dict]:
    """Method, path and body for the connector's REST plugin."""
    method, path, value = CC_PATHS[cmd.key]
    return method, f"garage/{vin}/{path}", {"value": arg if value is None else value(arg) if callable(value) else value}


def cc_refusal(body) -> str | None:
    """The connector's own reason for not sending a command, when it gives one: the Lucid one,
    from 0.4.0, refuses what the car's config says it is not fitted for ("Not sent: this car's
    frunk has a gas strut and closes by hand"). Any other error text stays out of the page."""
    said = body.get("error") if isinstance(body, dict) else None
    if not isinstance(said, str) or not said.startswith("Not sent: "):
        return None
    said = said.strip()[:200]
    return said if said.endswith(".") else said + "."


def cc_offered(tree: dict | None) -> set[str]:
    """The command keys a car's connector tree offers. A command is in the tree (as
    {"val": null}) only when the connector enabled it; a setting (the charge limit, the AC
    current) is an attribute every car reports, so it counts only beside commands, when the
    connector takes them at all (the connector refuses it plainly if it is read-only). From
    the tree the connector already sent, so nothing is asked of it to find out."""
    out, settings = set(), set()
    for key, (method, path, _value) in CC_PATHS.items():
        node = tree
        for part in path.split("/"):
            node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            continue
        (out if method == "POST" else settings).add(key)
    return out | settings if out else out


def said(cmd: Command, arg, ok: bool, detail: str | None, at: str) -> str:
    """The inline result line: what happened, and when."""
    if not ok:
        return detail or f"{cmd.label} didn't go through."
    text = cmd.done
    if arg is not None:
        unit = {"charge_limit": "%", "charge_amps": " A", "climate_temp": " °C"}.get(cmd.key, "")
        shown = clockfmt.tod(f"{arg // 60}:{arg % 60:02d}") if cmd.key == "sched_charge" else f"{arg:g}{unit}"
        text = f"{text} to {shown}"
    return f"{text} {at}" + (f". {detail}" if detail else "")


def log(conn, vin: str, cmd: str, arg, tier: int, source: str, ip: str | None, ok: bool, detail: str | None, ms: int | None) -> None:
    conn.execute(
        "INSERT INTO cc_command_log (vin, command, arg, tier, source, ip, ok, detail, ms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (vin, cmd[:40], arg, tier, (source or "")[:80], (ip or None) and ip[:64], ok, (detail or "")[:300] or None, ms),
    )


def recent(conn, vin: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT at, command, arg, tier, source, ok, detail FROM cc_command_log WHERE vin = %s ORDER BY at DESC LIMIT %s",
        (vin, limit),
    ).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        cmd = CATALOGUE.get(r["command"])
        r["label"] = cmd.label if cmd else r["command"]
        out.append(r)
    return out


def prune(conn) -> None:
    conn.execute("DELETE FROM cc_command_log WHERE at < now() - make_interval(days => %s)", (KEEP_DAYS,))
