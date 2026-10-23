"""What the Software page says about each version: the range at 100 % the pack showed while
it ran (and over how many charges), the outside temperature it ran in, how long it stayed;
across them, how often updates come, and a year of them as dots along a line.

Pure. The charges are the battery report's points (lifetime.clean), the temperatures the
drives' own from their facts; nothing here reads the raw history."""

from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta
from statistics import median

# A change in range at 100 % between versions is said quietly when either rests on fewer
# charges than this, or it is under this many miles: the estimate wobbles that much anyway.
QUIET_N = 8
QUIET_MI = 1.0
# An install this soon after the one before is the same update: a car that steps through
# several versions in one sitting (the factory's chain, or a catch-up) was updated once.
SITTING = timedelta(hours=24)


def ran(seen_at: datetime, until: datetime | None, now: datetime, tz) -> dict:
    """How long a version ran: `days` between the local dates the page shows (Jun 25 to Jul 2
    is 7, not the 6 a floor of the hours gives), `secs` to rank by, and `brief` (count, unit)
    for one that ran under a day, which a count of days would call 0."""
    end = until or now
    secs = (end - seen_at).total_seconds()
    brief = None
    if secs < 86400:
        brief = (max(1, round(secs / 60)), "min") if secs < 3600 else (round(secs / 3600), "h")
    return {"days": (end.astimezone(tz).date() - seen_at.astimezone(tz).date()).days, "secs": secs, "brief": brief}


def per_version(rows: list[dict], charges: list[tuple], temps: list[tuple]) -> list[dict]:
    """Each version row (seen_at, until; newest first, as software_rows gives them) with
    `full` (median range at 100 % of the charges while it ran), `full_n`, `full_change`
    (against the version before it, when both have one), and `temp_f` (the mean outside
    temperature at the start of its drives). `charges` are (at, odometer, full) points,
    `temps` (start, °F) pairs, both in any order."""
    pts = sorted((c[0], c[2]) for c in charges)
    when = [p[0] for p in pts]
    tt = sorted(temps)
    tw = [t[0] for t in tt]
    sums = [0.0]
    for _, v in tt:
        sums.append(sums[-1] + v)
    out = []
    for r in rows:
        a, b = r["seen_at"], r.get("until")
        i, j = bisect_left(when, a), (bisect_left(when, b) if b else len(when))
        mine = [p[1] for p in pts[i:j]]
        k, m = bisect_left(tw, a), (bisect_left(tw, b) if b else len(tw))
        out.append(dict(r, full=median(mine) if mine else None, full_n=len(mine), temp_f=(sums[m] - sums[k]) / (m - k) if m > k else None, full_change=None))
    # newest first: each version against the one that ran before it, the next row down
    for newer, older in zip(out, out[1:]):
        if newer["full"] is not None and older["full"] is not None:
            newer["full_change"] = newer["full"] - older["full"]
            # a change resting on a few charges, or inside the estimate's own wobble, is shown quietly
            newer["full_quiet"] = min(newer["full_n"], older["full_n"]) < QUIET_N or abs(newer["full_change"]) < QUIET_MI
    return out


def waiting(car: dict | None) -> dict | None:
    """The update a car says is waiting (connector 0.4.0), from its live reading: version, minutes
    to install, and the maker's notes and their link where the car's model has them (an Air's; a
    Gravity's never come). None when nothing is waiting or the car does not say which version."""
    if not car or car.get("update_available") is not True or not car.get("update_version"):
        return None
    notes = (car.get("update_notes") or "").strip()
    url = car.get("update_notes_url") or ""
    return {
        "version": car["update_version"],
        "minutes": car.get("update_install_min"),
        "notes": [line.strip() for line in notes.splitlines() if line.strip()],
        "url": url if url.startswith("https://") else None,
    }


def cadence(rows: list[dict], now: datetime) -> dict:
    """How often updates come: the mean days between one update and the next, installs in one
    sitting counted once, and the days since the newest. Rows newest first."""
    seen = sorted(r["seen_at"] for r in rows)
    starts = [t for i, t in enumerate(seen) if not i or t - seen[i - 1] >= SITTING]
    every = (starts[-1] - starts[0]).total_seconds() / 86400 / (len(starts) - 1) if len(starts) > 1 else None
    return {"every": every, "since": (now - seen[-1]).days if seen else None}


def timeline(rows: list[dict], now: datetime, tz, days: int = 365) -> dict:
    """The last `days` as a line, a dot on the day each update arrived (named on hover) and
    every other month labelled: the spacing between updates is the thing to see."""
    start = now - timedelta(days=days)
    span = (now - start).total_seconds()
    dots = [
        {"x": round((r["seen_at"] - start).total_seconds() / span * 100, 2), "label": f"{r['version']} · {r['seen_at'].astimezone(tz):%b %-d, %Y}", "current": not r.get("until")}
        for r in rows
        if start <= r["seen_at"] <= now
    ]
    ticks = []
    local = start.astimezone(tz)
    y, m = local.year, local.month
    for _ in range(14):
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
        at = datetime(y, m, 1, tzinfo=tz)
        if at > now:
            break
        if m % 2 == 1:
            ticks.append({"x": round((at - start).total_seconds() / span * 100, 2), "text": f"{at:%b}" if m != 1 else f"{at:%b %Y}"})
    return {"dots": dots, "ticks": ticks, "n": len(dots)}
