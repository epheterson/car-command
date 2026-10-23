"""Closing the garage behind its car (2026-10-20, reworked 2026-10-22): the door is read, the car's
own signals with it, and while the door stands open for the car to go the driveway camera watches
for it going out. The door closes the moment the camera sees a vehicle pull out, never waiting on
the car's own report; whether the car had shown life (unlocked, woke, Ready, Driving) is noted.

What arms the camera. The door opening, briefly (DOOR_WATCH_S); then every sign of life from the car
while the door stands open, each for its own window (SIGNAL_WATCH_S) and each new one extends the
watch: the doors unlocking or opening, the car waking from offline, Ready (ignition on), Driving or
a speed. The watch ends with the door closing or the car seen out.

Why it is shaped so (10-22 08:47 to 09:26): the door opened 08:47 for another car; the Lucid was
unlocked at 09:16, locked 09:18, offline until 09:26, and its first report after was already Driving
(its state from 09:26:21, read 09:26:58, the door closed by hand 09:26:26). Gear and door alone come
about a minute late; the unlock came ten minutes early (SIGNAL_WATCH_S["unlock"] reaches it). Yesterday
the camera saw the car out 146-191 s after the door opened, and the Lucid reported Driving 43 s after
the camera on 10-19 (Ready +112 s, Driving +248 s, camera +205 s): so the car's confirmation trails
the camera by up to a poll (60 s) and a little. CONFIRM is two polls either side of the sighting.

The camera alone cannot tell this car from the other cars of the house, and need not: a vehicle
pulling out with nobody in the drive closes the door whichever car it was (the log says which).

Pure: fed every few seconds, it says what to do; the site does it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

WAKE = timedelta(minutes=3)  # a sign of life this long before the door opened still belongs to this departure
ARRIVING = timedelta(minutes=5)  # the car away from home this recently, when the door opens: it is coming in
CONFIRM = timedelta(seconds=120)  # the car's moving report and the camera's sighting this close agree: two idle polls either side
DOOR_WATCH_S = 600.0  # the door opening alone arms ten minutes; the car's signs of life extend it
MOVING_WATCH_S = 60.0  # a car reporting a speed while the door is open
MIN_WATCH_S = 10.0  # less than this is not worth a request
MAX_WATCH_S = 600.0  # the camera's own ceiling for one watch; the next tick starts another while the wish holds
SIGNAL_WATCH_S = {
    "unlock": 720.0,  # unlocked 09:16:21, left 09:26:21: ten minutes, with the car locked and asleep between
    "doors": 720.0,
    "wake": 300.0,  # Ready to Driving took 136 s on 10-19
    "ignition": 300.0,
    "driving": 180.0,
}
AWAKE = ("Ready", "Driving")
MODES = ("off", "watch", "close")
SIGNALS = ("unlock", "doors", "wake", "ignition", "driving")


@dataclass
class Departure:
    door: str | None = None  # the door's state at the last step
    status: str | None = None  # the car's
    locked: bool | None = None  # last known; None only until it first says
    doors_open: bool | None = None
    moving: bool = False
    opened: datetime | None = None  # when the door was seen to open
    signals: dict[str, datetime] = field(default_factory=dict)  # the first of each sign of life since the door opened (the last, while it is closed)
    until: datetime | None = None  # the watch is wanted until this
    moved: datetime | None = None  # the car last seen moving
    seen: datetime | None = None  # the camera saw a vehicle go
    people: int = 0
    pending: bool = False  # seen, and the car's word on it not in yet
    other: datetime | None = None  # a vehicle left and the car did not
    did: str | None = None  # what was done then
    watching: bool = False
    arriving: bool = False  # the door opened for the car driving in: no departure until it shows life again
    pulling_in: bool = False  # that drive, until the car stops: its Driving is no sign of leaving
    away: datetime | None = None  # the car last seen away from home
    home: bool | None = None

    def sighted(self, now: datetime, people: int = 0) -> None:
        """The camera saw a vehicle go out; the next step decides, on what the car says."""
        self.seen, self.people, self.pending = now, people, True

    def _arm(self, now: datetime, seconds: float) -> None:
        self.until = max(self.until or now, now + timedelta(seconds=seconds))

    def step(
        self,
        door: str | None,
        status: str | None,
        at_home: bool | None,
        now: datetime,
        *,
        locked: bool | None = None,
        doors_open: bool | None = None,
        speed: float | None = None,
    ) -> list[tuple[str, dict]]:
        """One look at the door ("open", "closed", ...; None unread) and the car (its status word,
        whether it is home, the doors' lock and open state, its speed). Events: ("watch", {"seconds"})
        start the camera; ("stop", {}) end a watch the door closing made pointless; ("close", {...})
        a vehicle pulled out (its "car" says whether it was this one); ("departure", {timings}) when
        the door closes on one."""
        out: list[tuple[str, dict]] = []
        if door is None:
            return out  # HA away: nothing is known, nothing changes
        was_closed, is_open = self.door == "closed", door != "closed"
        self.home = at_home
        if at_home is False:
            self.away = now
        if is_open and was_closed:
            # coming home, not going: in the garage, the car leaving is home throughout
            self.arriving = self.pulling_in = bool(self.away and now - self.away < ARRIVING)
            self.signals = {} if self.arriving else {k: t for k, t in self.signals.items() if now - t < WAKE}  # woken just before the door: the same departure
            self.opened, self.seen, self.did, self.pending, self.other, self.until = now, None, None, False, None, None
            if not self.arriving:
                self._arm(now, DOOR_WATCH_S)
        if status not in AWAKE:
            self.pulling_in = False
        moving = status == "Driving" or (speed or 0) > 0
        found = []
        if self.locked is True and locked is False:
            found.append("unlock")
        if self.doors_open is False and doors_open is True:
            found.append("doors")
        if self.status == "Sleeping" and status not in (None, "Sleeping"):
            found.append("wake")
        if status == "Ready" and self.status != "Ready":
            found.append("ignition")
        if moving and not self.moving:
            found.append("driving")
        settled = not self.arriving or (self.opened is not None and now - self.opened >= ARRIVING)
        for sign in found:
            if sign in ("unlock", "doors") and not settled or sign not in ("unlock", "doors") and self.pulling_in:
                continue  # the owner getting out of the car that just came in
            if is_open:
                self.signals.setdefault(sign, now)
                self._arm(now, SIGNAL_WATCH_S[sign])
            else:
                self.signals[sign] = now
        if moving:
            self.moved = now
            if is_open and not self.pulling_in:
                self._arm(now, MOVING_WATCH_S)
        if locked is not None:
            self.locked = locked
        if doors_open is not None:
            self.doors_open = doors_open
        self.moving = moving
        if self.pending and is_open:
            # Closed as the vehicle pulls out, never waiting on the car's report (a poll behind). Any
            # vehicle out with nobody in the drive closes it: the door is not left open behind another
            # car of the house either. Whether this car had shown life or was moving is noted, not waited for.
            mine = bool(self.signals) or bool(self.moved and abs(self.moved - self.seen) <= CONFIRM)
            self.pending, self.until = False, None
            out.append(("close", {"people": self.people, "seen": self.seen, "moved": self.moved, "car": mine}))
        if door == "closed" and self.door not in (None, "closed") and (self.opened or self.seen):
            if self.pending and not self.did:
                self.did = "door closed before the car confirmed"
            if not (self.arriving and not self.seen and not self.signals):  # an arrival's door is the opener's to log
                out.append(("departure", self.timings(now)))
            if self.watching:
                out.append(("stop", {}))
            self.opened, self.signals, self.seen, self.did, self.arriving, self.pulling_in = None, {}, None, None, False, False
            self.until, self.pending, self.other, self.moved = None, False, None, None
        self.door, self.status = door, status
        if not self.watching and (seconds := self.wanted(now)):
            self.watching = True
            out.append(("watch", {"seconds": seconds}))
        return out

    def wanted(self, now: datetime) -> float:
        """Seconds of camera still wanted, 0 for none: the door open, the car home, nothing seen yet
        and some signal's window not run out. A watch that times out asks again, so no gap opens
        between one watch and the next."""
        if self.door in (None, "closed") or self.home is False or self.pending or self.seen or not self.until:
            return 0.0
        seconds = min(MAX_WATCH_S, (self.until - now).total_seconds())
        return seconds if seconds >= MIN_WATCH_S else 0.0

    def timings(self, closed: datetime) -> dict:
        """Seconds from the door opening (when it was seen to) to each thing after: the measure of
        when each sign of life comes and the car goes, and of the camera against the owner's own close."""
        base = self.opened

        def s(t: datetime | None) -> int | None:
            return round((t - base).total_seconds()) if t and base else None

        return {
            "opened": base,
            **{f"{k}_s": s(self.signals.get(k)) for k in SIGNALS},
            "other_s": s(self.other),
            "seen_s": s(self.seen),
            "closed_s": s(closed),
            "did": self.did,
        }
