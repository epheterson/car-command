"""Hold home charging for cheaper hours.

The rule, per car the owner turns on in Settings › Commands: a charge at home that runs
inside the held hours (by default 06:00 to midnight: home charging waits for midnight) is
stopped once it has run GRACE,
and the car is started again RESUME_AFTER past the end of the hours. Started again by hand
after a stop, a car is left alone until then. Unplugging forgets the stop, so a car that
leaves and comes back is held again. A charge already running when the hours begin (the
night's charge, finishing in the morning) is left to finish. A charge stopped inside the hours some other way (by hand, from the app) is
started at midnight all the same, unless the car is full by then.

GRACE is there because a Tesla on its own schedule charges for three to six minutes after
it is plugged in, then stops itself; holding that would send an alert most evenings.

`step` is pure: a car as /api/now shows it, what the rule remembers about it, the clock and
the car's rule in; what to do and what to remember out. `Worker` runs it once a minute in
the background (never on a page's path), sends the stop or start through the command path,
which logs it, and says what happened through the alert channels.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

import clockfmt

LOG = logging.getLogger("carcommand.hold")

GRACE = timedelta(minutes=10)
RETRY_AFTER = timedelta(minutes=3)  # a stop the car has not taken by then is sent again, once
RESUME_CHECK = timedelta(minutes=10)  # a car told to start that is not charging by then is said
RESUME_AFTER = timedelta(seconds=30)  # the hours are read this late, so the start lands inside the cheaper rate
DEFAULTS = {"from": "06:00", "until": "00:00"}
TICK_S = 60
STATE_PREFIX = "charge_hold:"


def rule_for(rules: dict | None, vin: str) -> dict | None:
    """The car's rule from settings ({vin: {"from", "until"}}), or None when it is off."""
    r = (rules or {}).get(vin)
    if not isinstance(r, dict):
        return None
    return {"from": hhmm(r.get("from"), DEFAULTS["from"]), "until": hhmm(r.get("until"), DEFAULTS["until"])}


def hhmm(text, default: str) -> str:
    return clock(text, default).strftime("%H:%M")


@dataclass
class Memory:
    """What the rule remembers about one car between ticks (kept in cc_state as JSON)."""

    since: str | None = None  # first seen charging inside the hours
    held: str | None = None  # when the rule stopped it
    stopped: bool = False  # the stop was seen to take
    tries: int = 0  # stops sent
    let_go: bool = False  # started again by hand, or the stop never took: hands off until the hours end
    resumed: str | None = None  # when the rule started it again
    night: bool = False  # charging since before the hours began: left to finish

    @classmethod
    def of(cls, d: dict | None) -> "Memory":
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class Step:
    action: str | None = None  # "stop", "start" or None
    say: str | None = None  # an alert to send, if any
    kind: str = "charge_hold"
    memory: Memory = field(default_factory=Memory)


def clock(text: str | None, default: str) -> time:
    try:
        h, m = (int(x) for x in str(text).split(":"))
        return time(h, m)
    except (TypeError, ValueError):  # also an hour or minute out of range
        h, m = (int(x) for x in default.split(":"))
        return time(h, m)


def inside(now: time, start: time, end: time) -> bool:
    """Whether a clock time falls in [start, end), across midnight when end <= start."""
    if start == end:
        return False
    return start <= now < end if start < end else (now >= start or now < end)


def _age(now: datetime, text: str | None) -> timedelta:
    """How long ago a remembered moment was; none remembered is long ago."""
    return now - datetime.fromisoformat(text) if text else timedelta.max


def _hm(t: time) -> str:
    return clockfmt.text(datetime.combine(datetime.min, t), "%-I:%M %p").replace(":00 ", " ")


def step(car: dict, mem: Memory, now: datetime, cfg: dict) -> Step:
    """One tick for one car. `now` is aware local time."""
    start, end = clock(cfg.get("from"), DEFAULTS["from"]), clock(cfg.get("until"), DEFAULTS["until"])
    name = car.get("name") or "The car"
    charging = car.get("status") == "Charging"
    home = car.get("at_home") is True
    plugged = car.get("plugged")
    out = Step(memory=Memory.of(mem.as_dict()))
    m = out.memory

    if plugged is False:
        # Unplugged: whatever was held is over; a car that comes back is a new evening.
        out.memory = Memory()
        return out

    if inside((now - RESUME_AFTER).time(), start, end):
        if m.resumed:
            m.resumed = None  # a new evening: the last resume is history
        if not charging:
            if m.since and not m.held and home:
                m.held, m.stopped = now.isoformat(), True  # stopped inside the hours, by hand or by the car: started at midnight all the same
            m.since, m.night = None, False
            if m.held:
                m.stopped = True
            return out
        if not home or m.let_go or m.night:
            return out
        m.since = m.since or now.isoformat()
        if m.held:
            if m.stopped:
                # Charging again after the stop took: started by hand. Leave it be.
                m.let_go = True
                return out
            if _age(now, m.held) < RETRY_AFTER:
                return out
            if m.tries < 2:
                m.tries += 1
                out.action = "stop"
                return out
            m.let_go = True
            out.kind = "command"
            out.say = f"{name} is still charging at home after two stops. Stop it from the car's app if you don't want it charging now."
            return out
        if _age(now, m.since) < GRACE:
            return out
        m.held, m.tries, m.since = now.isoformat(), 1, None
        out.action = "stop"
        out.say = f"Paused {name} charging at home ({_hm(now.time())}). It starts again at {_hm(end)}. Start it yourself and Car Command leaves it alone until then."
        return out

    # Outside the held hours.
    if m.held and not m.resumed:
        soc, target = car.get("soc"), car.get("charge_target")
        full = soc is not None and target is not None and soc >= target - 1
        if home and not charging and not full and not m.let_go:
            out.action, out.memory = "start", Memory(resumed=now.isoformat())
        else:
            out.memory = Memory(night=charging)  # charging already, gone, full, or let go: nothing to start
        return out
    if m.resumed:
        if charging:
            out.memory = Memory(night=True)
        elif _age(now, m.resumed) >= RESUME_CHECK:
            out.memory = Memory()
            out.kind = "command"
            out.say = f"{name} didn't start charging at {_hm(end)}. Start it from the car's app or it won't be full by morning."
        return out
    if m.since:
        m.since = None
    m.night = charging
    return out


class Worker:
    """The rule for every car that has one, once a minute, in its own thread.

    `rules()` the cars' rules from settings; `enabled()` whether commands are on (a hold
    sends nothing while they are off); `car(vin)` the car as /api/now shows it; `send(vin,
    key)` a command through the command path, (ok, sentence); `notify(text, kind)` the
    alert channels; `get`/`set` cc_state, where each car's memory survives a restart."""

    def __init__(self, rules, enabled, car, send, notify, get, set, now):
        self.rules, self.enabled, self.car, self.send, self.notify = rules, enabled, car, send, notify
        self.get, self.set, self.now = get, set, now
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None
        self.last: dict[str, str] = {}  # vin -> what the rule last did, for Settings

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.run, daemon=True, name="charge-hold")
        self.thread.start()

    def run(self) -> None:
        while not self.stopped.wait(TICK_S):
            try:
                self.tick()
            except Exception as err:  # noqa: BLE001 - the next minute tries again
                LOG.warning("charge hold: %s", type(err).__name__)

    def tick(self) -> None:
        rules = self.rules() or {}
        if not rules or not self.enabled():
            return
        for vin in rules:
            rule = rule_for(rules, vin)
            if rule:
                self.one(vin, rule)

    def one(self, vin: str, rule: dict) -> None:
        try:
            mem = Memory.of(json.loads(self.get(STATE_PREFIX + vin) or "{}"))
        except ValueError:
            mem = Memory()
        car = self.car(vin)
        if not car:
            return
        now = self.now()
        out = step(car, mem, now, rule)
        if out.action:
            ok, said = self.send(vin, "charge_stop" if out.action == "stop" else "charge_start")
            self.last[vin] = f"{'Paused' if out.action == 'stop' else 'Started'} {clockfmt.text(now, '%-I:%M %p')}" if ok else said
            if not ok:
                name = car.get("name") or "The car"
                out.say, out.kind = f"Car Command couldn't {'pause' if out.action == 'stop' else 'start'} {name} charging: {said}", "command"
                if out.action == "start":
                    out.memory = Memory(resumed=now.isoformat())  # checked again in ten minutes, said once
        if out.memory != mem:
            self.set(STATE_PREFIX + vin, json.dumps(out.memory.as_dict()))
        if out.say:
            self.notify(out.say, out.kind)
