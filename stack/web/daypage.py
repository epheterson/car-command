"""What the Day page says about each row: its title, pills and figures, and the thin
line for a parked stretch.

Each function takes `units`, the active units object (units_mod.ACTIVE): its `labels`
name the distance and efficiency units, and `distance` and `per_distance` convert the
stored imperial figures, so the rows and the chart's card read in the chosen units."""

from __future__ import annotations

from datetime import timedelta

import identity


def _hm(d: timedelta | None) -> str:
    if d is None:
        return ""
    m = round(d.total_seconds() / 60)
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h" + (f" {m} min" if m else "")


def _battery(r: dict) -> str:
    a, b = r.get("level_start"), r.get("level_end")
    return f"{a:.0f}→{b:.0f}%" if a is not None and b is not None else ""


def _money(x, cur: str, estimate: bool = False) -> str:
    """'$2.64', or '≈$2.64' for a cost priced on a regional average."""
    return f"{'≈' if estimate else ''}{cur}{x:.2f}" if x else ""


# What a title says when the place is not known: never "Parked at Charge"
PLACEHOLDERS = {"", "?", "Charge", "In progress"}


def row_parts(r: dict, units, cur: str) -> dict:
    if r["kind"] == "drive":
        miles = r.get("miles")
        wh = r.get("wh_per_mi")
        pills = [_hm(r.get("duration")), f"{units.per_distance(wh):.0f} {units.labels['econ']}" if wh else "", _money(r.get("cost"), cur, r.get("cost_estimate"))]
        big = f"{units.distance(miles):.1f} {units.labels['dist']}" if miles is not None else "—"
    else:
        estimate = r.get("cost_estimate") or r.get("cost_source") == "average"
        pills = [_hm(r.get("duration")), "fast" if r.get("charging_type") == "DC" else "", _money(r.get("cost"), cur, estimate)]
        big = f"{r['kwh']:.1f} kWh" if r.get("kwh") is not None else "—"
    return {"title": r.get("title") or "", "pills": [p for p in pills if p], "big": big, "sub": _battery(r)}


def gap_text(r: dict, where: str | None, units, guard: str | None = None) -> str:
    guard = guard or identity.guard_name(None)
    text = ("Asleep " if r.get("sub") == "sleep" else "Parked ") + _hm(r.get("duration"))
    if where and where not in PLACEHOLDERS:
        text += f" at {where}"
    loss, drop, rng = r.get("kwh_loss"), r.get("drop"), r.get("range_loss")
    if loss is not None and loss >= 0.05:
        text += f" · lost {loss:.1f} kWh" + (f", {units.distance(rng):.1f} {units.labels['dist']}" if rng is not None and rng >= 0.05 else "")
    elif drop is not None and drop >= 0.5:  # no kWh figure, or one too coarse to show: say it in battery percent
        text += f" · lost {drop:.1f}%"
    cabin = _cabin(r, units)
    if cabin:
        text += " · " + cabin
    sentry, d = r.get("sentry_h") or 0.0, r.get("duration")
    if sentry >= 0.5:
        whole = d is not None and sentry * 3600 >= 0.9 * d.total_seconds()
        text += f" · {guard} on" if whole else f" · {guard} {_hm(timedelta(hours=sentry))}"
    return text


def day_figs(r: dict, units, cur: str) -> str:
    p = row_parts(dict(r, title=r.get("title", "")), units, cur)
    if r["kind"] == "drive":
        parts = [p["big"], p["sub"], *p["pills"]]
    else:
        parts = [p["sub"], p["big"], *p["pills"]]
    return " · ".join(x for x in parts if x and x != "—")


def _cabin(r: dict, units) -> str:
    a, b = r.get("inside_start"), r.get("inside_end")
    if a is None or b is None:
        return ""
    a, b = f"{units.temperature(a):.0f}", f"{units.temperature(b):.0f}"
    return f"cabin {a}{units.labels['temp']}" if a == b else f"cabin {a}→{b}{units.labels['temp']}"


def detail_line(r: dict, units, cur: str) -> list[str]:
    """The row's second tier: the rest of what a drive or charge line can say beyond its
    title, pills and figures (energy, rated miles, speeds, temperatures, climb, rate plan),
    each part only when it is known."""
    lb, out = units.labels, []
    d = r.get("duration")
    hours = d.total_seconds() / 3600 if d else 0.0
    if r["kind"] == "drive":
        miles = r.get("miles")
        rated = r.get("rated_used")
        if rated is None and r.get("range_start") is not None and r.get("range_end") is not None:
            rated = r["range_start"] - r["range_end"]
        if r.get("kwh_used"):
            out.append(f"{r['kwh_used']:.1f} kWh")
        if rated is not None and rated > 0:
            out.append(f"{units.distance(rated):.1f} {lb['dist']} rated")
        if miles and miles >= 0.5 and hours >= 1 / 60:
            out.append(f"avg {units.speed(miles / hours):.0f} {lb['speed']}")
        if r.get("max_mph"):
            out.append(f"max {units.speed(r['max_mph']):.0f} {lb['speed']}")
        if r.get("temp_f") is not None:
            out.append(f"{units.temperature(r['temp_f']):.0f}{lb['temp']} out")
        cabin = _cabin(r, units)
        if cabin:
            out.append(cabin)
        if r.get("climb_ft") or r.get("descent_ft"):
            out.append(f"↑{units.elevation(r.get('climb_ft') or 0):.0f} {lb['elev']} ↓{units.elevation(r.get('descent_ft') or 0):.0f} {lb['elev']}")
        if r.get("fsd_miles"):
            out.append(f"FSD {units.distance(r['fsd_miles']):.1f} {lb['dist']}")
        if r.get("weather"):
            out.append(r["weather"])
    elif r["kind"] == "charge":
        if r.get("range_added"):
            out.append(f"+{units.distance(r['range_added']):.0f} {lb['dist']} rated")
        if r.get("kwh") and hours >= 1 / 60:
            out.append(f"{r['kwh'] / hours:.1f} kW avg")
        if r.get("rate_plan"):
            out.append(r["rate_plan"])
    return out


def as_drive(d: dict) -> dict:
    """A drive from the lists in the shape the rows read."""
    return dict(d, kind="drive", title=f"{d.get('from') or '?'} → {d.get('to') or '?'}", start=d.get("start_date"), end=d.get("destination_date"))


def as_charge(c: dict) -> dict:
    """A charge from the lists in the shape the rows read."""
    return dict(c, kind="charge", title=c.get("where") or "Charge", start=c.get("session_start_date"), end=c.get("session_end_date"), level_start=c.get("start_level"), level_end=c.get("end_level"))


def by_day(rows: list[dict], tz, at: str) -> list[dict]:
    """Rows under local-day headers, in the order given, with each day's miles and kWh."""
    out: list[dict] = []
    for r in rows:
        t = r.get(at)
        day = t.astimezone(tz).date() if t else None  # a charge that never started: only the hidden list has one
        if not out or out[-1]["date"] != day:
            out.append({"date": day, "rows": [], "miles": 0.0, "kwh": 0.0})
        out[-1]["rows"].append(r)
        out[-1]["miles"] += r.get("miles") or 0.0
        if "session_start_date" in r:
            out[-1]["kwh"] += r.get("kwh") or 0.0
    return out
