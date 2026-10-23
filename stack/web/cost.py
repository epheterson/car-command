"""Home charging cost from a time-of-use plan.

A plan is a name, an optional utility, where it came from (`source`: "manual" for one typed
in on Settings), and dated versions, oldest first. A version lists its periods, each a key,
a label and a price per kWh, and two 12x24 grids, `weekday` and `weekend`: month by local
hour, each cell the index of the period in force. A charge is priced at the version in force
when it started. Energy is integrated from charging_powers rows a local hour at a time, so
each kW interval is split across the periods it touches.

With no plan nothing is priced: session_cost returns None. It does the same for a charge
older than every version of a plan that does not reach back (only a manual plan's first
version stands for everything before it), so a caller can fall back to something else.

The Settings form edits a manual plan as rows: three prices per dated row, and schedule
rows saying when each period applies. compile_rows turns those into versions; plan_rows
turns a plan that fits back into them.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import clockfmt

# The three periods the rows editor knows, cheapest first. Plans with other periods are
# described by role on pages that want three columns: cheapest is off-peak, dearest is
# peak, anything between is part-peak.
ROLES = ("off_peak", "part_peak", "peak")
PERIODS = {"off_peak": "Off-peak", "part_peak": "Part-peak", "peak": "Peak"}
DAYS = {"all": "every day", "weekdays": "weekdays", "weekends": "weekends"}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# What an install from before plans existed priced with when it had saved no rates or
# schedule of its own. Only the upgrade reads these (settings.load), so that upgrading
# changes no cost; a new install starts with no plan.
LEGACY_PLAN_NAME = "Earlier rates"  # a label only (stored nowhere but the plan itself); the rates and schedule below are what priced those charges
LEGACY_RATES = [{"from": "2000-01-01", "off_peak": 0.40, "part_peak": 0.60, "peak": 0.63}]
LEGACY_SCHEDULE = [
    {"period": "peak", "days": "all", "start": 16, "end": 21, "months": None},
    {"period": "part_peak", "days": "all", "start": 15, "end": 16, "months": None},
    {"period": "part_peak", "days": "all", "start": 21, "end": 22, "months": [1, 2, 3, 4, 5, 10, 11, 12]},
]

_ACTIVE: dict | None = None  # the plan in force, prepared by use_plan


# --- schedule rows (the Settings editor) ---------------------------------------------


def clean_schedule(rows) -> list[dict]:
    """Rows from Settings as the schedule wants them; a row that makes no sense is dropped.
    Rows are (period, days, from hour, to hour, months): first match wins, anything
    unmatched is off-peak. Days: "all", "weekdays" or "weekends"; months: None for the whole
    year, else a list of month numbers."""
    out = []
    for r in rows or []:
        try:
            period, days = r.get("period"), r.get("days") or "all"
            start, end = int(r.get("start")), int(r.get("end"))
            months = r.get("months")
            months = sorted({int(m) for m in months if 1 <= int(m) <= 12}) if months else None
            if months and len(months) == 12:
                months = None  # every month ticked is the whole year, the same as none
        except (TypeError, ValueError, AttributeError):
            continue
        if period not in PERIODS or days not in DAYS or not (0 <= start < 24 and 0 < end <= 24 and start < end):
            continue
        out.append({"period": period, "days": days, "start": start, "end": end, "months": months or None})
    return out


def _row_period(schedule: list[dict], month: int, hour: int, weekend: bool) -> str:
    for r in schedule:
        if r["days"] == "weekdays" and weekend or r["days"] == "weekends" and not weekend:
            continue
        if r["months"] and month not in r["months"]:
            continue
        if r["start"] <= hour < r["end"]:
            return r["period"]
    return "off_peak"


def period_at(local: datetime, schedule: list[dict]) -> str:
    """The period schedule rows put `local` in."""
    return _row_period(schedule, local.month, local.hour, local.weekday() >= 5)


def schedule_text(schedule: list[dict]) -> str:
    """Schedule rows as a sentence, for the Settings page."""
    bits = []
    for r in schedule:
        when = f"{_h(r['start'])}–{_h(r['end'])}"
        days = "" if r["days"] == "all" else f" {DAYS[r['days']]}"
        months = "" if not r["months"] else " in " + ", ".join(MONTHS[m - 1] for m in r["months"])
        bits.append(f"{PERIODS[r['period']].lower()} {when}{days}{months}")
    return "; ".join(bits) + "; off-peak otherwise." if bits else "off-peak all day."


def _h(h: int) -> str:
    if clockfmt.PREFS["clock"] == "24":
        return f"{h % 24:02d}:00"
    if h in (0, 24):
        return "12am"
    if h == 12:
        return "12pm"
    return f"{h}am" if h < 12 else f"{h - 12}pm"


def _runs(xs: list[int]) -> list[tuple[int, int]]:
    """Sorted integers as (first, last) runs of consecutive ones."""
    out: list[list[int]] = []
    for x in xs:
        if out and x == out[-1][1] + 1:
            out[-1][1] = x
        else:
            out.append([x, x])
    return [(a, b) for a, b in out]


def hour_roles(version: dict, weekend: bool, month: int) -> list[str]:
    """The role of each local hour of a month's weekdays or weekends, ranked among the periods
    that month uses (cheapest off-peak, dearest peak, any between part-peak): a plan filed with
    summer and winter periods side by side compares hour for hour with a three-period one."""
    row = (version["weekend"] if weekend else version["weekday"])[month - 1]
    used = sorted(set(row), key=lambda i: (float(version["periods"][i]["price"]), i))
    role = dict.fromkeys(used, "part_peak")
    role[used[0]] = "off_peak"
    if len(used) > 1:
        role[used[-1]] = "peak"
    return [role[i] for i in row]


def hours_differ(mine: dict, theirs: dict) -> list[str]:
    """Where two versions put an hour in a different role, one line per pair of roles:
    "off-peak here, part-peak there: 10pm–12am Jan–May, Oct–Dec; 9pm–12am Jun–Sep"."""
    found: dict[tuple[str, str], dict[int, tuple]] = {}
    for month in range(1, 13):
        for weekend in (False, True):
            for h, (a, b) in enumerate(zip(hour_roles(mine, weekend, month), hour_roles(theirs, weekend, month))):
                if a != b:
                    found.setdefault((a, b), {}).setdefault(month, ([], []))[weekend].append(h)
    lines = []
    for (a, b), months in found.items():
        groups: dict[tuple, list[int]] = {}
        for month, (wd, we) in months.items():
            groups.setdefault((tuple(wd), tuple(we)), []).append(month)
        parts = []
        for (wd, we), ms in sorted(groups.items(), key=lambda g: g[1][0]):
            when = "every month" if len(ms) == 12 else ", ".join(MONTHS[x - 1] if x == y else f"{MONTHS[x - 1]}–{MONTHS[y - 1]}" for x, y in _runs(ms))
            hours = [(hs, "") for hs in [wd] if wd == we] or [(hs, f" {DAYS[k]}") for hs, k in ((wd, "weekdays"), (we, "weekends")) if hs]
            parts += [", ".join(f"{_h(x)}–{_h(y + 1)}" for x, y in _runs(list(hs))) + f"{days} {when}" for hs, days in hours]
        lines.append(f"{PERIODS[a].lower()} here, {PERIODS[b].lower()} there: " + "; ".join(parts))
    return lines


# --- rows <-> grid -------------------------------------------------------------------


def grids_from_rows(schedule: list[dict]) -> tuple[list[list[int]], list[list[int]]]:
    """(weekday, weekend) 12x24 grids of indexes into ROLES."""
    idx = {k: i for i, k in enumerate(ROLES)}
    weekday, weekend = ([[idx[_row_period(schedule, m, h, we)] for h in range(24)] for m in range(1, 13)] for we in (False, True))
    return weekday, weekend


def rows_from_grids(weekday: list[list[int]], weekend: list[list[int]]) -> list[dict]:
    """Schedule rows that compile back to these grids (indexes into ROLES, 0 the default).
    Each run of hours a period holds in a month becomes a span; spans shared by months are
    one row, and a span both day kinds share is an every-day row. The rows never overlap,
    so their order does not change a price; it is dearest first, then by hour."""
    spans: dict[tuple[int, int, int], dict[str, set[int]]] = {}
    for kind, grid in (("weekdays", weekday), ("weekends", weekend)):
        for m, hours in enumerate(grid, 1):
            h = 0
            while h < 24:
                p, e = hours[h], h + 1
                while e < 24 and hours[e] == p:
                    e += 1
                if p:
                    spans.setdefault((p, h, e), {"weekdays": set(), "weekends": set()})[kind].add(m)
                h = e
    rows = []
    for (p, s, e), kinds in spans.items():
        wd, we = kinds["weekdays"], kinds["weekends"]
        for days, months in (("all", wd & we), ("weekdays", wd - we), ("weekends", we - wd)):
            if months:
                rows.append({"period": ROLES[p], "days": days, "start": s, "end": e, "months": None if len(months) == 12 else sorted(months)})
    order = {"all": 0, "weekdays": 1, "weekends": 2}
    return sorted(rows, key=lambda r: (-ROLES.index(r["period"]), r["start"], order[r["days"]], (r["months"] or [0])[0]))


def compile_rows(name: str, rates: list[dict], schedule: list[dict], utility: str | None = None) -> dict:
    """A manual plan from the rows editor: one version per dated rate row, all on one schedule."""
    weekday, weekend = grids_from_rows(schedule)
    versions = [
        {"from": r["from"], "periods": [{"key": k, "label": PERIODS[k], "price": float(r[k])} for k in ROLES], "weekday": weekday, "weekend": weekend} for r in sorted(rates, key=lambda r: r["from"])
    ]
    return {"name": name, "utility": utility, "source": "manual", "versions": versions}


def fits_rows(plan: dict | None) -> bool:
    """Whether the rows editor can show this plan: manual, the three periods, one schedule."""
    if not plan or plan.get("source") != "manual" or not plan.get("versions"):
        return False
    first = plan["versions"][0]
    return all([p["key"] for p in v["periods"]] == list(ROLES) and v["weekday"] == first["weekday"] and v["weekend"] == first["weekend"] for v in plan["versions"])


def plan_rows(plan: dict | None) -> tuple[list[dict], list[dict]]:
    """(rate rows, schedule rows) for the editor; empty for no plan or one that does not fit."""
    if not fits_rows(plan):
        return [], []
    rates = [{"from": v["from"], **{p["key"]: p["price"] for p in v["periods"]}} for v in plan["versions"]]
    v0 = plan["versions"][0]
    return rates, rows_from_grids(v0["weekday"], v0["weekend"])


def legacy_plan(name: str | None, rates, schedule) -> dict:
    """The plan an install from before plans was pricing with: its saved rate rows (or the
    built-in ones it fell back to) on its saved schedule (or the built-in one), under its
    saved name or the built-in one."""
    rows = []
    for r in rates or []:
        try:
            rows.append({"from": date.fromisoformat(r["from"]).isoformat(), **{k: float(r.get(k, 0)) for k in ROLES}})
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return compile_rows(name or LEGACY_PLAN_NAME, rows or LEGACY_RATES, clean_schedule(schedule) or LEGACY_SCHEDULE)


def with_prices(plan: dict, prices: dict[tuple[int, int], float]) -> dict:
    """A copy of `plan` with (version index, period index) prices replaced. A price typed
    over an estimated one (`est`) is no longer an estimate."""
    out = {**plan, "versions": [{**v, "periods": [dict(p) for p in v["periods"]]} for v in plan["versions"]]}
    for (vi, pi), price in prices.items():
        if 0 <= vi < len(out["versions"]) and 0 <= pi < len(out["versions"][vi]["periods"]):
            p = out["versions"][vi]["periods"][pi]
            if float(p["price"]) != float(price):
                p.pop("est", None)
            p["price"] = float(price)
    return out


def prices_differ(a: dict | None, b: dict | None) -> bool:
    """Whether two plans could price a charge differently (the name is only a label)."""
    return ((a or {}).get("versions"), (a or {}).get("source")) != ((b or {}).get("versions"), (b or {}).get("source"))


def valid_plan(plan) -> bool:
    """Whether a stored plan has the shape pricing needs; anything else prices nothing."""
    try:
        if not plan or not plan["versions"]:
            return False
        for v in plan["versions"]:
            date.fromisoformat(v["from"])
            n = len(v["periods"])
            if not n or any(not isinstance(p["key"], str) or float(p["price"]) < 0 for p in v["periods"]):
                return False
            for g in (v["weekday"], v["weekend"]):
                if len(g) != 12 or any(len(row) != 24 or any(not (isinstance(i, int) and 0 <= i < n) for i in row) for row in g):
                    return False
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    return True


# --- the plan in force ----------------------------------------------------------------


def use_plan(plan: dict | None) -> None:
    """Price with this plan from now on (from Settings); None, or one that is not valid, prices nothing."""
    global _ACTIVE  # noqa: PLW0603 - the one place the plan is set
    _ACTIVE = _prepare(plan)


def _prepare(plan: dict | None) -> dict | None:
    if not valid_plan(plan):
        return None
    versions = sorted(((date.fromisoformat(v["from"]), v) for v in plan["versions"]), key=lambda x: x[0])
    return {"name": plan.get("name") or "Home", "source": plan.get("source"), "versions": versions}


def current_plan() -> str | None:
    return _ACTIVE["name"] if _ACTIVE else None


def version_at(when, plan: dict | None = None) -> dict | None:
    """The version in force on `when`'s date (a date or a local datetime), or None when no
    plan covers it. `plan` defaults to the one in force."""
    p = _ACTIVE if plan is None else _prepare(plan)
    if not p:
        return None
    d = when.date() if isinstance(when, datetime) else when
    current = None
    for start, v in p["versions"]:
        if start <= d:
            current = v
    if current is None and p["source"] == "manual":
        current = p["versions"][0][1]  # a manual plan's first row stands for everything before it
    return current


def roles(version: dict) -> list[str]:
    """The role (off_peak, part_peak, peak) of each period of a version, in period order.
    Periods already keyed by role keep their key; otherwise cheapest is off-peak, dearest
    peak, and anything between part-peak."""
    keys = [p["key"] for p in version["periods"]]
    if set(keys) <= set(ROLES) and len(set(keys)) == len(keys):
        return keys
    order = sorted(range(len(keys)), key=lambda i: (float(version["periods"][i]["price"]), i))
    out = ["part_peak"] * len(keys)
    out[order[0]] = "off_peak"
    if len(order) > 1:
        out[order[-1]] = "peak"
    return out


def role_split(version: dict, kwh: dict[str, float]) -> dict[str, float]:
    """kWh per period key folded into the three roles."""
    out = dict.fromkeys(ROLES, 0.0)
    for p, role in zip(version["periods"], roles(version)):
        out[role] += kwh.get(p["key"]) or 0.0
    return out


def role_costs(version: dict, kwh: dict[str, float]) -> dict[str, float]:
    """What the energy in each role cost, period by period at its own price."""
    out = dict.fromkeys(ROLES, 0.0)
    for p, role in zip(version["periods"], roles(version)):
        out[role] += (kwh.get(p["key"]) or 0.0) * float(p["price"])
    return out


def rates_at(when) -> dict[str, float]:
    """Price per role in force at `when`: off-peak and peak are the cheapest and dearest
    periods, part-peak the mean of any between. Empty when no plan covers `when`."""
    v = version_at(when)
    if v is None:
        return {}
    by_role: dict[str, list[float]] = {}
    for p, role in zip(v["periods"], roles(v)):
        by_role.setdefault(role, []).append(float(p["price"]))
    return {k: sum(xs) / len(xs) for k, xs in by_role.items()}


def hour_rate(local: datetime) -> tuple[str | None, float | None]:
    """The role (off_peak, part_peak, peak) and all-in price per kWh of the local hour `local`
    is in, under the plan in force on its date; (None, None) when no plan covers it."""
    v = version_at(local)
    if v is None:
        return None, None
    i = period_index(v, local)
    return roles(v)[i], float(v["periods"][i]["price"])


def period_index(version: dict, local: datetime) -> int:
    grid = version["weekend"] if local.weekday() >= 5 else version["weekday"]
    return grid[local.month - 1][local.hour]


def _hourly(powers: list[tuple[datetime, datetime, float]], start: datetime, end: datetime, tz):
    """(local time, kWh) for each piece of each power interval inside [start, end), cut at
    the local hour."""
    for a, b, kw in powers:
        if kw is None or kw <= 0:
            continue
        t = max(a, start)
        stop = min(b, end)
        while t < stop:
            local = t.astimezone(tz)
            next_hour = (local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).astimezone(t.tzinfo)
            seg_end = min(stop, next_hour)
            yield local, kw * (seg_end - t).total_seconds() / 3600
            t = seg_end


def split_kwh(powers: list[tuple[datetime, datetime, float]], start: datetime, end: datetime, tz, version: dict) -> dict[str, float]:
    """kWh per period key inside [start, end), stepping through each power interval by the local hour."""
    keys = [p["key"] for p in version["periods"]]
    kwh = dict.fromkeys(keys, 0.0)
    for local, k in _hourly(powers, start, end, tz):
        kwh[keys[period_index(version, local)]] += k
    return kwh


# --- a plan against the charging already done ------------------------------------------
# Home charges as kWh per (weekday or weekend, month, local hour): what any plan's grid
# prices, so one profile answers "what would this plan have cost" for every plan without
# touching a charge.


def profile_add(profile: list[float], powers, start: datetime, end: datetime, tz) -> None:
    """Add one charge's energy to a profile of 2 x 12 x 24 hours (see profile_index)."""
    for local, k in _hourly(powers, start, end, tz):
        profile[profile_index(local.weekday() >= 5, local.month, local.hour)] += k


def profile_index(weekend: bool, month: int, hour: int) -> int:
    return (12 * weekend + month - 1) * 24 + hour


def profile_cost(plan: dict | None, profile: list[float], on: date) -> float | None:
    """What the energy in `profile` costs on `plan` at the prices in force on `on` (its
    oldest version when it starts later). None for no plan."""
    if not valid_plan(plan):
        return None
    v = version_at(on, plan) or min(plan["versions"], key=lambda x: x["from"])
    prices = [float(p["price"]) for p in v["periods"]]
    total = 0.0
    for weekend, grid in ((False, v["weekday"]), (True, v["weekend"])):
        for m in range(12):
            for h in range(24):
                total += profile[profile_index(weekend, m + 1, h)] * prices[grid[m][h]]
    return total


def session_cost(powers: list[tuple[datetime, datetime, float]], start: datetime, end: datetime, tz, plan: dict | None = None) -> dict | None:
    """Price a charge on the plan in force (or `plan`): kWh per period key and per role, and
    the cost. None when no plan covers the charge's start."""
    version = version_at(start.astimezone(tz), plan)
    if version is None:
        return None
    parts = split_kwh(powers, start, end, tz, version)
    prices = {p["key"]: float(p["price"]) for p in version["periods"]}
    dollars = sum(parts[k] * prices[k] for k in parts)
    total = sum(parts.values())
    name = (plan.get("name") if plan is not None else current_plan()) or "Home"
    return {
        "kwh": parts,
        "roles": role_split(version, parts),
        "labels": {p["key"]: p.get("label") or p["key"] for p in version["periods"]},
        "total_kwh": total,
        "cost": dollars,
        "avg_rate": (dollars / total) if total > 0 else None,
        "plan": name,
    }
