"""Events worth telling a person about, derived from two successive live snapshots.

Pure: `events(prev, now)` returns a list of messages. Delivery (Discord) is a
separate, tiny function so the logic is testable without a network.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, time, timedelta, timezone
import urllib.parse
import urllib.request

import identity
from places import car_at_home

LOG = logging.getLogger("carcommand.notify")
LOW_BATTERY_PCT = 20.0


def _n(x, nd=0):
    return "?" if x is None else f"{x:,.{nd}f}"


def event_kinds(prev: dict | None, now: dict) -> list[tuple[str, str]]:
    """Messages for what changed between `prev` and `now` (both live_vehicle() dicts)."""
    if prev is None:
        return []
    out: list[str] = []
    name = now.get("name", "Car")
    where = now.get("place") or now.get("address") or "an unnamed spot"
    ps, ns = prev.get("status"), now.get("status")
    guard = identity.guard_name(identity.make_of(now.get("vin"), now.get("make")))

    # Someone, or an animal, left inside: before anything else, and again if the car's own level rises.
    inside, was_inside = occupant_level(now), occupant_level(prev)
    if inside and inside > was_inside:
        out.append(("occupant", f"{name} senses someone left inside at {where}{OCCUPANT_WORDS[inside]}"))

    if ps != "Charging" and ns == "Charging":
        # What a source does not carry (a Tesla's feed has no charging power) is left out, not a "?"; nor is
        # a power still ramping up (a Lucid says 0.0 kW for its first reading): it is not what it charges at
        kw = now.get("charging_power_kw")
        said = [f"{_n(now.get('soc'))}% now"] + [
            s for s, x in ((f"limit {_n(now.get('charge_target'))}%", now.get("charge_target")), (f"{_n(kw, 1)} kW", kw if kw is not None and kw >= RAMPING_KW else None)) if x is not None
        ]
        out.append(("charge_start", f"{name} started charging at {where}: {', '.join(said)}."))
    if ps == "Charging" and ns != "Charging":
        out.append(("charge_done", f"{name} finished charging at {where}: {_n(now.get('soc'))}%, {_n(now.get('range_mi'))} mi of range."))
    if ps == "Driving" and ns != "Driving":
        miles = (now.get("odometer_mi") or 0) - (prev.get("odometer_mi") or 0)
        out.append(("parked", f"{name} parked at {where}: {_n(now.get('soc'))}%, {_n(now.get('range_mi'))} mi left" + (f", {_n(miles, 1)} mi since it last woke." if miles > 0.2 else ".")))
    if ps != "Driving" and ns == "Driving":
        out.append(("driving", f"{name} is driving, leaving {prev.get('place') or prev.get('address') or 'an unnamed spot'} at {_n(now.get('soc'))}%."))

    pp, np_ = prev.get("place"), now.get("place")
    if pp != np_ and np_ and ns != "Driving":
        out.append(("arrived", f"{name} arrived at {np_}."))

    soc_prev, soc_now = prev.get("soc"), now.get("soc")
    if soc_prev is not None and soc_now is not None and soc_prev >= LOW_BATTERY_PCT > soc_now:
        out.append(("low_battery", f"{name} is down to {_n(soc_now)}% ({_n(now.get('range_mi'))} mi)."))

    # `is False`, not falsy. These fields are tri-state: None means the car has not said,
    # and a car that has stopped answering is not a car that has been left open. Reading
    # unknown as unlocked is how a logger wakes someone at night about nothing.
    if prev.get("locked") is True and now.get("locked") is False and ns in ("Sleeping", "Idle"):
        out.append(("unlocked", f"{name} is unlocked at {where}."))
    if now.get("doors_open") is True and prev.get("doors_open") is not True and ns in ("Sleeping", "Idle"):
        out.append(("door", f"A door on {name} is open at {where}."))
    # The charge port door stands open for every charge, so open alone says nothing: open, unplugged, and the
    # car locked or asleep is a door left open after the cable came out, once when that becomes true.
    port_left = now.get("port_open") is True and now.get("plugged") is False and (now.get("locked") is True or ns == "Sleeping")
    was_left = prev.get("port_open") is True and prev.get("plugged") is False and (prev.get("locked") is True or ps == "Sleeping")
    if port_left and not was_left:
        out.append(("port", f"The charge port door on {name} is open at {where}, with no cable in."))
    if now.get("windows_open") is True and prev.get("windows_open") is not True and ns in ("Sleeping", "Idle"):
        out.append(("window", f"A window on {name} is open at {where}."))
    # The car's guard mode (Sentry, Halo Secure) reacting to something, once when it starts; the level is not carried, the
    # fact that it is happening is what the message is for.
    if now.get("sentry_threat") is True and prev.get("sentry_threat") is not True:
        out.append(("sentry", f"{guard} on {name} is reacting to something at {where}."))
    # The alarm going off: once when it starts, and once more if a warning becomes the alarm itself.
    alarm, was = now.get("alarm_status"), prev.get("alarm_status")
    if alarm in ALARMS and (was not in ALARMS or (was == "pre-alarm" and alarm != "pre-alarm")):
        out.append(("alarm", f"The alarm on {name} is warning someone off at {where}." if alarm == "pre-alarm" else f"The alarm on {name} is going off at {where}: {ALARMS[alarm]}."))
    # Armed with nothing to record to. Unknown (the car's idle drive state) is not out, and a
    # guard mode that has gone unknown has not been turned off.
    if now.get("sentry") is True and now.get("sentry_usb") is False and not (prev.get("sentry") is not False and prev.get("sentry_usb") is False):
        out.append(("sentry_usb", f"{guard} on {name} is on with no USB drive to record to."))
    if now.get("battery_warning") is True and prev.get("battery_warning") is not True:
        out.append(("battery_warning", f"{name} is flagging a battery health warning."))
    if now.get("update_available") is True and prev.get("update_available") is not True:
        ver, mins = now.get("update_version"), now.get("update_install_min")
        out.append(("update", f"A software update is waiting on {name}" + (f": {ver}" if ver else "") + (f", about {mins} min to install." if mins else ".")))
    if now.get("keyfob_low") is True and prev.get("keyfob_low") is not True:
        out.append(("keyfob", f"A key fob for {name} has a low battery."))
    # The car's own tire warning, once when it comes on. Not a threshold applied here:
    # the proto carries no target pressure, and the car knows its own placard.
    if now.get("tire_warning") is True and prev.get("tire_warning") is not True:
        psi = now.get("tires_psi") or {}
        low = min(psi.values()) if psi else None
        out.append(("tire", f"{name} is warning about a tire" + (f" ({_n(low, 1)} psi at the lowest)." if low is not None else ".")))
    out.extend(("tire", t) for t in tire_limits(prev, now, TIRE_LOW, TIRE_HIGH))
    return out


# The alarm's states that mean it is going off, in the words a message uses.
ALARMS = {"pre-alarm": "a warning", "tilt": "the car was tilted", "shock": "the car was struck", "intrusion": "an intrusion", "panic": "the panic alarm"}
OCCUPANT_WORDS = {1: ".", 2: ": its second warning.", 3: ": its third and highest warning."}


def occupant_level(car: dict) -> int:
    """How strongly the car is warning of someone left inside, 0 to 3: the higher of its living
    object detection (level-1 to -3) and its unattended occupant warning (stage-1 to -3)."""
    levels = [int(w[-1]) for w in (car.get("living_object"), car.get("occupant_warning")) if isinstance(w, str) and w[:-1] in ("level-", "stage-") and w[-1] in "123"]
    return max(levels, default=0)


# Kept from the reading before when the car stops saying (asleep, out of reach), so an alert
# that went once does not go again the next time the car wakes still saying the same thing.
CARRIED = ("alarm_status", "sentry_usb", "living_object", "occupant_warning", "keyfob_low")


def carried(now: dict, prev: dict | None) -> dict:
    """`now`, with each CARRIED value it does not have taken from `prev`."""
    if not prev:
        return now
    return {**now, **{k: prev[k] for k in CARRIED if now.get(k) is None and prev.get(k) is not None}}


# Sent with Pushover's high priority, which sounds through quiet hours.
URGENT = ("occupant",)


TIRE_LOW: float | None = None  # from Settings, applied at startup and on save
TIRE_HIGH: float | None = None
WHEEL = {"front_left": "front left", "front_right": "front right", "rear_left": "rear left", "rear_right": "rear right"}


def tire_limits(prev: dict, now: dict, low: float | None, high: float | None) -> list[str]:
    """Your own pressure limits, on top of the car's warning: one message per tire the
    moment it crosses, none while it stays there, none if no limit is set."""
    if low is None and high is None:
        return []
    before, after = prev.get("tires_psi") or {}, now.get("tires_psi") or {}
    name = now.get("name") or "The car"
    out = []
    for wheel, psi in after.items():
        was = before.get(wheel)
        if low is not None and psi < low and (was is None or was >= low):
            out.append(f"{name}'s {WHEEL.get(wheel, wheel)} tire is at {_n(psi, 1)} psi, below your {_n(low, 1)} psi limit.")
        if high is not None and psi > high and (was is None or was <= high):
            out.append(f"{name}'s {WHEEL.get(wheel, wheel)} tire is at {_n(psi, 1)} psi, above your {_n(high, 1)} psi limit.")
    return out


# The plug-in reminder. Fires once a day, after a set local time, when a car is home,
# parked, below a level, and the car has said it is not plugged in. "Said" matters:
# `plugged` is tri-state, and a car that has not reported a cable is not a car left
# unplugged. That is the same rule as the unlocked alert and for the same reason.
PLUG_REMINDER_AT = time(21, 0)
PLUG_REMINDER_BELOW_PCT = 50.0


KINDS = {
    "charge_start": "Charging started",
    "charge_done": "Charging finished",
    "parked": "Parked",
    "driving": "Set off",
    "arrived": "Arrived at a place",
    "low_battery": "Battery low",
    "unlocked": "Left unlocked",
    "door": "Door open",
    "window": "Window open",
    "port": "Charge port left open",
    "garage_open": "Garage opened or closed for a car",
    "occupant": "Someone left inside",
    "alarm": "Alarm going off",
    "sentry": "Sentry reacting",
    "sentry_usb": "Sentry with no USB drive",
    "battery_warning": "Battery health warning",
    "update": "Software update waiting",
    "keyfob": "Key fob battery low",
    "tire": "Tire warning",
    "connector": "Connector down or back",
    "drive_summary": "Drive summary",
    "daily_summary": "Daily summary",
    "plug_reminder": "Plug-in reminder",
    "login": "Sign-in",
    "data": "Data check",
    "charge_hold": "Charging held for cheaper hours",
    "command": "Car commands",
    "tesla_api": "Tesla API allowance",
}


# Settings › Alerts lists the kinds in these groups, each kind in exactly one (a test holds it).
GROUPS = (
    ("Charging", ("charge_start", "charge_done", "low_battery", "plug_reminder", "charge_hold")),
    ("Driving", ("driving", "arrived", "parked")),
    ("Security", ("occupant", "alarm", "unlocked", "door", "window", "port", "garage_open", "sentry", "sentry_usb")),
    ("Car health", ("battery_warning", "tire", "update", "keyfob")),
    ("Summaries", ("drive_summary", "daily_summary")),
    ("System", ("connector", "data", "login", "command", "tesla_api")),
)

# Kinds that mean nothing without a certain car or link. Settings › Alerts lists a kind only when the
# install has what it needs (hidden_kinds); a hidden kind keeps whatever routing it was saved with.
TESLA_ONLY = ("occupant", "sentry_usb", "keyfob", "tesla_api")


def hidden_kinds(makes, *, commandable: bool, ha: bool, connector: bool) -> set[str]:
    """The alert kinds this install cannot raise: `makes` are the cars' makes, `commandable` is a
    Tesla or a connector car (a car that takes commands), `ha` Home Assistant linked, `connector`
    a car read through the connector."""
    kept = [(m or "").strip().lower() for m in makes]
    tesla = "tesla" in kept
    out = set()
    if not tesla:
        out.update(TESLA_ONLY)
    if not any(m in identity.GUARD_NAMES for m in kept):
        out.add("sentry")
    if not (tesla or commandable):
        out.update(("command", "charge_hold"))
    if not ha:
        out.add("garage_open")
    if not connector:
        out.add("connector")
    return out


def visible_groups(hidden) -> tuple:
    """GROUPS without the hidden kinds, and without a group left empty."""
    return tuple((title, kept) for title, keys in GROUPS if (kept := tuple(k for k in keys if k not in hidden)))


CHANNELS = {"discord": "Discord", "telegram": "Telegram", "email": "E-mail", "pushover": "Pushover", "webhook": "Webhook"}


def kinds_for(makes) -> dict[str, str]:
    """KINDS as Settings shows them: the guard alert under the name of the cars' own mode."""
    guard = identity.guard_label(makes)
    return {**KINDS, "sentry": f"{guard} reacting", "sentry_usb": f"{guard} with no USB drive"}


def routes_from(st: dict) -> dict[str, set[str]]:
    """Which channels each alert kind goes to, from Settings. `alert_routes` is kind -> list
    of channel names; a kind that is missing goes everywhere, one with an empty list is
    off. Installs from before routing had `muted_alerts`, a list of kinds switched off;
    that still reads the same way."""
    routes = st.get("alert_routes")
    if isinstance(routes, dict):
        return {k: {c for c in v if c in CHANNELS} for k, v in routes.items() if k in KINDS and isinstance(v, list)}
    return {k: set() for k in (st.get("muted_alerts") or []) if k in KINDS}


# What a channel added in Settings starts with: charging done, the alerts that mean something is wrong
# or someone is near the car, and the site's own problems. The rest the person opts into.
DEFAULT_KINDS = ("charge_done", "alarm", "occupant", "sentry", "login", "connector", "data", "tesla_api")


def routes_with_channel(routes: dict[str, set[str]], have, new: str) -> dict[str, list[str]]:
    """Every kind's channels once `new` is added beside `have`. A kind with no entry went to every
    channel there was; written out, so the new one does not inherit it, and `new` takes only the
    DEFAULT_KINDS."""
    have = sorted(have)
    return {k: sorted((set(routes[k]) if k in routes else set(have)) | ({new} if k in DEFAULT_KINDS else set())) for k in KINDS}


def settle_routes(st: dict, channels) -> tuple[dict[str, list[str]] | None, list[str]] | None:
    """Routing for a channel that is set up but has never had its alerts chosen (one set in .env): the same short set a channel added
    in Settings starts with. `alert_routed` lists the channels whose routing was ever decided; an install from before it existed has
    decided every channel it has if any routing was saved, so a person's choice (even every alert off) is never touched.
    Returns (routing to save or None to leave it, the routed list to save), or None when nothing needs writing."""
    channels = [c for c in CHANNELS if c in channels]
    kept = st.get("alert_routed")
    routed = list(kept) if isinstance(kept, list) else (list(channels) if isinstance(st.get("alert_routes"), dict) or st.get("muted_alerts") else [])
    fresh = [c for c in channels if c not in routed]
    if not fresh:
        return None if isinstance(kept, list) else (None, routed)  # an install from before: write the seed down, so a later channel is not taken for old
    routes: dict[str, set[str]] = routes_from(st)
    have = [c for c in channels if c in routed]
    out: dict[str, list[str]] = {}
    for new in fresh:
        out = routes_with_channel(routes, have, new)
        routes = {k: set(v) for k, v in out.items()}
        have.append(new)
    return out, sorted(set(routed) | set(channels))


def events(prev: dict | None, now: dict) -> list[str]:
    """The messages alone, for callers that do not filter by kind."""
    return [text for _kind, text in event_kinds(prev, now)]


# What event_kinds compares between two snapshots: all that is kept of a car between ticks and across a restart.
WATCHED = (
    "status",
    "soc",
    "odometer_mi",
    "place",
    "address",
    "locked",
    "doors_open",
    "port_open",
    "plugged",
    "windows_open",
    "sentry_threat",
    "battery_warning",
    "update_available",
    "tire_warning",
    "tires_psi",
    "sentry",
    "alarm_status",
    "sentry_usb",
    "living_object",
    "occupant_warning",
    "keyfob_low",
)
HELD_KINDS = ("low_battery", "unlocked", "door")  # event_kinds' own reading of these is replaced by the Watcher's (_held)
LOW_REARM = 5.0
HOLD_OPEN = timedelta(minutes=10)  # unlocked or a door open this long, not driving, is left so: an arrival is not
REMEMBER_FOR = timedelta(minutes=30)  # a snapshot kept longer than this is no "before" for now
RAMPING_KW = 0.5  # a charge start read below this is still ramping up: its power is left out
HOLD_DONE = timedelta(minutes=5)  # how long "finished charging" waits for the charge's kWh when the car did not say


def fresh_reading(car: dict) -> bool:
    """A snapshot of now: the connector's tree, or a Tesla's feed or stream within their freshness. A car
    read from history alone says nothing new, and comparing it would invent a change."""
    return car.get("live") is True or car.get("source") in ("feed", "stream")


def read_in(at, start: str | None, now: datetime, slack: timedelta = timedelta(minutes=2)) -> bool:
    """Whether a reading taken `at` belongs to the charge the Watcher saw start at `start`: the car's own
    kWh count is held from the charge before until a new one moves it, so a row from before says nothing."""
    if at is None or start is None:
        return False
    if isinstance(at, str):
        at = datetime.fromisoformat(at)
    return datetime.fromisoformat(start) - slack <= at <= now + slack


def charge_done_text(name: str, where: str, start: float | None, end: float | None, target: float | None, kwh: float | None, cost: float | None) -> str:
    """Charge finished: from→to, the kWh and the cost when known. Below its limit it stopped rather than finished."""
    stopped = end is not None and target is not None and end < target - 2
    bits = [f"{_n(start)}→{_n(end)}%" if start is not None else f"{_n(end)}%"]
    if kwh is not None:
        bits.append(f"{_n(kwh, 1)} kWh")
    if cost is not None:
        bits.append(f"${cost:,.2f}")
    if stopped:
        bits.append(f"limit {_n(target)}%")
    return f"{name} {'stopped' if stopped else 'finished'} charging at {where}: {', '.join(bits)}."


class Watcher:
    """Every car's events from its successive snapshots, whichever source the snapshot came from: the
    connector's recorder and the timer for every other car feed the same one, keyed by VIN, so a car with
    two sources raises each event once. The last snapshot is saved (`save(vin, text)`) before the events
    go out, so a restart neither repeats them nor misses a change made while it was down.
    `facts(vin, since)` is a finished charge's {"kwh", "cost"} from the charge facts, or None."""

    def __init__(self, load, save, facts=None, clock=None) -> None:
        self.load, self.save, self.facts = load, save, facts
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.last: dict[str, dict] = {}
        self.lock = threading.Lock()

    def step(self, car: dict) -> list[tuple[str, str]]:
        if not car.get("vin") or not fresh_reading(car):
            return []
        with self.lock:
            return self._step(car)

    def last_driving(self, vin: str) -> datetime | None:
        """The last moment this car was seen driving, or None: for a drive summary, which waits for the whole drive."""
        at = (self.last.get(vin) or {}).get("driving_at")
        return datetime.fromisoformat(at) if at else None

    def _restore(self, vin: str, now: datetime) -> dict | None:
        try:
            kept = json.loads(self.load(vin) or "null")
            at = datetime.fromisoformat(kept["at"])
        except (TypeError, ValueError, KeyError):
            return None
        return kept if now - at <= REMEMBER_FOR else None

    def _step(self, car: dict) -> list[tuple[str, str]]:
        vin, now = car["vin"], self.clock()
        prev = self.last.get(vin) or self._restore(vin, now)
        kept = carried({k: car.get(k) for k in WATCHED}, prev)
        for k in ("charge_from", "charge_at", "done", "driving_at"):
            kept[k] = (prev or {}).get(k)
        if car.get("status") == "Driving":
            kept["driving_at"] = now.isoformat()
        out = []
        found = [(k, t) for k, t in event_kinds(prev, car) if k not in HELD_KINDS] + self._held(prev, car, kept, now)
        if kept["done"] and (any(k == "charge_start" for k, _ in found) or self._done_ready(vin, kept, now)):
            out.append(("charge_done", self._done_text(vin, kept)))
            kept["done"] = None
        for kind, text in found:
            if kind == "charge_start":
                kept["charge_from"], kept["charge_at"] = car.get("soc"), now.isoformat()
                out.append((kind, text))
            elif kind == "charge_done":
                d = {
                    "name": car.get("name") or "Car",
                    "where": car.get("place") or car.get("address") or "an unnamed spot",
                    "end": car.get("soc"),
                    "target": car.get("charge_target"),
                    "kwh": car.get("charge_kwh_added") if read_in(car.get("charge_kwh_at"), kept.get("charge_at"), now) else None,
                    "since": now.isoformat(),
                }
                kept["done"] = d
                if self._done_ready(vin, kept, now):
                    out.append(("charge_done", self._done_text(vin, kept)))
                    kept["done"] = None
            else:
                out.append((kind, text))
        kept["at"] = now.isoformat()
        same = prev is not None and all(prev.get(k) == v for k, v in kept.items() if k != "at")
        if not same:
            self.save(vin, json.dumps(kept))  # before the messages: a duplicate reads as news, a miss does not
        self.last[vin] = kept
        return out

    def _held(self, prev: dict | None, car: dict, kept: dict, now: datetime) -> list[tuple[str, str]]:
        """The alerts that need more than one change to mean something, with what they hold in `kept`."""
        out, p = [], prev or {}
        name = car.get("name") or "Car"
        # Low battery once on the way down, in whole percent: the feed's 20 and the stream's 19.8 take turns, and
        # each turn crossed the line again. It re-arms only once the level is back LOW_REARM points above it.
        armed, soc = p.get("low_armed"), car.get("soc")
        if armed is None and p.get("soc") is not None:
            armed = round(p["soc"]) >= LOW_BATTERY_PCT
        if soc is not None:
            if prev is not None and armed and round(soc) < LOW_BATTERY_PCT:
                out.append(("low_battery", f"{name} is down to {_n(soc)}% ({_n(car.get('range_mi'))} mi)."))
                armed = False
            elif round(soc) >= LOW_BATTERY_PCT + LOW_REARM or (prev is None and round(soc) >= LOW_BATTERY_PCT):
                armed = True
        kept["low_armed"] = armed
        # Left unlocked, a door left open: the state held HOLD_OPEN while not driving. Every arrival opens a
        # door and unlocks the car for a minute; that is not news. Unknown (None) changes nothing.
        where = car.get("place") or car.get("address") or "an unnamed spot"
        for kind, key, bad, text in (("unlocked", "locked", False, f"{name} is unlocked at {where}."), ("door", "doors_open", True, f"A door on {name} is open at {where}.")):
            since, sent = p.get(f"{kind}_since"), p.get(f"{kind}_sent")
            v = car.get(key)
            if v is bad and car.get("status") != "Driving":
                since = since or now.isoformat()
                if not sent and now - datetime.fromisoformat(since) >= HOLD_OPEN:
                    out.append((kind, text))
                    sent = True
            elif v is not None or car.get("status") == "Driving":
                since, sent = None, False
            kept[f"{kind}_since"], kept[f"{kind}_sent"] = since, sent
        return out

    def _done_facts(self, vin: str, kept: dict) -> dict:
        if not self.facts or not kept.get("charge_at"):
            return {}
        try:
            return self.facts(vin, datetime.fromisoformat(kept["charge_at"])) or {}
        except Exception as err:  # noqa: BLE001 - the message goes out without them
            LOG.warning("charge facts: %s", err)
            return {}

    def _done_ready(self, vin: str, kept: dict, now: datetime) -> bool:
        d = kept["done"]
        if d.get("kwh") is not None or now - datetime.fromisoformat(d["since"]) >= HOLD_DONE:
            return True
        found = self._done_facts(vin, kept)
        if found.get("kwh") is not None:
            d["kwh"], d["cost"] = found["kwh"], found.get("cost")
            return True
        return False

    def _done_text(self, vin: str, kept: dict) -> str:
        d = kept["done"]
        if d.get("cost") is None:
            found = self._done_facts(vin, kept)
            d["cost"] = found.get("cost")
            d["kwh"] = d.get("kwh") if d.get("kwh") is not None else found.get("kwh")
        return charge_done_text(d["name"], d["where"], kept.get("charge_from"), d["end"], d["target"], d.get("kwh"), d.get("cost"))


def plug_in_reminder(car: dict, now_local: datetime, last_sent: date | None, at: time = PLUG_REMINDER_AT, below: float = PLUG_REMINDER_BELOW_PCT) -> str | None:
    """The reminder text if one is due now, else None. `last_sent` is the local date it
    last went out for this car, kept in the database so a restart cannot repeat it."""
    if now_local.time() < at or last_sent == now_local.date():
        return None
    if car.get("plugged") is not False:
        return None
    if not car_at_home(car):
        return None
    if car.get("status") not in ("Sleeping", "Idle"):
        return None
    soc = car.get("soc")
    if soc is None or soc >= below:
        return None
    name = car.get("name", "Car")
    return f"{name} is home at {_n(soc)}% and not plugged in."


CONNECTOR_DOWN_AFTER = timedelta(minutes=10)


def connector_down(last_answer: datetime, now: datetime, after: timedelta = CONNECTOR_DOWN_AFTER) -> bool:
    """Whether the connector has been silent long enough to say so: by the clock, not by ticks, which
    come every ten seconds while a car drives."""
    return now - last_answer >= after


def send_telegram(bot_token: str | None, chat_id: str | None, text: str) -> bool:
    """Telegram Bot API sendMessage; the token never appears in logs."""
    if not bot_token or not chat_id:
        return False
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{bot_token}/sendMessage", data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "carcommand/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as err:  # noqa: BLE001
        LOG.warning("telegram send failed: %s", err.__class__.__name__)
        return False


def send_discord(webhook_url: str | None, text: str) -> bool:
    if not webhook_url:
        return False
    body = json.dumps({"content": text[:1900], "username": "Car Command"}).encode()
    req = urllib.request.Request(webhook_url, data=body, headers={"Content-Type": "application/json", "User-Agent": "carcommand/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as err:  # noqa: BLE001
        LOG.warning("discord send failed: %s", err.__class__.__name__)
        return False


def parse_smtp_url(url: str | None) -> dict | None:
    """smtp://user:pass@host:587 (STARTTLS) or smtps://user:pass@host:465 (TLS). None when unset or malformed."""
    if not url:
        return None
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme not in ("smtp", "smtps") or not u.hostname:
        return None
    return {
        "tls": u.scheme == "smtps",
        "host": u.hostname,
        "port": u.port or (465 if u.scheme == "smtps" else 587),
        "user": urllib.parse.unquote(u.username or ""),
        "password": urllib.parse.unquote(u.password or ""),
    }


def send_email(smtp_url: str | None, to: str | None, sender: str | None, subject: str, text: str) -> bool:
    """One plain-text mail per event. The password
    lives in the URL in the environment and never in a log line."""
    conf = parse_smtp_url(smtp_url)
    if conf is None or not to:
        return False
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = subject[:200]
    msg["From"] = sender or conf["user"] or f"carcommand@{conf['host']}"
    msg["To"] = to
    msg.set_content(text[:8000])
    try:
        if conf["tls"]:
            server = smtplib.SMTP_SSL(conf["host"], conf["port"], timeout=15)
        else:
            server = smtplib.SMTP(conf["host"], conf["port"], timeout=15)
            server.starttls()
        with server:
            if conf["user"]:
                server.login(conf["user"], conf["password"])
            server.send_message(msg)
        return True
    except Exception as err:  # noqa: BLE001
        LOG.warning("email send failed: %s", err.__class__.__name__)
        return False


def send_pushover(app_token: str | None, user_key: str | None, text: str, title: str = "Car Command", priority: int = 0) -> bool:
    """Pushover: an app token and a user key. Priority 1 sounds through the user's quiet hours."""
    if not app_token or not user_key:
        return False
    body = urllib.parse.urlencode({"token": app_token, "user": user_key, "title": title[:100], "message": text[:1024], "priority": priority}).encode()
    req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "carcommand/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as err:  # noqa: BLE001
        LOG.warning("pushover send failed: %s", err.__class__.__name__)
        return False


def send_webhook(url: str | None, token: str | None, text: str, kind: str = "other") -> bool:
    """Any receiver that takes JSON: {"source", "kind", "summary"}, with the token as a
    Bearer header when there is one; the shape a home hub's /alert endpoint reads."""
    if not url:
        return False
    body = json.dumps({"source": "carcommand", "kind": kind, "summary": text[:4000]}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "carcommand/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as err:  # noqa: BLE001
        LOG.warning("webhook send failed: %s", err.__class__.__name__)
        return False
