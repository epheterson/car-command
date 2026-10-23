"""What an accepted command just did to a car, shown until the car says so itself.

The car takes a lock in a second, but its next report can be a minute away, so the card
would still say Unlocked after a tap that worked. Each accepted command leaves the state
it set here, and a car's card shows that state until the car's own report agrees, until the
car reports again well after the command (it has spoken; believe it), or for two minutes
at most. Memory only: a restart forgets, and the car's report is back in charge.
"""

from __future__ import annotations

from datetime import datetime, timedelta

TTL = timedelta(minutes=2)
# a report this long after the command is the car's answer to it, whatever it says
SPOKEN = timedelta(seconds=30)


def _sets(key: str, arg, car: dict) -> dict:
    """The card fields one command sets, from the command, its argument and the car now."""
    if key == "trunk" and car.get("make") != "Lucid":  # a Tesla's rear trunk toggles: open closes a powered one
        return {"trunk_open": not car.get("trunk_open")} if car.get("trunk_open") is not None else {}
    return {
        "lock": {"locked": True},
        "unlock": {"locked": False},
        "climate_on": {"climate": "on"},
        "climate_off": {"climate": "off"},
        "sentry_on": {"sentry": True},
        "sentry_off": {"sentry": False},
        "charge_limit": {"charge_target": arg} if arg is not None else {},
        "windows_vent": {"windows_open": True},
        "windows_close": {"windows_open": False},
        "trunk": {"trunk_open": True},
        "frunk": {"frunk_open": True},
        "trunk_close": {"trunk_open": False},
        "frunk_close": {"frunk_open": False},
    }.get(key, {})


def _climate_on(value) -> bool | None:
    text = str(value or "").lower()
    return None if not text or text in ("unknown", "invalid") else text != "off"


def _agrees(field: str, reported, want) -> bool:
    if field == "climate":
        return _climate_on(reported) == _climate_on(want)
    if field == "charge_target" and isinstance(reported, (int, float)):
        return round(reported) == round(want)
    return reported == want


class Expected:
    def __init__(self) -> None:
        self._by_vin: dict[str, dict[str, tuple[object, datetime]]] = {}

    def note(self, vin: str, key: str, arg, car: dict, now: datetime) -> bool:
        """Keep what an accepted command set. True when it set anything a card shows."""
        sets = _sets(key, arg, car)
        if sets:
            self._by_vin.setdefault(vin, {}).update({f: (v, now) for f, v in sets.items()})
        return bool(sets)

    def apply(self, car: dict, now: datetime) -> dict:
        """The car with what its commands set laid over its own report, while that holds."""
        held = self._by_vin.get(car.get("vin") or "")
        if not held:
            return car
        updated = car.get("updated")
        out = dict(car)
        for field, (want, at) in list(held.items()):
            done = now - at > TTL or _agrees(field, car.get(field), want) or (updated is not None and updated - at > SPOKEN)
            if done:
                del held[field]
            else:
                out[field] = want
        if not held:
            self._by_vin.pop(car["vin"], None)
        return out
