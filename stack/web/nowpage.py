"""What the Now page says: a car's state as a word, a sentence for what each car did today,
the battery gauge, and the labels under the 24-hour strip. Pure; the route gathers the data."""

from __future__ import annotations

from datetime import datetime, timedelta

import clockfmt
from notify import ALARMS, occupant_level
from places import car_at_home

# "Ready" is ignition on in Park: the car is on and going nowhere yet, so it reads as idling.
_WORDS = {"Sleeping": "Asleep", "Idle": "Parked", "Ready": "Idling", "Driving": "Driving", "Charging": "Charging"}


# Why the car's guard mode is paused (connector 0.4.0's sentry_inactive_reason), in a tile's words.
_PAUSED = {
    "low-battery": "low battery",
    "updating": "updating",
    "occupied": "someone inside",
    "at-home": "at home",
    "at-work": "at work",
    "usb-full": "USB full",
    "usb-error": "USB error",
    "tow-mode": "tow mode",
    "camera-error": "camera error",
    "unattended-occupant": "someone left inside",
    "creature-comfort": "creature comfort on",
}
# The reasons that mean something is wrong, not a choice the owner made (at home, at work)
_PAUSED_WRONG = ("low-battery", "usb-full", "usb-error", "camera-error")


def paused_word(reason: str | None) -> str | None:
    """A paused guard mode's reason as a tile says it; None when the car gave none."""
    return _PAUSED.get(reason, reason.replace("-", " ")) if reason else None


def alarm_word(status: str | None) -> str | None:
    """The alarm going off, in a few words ("alarm: intrusion"); None while it is not."""
    if status not in ALARMS:
        return None
    return "alarm warning" if status == "pre-alarm" else f"alarm: {status}"


def security_notes(car: dict, guard: str) -> list[str]:
    """What the car says that is out of the ordinary about its safety, most pressing first, for
    the card's line of odd things. Every key is connector 0.4.0's; a car without them has none."""
    out = []
    if occupant_level(car):
        out.append("Someone left inside")
    if alarm := alarm_word(car.get("alarm_status")):
        out.append(alarm[0].upper() + alarm[1:])
    if car.get("sentry") is True:
        if car.get("sentry_usb") is False:
            out.append(f"{guard}: no USB drive")
        elif car.get("sentry_paused") in _PAUSED_WRONG:
            out.append(f"{guard} paused: {paused_word(car['sentry_paused'])}")
    if car.get("keyfob_low") is True:
        out.append("Key fob battery low")
    return out


def state_word(status: str | None) -> tuple[str, str]:
    word = _WORDS.get(status or "")
    if word:
        return word, word.lower()
    return (status or "Unknown"), "unknown"


# TeslaFi's carState in the status words above. A feed row arrives each minute, but the car's
# history is rewritten from the rows once an hour, so the newest row is the car's state now.
_FEED_STATES = {"Driving": "Driving", "Charging": "Charging", "Idling": "Idle", "Sentry": "Idle", "Sleeping": "Sleeping"}
FEED_FRESH = timedelta(minutes=15)
_CHARGE_ENDED = {"Stopped", "Complete", "Disconnected", "NoPower"}


def feed_status(row: dict | None, stored_at: datetime | None, now: datetime) -> str | None:
    """The status from the newest feed row, when it is newer than the history and recent."""
    if not row or not row.get("at") or now - row["at"] > FEED_FRESH:
        return None
    # Strictly older: the position the feed wrote from this very row carries the row's own time,
    # and losing to it left a driving or charging car to the history's stale state for a minute.
    if stored_at and row["at"] < stored_at:
        return None
    status = _FEED_STATES.get(row.get("car_state") or "")
    # TeslaFi's carState lingers on "Charging" a minute after the car's own charging_state says the
    # charge ended; the car's word wins, or a stop is followed by a phantom start and stop.
    if status == "Charging" and row.get("charging_state") in _CHARGE_ENDED:
        return "Idle"
    return status


class _Imperial:
    labels = {"dist": "mi"}

    @staticmethod
    def distance(mi):
        return mi


def today_line(cars: list[dict], today: dict[str, dict], units=None, active: dict[str, str] | None = None) -> list[dict]:
    """`units` is the active units object (units_mod.ACTIVE): stored miles, shown in its distance.
    `active` is vin -> "driving" or "charging" for the cars out now (Now's banners): a drive or a
    charge under way is part of the day, not "stayed put" under its banner."""
    active = active or {}
    units = units or _Imperial()
    out = []
    for c in cars:
        t = today.get(c["vin"]) or {}
        drives, miles, kwh = int(t.get("drives") or 0), float(t.get("miles") or 0.0), float(t.get("kwh") or 0.0)
        parts = []
        if drives:
            parts.append(f"drove {units.distance(miles):.0f} {units.labels['dist']}" + (f" in {drives} drives" if drives > 1 and kwh < 1 else ""))
        if kwh >= 1:
            parts.append(f"charged {kwh:.0f} kWh")
        doing = active.get(c["vin"])
        if doing and not parts:
            # The banner above says what it is doing; the day only says it when nothing else
            # happened yet, rather than "stayed put" under its own banner.
            parts.append(f"is {doing} now")
        if parts:
            text = " and ".join(parts)
        else:
            text = "stayed home" if car_at_home(c) else "stayed put"
        out.append({"vin": c["vin"], "name": c["name"], "text": text, "moved": bool(drives) or kwh >= 1 or bool(doing)})
    return out


def gauge(soc, limit) -> dict:
    fill = 0.0 if soc is None else max(0.0, min(100.0, float(soc)))
    lim = float(limit) if limit is not None and 0 < float(limit) <= 100 else None
    return {"fill": round(fill, 1), "limit": lim}


# The Now page shows the car over one window: the strip and the battery chart share its axis. The
# toggle's choice is kept in a cookie, since the page is drawn on the server.
WINDOWS = {"24h": 24, "7d": 168}
WINDOW_COOKIE = "cc_win"
TICKS = {"24h": {}, "7d": {"days": True}}


def window_of(asked: str | None, kept: str | None) -> str:
    """The window to draw: the one asked for in the address, else the one the browser kept, else a day."""
    return next((w for w in (asked, kept) if w in WINDOWS), "24h")


def strip_ticks(start: datetime, end: datetime, tz, days: bool = False) -> list[dict]:
    """The strip's axis, each {left, label, kind} at its place along the window in percent. Every
    local midnight is a "day" tick named by its weekday ("Thu": the day that begins there), so a
    reader sees where the day changes. A day's view (`days` false) adds "hour" ticks at 6 AM, noon
    and 6 PM (00:00-style on a 24-hour clock); a week's view only the midnights. A last "now" sits
    at the right edge. Hours too near an edge to hold their label are left out."""
    span = (end - start).total_seconds()
    local_start, local_end = start.astimezone(tz), end.astimezone(tz)
    out = []
    day = local_start.date()
    while day <= local_end.date():
        for hour in (0,) if days else (0, 6, 12, 18):
            at = datetime(day.year, day.month, day.day, hour, tzinfo=tz)
            left = (at - start).total_seconds() / span * 100
            if hour == 0 and 0 <= left < 100:
                out.append({"left": round(left, 3), "label": at.strftime("%a") if left < 94 else "", "kind": "day"})
            elif hour and 4 <= left <= 90:
                out.append({"left": round(left, 3), "label": "noon" if hour == 12 and clockfmt.PREFS["clock"] != "24" else clockfmt.hour_label(hour), "kind": "hour"})
        day += timedelta(days=1)
    out.sort(key=lambda t: t["left"])
    out.append({"left": 100.0, "label": "now", "kind": "now"})
    return out


def day_path(drives: list[dict], points: dict, day_start: datetime) -> list[list]:
    """The car's drives since `day_start`, oldest first, each as the [lat, lon] line already
    drawn for its thumbnail: the day's path on the Now map, from rows the page reads anyway."""
    today = sorted((d for d in drives if d["start_date"] >= day_start and points.get(d["id"])), key=lambda d: d["start_date"])
    return [points[d["id"]] for d in today]


def car_sort_key(name: str) -> str:
    """A car's place in every list: its name, its case and punctuation aside (a car named "A.B.C." sorts by A)."""
    return "".join(ch for ch in (name or "").casefold() if ch.isalnum())


def strip_marks(segments: list[dict], drives: list[dict], charges: list[dict]) -> list[dict]:
    """For each stretch of the 24-hour strip, the drive or charge it was, from the lists the
    page already holds: the one overlapping it most. {} when none does (the strip then opens
    the day)."""

    def best(seg, rows, a, b):
        def overlap(r):
            lo, hi = max(seg["start"], r[a]), min(seg["end"], r[b] or seg["end"])
            return (hi - lo).total_seconds()

        hit = max(rows, key=overlap, default=None)
        return hit if hit is not None and overlap(hit) > 0 else None

    out = []
    for s in segments:
        if s["label"] == "driving" and (d := best(s, drives, "start_date", "destination_date")):
            out.append({"drive": d})
        elif s["label"] == "charging" and (c := best(s, charges, "session_start_date", "session_end_date")):
            out.append({"charge": c})
        else:
            out.append({})
    return out
