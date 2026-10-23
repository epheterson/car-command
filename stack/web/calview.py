"""The Calendar's figures beyond the grid: the bar strips beside a month's or a year's totals,
the month's battery line with its drives and charges shaded and its average drawn across,
and the daily tire pressures the Battery page draws over the whole history.

Pure: rows and series in, what the templates and chart.js draw out. The strips are built
from the day and month totals the page has already added up, the battery line from the
month's level runs (the same rows every battery chart reads), and the tire days from runs
grouped a day at a time by the database."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import chartdata
from strips import month_days, year_months, year_weeks

# What a strip can count, in the order the switch offers them: key, switch label, unit kind
MEASURES = (("dist", "Driven"), ("kwh", "Charged"), ("cost", "Cost"))


def _formats(u, cur: str) -> dict:
    """Each measure as words for a bar's tooltip and the headline, in the active units."""
    d = u.labels["dist"]
    return {
        "dist": lambda v: f"{v:,.0f} {d}" if v >= 10 else f"{v:,.1f} {d}",
        "kwh": lambda v: f"{v:,.0f} kWh" if v >= 10 else f"{v:,.1f} kWh",
        "cost": lambda v: f"{cur}{v:,.2f}",
    }


def day_values(cells: list[dict], day_rows: dict, u) -> dict[str, dict]:
    """Distance, kWh charged and charging cost by date, from the grid's cells (miles, kWh)
    and summarize()'s day rows (cost). A day with none of a measure is left out."""
    out: dict[str, dict] = {k: {} for k, _ in MEASURES}
    for c in cells:
        if c.get("other"):
            continue
        if c["miles"]:
            out["dist"][c["date"]] = u.distance(c["miles"])
        if c["kwh"]:
            out["kwh"][c["date"]] = c["kwh"]
        cost = (day_rows.get(c["date"]) or {}).get("cost")
        if cost:
            out["cost"][c["date"]] = cost
    return out


def month_strips(year: int, month: int, values: dict[str, dict], today: date, u, cur: str, href=None) -> list[dict]:
    """A strip a day for each measure, with its total in words: [{key, label, total, strip}]."""
    fmt = _formats(u, cur)
    out = []
    for key, label in MEASURES:
        s = month_days(year, month, values[key], today, fmt[key], href)
        out.append({"key": key, "label": label, "total": fmt[key](s["total"]) if s["total"] else "—", "strip": s})
    return out


def year_strips(year: int, values: dict[str, dict], today: date, u, cur: str, month_href=None, week_href=None, week_start: str = "monday") -> list[dict]:
    """Twelve months and every week of the year for each measure: [{key, label, total, months, weeks}].
    The months are the days summed, so the two strips and the total always agree."""
    fmt = _formats(u, cur)
    out = []
    for key, label in MEASURES:
        by_month: dict[int, float] = {}
        for d, v in values[key].items():
            if d.year == year:
                by_month[d.month] = by_month.get(d.month, 0.0) + v
        ms = year_months(year, by_month, today, fmt[key], month_href)
        ws = year_weeks(year, values[key], today, fmt[key], week_href, week_start)
        out.append({"key": key, "label": label, "total": fmt[key](ms["total"]) if ms["total"] else "—", "months": ms, "weeks": ws})
    return out


def time_average(series: list[tuple[datetime, float]], t0: datetime, t1: datetime) -> float | None:
    """The level a step series held on average over [t0, t1): each value weighted by how long
    it lasted, a gap between readings holding the one before it. None with no reading inside."""
    pts = sorted(((t, v) for t, v in series if v is not None), key=lambda p: p[0])  # stable: a run that ends where the next begins keeps its order
    total = weight = 0.0
    for i, (t, v) in enumerate(pts):
        a = max(t, t0)
        b = min(pts[i + 1][0] if i + 1 < len(pts) else t1, t1)
        if b > a:
            s = (b - a).total_seconds()
            total += v * s
            weight += s
    return total / weight if weight else None


def round_levels(vals: list[float]) -> tuple[int, int]:
    """A battery axis whose three labels are all round tens (60 / 70 / 80): its ends on tens,
    an even number of tens apart, at least 20 points tall, inside 0-100."""
    lo = max(0, int((min(vals) - 1) // 10) * 10)
    hi = min(100, -int(-(max(vals) + 1) // 10) * 10)
    if hi - lo < 20:
        hi = min(100, lo + 20)
        lo = hi - 20
    if (hi - lo) % 20:
        if hi < 100:
            hi += 10
        else:
            lo -= 10
    return lo, hi


def month_chart(levels: list[tuple[datetime, float]], drives: list[dict], charges: list[dict], t0: datetime, t1: datetime, now: datetime, tz, u) -> dict | None:
    """The month's battery line: the level as steps, every drive and charge a shaded span that
    names itself under the pointer, and the month's average level drawn across. None when the
    month has no level reading."""
    end = min(t1, now)
    pts = chartdata.thin(levels, t0, t1, 700)
    if not pts:
        return None
    lo, hi = round_levels([v for _, v in pts])
    d = u.labels["dist"]
    bands = []
    for r in drives:
        a, b = r.get("start_date"), r.get("destination_date")
        if a and b and b > t0 and a < t1:
            miles = u.distance(r.get("miles") or 0)
            bands.append([a, b, "driving", f"Drive {miles:,.1f} {d} · {r.get('from') or '?'} → {r.get('to') or '?'}"])
    for c in charges:
        a, b = c.get("session_start_date"), c.get("session_end_date")
        if a and b and b > t0 and a < t1:
            bands.append([a, b, "charging", f"Charge {c.get('kwh') or 0:,.1f} kWh · {c.get('where') or '?'}"])
    avg = time_average(levels, t0, end)
    return chartdata.payload(
        t0,
        t1,
        {"key": "level", "label": "Battery", "unit": "%", "lo": lo, "hi": hi, "points": pts},
        [],
        [],
        [],
        chartdata.ticks(t0, t1, tz),
        bands=sorted(bands, key=lambda b: b[0]),
        avg={"value": avg, "label": "Average"} if avg is not None else None,
    )


TIRE_NAMES = (("fl", "Front left"), ("fr", "Front right"), ("rl", "Rear left"), ("rr", "Rear right"))


def tire_chart(t0: datetime, t1: datetime, per: dict, temps: list, u, tz, low: float | None = None, n: int = 500, temp_on: bool = True) -> dict | None:
    """Four tires as thin lines on one scale over their average, filled, with the outside
    temperature on its own right-hand scale. `per` is {fl: [[epoch, psi], ...], ...}, `temps`
    [[epoch, display degrees], ...]. The tires are on at first, and the temperature when
    `temp_on`; it comes last so the right-hand scale is its own. `low` (the owner's alert limit)
    is drawn across as a dashed line. None with no pressure at all."""
    every = [v for pts in per.values() for _, v in pts]
    if not every:
        return None
    lo, hi = chartdata.nice(every + ([low] if low else []), zero=False)
    a, b = t0.timestamp(), t1.timestamp()
    thin = lambda pts: chartdata.thin_x(pts, a, b, n)  # noqa: E731
    # the average wherever all four are known, at every moment any of them changed
    events = sorted((t, i, v) for i, (w, _) in enumerate(TIRE_NAMES) for t, v in per.get(w) or [])
    held: list[float | None] = [None] * 4
    mean: list[list[float]] = []
    for k, (t, i, v) in enumerate(events):
        held[i] = v
        if (k + 1 == len(events) or events[k + 1][0] != t) and None not in held:
            mean.append([t, round(sum(held) / 4, 2)])  # type: ignore[arg-type]
    extras = [{"key": w, "label": label, "unit": " psi", "lo": lo, "hi": hi, "points": thin(per[w])} for w, label in TIRE_NAMES if per.get(w)]
    if temps:
        tlo, thi = chartdata.nice([v for _, v in temps], zero=False)
        extras.append({"key": "temp", "label": "Outside", "unit": u.labels["temp"], "lo": tlo, "hi": thi, "points": thin(temps)})
    primary = {"key": "tires", "label": "Average of four", "unit": " psi", "lo": lo, "hi": hi, "points": thin(mean) or thin(extras[-1]["points"])}
    on = [w for w, _ in TIRE_NAMES if per.get(w)] + (["temp"] if temps and temp_on else [])
    return chartdata.payload(t0, t1, primary, extras, [], [], chartdata.ticks(t0, t1, tz), avg={"value": low, "label": "Alert under"} if low else None, on=on)


def tire_days(rows: list[dict], temps: list[dict], u, tz, low: float | None = None) -> dict | None:
    """tire_chart over a row a day: `rows` {day, fl, fr, rl, rr} and `temps` {day, f}, days as
    dates, each day's figure drawn at its midday. None with fewer than two days of pressures."""
    if len(rows) < 2:
        return None
    at = lambda d: datetime(d.year, d.month, d.day, 12, tzinfo=tz).timestamp()  # noqa: E731
    first, last = rows[0]["day"], rows[-1]["day"]
    per = {w: [[at(r["day"]), round(r[w], 2)] for r in rows if r.get(w) is not None] for w, _ in TIRE_NAMES}
    tpts = [[at(r["day"]), round(u.temperature(r["f"]), 1)] for r in temps if r.get("f") is not None and r["f"] > chartdata.NO_READING and first <= r["day"] <= last]
    t0 = datetime(first.year, first.month, first.day, tzinfo=tz)
    t1 = datetime(last.year, last.month, last.day, tzinfo=tz) + timedelta(days=1)
    c = tire_chart(t0, t1, per, tpts, u, tz, low)
    return dict(c, days=len(rows)) if c else None


def tire_now(per: dict) -> list[dict] | None:
    """Each tire's latest pressure for the car outline, and whether it sits more than 2 psi
    under the middle of the other three (a slow leak, not a cold morning, which moves all four)."""
    last = {w: pts[-1][1] for w, pts in per.items() if pts}
    if len(last) < 4:
        return None
    out = []
    for w, label in TIRE_NAMES:
        others = sorted(v for k, v in last.items() if k != w)
        out.append({"key": w, "label": label, "psi": last[w], "low": last[w] < others[1] - 2})
    return out


def charge_split(charges: list[dict]) -> list[dict]:
    """A month's charges three ways, as the Charging table rows: at home, fast (DC) and the
    rest (public AC), each {key, label, n, kwh, cost}; a kind with no charge is left out."""
    kinds = {"home": "Home", "public": "Public", "fast": "Fast"}
    out = {k: {"key": k, "label": label, "n": 0, "kwh": 0.0, "cost": 0.0} for k, label in kinds.items()}
    for c in charges:
        k = "home" if c.get("at_home") else "fast" if (c.get("charging_type") or "").upper() == "DC" else "public"
        out[k]["n"] += 1
        out[k]["kwh"] += c.get("kwh") or 0.0
        out[k]["cost"] += c.get("cost") or 0.0
    return [r for r in out.values() if r["n"]]


def day_temps(drives: list[dict], tz) -> dict[date, float]:
    """Each day's mean outside temperature at the start of its drives (°F), from the
    temperature the drive's facts already carry."""
    acc: dict[date, list[float]] = {}
    for d in drives:
        if d.get("temp_f") is not None and d["temp_f"] > chartdata.NO_READING and d.get("start_date"):
            acc.setdefault(d["start_date"].astimezone(tz).date(), []).append(d["temp_f"])
    return {k: sum(v) / len(v) for k, v in acc.items()}


def charge_places(spots: list[dict], name_of, top: int = 5) -> list[dict]:
    """The places a car charges most, from charges grouped by ~100 m spot ({lat, lon, n, kwh,
    cost}): spots under one name folded together, most charges first, each {where, n, kwh,
    per_kwh} (per_kwh None where nothing was priced). `name_of(lat, lon)` names a spot."""
    out: dict[str, dict] = {}
    for s in spots:
        where = name_of(s["lat"], s["lon"]) or "Unnamed spot"
        p = out.setdefault(where, {"where": where, "n": 0, "kwh": 0.0, "cost": 0.0, "priced_kwh": 0.0})
        p["n"] += s["n"]
        p["kwh"] += s["kwh"] or 0.0
        if s.get("cost"):
            p["cost"] += s["cost"]
            p["priced_kwh"] += s.get("priced_kwh") or 0.0
    rows = sorted(out.values(), key=lambda p: (-p["n"], -p["kwh"]))[:top]
    return [{"where": p["where"], "n": p["n"], "kwh": p["kwh"], "per_kwh": p["cost"] / p["priced_kwh"] if p["priced_kwh"] else None} for p in rows]
