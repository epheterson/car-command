"""The mileage logbook: one CSV row per finished drive, for a business or tax mileage record.

Rows are oldest first, in the site's time zone and the operator's distance unit. Places are the ones the household named; an unnamed end reads as its town, or street and city when
addresses are asked for, and a street address column is added only then.
"""

from __future__ import annotations

import csv
import io
from datetime import tzinfo

UNTAGGED = "(untagged)"


def select(drives: list[dict], tags: list[str] | None) -> list[dict]:
    """Finished drives, oldest first, narrowed to the given tags (any tag when none are given)."""
    want = {t for t in (tags or []) if t}
    rows = [d for d in drives if d.get("destination_date") and (not want or d.get("tag") in want)]
    return sorted(rows, key=lambda d: d["start_date"])


def _safe(text: str | None) -> str:
    """Text a spreadsheet will not run as a formula: a leading = + - @ gets a quote in front."""
    text = text or ""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _address(road: str | None, city: str | None) -> str:
    return ", ".join(x for x in (road, city) if x)


def _place(d: dict, end: str, addresses: bool) -> str:
    """A place for one end ("start" or "dest"): the named place; else, with addresses on, street and city; else its town (city and region), then its city. Blank only when nothing is known."""
    if d.get("from_named" if end == "start" else "to_named"):
        return d.get("from" if end == "start" else "to") or ""
    pre = "start" if end == "start" else "dest"
    town = d.get(f"{pre}_town") or d.get(f"{pre}_city") or ""
    if not addresses:
        return town
    return _address(d.get(f"{pre}_road"), d.get(f"{pre}_city")) or d.get(f"{pre}_street") or town


def _num(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}"


def header(dist: str, addresses: bool) -> list[str]:
    cols = ["Date", "Start time", "End time", "Car", "Tag", f"Start odometer ({dist})", f"End odometer ({dist})", f"Distance ({dist})", "From", "To"]
    return cols + (["From address", "To address"] if addresses else [])


def row(d: dict, units, tz: tzinfo, addresses: bool) -> list[str]:
    """One drive as its logbook cells. A place nobody named is left blank rather than guessed."""
    start, end = d["start_date"].astimezone(tz), d["destination_date"].astimezone(tz)
    cells = [
        start.strftime("%Y-%m-%d"),
        start.strftime("%H:%M"),
        end.strftime("%H:%M"),
        _safe(d.get("car_name")),
        _safe(d.get("tag")),
        _num(units.distance(d.get("start_odometer"))),
        _num(units.distance(d.get("destination_odometer"))),
        _num(units.distance(d.get("miles"))),
        _safe(_place(d, "start", addresses)),
        _safe(_place(d, "dest", addresses)),
    ]
    if addresses:
        cells += [_safe(_address(d.get("start_road"), d.get("start_city")) or d.get("start_street")), _safe(_address(d.get("dest_road"), d.get("dest_city")) or d.get("dest_street"))]
    return cells


def totals(drives: list[dict], units) -> list[tuple[str, float, int]]:
    """(tag, distance, drives) for each tag in the rows, busiest first, and an All row last when there are two or more tags."""
    by: dict[str, list[float]] = {}
    for d in drives:
        t = by.setdefault(d.get("tag") or UNTAGGED, [0.0, 0])
        t[0] += units.distance(d.get("miles")) or 0.0
        t[1] += 1
    out = [(k, v[0], v[1]) for k, v in sorted(by.items(), key=lambda kv: (-kv[1][0], kv[0]))]
    if len(out) > 1:
        out.append(("All", sum(v[0] for v in by.values()), sum(v[1] for v in by.values())))
    return out


def build(drives: list[dict], units, tz: tzinfo, addresses: bool = False, with_totals: bool = False) -> str:
    """The whole file. Totals follow a blank line as `Total, , , , tag, , , distance` rows, with the drive count in the From column."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    cols = header(units.labels["dist"], addresses)
    w.writerow(cols)
    for d in drives:
        w.writerow(row(d, units, tz, addresses))
    if with_totals and drives:
        w.writerow([])
        for tag, dist, n in totals(drives, units):
            cells = [""] * len(cols)
            cells[0], cells[4], cells[7], cells[8] = "Total", _safe(tag), _num(dist), f"{n} drive{'s' if n != 1 else ''}"
            w.writerow(cells)
    return buf.getvalue()
