"""What the Lucid connector publishes from 0.5.0 that an owner can use, read from the connector's
tree the way app.reported_vehicle reads every other leaf. A connector before 0.5.0 publishes none of
these, so every key here is None (or an empty list) for it and nothing renders. Pure: the tree in,
plain values out; the pages and alerts decide what to say.

The raw-unit leaves (`*_raw`: a figure whose unit the connector could not settle) are never read."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from units import km_to_mi

SENTRY_SHOWN = 30  # the newest events on the Security pane; the connector reads a window of them
LEVELS = {1: "Walk-by", 2: "Tampering", 3: "Breach"}  # Sentry's threat levels, as the connector counts them
# A plan whose name says it only discounts a charge is not free charging; any other plan (the one that
# comes with a car) is. Checked on the name because the connector publishes nothing else about the plan.
FREE = ("complimentary", "unlimited", "free")  # only a plan that says so is free: "Lucid Charging Plan" is retail rate
REMIND_DAYS = (30, 7)  # the days before an Electrify America plan ends that an alert is sent
SLOW_TITLES = {"derated": "The car is limiting its charging power", "preconditioning-saves-time": "The pack is preconditioning, which saves time"}


def _typed(value, kind: type):
    return value if isinstance(value, kind) and not (kind is float and isinstance(value, bool)) else None


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _when(value) -> datetime | None:
    """A UTC-aware time from the ISO text a date leaf carries; None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _rows(text) -> list[dict]:
    """The JSON list a text leaf holds; [] for none, bad JSON or anything but a list of objects."""
    if not isinstance(text, str) or not text:
        return []
    try:
        got = json.loads(text)
    except ValueError:
        return []
    return [r for r in got if isinstance(r, dict)] if isinstance(got, list) else []


def sentry_events(text) -> list[dict]:
    """Sentry's event log, newest first, as metadata only: when, the threat level (1 to 3, with its word),
    what kind, and how many pictures the car holds. The connector never sends a picture's address and
    nothing here asks for one."""
    out = []
    for r in _rows(text):
        at = _when(r.get("at"))
        if at is None:
            continue
        level = r.get("level") if isinstance(r.get("level"), int) and not isinstance(r.get("level"), bool) else None
        out.append({"at": at, "level": level, "level_word": LEVELS.get(level), "type": _typed(r.get("type"), str), "pictures": r["pictures"] if isinstance(r.get("pictures"), int) else 0})
    out.sort(key=lambda e: e["at"], reverse=True)
    return out[:SENTRY_SHOWN]


def charge_records(text) -> list[dict]:
    """Lucid's own charge records, oldest first, with a start and an id; energy is a number or None.
    The connector gives the energy no unit; it is read as kWh and checked against our own meter where they meet."""
    out = []
    for r in _rows(text):
        start = _when(r.get("start"))
        if start is None or not isinstance(r.get("id"), str):
            continue
        out.append({"id": r["id"], "start": start, "stop": _when(r.get("stop")), "kwh": _num(r.get("energy")), "charger": _typed(r.get("charger"), str)})
    out.sort(key=lambda r: r["start"])
    return out


def plan_is_free(name) -> bool:
    """Whether an Electrify America plan of this name means no charge at their chargers."""
    return bool(name) and any(w in name.lower() for w in FREE)


def plausible_kwh(record, integrated: float) -> bool:
    """Whether a Lucid charge record's energy can be read as kWh: positive, and (when the power profile has an
    estimate) between half and twice of it. The record gives the figure no unit; a wrong guess is not used."""
    if not isinstance(record, (int, float)) or isinstance(record, bool) or record <= 0:
        return False
    return not integrated or 0.5 <= record / integrated <= 2.0


def tire_stamp(read_at: datetime | None, now: datetime) -> datetime | None:
    """The time to record a tire reading at: the car's own read time, never later than now. None when the
    car does not say, which is the connector before 0.5.0: the caller records it at `now`, as it always did."""
    return None if read_at is None else min(read_at, now)


def days_until(day: date | None, today: date) -> int | None:
    return (day - today).days if day else None


def read(v: dict, leaf, today: date | None = None) -> dict:
    """Every 0.5.0 leaf a car's tree holds, under the names the car dict uses. `leaf` is app.leaf."""
    today = today or date.today()
    plan_ends = _when(leaf(v, "ea_plan_expires"))
    plan_name = _typed(leaf(v, "ea_plan_name"), str)
    held_back = _num(leaf(v, "battery_range_unavailable"))
    status = _typed(leaf(v, "charge_status"), str)
    return {
        "range_held_back_mi": km_to_mi(held_back) if held_back else None,
        "charge_scheduled": _typed(leaf(v, "scheduled_charge"), str),
        "charge_status": status,
        "charge_limit_amps": _num(leaf(v, "charge_session_current_limit")),
        "alarm_sounding": _typed(leaf(v, "sentry_alarm_sounding"), bool),
        "sentry_events": sentry_events(leaf(v, "sentry_events")),
        "tire_defective": _typed(leaf(v, "tire_sensor_defective"), bool),
        "tires_read_at": _when(leaf(v, "tire_pressure_updated")),
        "gear": _typed(leaf(v, "gear"), str),
        "drive_mode": _typed(leaf(v, "drive_mode"), str),
        "update_state": _typed(leaf(v, "software_update_state"), str),
        "update_percent": _num(leaf(v, "software_update_percent")),
        "trim": _typed(leaf(v, "trim"), str),
        "paint": _typed(leaf(v, "paint_color"), str),
        "ea_plan": plan_name,
        "ea_plan_ends": plan_ends.date() if plan_ends else None,
        "ea_days_left": days_until(plan_ends.date() if plan_ends else None, today),
        "privacy_mode": _typed(leaf(v, "privacy_mode"), bool),
        "mpb_fault": _typed(leaf(v, "mpb_fault"), str),
    }


def records_of(v: dict, leaf) -> list[dict]:
    """The charge records on a car's tree: for the background worker, never a page."""
    return charge_records(leaf(v, "charging_records"))


def slow_reason(car: dict) -> str | None:
    """Why a charge is slow, or why a cable in is not charging, from what the car and the charger say.
    The charger's own limit comes first: it is the wall unit, not the car, and the owner can change it."""
    amps, set_amps = car.get("charge_limit_amps"), car.get("charge_amps_set")
    if car.get("status") == "Charging":
        if amps and set_amps and amps < set_amps - 0.5:
            return f"charger limits to {amps:.0f} A"
        if car.get("charge_status") == "derated":
            return "power reduced by the car"
        if car.get("charge_status") == "preconditioning-saves-time":
            return "preconditioning the pack"
    elif car.get("plugged") is True and car.get("charge_scheduled") == "scheduled":
        return "waiting for its schedule"
    return None


def update_line(car: dict) -> str | None:
    """The software update under way or just failed, as one phrase."""
    state, pct = car.get("update_state"), car.get("update_percent")
    if state == "installing":
        return f"Installing {pct:.0f}%" if pct is not None else "Installing an update"
    if state in ("failed", "failed-drivable"):
        return "Software update failed"
    return None


def notes(car: dict) -> list[str]:
    """What the car says that is out of the ordinary and is not about its safety (nowpage.security_notes
    has those), most pressing first, for the card's line of odd things."""
    out = []
    if car.get("alarm_sounding") is True:
        out.append("Alarm sounding")
    if car.get("mpb_fault") in ("warning", "critical", "hardware-fault"):
        out.append(f"Car fault: {car['mpb_fault'].replace('-', ' ')}")
    if line := update_line(car):
        out.append(line)
    if car.get("tire_defective") is True:
        out.append("Tire sensor fault")
    if car.get("privacy_mode") is True:
        out.append("Privacy mode on")
    return out
