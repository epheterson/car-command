"""The morning message: yesterday, in four lines. Pure text assembly; scheduling lives in the recorder."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta


def _n(x, nd=0):
    return "?" if x is None else f"{x:,.{nd}f}"


def daily_summary_text(name: str, day: date, breakdown: dict | None, drives: list[dict], charges: list[dict], soc_now: float | None, range_now: float | None) -> str:
    lines = [f"{name}, {day:%A %b %-d}:"]
    if drives:
        miles = sum(d.get("miles") or 0 for d in drives)
        mins = sum((d["duration"].total_seconds() / 60) for d in drives if d.get("duration"))
        used = [d for d in drives if d.get("level_start") is not None and d.get("level_end") is not None]
        drop = sum(d["level_start"] - d["level_end"] for d in used)
        lines.append(f"- {len(drives)} drive{'s' if len(drives) != 1 else ''}, {_n(miles, 1)} mi in {_n(mins)} min" + (f", {_n(drop)}% of battery." if used else "."))
    else:
        lines.append("- No drives.")
    if charges:
        kwh = sum(c.get("kwh") or 0 for c in charges)
        cost = sum(c.get("cost") or 0 for c in charges)
        gained = sum(c.get("gained") or 0 for c in charges)
        lines.append(f"- {len(charges)} charge{'s' if len(charges) != 1 else ''}: +{_n(gained)}%, {_n(kwh, 1)} kWh" + (f", ${_n(cost, 2)} at home." if cost else "."))
    else:
        lines.append("- No charging.")
    if breakdown and breakdown.get("known_h", 0) > 0:
        h = breakdown["hours"]
        lines.append(
            f"- Asleep {_n(breakdown['asleep_pct'])}% of the day ({_n(h['sleeping'], 1)} h), idle awake {_n(h['idle'], 1)} h, {breakdown['wakeups']} wake-up{'s' if breakdown['wakeups'] != 1 else ''}."
        )
    lines.append(f"- Now {_n(soc_now)}% and {_n(range_now)} mi.")
    return "\n".join(lines)


def summary_due(now_local: datetime, last_sent: date | None, at: time = time(7, 0)) -> date | None:
    """The day to summarize (yesterday) when it is past `at` and today's summary has not gone out; else None."""
    if now_local.time() < at:
        return None
    today = now_local.date()
    if last_sent == today:
        return None
    return today - timedelta(days=1)


def drive_announcement_due(latest: dict | None, announced: str | None, now: datetime, quiet_after: timedelta = timedelta(minutes=15), min_miles: float = 0.3) -> bool:
    """Whether a finished drive has earned its one message and not had it yet.

    `announced` is the trip id of the last drive this car sent a message about, read from
    the database rather than from memory. It used to be a dict in the process, guarded by
    a "first tick after start" rule that swallowed whatever it found so that a restart
    could not repeat a drive. That bought the opposite mistake: a drive finishing in the
    minutes before a deploy never got announced at all, because the tick that would have
    announced it was the first tick. Remembered across the restart, neither happens — the
    drive is still new and still recent, so it goes out, once.
    """
    if not latest or latest.get("destination_date") is None:
        return False
    if announced == str(latest["id"]):
        return False
    if now - latest["destination_date"] > quiet_after:
        return False
    return (latest.get("miles") or 0) >= min_miles
