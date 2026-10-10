"""What the Now page says: a car's state as a word, a sentence for what each car did today,
the battery gauge, and the labels under the 24-hour strip. Pure; the route gathers the data."""

from __future__ import annotations

from datetime import datetime, timedelta

from places import car_at_home

# "Ready" is ignition on in Park: the car is on and going nowhere yet, so it reads as idling.
_WORDS = {"Sleeping": "Asleep", "Idle": "Parked", "Ready": "Idling", "Driving": "Driving", "Charging": "Charging"}


def state_word(status: str | None) -> tuple[str, str]:
    word = _WORDS.get(status or "")
    if word:
        return word, word.lower()
    return (status or "Unknown"), "unknown"


# TeslaFi's carState in the status words above. A feed row arrives each minute, but the car's
# history is rewritten from the rows once an hour, so the newest row is the car's state now.
_FEED_STATES = {"Driving": "Driving", "Charging": "Charging", "Idling": "Idle", "Sentry": "Idle", "Sleeping": "Sleeping"}
FEED_FRESH = timedelta(minutes=15)


def feed_status(row: dict | None, stored_at: datetime | None, now: datetime) -> str | None:
    """The status from the newest feed row, when it is newer than the history and recent."""
    if not row or not row.get("at") or now - row["at"] > FEED_FRESH:
        return None
    if stored_at and row["at"] <= stored_at:
        return None
    return _FEED_STATES.get(row.get("car_state") or "")


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
        if doing:
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


def strip_ticks(start: datetime, end: datetime, tz, every_h: int = 6) -> list[dict]:
    """Labels on whole local hours, every six, placed where those hours fall, then "now"
    at the right edge. A label at start+6h would read "4 AM" at 4:25."""
    span = (end - start).total_seconds()
    local = start.astimezone(tz)
    first = local.replace(minute=0, second=0, microsecond=0)
    if first < local:
        first += timedelta(hours=1)
    out, at = [], first
    while (end - at).total_seconds() > 3600 * 1.5:
        out.append({"left": round((at - start).total_seconds() / span * 100, 3), "label": at.strftime("%-I %p")})
        at += timedelta(hours=every_h)
    out.append({"left": 100.0, "label": "now"})
    return out
