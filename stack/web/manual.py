"""Drives and charges entered by hand.

A charge at a friend's house while the car was out of coverage, a drive the logger
slept through, the month before the connector existed: a log with a hole in it is a log nobody trusts. A hand-entered row goes into the
same tables the loggers write — a charging_sessions row, a trips row — so every page,
total and fact treats it as one of the car's own. What marks it is a row in cc_manual,
which is what lets it be removed again and shows the "by hand" chip on the lists.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

DDL = """
CREATE TABLE IF NOT EXISTS cc_hidden (
    kind   TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    PRIMARY KEY (kind, ref_id)
);
CREATE TABLE IF NOT EXISTS cc_manual (
    kind       TEXT NOT NULL,
    ref_id     INTEGER NOT NULL,
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, ref_id)
)
"""


def parse_local(value: str | None, tz: ZoneInfo) -> datetime | None:
    """A datetime-local field ("2026-09-17T18:30") as an aware datetime in the site's zone."""
    if not value or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


def _num(value, lo: float, hi: float) -> float | None:
    try:
        x = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi else None


def charge_from_form(form: dict, tz: ZoneInfo, now: datetime, place: dict | None) -> dict | str:
    """Columns for a charging_sessions row, or the reason the form is refused."""
    vin = (form.get("vin") or "").strip()
    start, end = parse_local(form.get("start"), tz), parse_local(form.get("end"), tz)
    if not vin:
        return "pick a car"
    if start is None or end is None:
        return "start and end are both needed"
    if end <= start:
        return "the end is before the start"
    if end > now:
        return "the end is in the future"
    kwh = _num(form.get("kwh"), 0.1, 500)
    if kwh is None:
        return "kWh added is needed, between 0.1 and 500"
    level_start, level_end = _num(form.get("level_start"), 0, 100), _num(form.get("level_end"), 0, 100)
    if (level_start is None) != (level_end is None):
        return "give both battery levels or neither"
    if level_start is not None and level_end < level_start:
        return "the battery ended lower than it started"
    cost = _num(form.get("cost"), 0, 10000) if (form.get("cost") or "").strip() else None
    return {
        "vin": vin,
        "session_start_date": start,
        "session_end_date": end,
        "start_level": level_start,
        "end_level": level_end,
        "charging_type": "DC" if (form.get("type") or "").upper() == "DC" else "AC",
        "real_charged": kwh,
        "real_cost": cost,
        "latitude": place["latitude"] if place else None,
        "longitude": place["longitude"] if place else None,
    }


def drive_from_form(form: dict, tz: ZoneInfo, now: datetime, start_place: dict | None, end_place: dict | None, last_odometer: float | None) -> dict | str:
    """Columns for a trips row, or the reason. Distance is entered; the odometers are
    laid on from the car's last known reading so the list's odometer column still reads."""
    vin = (form.get("vin") or "").strip()
    start, end = parse_local(form.get("start"), tz), parse_local(form.get("end"), tz)
    if not vin:
        return "pick a car"
    if start is None or end is None:
        return "start and end are both needed"
    if end <= start:
        return "the end is before the start"
    if end > now:
        return "the end is in the future"
    miles = _num(form.get("miles"), 0.1, 2000)
    if miles is None:
        return "distance is needed, between 0.1 and 2,000"
    level_start, level_end = _num(form.get("level_start"), 0, 100), _num(form.get("level_end"), 0, 100)
    if (level_start is None) != (level_end is None):
        return "give both battery levels or neither"
    base = last_odometer or 0.0
    return {
        "vin": vin,
        "start_date": start,
        "destination_date": end,
        "start_odometer": base,
        "destination_odometer": base + miles,
        "start_latitude": start_place["latitude"] if start_place else None,
        "start_longitude": start_place["longitude"] if start_place else None,
        "end_latitude": end_place["latitude"] if end_place else None,
        "end_longitude": end_place["longitude"] if end_place else None,
        "level_start": level_start,
        "level_end": level_end,
    }
