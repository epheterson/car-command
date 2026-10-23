"""Whether a car can be reached. A car out of cell service is handed back by its maker's cloud with
the last values it sent and the state it was in, so "driving" can read true for an hour after the
car stopped talking. Pure; the route gathers the times."""

from __future__ import annotations

from datetime import datetime, timedelta

# A driving car reports every few seconds; ten quiet minutes is a dead zone, not a long red light.
DRIVING_QUIET = timedelta(minutes=10)
# A parked or charging car is polled far less often, and a sleeping one may say nothing for hours.
PARKED_QUIET = timedelta(hours=3)


def last_seen(car: dict, fix_at: datetime | None) -> datetime | None:
    """The newest real reading of the car. A driving car on the connector repeats the cloud's last
    values, so only a position the recorder saw move counts (`fix_at`); any other car, and any other
    source (a Tesla's feed, its stream, or its history), has its own `updated`."""
    updated = car.get("updated")
    if car.get("status") == "Driving" and car.get("live") is True:
        return fix_at or updated
    return updated


def needs_fix(car: dict) -> bool:
    """Whether `last_seen` wants the newest position: only a driving car on the connector does."""
    return car.get("status") == "Driving" and car.get("live") is True


def seen_if_offline(car: dict, fix_at: datetime | None, now: datetime) -> datetime | None:
    """When the car was last seen, if it is out of reach now; None when it is reachable or there is
    no reading to judge by."""
    seen = last_seen(car, fix_at)
    if seen is None:
        return None
    limit = DRIVING_QUIET if car.get("status") == "Driving" else PARKED_QUIET
    return seen if now - seen > limit else None
