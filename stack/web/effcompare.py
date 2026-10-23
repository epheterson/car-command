"""Efficiency compare-with: the Stats page's efficiency by temperature and by speed for a
second set of drives, drawn over the first.

The second set is another period (the one before, or the same dates a year back), a tire
set, or a tag. A tire set or a tag is compared within the page's own period, against the
period's other drives: winter tires against the rest of the year, not against a total that
already contains them. The headline is read at the same temperatures (matched), and the
speed table holds temperature level too (speed_matched): winter tires driven in the cold
would otherwise read as the cold. Pure: drives and the choices in, the paired rows out."""

from __future__ import annotations

from datetime import date, datetime


def choices(tire_names: list[str], tags: list[str], periods: bool) -> list[dict]:
    """What the page can compare with, as {key, label}: the periods when the page has one
    to step back from, each tire set by name, each tag."""
    out = [{"key": "", "label": "Nothing"}]
    if periods:
        out += [{"key": "prev", "label": "The period before"}, {"key": "yoy", "label": "A year earlier"}]
    out += [{"key": f"tires:{n}", "label": f"{n} tires"} for n in tire_names]
    out += [{"key": f"tag:{t}", "label": f"{t} tag"} for t in tags]
    return out


def _local_day(dt: datetime, tz) -> date:
    return dt.astimezone(tz).date()


def on_tires(d: dict, windows: list[tuple], tz) -> bool:
    """Whether a drive started while a set was fitted: (fitted, removed) dates, removed
    inclusive and None while it is still on."""
    day = _local_day(d["start_date"], tz)
    return any(a <= day and (b is None or day <= b) for a, b in windows)


def split(drives: list[dict], key: str, windows: list[tuple], tz) -> tuple[list[dict], list[dict]]:
    """(the rest, the set) for a tire set or tag key; the period's drives either side."""
    if key.startswith("tires:"):
        inside = lambda d: on_tires(d, windows, tz)  # noqa: E731
    else:
        want = key[4:].strip().lower()
        inside = lambda d: (d.get("tag") or "").strip().lower() == want  # noqa: E731
    rest, chosen = [], []
    for d in drives:
        (chosen if inside(d) else rest).append(d)
    return rest, chosen


def overall(drives: list[dict], over: float) -> dict | None:
    """Wh per mile across a set's drives over `over` miles, weighted by distance."""
    kwh = miles = 0.0
    n = 0
    for d in drives:
        mi, k = d.get("miles"), d.get("kwh_used")
        if mi and mi >= over and k:
            kwh, miles, n = kwh + k, miles + mi, n + 1
    return {"wh_per_mi": kwh * 1000 / miles, "drives": n, "miles": miles} if miles > 0 else None


# A side resting on fewer drives than this in a row is shown, but quietly: its figure is a
# handful of trips, not a measurement. A change under STRONG of the first side is not called.
FEW = 10
STRONG = 0.03


def _row(lo, hi, ra, rb, top) -> dict:
    both = ra is not None and rb is not None
    change = (rb["wh_per_mi"] - ra["wh_per_mi"]) / ra["wh_per_mi"] if both else None
    few = not both or ra["drives"] < FEW or rb["drives"] < FEW
    return {
        "lo": lo,
        "hi": hi,
        "a": ra,
        "b": rb,
        "share_a": ra["wh_per_mi"] / top if ra and top else None,
        "share_b": rb["wh_per_mi"] / top if rb and top else None,
        "diff": rb["wh_per_mi"] - ra["wh_per_mi"] if both else None,
        "change": change,
        "few": few,
        "strong": change is not None and not few and abs(change) >= STRONG,
    }


def pair(a: list[dict], b: list[dict]) -> list[dict]:
    """Two sets of efficiency bins (stats.efficiency_bins rows) as one row a bin: each
    side's row or None, each bar from zero against the longest of both sides, the second
    side's difference in Wh/mi and as a share of the first, and whether either side rests
    on too few drives (`few`) or the change is big enough to call (`strong`)."""
    by_a, by_b = {r["lo"]: r for r in a}, {r["lo"]: r for r in b}
    top = max((r["wh_per_mi"] for r in (*a, *b)), default=0)
    return [_row((by_a.get(k) or by_b[k])["lo"], (by_a.get(k) or by_b[k])["hi"], by_a.get(k), by_b.get(k), top) for k in sorted(set(by_a) | set(by_b))]


def matched(rows: list[dict]) -> dict | None:
    """The overall change at the same temperatures: across the temperature rows where both
    sides drove, each side's Wh/mi weighted by the second side's miles in the row, so
    winter tires are read against the other drives at winter's temperatures, not against
    a summer. None when no row has both."""
    both = [r for r in rows if r["a"] and r["b"]]
    w = sum(r["b"]["miles"] for r in both)
    if not both or w <= 0:
        return None
    a = sum(r["b"]["miles"] * r["a"]["wh_per_mi"] for r in both) / w
    b = sum(r["b"]["miles"] * r["b"]["wh_per_mi"] for r in both) / w
    return {"a": a, "b": b, "change": (b - a) / a, "rows": len(both)}


def _cells(drives: list[dict], over: float, temp_bins, speed_bins) -> dict:
    out: dict[tuple, list] = {}
    for d in drives:
        mi, kwh, t, dur = d.get("miles"), d.get("kwh_used"), d.get("temp_f"), d.get("duration")
        if not mi or mi < over or not kwh or t is None or t <= -39.5 or not dur or dur.total_seconds() <= 60:
            continue
        mph = mi / (dur.total_seconds() / 3600)
        ti = next((i for i, (lo, hi) in enumerate(temp_bins) if lo <= t < hi), None)
        si = next((i for i, (lo, hi) in enumerate(speed_bins) if lo <= mph < hi), None)
        if ti is None or si is None:
            continue
        c = out.setdefault((si, ti), [0, 0.0, 0.0])
        c[0], c[1], c[2] = c[0] + 1, c[1] + mi, c[2] + kwh
    return out


def speed_matched(rest: list[dict], chosen: list[dict], over: float, temp_bins, speed_bins) -> list[dict]:
    """Efficiency by speed with temperature held level: within each speed band, only the
    temperature bands both sets drove in (two drives a side or more), the first set's Wh/mi
    in each weighted by the second set's miles there. Rows shaped like pair()'s."""
    ca, cb = _cells(rest, over, temp_bins, speed_bins), _cells(chosen, over, temp_bins, speed_bins)
    raw = []
    for si, (lo, hi) in enumerate(speed_bins):
        keys = [k for k in cb if k[0] == si and k in ca and ca[k][0] >= 2 and cb[k][0] >= 2]
        if not keys:
            continue
        w = sum(cb[k][1] for k in keys)
        a = sum(cb[k][1] * ca[k][2] * 1000 / ca[k][1] for k in keys) / w
        b = sum(cb[k][2] for k in keys) * 1000 / w
        side = lambda c, wh: {"wh_per_mi": wh, "drives": sum(c[k][0] for k in keys), "miles": sum(c[k][1] for k in keys)}  # noqa: E731
        raw.append((lo, hi, side(ca, a), side(cb, b)))
    top = max((max(x[2]["wh_per_mi"], x[3]["wh_per_mi"]) for x in raw), default=0)
    return [_row(lo, hi, ra, rb, top) for lo, hi, ra, rb in raw]
