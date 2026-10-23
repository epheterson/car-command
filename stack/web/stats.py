"""Period summaries for the Statistics page: pure aggregation over drive and charge rows."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta

from cost import rates_at, role_costs, version_at
from units import drive_kwh


def drive_cost(kwh_used, rate) -> tuple[float | None, bool]:
    """A drive's energy at the car's home rate, and whether that rate rests partly on a
    regional average. `rate` is (price per kWh, estimated) or None."""
    if not kwh_used or not rate or not rate[0]:
        return None, False
    return kwh_used * rate[0], bool(rate[1])


# A charge that added less than this is the car topping up at the plug (a third of the
# Teslas' sessions): its energy and cost count, but not as a charge in a count or an average.
TOPUP_KWH = 1.0


def charge_kind(c: dict) -> str:
    """'open' for a session still going (an end that is not there yet), 'topup' for one under
    TOPUP_KWH, else 'charge'. A finished session whose energy is not worked out yet is a charge."""
    if "session_end_date" in c and c["session_end_date"] is None:
        return "open"
    return "topup" if c.get("kwh") is not None and c["kwh"] < TOPUP_KWH else "charge"


def bucket(d: datetime, by: str, tz) -> date:
    local = d.astimezone(tz).date()
    if by == "day":
        return local
    if by == "week":
        return local - timedelta(days=local.weekday())
    return local.replace(day=1)


def summarize(drives: list[dict], charges: list[dict], by: str, tz, capacity_kwh: float | None, kwh_per_rated_mi: float | None = None) -> list[dict]:
    """One row per period with drives, miles, driving time, energy used (capacity × SoC drop), efficiency, charges, kWh added, cost."""
    rows: "OrderedDict[date, dict]" = OrderedDict()

    def row(key: date) -> dict:
        return rows.setdefault(
            key,
            {
                "period": key,
                "drives": 0,
                "miles": 0.0,
                "drive_h": 0.0,
                "used_kwh": 0.0,
                "drive_cost": 0.0,
                "fsd_miles": 0.0,
                "charges": 0,
                "topups": 0,
                "added_kwh": 0.0,
                "cost": 0.0,
                "priced": 0,
                "avg_cost": 0.0,
                "drive_cost_estimate": False,
            },
        )

    for d in drives:
        if d.get("destination_date") is None:
            continue
        r = row(bucket(d["start_date"], by, tz))
        r["drives"] += 1
        r["miles"] += d.get("miles") or 0.0
        r["drive_h"] += (d["duration"].total_seconds() / 3600) if d.get("duration") else 0.0
        r["drive_cost"] += d.get("cost") or 0.0
        r["drive_cost_estimate"] = r["drive_cost_estimate"] or bool(d.get("cost") and d.get("cost_estimate"))
        r["fsd_miles"] += d.get("fsd_miles") or 0.0
        r["used_kwh"] += (
            d["kwh_used"] if d.get("kwh_used") is not None else drive_kwh(d.get("level_start"), d.get("level_end"), capacity_kwh, d.get("range_start"), d.get("range_end"), kwh_per_rated_mi)
        ) or 0.0
    for c in charges:
        kind = charge_kind(c)
        if c.get("session_start_date") is None or kind == "open":
            continue
        r = row(bucket(c["session_start_date"], by, tz))
        r["topups" if kind == "topup" else "charges"] += 1
        r["added_kwh"] += c.get("kwh") or 0.0
        r["cost"] += c.get("cost") or 0.0
        r["priced"] += c.get("cost") is not None
        if c.get("cost_source") == "average" or c.get("cost_estimate"):
            r["avg_cost"] += c.get("cost") or 0.0
    out = []
    for r in sorted(rows.values(), key=lambda x: x["period"], reverse=True):
        r["wh_per_mi"] = (r["used_kwh"] * 1000 / r["miles"]) if r["miles"] > 1 and r["used_kwh"] > 0 else None
        r["cost_per_mi"] = (r["cost"] / r["miles"]) if r["miles"] > 1 and r["cost"] > 0 else None
        out.append(r)
    return out


def month_window(ym: str | None, tz, latest: datetime | None = None) -> dict:
    """[start, end) of one calendar month in local time, plus the labels the
    list pages need. `ym` is "YYYY-MM"; when absent or malformed the month of
    `latest` (the newest row) is used, so a car that has not driven this month
    still opens on something."""
    anchor = None
    if ym:
        try:
            anchor = datetime.strptime(ym, "%Y-%m").date().replace(day=1)
        except ValueError:
            anchor = None
    if anchor is None:
        anchor = (latest.astimezone(tz).date() if latest else datetime.now(tz).date()).replace(day=1)
    nxt = (anchor.replace(day=28) + timedelta(days=4)).replace(day=1)
    prev = (anchor - timedelta(days=1)).replace(day=1)
    start = datetime(anchor.year, anchor.month, 1, tzinfo=tz)
    end = datetime(nxt.year, nxt.month, 1, tzinfo=tz)
    return {
        "start": start,
        "end": end,
        "label": anchor.strftime("%B %Y"),
        "ym": anchor.strftime("%Y-%m"),
        "prev": prev.strftime("%Y-%m"),
        "prev_label": prev.strftime("%b %Y"),
        "next": nxt.strftime("%Y-%m"),
        "next_label": nxt.strftime("%b %Y"),
        "is_current": anchor == datetime.now(tz).date().replace(day=1),
    }


def year_grids(drives: list[dict], charges: list[dict], year: int, tz, week_start: int = 0) -> list[dict]:
    """Twelve months of `calendar_grid`, each with its label, its key for the month page and
    its totals. The rows are the whole year's; each month's grid keeps the days it owns."""
    out = []
    for m in range(1, 13):
        w = month_window(f"{year}-{m:02d}", tz)
        inside = lambda dt: dt is not None and w["start"] <= dt < w["end"]  # noqa: E731
        ds = [d for d in drives if inside(d.get("start_date"))]
        cs = [c for c in charges if inside(c.get("session_start_date"))]
        weeks = calendar_grid(ds, cs, w, tz, week_start)
        cells = [c for week in weeks for c in week if not c["other"]]
        out.append(
            {
                "label": w["start"].strftime("%B"),
                "ym": w["ym"],
                "weeks": weeks,
                "totals": {k: sum(c[k] for c in cells) for k in ("drives", "miles", "charges", "kwh")},
                "days_driven": sum(1 for c in cells if c["drives"]),
            }
        )
    return out


def calendar_grid(drives: list[dict], charges: list[dict], window: dict, tz, week_start: int = 0) -> list[list[dict]]:
    """Weeks of days for one month, each day with its drives, miles, charges, kWh. `week_start`
    is the weekday the grid's columns begin on (0 Monday, 6 Sunday), from Settings.
    Days outside the month are present (so the grid is rectangular) and marked `other`."""
    first = window["start"].date()
    last = (window["end"] - timedelta(days=1)).date()
    by_day: dict[date, dict] = {}

    def cell(d: date) -> dict:
        return by_day.setdefault(d, {"date": d, "other": not (first <= d <= last), "drives": 0, "miles": 0.0, "charges": 0, "kwh": 0.0, "home_kwh": 0.0, "cars": {}})

    def car(c: dict, vin) -> dict:
        return c["cars"].setdefault(vin, {"drives": 0, "miles": 0.0, "charges": 0, "kwh": 0.0})

    for dr in drives:
        c = cell(dr["start_date"].astimezone(tz).date())
        c["drives"] += 1
        c["miles"] += dr.get("miles") or 0.0
        k = car(c, dr.get("vin"))
        k["drives"] += 1
        k["miles"] += dr.get("miles") or 0.0
    for ch in charges:
        kind = charge_kind(ch)
        if ch.get("session_start_date") is None or kind == "open":
            continue
        c = cell(ch["session_start_date"].astimezone(tz).date())
        c["charges"] += kind == "charge"
        c["kwh"] += ch.get("kwh") or 0.0
        if ch.get("at_home"):
            c["home_kwh"] += ch.get("kwh") or 0.0
        k = car(c, ch.get("vin"))
        k["charges"] += kind == "charge"
        k["kwh"] += ch.get("kwh") or 0.0
    start = first - timedelta(days=(first.weekday() - week_start) % 7)
    end = last + timedelta(days=6 - (last.weekday() - week_start) % 7)
    weeks, week = [], []
    d = start
    while d <= end:
        week.append(cell(d))
        if len(week) == 7:
            weeks.append(week)
            week = []
        d += timedelta(days=1)
    return weeks


TEMP_BINS = [(-100, 40), (40, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 200)]
SPEED_BINS = [(0, 15), (15, 25), (25, 35), (35, 45), (45, 55), (55, 65), (65, 200)]


def efficiency_bins(drives: list[dict], capacity_kwh: float | None, kwh_per_rated_mi: float | None = None, over: float = 1.0, temp_bins=None) -> dict:
    """Wh/mi and % of rated, binned by outside temperature at the start and by average speed.
    Only drives over `over` miles with a known energy count. `temp_bins` (°F) in place of
    TEMP_BINS: the chart's finer bands (effchart.py)."""
    if not capacity_kwh and not kwh_per_rated_mi:
        return {"temp": [], "speed": []}

    def rows(bins, key):
        acc = [{"lo": lo, "hi": hi, "drives": 0, "miles": 0.0, "kwh": 0.0, "rated": 0.0} for lo, hi in bins]
        for d in drives:
            miles, dur = d.get("miles"), d.get("duration")
            # The caller's constant is the all-time one and these drives span years, so
            # prefer the energy the drive already carries, which was priced by its own
            # year. summarize() has always read it this way; this bin did not, which made
            # the efficiency chart disagree with the table above it on the same page.
            kwh = d["kwh_used"] if d.get("kwh_used") is not None else drive_kwh(d.get("level_start"), d.get("level_end"), capacity_kwh, d.get("range_start"), d.get("range_end"), kwh_per_rated_mi)
            if not miles or miles < over or not kwh:
                continue
            v = key(d, miles, dur)
            if v is None:
                continue
            for b in acc:
                if b["lo"] <= v < b["hi"]:
                    b["drives"] += 1
                    b["miles"] += miles
                    b["kwh"] += kwh
                    if d.get("range_start") is not None and d.get("range_end") is not None and d["range_start"] > d["range_end"]:
                        b["rated"] += d["range_start"] - d["range_end"]
                    break
        out = []
        for b in acc:
            if b["drives"] >= 3 and b["miles"] > 0:
                b["wh_per_mi"] = b["kwh"] * 1000 / b["miles"]
                b["of_rated"] = (b["miles"] / b["rated"]) if b["rated"] > 0 else None
                out.append(b)
        # a bar from zero, as long as its figure against the longest: a bar that started at
        # the lowest row drew 264 against 331 as one to five
        top = max((b["wh_per_mi"] for b in out), default=0)
        for b in out:
            b["share"] = b["wh_per_mi"] / top if top else 0.0
        return out

    return {
        "temp": rows(temp_bins or TEMP_BINS, lambda d, miles, dur: d.get("temp_f")),
        "speed": rows(SPEED_BINS, lambda d, miles, dur: (miles / (dur.total_seconds() / 3600)) if dur and dur.total_seconds() > 60 else None),
    }


PARKED_AFTER = timedelta(hours=1)


def with_parked(events: list[dict], parked_after: timedelta = PARKED_AFTER, car_on=()) -> list[dict]:
    """The same events plus one "parked" row for every stretch of an hour or more a car
    sat between two of them, carrying the battery lost across it.

    The day page has always shown these; the month feed showed only what moved, so a
    car that lost 5 % over a three-day weekend showed two drives with a gap between
    them and nothing about the gap. The figures are already on the neighbouring
    events — a drive ends at a level and the next one starts at one — so this reads
    nothing new. Software rows have no end and are left out of the pairing; the last
    event of a car is open-ended and gets no row.
    """
    out = list(events)
    by_car: dict = {}
    for e in events:
        if e["kind"] in ("drive", "charge") and e.get("end"):
            by_car.setdefault(e.get("vin"), []).append(e)
    for vin, mine in by_car.items():
        mine.sort(key=lambda e: e["at"])
        for a, b in zip(mine, mine[1:]):
            gap = b["at"] - a["end"]
            if gap < parked_after:
                continue
            start, end = a.get("level_end"), b.get("level_start")
            out.append(
                {
                    "at": a["end"],
                    "kind": "parked",
                    "vin": vin,
                    "car": a.get("car"),
                    "id": None,
                    "title": a.get("to") if a["kind"] == "drive" else a.get("title"),
                    "refs": (a.get("refs") or [None])[-1:],
                    "duration": gap,
                    "end": b["at"],
                    "level_start": start,
                    "level_end": end,
                    "lost": (start - end) if start is not None and end is not None else None,
                    "on_h": sum((min(y, b["at"]) - max(x, a["end"])).total_seconds() for x, y, v in car_on if v == vin and x < b["at"] and y > a["end"]) / 3600,
                }
            )
    return out


def timeline_days(drives: list[dict], charges: list[dict], software: list[dict], tz, parked: bool = True, garage: dict | None = None, car_on=()) -> list[dict]:
    """One feed, newest first, grouped by local day: drives, charges and software
    updates from every car given, each event carrying its car's vin and name."""
    events: list[dict] = []
    for d in drives:
        events.append(
            {
                "at": d["start_date"],
                "kind": "drive",
                "vin": d.get("vin"),
                "car": d.get("car_name"),
                "id": d["id"],
                "title": f"{d.get('from', '?')} → {d.get('to', '?')}",
                "miles": d.get("miles"),
                "duration": d.get("duration"),
                "end": d.get("destination_date"),
                "level_start": d.get("level_start"),
                "level_end": d.get("level_end"),
                "tag": d.get("tag"),
                "from": d.get("from"),
                "to": d.get("to"),
                "refs": [d.get("from_ref"), d.get("to_ref")],
                "wh_per_mi": d.get("wh_per_mi"),
                "kwh_used": d.get("kwh_used"),
                "rated_used": d.get("rated_used"),
                "max_mph": d.get("max_mph"),
                "temp_f": d.get("temp_f"),
                "cost": d.get("cost"),
                "weather": d.get("weather"),
                "garage": (garage or {}).get(d["id"]),
            }
        )
    for c in charges:
        if c.get("session_start_date") is None:
            continue
        events.append(
            {
                "at": c["session_start_date"],
                "kind": "charge",
                "vin": c.get("vin"),
                "car": c.get("car_name"),
                "id": c["id"],
                "title": c.get("where") or "Charge",
                "refs": [c.get("where_ref")],
                "kwh": c.get("kwh"),
                "cost": c.get("cost"),
                "duration": c.get("duration"),
                "end": c.get("session_end_date"),
                "level_start": c.get("start_level"),
                "level_end": c.get("end_level"),
                "charging_type": c.get("charging_type"),
                "range_added": c.get("range_added"),
                "rate_plan": c.get("rate_plan"),
            }
        )
    for s in software:
        events.append({"at": s["seen_at"], "kind": "software", "vin": s.get("vin"), "car": s.get("car_name"), "id": None, "title": f"Software {s['version']}"})
    if parked:  # only the whole feed knows every drive and charge a gap sits between
        events = with_parked(events, car_on=car_on)
    events.sort(key=lambda e: e["at"], reverse=True)
    days: list[dict] = []
    for e in events:
        day = e["at"].astimezone(tz).date()
        if not days or days[-1]["date"] != day:
            days.append({"date": day, "events": [], "miles": 0.0, "kwh": 0.0})
        days[-1]["events"].append(e)
        days[-1]["miles"] += e.get("miles") or 0.0
        days[-1]["kwh"] += e.get("kwh") or 0.0
    return days


def charging_summary(charges: list[dict]) -> dict:
    """Charging summary: charges by place, by the level they ended at, and by fast-charging station.
    `charges` are the rows charges() returns (where, kwh, cost, start/end level, charging_type)."""
    by_place: dict[str, dict] = {}
    by_end: dict[int, dict] = {}
    superchargers: dict[str, dict] = {}
    priced, estimate, areas, charging_now = 0, 0.0, set(), 0
    for c in charges:
        kind = charge_kind(c)
        if c.get("session_start_date") is None:
            continue
        if kind == "open":
            charging_now += 1  # no energy yet: in no count, total or average
            continue
        where = c.get("where") or "Unnamed spot"
        p = by_place.setdefault(where, {"where": where, "charges": 0, "topups": 0, "topup_kwh": 0.0, "kwh": 0.0, "cost": 0.0, "gained": 0.0, "hours": 0.0, "dc": 0, "at_home": bool(c.get("at_home"))})
        p["kwh"] += c.get("kwh") or 0.0
        p["cost"] += c.get("cost") or 0.0
        if c.get("cost") is not None:
            priced += 1
        if c.get("cost_source") == "average" and c.get("cost") is not None:
            p["estimate"] = True
            estimate += c["cost"]
            areas.add((c.get("rate_plan") or "").removesuffix(" average") or "regional")
        if kind == "topup":
            p["topups"] += 1
            p["topup_kwh"] += c.get("kwh") or 0.0
            continue
        p["charges"] += 1
        p["gained"] += c.get("gained") or 0.0
        p["hours"] += (c["duration"].total_seconds() / 3600) if c.get("duration") else 0.0
        if c.get("charging_type") == "DC":
            p["dc"] += 1
            sc = superchargers.setdefault(where, {"where": where, "charges": 0, "kwh": 0.0, "cost": 0.0, "minutes": 0.0})
            sc["charges"] += 1
            sc["kwh"] += c.get("kwh") or 0.0
            sc["cost"] += c.get("cost") or 0.0
            sc["minutes"] += (c["duration"].total_seconds() / 60) if c.get("duration") else 0.0
        if c.get("end_level") is not None and (c.get("gained") or 0) >= 1:
            lvl = int(round(c["end_level"] / 5.0) * 5)
            e = by_end.setdefault(lvl, {"level": lvl, "charges": 0, "kwh": 0.0})
            e["charges"] += 1
            e["kwh"] += c.get("kwh") or 0.0
    places = sorted(by_place.values(), key=lambda p: p["kwh"], reverse=True)
    for p in places:
        p["rate"] = (p["cost"] / p["kwh"]) if p["cost"] and p["kwh"] else None
        p["avg_time"] = timedelta(hours=p["hours"] / p["charges"]) if p["charges"] and p["hours"] else None  # a session's average length
    ends = sorted(by_end.values(), key=lambda e: e["level"], reverse=True)
    # The share is of every charge that ended at a level; the bar is scaled to the top row
    # so the commonest level fills it.
    counted = sum(e["charges"] for e in ends)
    top = max((e["charges"] for e in ends), default=0)
    for e in ends:
        e["share"] = e["charges"] / counted if counted else 0
        e["bar"] = e["charges"] / top if top else 0
    return {
        "places": places,
        "ends": ends,
        "superchargers": sorted(superchargers.values(), key=lambda x: x["charges"], reverse=True),
        "total_kwh": sum(p["kwh"] for p in places),
        "total_charges": sum(p["charges"] for p in places),
        "total_topups": sum(p["topups"] for p in places),
        "topup_kwh": sum(p["topup_kwh"] for p in places),
        "charging_now": charging_now,
        "total_cost": sum(p["cost"] for p in places),
        # How many charges carry a price at all, and how much of the total is an estimate
        # at a regional average (and whose), so a page never shows a bare $0 or passes an
        # estimate off as a bill.
        "priced": priced,
        "estimate_cost": estimate,
        "estimate_areas": sorted(areas),
        "tou": tou_split(charges),
    }


# The rate periods, cheapest first, with the label each one goes by on a bill.
PERIODS = (("off_peak", "Off-peak"), ("part_peak", "Part-peak"), ("peak", "Peak"))


def tou_split(charges: list[dict]) -> dict:
    """How much home energy landed in each time-of-use period, and what it cost there.

    Whether you charge in the cheap hours is the whole of a time-of-use bill, and it is
    the one thing a logger that asks you to type a single flat rate cannot tell you.
    Every one of these numbers was already worked out to price the charge."""
    got = {k: {"key": k, "label": label, "kwh": 0.0, "cost": 0.0} for k, label in PERIODS}
    priced, newest = 0, None
    for c in charges:
        if not c.get("at_home") or c.get("off_peak_kwh") is None:
            continue
        priced += 1
        when = c.get("session_start_date")
        if when and (newest is None or when > newest):
            newest = when
        # Cost is stored per session, not per period, so it is apportioned by the rate
        # each period actually carried — which is what made the session's price. With the
        # exact split stored, each period at its own price; a role that holds several
        # periods (a plan with more than three) is the sum of them.
        kwh = {k: (c.get(f"{k}_kwh") or 0.0) for k, _ in PERIODS}
        version = version_at(when) if c.get("period_kwh") else None
        if version:
            spent = role_costs(version, c["period_kwh"])
        else:
            rates = rates_at(when)
            spent = {k: kwh[k] * (rates.get(k) or 0.0) for k, _ in PERIODS}
        for k, _ in PERIODS:
            got[k]["kwh"] += kwh[k]
            got[k]["cost"] += spent[k]
    total = sum(p["kwh"] for p in got.values())
    if not priced or total <= 0:
        return {}
    # A period's rate is a fact about the plan, not about whether you happened to draw
    # anything in it. Taking it from the schedule means a window with no peak charging
    # still says what peak would have cost, which is the comparison worth making.
    schedule = rates_at(newest) if newest else {}
    for key, p in got.items():
        p["share"] = p["kwh"] / total
        p["rate"] = (p["cost"] / p["kwh"]) if p["kwh"] else schedule.get(key)
    off, dearer = got["off_peak"], got["part_peak"]["kwh"] + got["peak"]["kwh"]
    return {
        "periods": [got[k] for k, _ in PERIODS],
        "total_kwh": total,
        "off_peak_share": off["share"],
        # What the energy drawn outside the cheap hours cost above what it would have
        # cost inside them. None when there is no off-peak rate to compare against.
        "excess": (got["part_peak"]["cost"] + got["peak"]["cost"] - dearer * off["rate"]) if off["rate"] and dearer else None,
        "peak_multiple": (got["peak"]["rate"] / off["rate"]) if off["rate"] and got["peak"]["rate"] else None,
    }


# The columns a list may be sorted by, and the row key each reads. A whitelist rather
# than a pass-through, because the key arrives in a URL and the row dicts hold things
# no visitor should be able to order a table by.
DRIVE_SORTS = {"start": "start_date", "miles": "miles", "time": "duration", "kwh": "kwh_used", "wh": "wh_per_mi", "cost": "cost", "odometer": "destination_odometer"}
CHARGE_SORTS = {"start": "session_start_date", "gained": "gained", "kwh": "kwh", "cost": "cost", "time": "duration"}


def sort_rows(rows: list[dict], key: str | None, desc: bool, allowed: dict[str, str], default: str = "start") -> tuple[list[dict], str, bool]:
    """`rows` ordered by a whitelisted column, plus the (key, desc) actually applied.

    Rows with no value for the column go last whichever way the table is sorted: a
    drive with no energy figure is not the cheapest drive of the month. Sorted in
    Python because the list is already bounded by its date window, and a sort key
    from a URL has no business anywhere near an ORDER BY.
    """
    key = key if key in allowed else default
    field = allowed[key]
    have = [r for r in rows if r.get(field) is not None]
    missing = [r for r in rows if r.get(field) is None]
    have.sort(key=lambda r: r[field], reverse=desc)
    return have + missing, key, desc


def _is(name: str | None, want: str | None) -> bool:
    return not want or bool(name) and name.strip().lower() == want.strip().lower()


def _id_is(have: int | None, want: int | None) -> bool:
    return want is None or have == want


def filter_drives(
    rows: list[dict], frm: str | None = None, to: str | None = None, both_ways: bool = False, min_mi: float | None = None, text: str | None = None, frm_id: int | None = None, to_id: int | None = None
) -> list[dict]:
    """The drive search, on rows the list already has: from and to a place, by name or by id
    (both ways if asked), longer than N, and a word in the tag or note. Blank means any."""
    needle = (text or "").strip().lower()
    out = []
    for d in rows:
        if min_mi and (d.get("miles") or 0) < min_mi:
            continue
        if frm or to or frm_id is not None or to_id is not None:
            straight = _is(d.get("from"), frm) and _is(d.get("to"), to) and _id_is(d.get("from_id"), frm_id) and _id_is(d.get("to_id"), to_id)
            reverse = both_ways and _is(d.get("to"), frm) and _is(d.get("from"), to) and _id_is(d.get("to_id"), frm_id) and _id_is(d.get("from_id"), to_id)
            if not (straight or reverse):
                continue
        if needle and needle not in f"{d.get('tag') or ''} {d.get('note') or ''} {d.get('from') or ''} {d.get('to') or ''}".lower():
            continue
        out.append(d)
    return out


def filter_charges(rows: list[dict], where: str | None = None, kind: str | None = None, min_kwh: float | None = None) -> list[dict]:
    """Charges at a named place, of one kind (home, away, fast), adding at least N kWh."""
    out = []
    for c in rows:
        if where and not _is(c.get("where"), where):
            continue
        if kind == "home" and not c.get("at_home"):
            continue
        if kind == "away" and (c.get("at_home") or (c.get("charging_type") or "").upper() == "DC"):
            continue
        if kind == "fast" and (c.get("charging_type") or "").upper() != "DC":
            continue
        if min_kwh and (c.get("kwh") or 0) < min_kwh:
            continue
        out.append(c)
    return out


LITRES_PER_GALLON = 3.78541
DEFAULT_MPG = 30.0  # the petrol car compared against when Settings names none


def efficiency_by_year(rows: list[tuple]) -> dict:
    """(vin, year, miles, kWh) rows -> {(vin, year): Wh a mile} for years with at least
    MIN_EFFICIENCY_MILES behind them, and {(vin, None): all time} for every car."""
    out, total = {}, {}
    for vin, year, miles, kwh in rows:
        if miles >= MIN_EFFICIENCY_MILES and kwh > 0:
            out[(vin, year)] = kwh * 1000 / miles
        t = total.setdefault(vin, [0.0, 0.0])
        t[0], t[1] = t[0] + miles, t[1] + kwh
    for vin, (miles, kwh) in total.items():
        if miles >= MIN_EFFICIENCY_MILES and kwh > 0:
            out[(vin, None)] = kwh * 1000 / miles
    return out


MIN_EFFICIENCY_MILES = 100  # fewer miles than this in a year say too little about the car


def estimate_note(groups: list[dict], rates) -> dict | None:
    """What the average-priced part of a window rests on, for the footnote: its cost, the
    regions and sources, and the months whose figures priced it. `groups` are charge rows
    grouped by (region, month, cost_source) with n, cost and the rate months used."""
    est = [g for g in groups if g.get("cost_source") == "average" and g.get("cost") is not None]
    if not est:
        return None
    areas, sources = set(), {}
    for g in est:
        code = g.get("region")
        areas.add((rates.name(code) if rates else None) or code)
        s = ((rates.regions.get(code) or {}).get("electricity") or {}) if rates else {}
        if s.get("source"):
            sources[s["source"]] = True
    used = [m for g in est for m in (g.get("rate_lo"), g.get("rate_hi")) if m]
    return {
        "cost": sum(g["cost"] for g in est),
        "charges": sum(g.get("n") or 0 for g in est),
        "areas": sorted(areas),
        "sources": list(sources),
        "from": min(used, default=None),
        "to": max(used, default=None),
        # charges from months whose own figure is not out yet, priced on the newest one
        "lagged": any(g.get("rate_hi") and g["rate_hi"] < g["ym"] for g in est),
    }


def petrol_comparison(groups: list[dict], wh_per_mi: dict, mpg: float | None, rates, own_price: float | None = None) -> dict:
    """What the energy each priced charge added would have cost as petrol, against what
    the charges cost. Per charge group: kWh -> miles at the car's measured Wh/mi for that
    year (all-time where the year has none) -> fuel at `mpg` -> the petrol price for the
    charge's region and month (or `own_price` per gallon from Settings, when set).

    `groups` are charge rows grouped by (vin, year, region, month, cost_source) with n,
    kwh and cost. A group with no cost is left out (there is nothing to compare it
    with), as is one with no efficiency or no petrol price; each is counted, so the page
    can say what is missing instead of showing a figure as if it were whole."""
    mpg = mpg if mpg and mpg > 0 else DEFAULT_MPG
    out = {"mpg": mpg, "l_per_100km": 235.215 / mpg, "petrol": 0.0, "charging": 0.0, "miles": 0.0, "gallons": 0.0, "charges": 0, "from": None, "to": None, "lagged": False}
    skipped = {"unpriced": 0, "efficiency": 0, "petrol": 0}
    sources, areas = {}, set()
    for g in groups:
        n, kwh = g.get("n") or 0, g.get("kwh") or 0.0
        if g.get("cost") is None:
            skipped["unpriced"] += n
            continue
        eff = wh_per_mi.get((g.get("vin"), g.get("year"))) or wh_per_mi.get((g.get("vin"), None))
        if not eff or kwh <= 0:
            skipped["efficiency"] += n
            continue
        if own_price:
            per_gallon = own_price
        else:
            p = rates.price(g.get("region"), "petrol", g["ym"]) if rates else None
            if not p:
                skipped["petrol"] += n
                continue
            per_gallon = p["value"] * (LITRES_PER_GALLON if p["unit"] == "L" else 1.0)
            sources[p["source"]] = True
            areas.add(p["area"])
            out["from"] = min(out["from"] or p["month"], p["month"])
            out["to"] = max(out["to"] or p["month"], p["month"])
            out["lagged"] = out["lagged"] or p["month"] != g["ym"]
        miles = kwh * 1000 / eff
        out["miles"] += miles
        out["gallons"] += miles / mpg
        out["petrol"] += miles / mpg * per_gallon
        out["charging"] += g["cost"]
        out["charges"] += n
    out.update(
        saved=out["petrol"] - out["charging"], litres=out["gallons"] * LITRES_PER_GALLON, sources=list(sources), areas=sorted(areas), skipped=skipped, own_price=own_price, ok=out["charges"] > 0
    )
    return out


def place_savings(charges: list[dict], tz, wh_per_mi: dict, mpg: float | None, rates, own_price: float | None = None) -> dict[str, dict]:
    """What each place's charges saved against a petrol car: petrol_comparison over that
    place's charges alone (each charge its own group, at its own region and month). Each
    place carries the saving of the charges it could compare, how many those were of how
    many, and why the rest were left out, so a partial figure is never shown as a whole one."""
    by: dict[str, list[dict]] = {}
    for c in charges:
        when = c.get("session_start_date")
        if when is None:
            continue
        local = when.astimezone(tz)
        by.setdefault(c.get("where") or "Unnamed spot", []).append(
            {"vin": c.get("vin"), "year": local.year, "ym": local.strftime("%Y-%m"), "region": c.get("region"), "n": 1, "kwh": c.get("kwh") or 0.0, "cost": c.get("cost")}
        )
    out = {}
    for where, groups in by.items():
        p = petrol_comparison(groups, wh_per_mi, mpg, rates, own_price)
        out[where] = {"saved": p["saved"] if p["ok"] else None, "compared": p["charges"], "of": len(groups), "skipped": p["skipped"]}
    return out


def saving_cell(cells: list[dict]) -> dict:
    """Several places' savings as one cell (a folded row, the All row): the savings that
    exist added up, the counts added up, and the reasons kept."""
    skipped = {k: sum((c.get("skipped") or {}).get(k, 0) for c in cells) for k in ("unpriced", "efficiency", "petrol")}
    got = [c["saved"] for c in cells if c.get("saved") is not None]
    return {"saved": sum(got) if got else None, "compared": sum(c.get("compared", 0) for c in cells), "of": sum(c.get("of", 0) for c in cells), "skipped": skipped}


def saving_note(cell: dict | None, distname: str = "mile") -> str | None:
    """The tooltip on a Saved cell that is partial or blank, naming what was left out and
    why: no price, no measured efficiency for the year, no petrol price for the region.
    None on a whole one."""
    if not cell:
        return None
    sk = cell.get("skipped") or {}
    why = [
        f"{n} with no price" if k == "unpriced" else f"{n} with no Wh a {distname} for that year" if k == "efficiency" else f"{n} with no petrol price for where and when" for k, n in sk.items() if n
    ]
    if cell.get("compared", 0) >= cell.get("of", 0):
        return None
    if not cell.get("compared"):
        return "Nothing to compare: " + ", ".join(why) if why else "Nothing to compare"
    return f"{cell['compared']} of {cell['of']} charges compared" + (": " + ", ".join(why) if why else "")
