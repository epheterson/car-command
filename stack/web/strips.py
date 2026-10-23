"""Bar strips: a small row of bars beside a headline total, one a day of a month, a month of a
year or a week of a year (templates/_strips.html draws them).

Built from totals the page already has, never from readings: a dict of value by day or by
month in, a list of bars out. Each bar carries its value, the words for its tooltip, a link
when the caller gives one, and whether it is the current day, month or week, already placed
in a 0..W by 0..H box so the template only writes rectangles."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

W, H = 300, 40  # the drawing box; the SVG stretches it across its container
GAP = 0.25  # of a bar's slot, left empty between bars


def _plain(v: float) -> str:
    return f"{v:,.0f}"


def strip(bins: list[dict], ticks: list[tuple[int, str]] | None = None, ref: float | None = None) -> dict:
    """Place `bins` ({value, label, href, current, future}) as bars on one scale, the tallest
    reaching the top. A bar with no value, or 0, keeps a sliver on the baseline so the slot
    still reads as a day that happened; one in the future is left empty. `ref` is a value drawn as a
    rule across the strip (a limit) and counted in the scale. `ticks` are
    (bin index, text) for the captions under the strip, as a share of the width."""
    n = max(1, len(bins))
    slot = W / n
    top = max([b["value"] for b in bins if b.get("value")] + ([ref] if ref else []), default=0) or 1
    bars = []
    for i, b in enumerate(bins):
        v = b.get("value") or 0
        h = 0 if b.get("future") else max(1.0, v / top * (H - 1)) if v > 0 else 1.0
        bars.append(dict(b, x=round(i * slot + slot * GAP / 2, 2), w=round(slot * (1 - GAP), 2), y=round(H - h, 2), h=round(h, 2), slot=round(slot, 2), sx=round(i * slot, 2)))
    caps = [{"left": round((i + 0.5) / n * 100, 2), "text": t} for i, t in ticks or ()]
    return {"bars": bars, "w": W, "h": H, "ticks": caps, "total": sum(b.get("value") or 0 for b in bins), "ref": round(H - ref / top * (H - 1), 2) if ref else None}


def month_days(year: int, month: int, values: dict, today: date | None = None, fmt=_plain, href=None) -> dict:
    """A bar a day of the month. `values` by date; `fmt(value)` words it in the tooltip
    ("42 mi"); `href(day)` links a bar, or None."""
    days = calendar.monthrange(year, month)[1]
    bins = []
    for d in range(1, days + 1):
        day = date(year, month, d)
        v = values.get(day)
        bins.append(
            {
                "value": v,
                "label": f"{day:%a %b} {d}: {fmt(v) if v else 'nothing'}",
                "href": href(day) if href else None,
                "current": day == today,
                "future": today is not None and day > today,
            }
        )
    return strip(bins, [(d - 1, str(d)) for d in (1, 8, 15, 22, 29) if d <= days])


def year_months(year: int, values: dict, today: date | None = None, fmt=_plain, href=None) -> dict:
    """A bar a month. `values` keyed by month number (1–12) or by "YYYY-MM"."""
    bins = []
    for m in range(1, 13):
        v = values.get(m, values.get(f"{year}-{m:02d}"))
        bins.append(
            {
                "value": v,
                "label": f"{calendar.month_name[m]} {year}: {fmt(v) if v else 'nothing'}",
                "href": href(year, m) if href else None,
                "current": today is not None and (today.year, today.month) == (year, m),
                "future": today is not None and (year, m) > (today.year, today.month),
            }
        )
    return strip(bins, [(m - 1, calendar.month_abbr[m][0]) for m in range(1, 13)])


def year_weeks(year: int, daily: dict, today: date | None = None, fmt=_plain, href=None, week_start: str | int = "monday") -> dict:
    """A bar a week, every week with a day in the year (53 most years), weeks starting on
    `week_start` ("sunday" or 6, as the calendar's setting holds it, else Monday). `daily` is values by date, summed into their week; days outside the year
    are not counted, so a week that straddles New Year holds only this year's part."""
    first = date(year, 1, 1)
    lead = (first.weekday() - (6 if week_start in ("sunday", 6) else 0)) % 7
    start = first - timedelta(days=lead)
    last = date(year, 12, 31)
    bins, ticks = [], []
    seen_months: set[int] = set()
    while start <= last:
        days = [start + timedelta(days=k) for k in range(7)]
        inside = [d for d in days if d.year == year]
        vals = [daily[d] for d in inside if daily.get(d) is not None]
        v = sum(vals) if vals else None
        shown = max(start, first)
        if shown.month not in seen_months and shown.month in (1, 4, 7, 10):
            ticks.append((len(bins), calendar.month_abbr[shown.month]))
        seen_months.add(shown.month)
        bins.append(
            {
                "value": v,
                "label": f"Week of {shown:%b} {shown.day}: {fmt(v) if v else 'nothing'}",
                "href": href(shown) if href else None,
                "current": today is not None and start <= today <= days[-1],
                "future": today is not None and start > today,
            }
        )
        start += timedelta(days=7)
    return strip(bins, ticks)
