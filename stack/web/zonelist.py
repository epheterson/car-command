"""The time zones a picker offers: every IANA zone grouped by region, each labelled with its city and its
UTC offset right now ("Los Angeles (UTC−7)"). Pure: the clock comes from the caller."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

REGIONS = ("Africa", "America", "Antarctica", "Asia", "Atlantic", "Australia", "Europe", "Indian", "Pacific")
MINUS = "−"  # a real minus sign, as the page shows other negative figures


def offset_label(minutes: int) -> str:
    """UTC−7, UTC+5:30, UTC."""
    if minutes == 0:
        return "UTC"
    sign = "+" if minutes > 0 else MINUS
    h, m = divmod(abs(minutes), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def city(name: str) -> str:
    return name.split("/", 1)[1].replace("_", " ").replace("/", " / ")


@lru_cache(maxsize=8)
def _build(hour: int) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    at = datetime.fromtimestamp(hour * 3600, timezone.utc)
    groups: dict[str, list[tuple[str, str]]] = {r: [] for r in REGIONS}
    for name in sorted(available_timezones()):
        region = name.split("/", 1)[0]
        if region not in groups or "/" not in name:
            continue
        off = ZoneInfo(name).utcoffset(at)
        groups[region].append((name, f"{city(name)} ({offset_label(int(off.total_seconds() // 60) if off else 0)})"))
    groups["UTC"] = [("UTC", "UTC")]
    return tuple((r, tuple(v)) for r, v in groups.items() if v)


def grouped(now: datetime | None = None) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """((region, ((iana name, label), ...)), ...): offsets as of `now`, rebuilt at most once an hour."""
    now = now or datetime.now(timezone.utc)
    return _build(int(now.timestamp() // 3600))


def names(now: datetime | None = None) -> set[str]:
    return {n for _, items in grouped(now) for n, _ in items}


def with_current(current: str | None, now: datetime | None = None) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """The groups, plus the zone in force when it is a valid one outside them (an old alias such as US/Pacific), so it stays selectable."""
    groups = grouped(now)
    if not current or current in names(now):
        return groups
    try:
        ZoneInfo(current)
    except Exception:  # noqa: BLE001 - an unknown or malformed name is not offered
        return groups
    return (("Current", ((current, current.replace("_", " ")),)),) + groups
