"""The tools the MCP server offers, read-only, in the shape a model reads best: a few figures, names
instead of ids where a name exists, and no more rows than asked for.

Every answer is built from what the site already computes (the facts tables behind the drive,
charge, Stats, Battery, Records, Routes and Software pages), reached through `Data`, the callables
app.py hands in; nothing here opens a connection, so the tests run on plain dicts.

Privacy, by construction. A token's `Scope` says which car it may see and whether it may see
coordinates. Without them an answer carries a place's name only when the owner named the place;
anywhere else it says the city, never the street, and no latitude, longitude, speed or heading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable

from mcpserver import Tool, ToolError

DEFAULT_ROWS, MAX_ROWS, MAX_OFFSET = 15, 40, 400
UNNAMED = "Unnamed spot"


@dataclass(frozen=True)
class Scope:
    """What one token may see: `vin` its car (None, every car) and `coords` whether places come with coordinates."""

    vin: str | None = None
    coords: bool = False


@dataclass
class Data:
    """The site's own readers, as app.py wires them. Each takes `vin` (None: every car)."""

    cars: Callable[[], list[dict]]  # [{vin, name}]
    status: Callable[[str], dict]  # one car as /api/now has it
    drives: Callable[..., list[dict]]  # (vin, limit, since, until)
    charges: Callable[..., list[dict]]
    drive: Callable[[str | None, int], dict | None]
    charge: Callable[[str | None, int], dict | None]
    summarize: Callable[..., list[dict]]  # (vin, by, since, until): summarize_sql's rows
    lifetime: Callable[[str], dict | None]  # lifetime.report of one car
    records: Callable[..., list[dict]]  # (vin, since, until): records.fold's groups
    routes: Callable[..., dict]  # (vin, days): {routes, destinations, drives}
    software: Callable[[dict], dict]  # {vin, name}: the Software page's block
    now: Callable[[], datetime]  # local, aware
    tz: object
    currency: str = "$"


# ---- time -------------------------------------------------------------------------------------

PERIODS = (
    "today",
    "yesterday",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "this_year",
    "last_year",
    "last_7_days",
    "last_30_days",
    "last_90_days",
    "last_12_months",
    "all",
)
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR = re.compile(r"^\d{4}$")


def _check_year(year: int, now: datetime) -> None:
    if not 2000 <= year <= now.year + 1:
        raise ToolError(f"year {year} is out of range: use 2000 to {now.year + 1}")


def _midnight(day: date, tz) -> datetime:
    return datetime.combine(day, time(), tz)


def moment(text: str, tz, end: bool = False) -> datetime:
    """An ISO date or date-time as an aware local moment. A bare date is its midnight, or, as an end, the
    midnight after it, so "until 2026-03-31" takes in the whole of the 31st."""
    text = text.strip()
    try:
        if len(text) == 10:
            return _midnight(date.fromisoformat(text) + timedelta(days=1 if end else 0), tz)
        got = datetime.fromisoformat(text)
    except (ValueError, OverflowError):
        raise ToolError(f"{text[:40]!r} is not a date: use YYYY-MM-DD, or YYYY-MM-DDTHH:MM for a moment") from None
    return got if got.tzinfo else got.replace(tzinfo=tz)


def period_window(period: str | None, since: str | None, until: str | None, now: datetime, tz, week_start: int = 0) -> tuple[datetime | None, datetime | None, str]:
    """(since, until, label) for a named period, a month (YYYY-MM) or year (YYYY), or the given
    dates; the end is exclusive and None means open. A named period wins over since/until."""
    today = now.date()
    if period:
        p = period.strip().lower()
        m = _MONTH.match(p)
        if m:
            if not 1 <= int(m[2]) <= 12:
                raise ToolError(f"{period!r} is not a month: use YYYY-MM")
            _check_year(int(m[1]), now)
            first = date(int(m[1]), int(m[2]), 1)
            return _midnight(first, tz), _midnight(_next_month(first), tz), p
        if _YEAR.match(p):
            _check_year(int(p), now)
            return _midnight(date(int(p), 1, 1), tz), _midnight(date(int(p) + 1, 1, 1), tz), p
        if p not in PERIODS:
            raise ToolError(f"period must be one of {', '.join(PERIODS)}, a month like 2026-03 or a year like 2025")
        if p == "all":
            return None, None, "all time"
        if p == "today":
            return _midnight(today, tz), None, p
        if p == "yesterday":
            return _midnight(today - timedelta(days=1), tz), _midnight(today, tz), p
        if p in ("this_week", "last_week"):
            start = today - timedelta(days=(today.weekday() - week_start) % 7)
            if p == "this_week":
                return _midnight(start, tz), None, p
            return _midnight(start - timedelta(days=7), tz), _midnight(start, tz), p
        if p in ("this_month", "last_month"):
            first = today.replace(day=1)
            if p == "this_month":
                return _midnight(first, tz), None, p
            return _midnight((first - timedelta(days=1)).replace(day=1), tz), _midnight(first, tz), p
        if p in ("this_year", "last_year"):
            if p == "this_year":
                return _midnight(date(today.year, 1, 1), tz), None, p
            return _midnight(date(today.year - 1, 1, 1), tz), _midnight(date(today.year, 1, 1), tz), p
        days = {"last_7_days": 7, "last_30_days": 30, "last_90_days": 90, "last_12_months": 365}[p]
        return now - timedelta(days=days), None, p
    lo = moment(since, tz) if since else None
    hi = moment(until, tz, end=True) if until else None
    for got in (lo, hi):
        if got:
            _check_year(got.year, now)
    if lo and hi and hi <= lo:
        raise ToolError("until must be after since")
    label = f"{since or 'the start'} to {until or 'now'}" if (since or until) else "all time"
    return lo, hi, label


def _next_month(first: date) -> date:
    return date(first.year + (first.month == 12), first.month % 12 + 1, 1)


def when(dt: datetime | None, tz) -> str | None:
    """Local time to the minute: 2026-03-14 08:05."""
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M") if dt else None


# ---- cars and scope ---------------------------------------------------------------------------


def visible(data: Data, scope: Scope) -> list[dict]:
    return [c for c in data.cars() if scope.vin is None or c["vin"] == scope.vin]


def target(data: Data, scope: Scope, car: str | None) -> tuple[str | None, list[dict]]:
    """(vin to query or None for the whole garage, the cars that covers). A token for one car is that
    car whatever it asks; a car it cannot see is answered as one that does not exist."""
    cars = visible(data, scope)
    if not car:
        return scope.vin, cars
    ask = car.strip().casefold()
    exact = [c for c in cars if ask in (c["vin"].casefold(), (c["name"] or "").casefold())]
    near = [c for c in cars if ask and ask in (c["name"] or "").casefold()]
    hit = exact or (near if len(near) == 1 else [])
    if not hit:
        # one answer for a car that does not exist and one this token may not see
        raise ToolError(f"no car called {car[:40]!r}; the cars are: {', '.join(c['name'] for c in cars) or 'none'}")
    return hit[0]["vin"], hit


# ---- shaping ----------------------------------------------------------------------------------


def num(x, nd: int = 1):
    return None if x is None else round(float(x), nd)


def place_name(label: str | None, ref: dict | None, city: str | None, scope: Scope) -> str | None:
    """A place as a token may read it: the name the owner gave, else the city; the street-level name the
    site made up only with coordinates."""
    if scope.coords:
        return label
    if ref and not ref.get("new"):
        return label
    return city or (UNNAMED if label else None)


def page(rows: list[dict], limit: int, offset: int, shape: Callable[[dict], dict]) -> dict:
    """One page of an already-fetched newest-first list: `offset`+`limit`+1 rows were read, so the
    extra one says whether there is more."""
    got = rows[offset : offset + limit]
    out = {"rows": [shape(r) for r in got]}
    if len(rows) > offset + limit:
        out["next_offset"] = offset + limit
    return out


def drive_row(r: dict, tz, scope: Scope, cur: str) -> dict:
    row = {
        "id": r["id"],
        "car": r.get("car_name"),
        "start": when(r["start_date"], tz),
        "minutes": round(r["duration"].total_seconds() / 60) if r.get("duration") else None,
        "miles": num(r.get("miles")),
        "from": place_name(r.get("from"), r.get("from_ref"), r.get("start_city"), scope),
        "to": place_name(r.get("to"), r.get("to_ref"), r.get("dest_city"), scope) if r.get("destination_date") else "in progress",
        "kwh": num(r.get("kwh_used")),
        "wh_per_mile": num(r.get("wh_per_mi"), 0),
        "battery": [num(r.get("level_start"), 0), num(r.get("level_end"), 0)],
        "cost": num(r.get("cost"), 2),
    }
    if r.get("tag"):
        row["tag"] = r["tag"]
    if scope.coords:
        row["start_at"] = [r.get("start_position_latitude"), r.get("start_position_longitude")]
        row["end_at"] = [r.get("destination_position_latitude"), r.get("destination_position_longitude")]
    return {k: v for k, v in row.items() if v is not None}


def ends(r: dict, scope: Scope) -> dict:
    return {"from": place_name(r.get("from"), r.get("from_ref"), r.get("start_city"), scope), "to": place_name(r.get("to"), r.get("to_ref"), r.get("dest_city"), scope)}


def charge_row(r: dict, tz, scope: Scope) -> dict:
    row = {
        "id": r["id"],
        "car": r.get("car_name"),
        "start": when(r["session_start_date"], tz),
        "minutes": round(r["duration"].total_seconds() / 60) if r.get("duration") else None,
        "where": place_name(r.get("where"), r.get("where_ref"), r.get("city"), scope),
        "kind": r.get("charging_type"),
        "battery": [num(r.get("start_level"), 0), num(r.get("end_level"), 0)],
        "kwh": num(r.get("kwh")),
        "cost": num(r.get("cost"), 2),
        "cost_is_estimate": True if r.get("cost_estimate") else None,
        "home": True if r.get("at_home") else None,
    }
    if scope.coords:
        row["at"] = [r.get("session_position_latitude"), r.get("session_position_longitude")]
    return {k: v for k, v in row.items() if v is not None}


def _limit(args: dict) -> tuple[int, int]:
    return int(args.get("limit") or DEFAULT_ROWS), int(args.get("offset") or 0)


# ---- the tools --------------------------------------------------------------------------------

CAR = {"type": "string", "description": "A car's name from list_cars. Leave out for every car."}
PERIOD = {"type": "string", "description": f"One of {', '.join(PERIODS)}, a month like 2026-03 or a year like 2025. Overrides since/until."}
SINCE = {"type": "string", "description": "Start, YYYY-MM-DD or YYYY-MM-DDTHH:MM, local time."}
UNTIL = {"type": "string", "description": "End, same form; a bare date includes that whole day."}
LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_ROWS, "description": f"Rows to return, newest first (default {DEFAULT_ROWS})."}
OFFSET = {"type": "integer", "minimum": 0, "maximum": MAX_OFFSET, "description": "Rows to skip, from next_offset of the last answer."}


def build(data: Data) -> dict[str, Tool]:
    """The tool table for one Data."""
    tz = data.tz

    def window(args: dict) -> tuple[datetime | None, datetime | None, str]:
        return period_window(args.get("period"), args.get("since"), args.get("until"), data.now(), tz)

    def list_cars(args, scope):
        # the VIN is the owner's identifier, not the model's: names are what the other tools take (a typed VIN still matches)
        return [{"name": c["name"], **({"vin": c["vin"]} if scope.coords else {})} for c in visible(data, scope)]

    def current_status(args, scope):
        _, cars = target(data, scope, args.get("car"))
        out = []
        for c in cars:
            s = data.status(c["vin"])
            row = {
                "car": c["name"],
                "state": s.get("status"),
                "battery_pct": num(s.get("soc"), 0),
                "range_mi": num(s.get("range_mi"), 0),
                "plugged_in": s.get("plugged"),
                "charging_kw": s.get("charging_power_kw") if s.get("charging_power_kw") else None,
                "charge_target_pct": s.get("charge_target") if s.get("status") == "Charging" else None,
                "minutes_to_target": s.get("charge_minutes_left"),
                "locked": s.get("locked"),
                "odometer_mi": num(s.get("odometer_mi"), 0),
                "outside_f": num(s.get("outside_f"), 0),
                "software": s.get("software"),
                "place": s.get("place_named") or s.get("locality"),
                "at_home": s.get("at_home"),
                "reported": s.get("updated_age"),
            }
            if scope.coords:
                row.update(lat=s.get("lat"), lon=s.get("lon"), address=s.get("address"), speed_mph=num(s.get("speed_mph"), 0), heading=s.get("heading"))
            out.append({k: v for k, v in row.items() if v is not None})
        return out

    def list_drives(args, scope):
        vin, _ = target(data, scope, args.get("car"))
        lo, hi, label = window(args)
        limit, offset = _limit(args)
        rows = data.drives(vin, offset + limit + 1, lo, hi)
        return {"period": label, **page(rows, limit, offset, lambda r: drive_row(r, tz, scope, data.currency))}

    def list_charges(args, scope):
        vin, _ = target(data, scope, args.get("car"))
        lo, hi, label = window(args)
        limit, offset = _limit(args)
        rows = data.charges(vin, offset + limit + 1, lo, hi)
        return {"period": label, "currency": data.currency, **page(rows, limit, offset, lambda r: charge_row(r, tz, scope))}

    def get_drive(args, scope):
        r = data.drive(scope.vin, args["id"])
        if r is None:
            raise ToolError(f"no drive {args['id']}")
        row = drive_row(r, tz, scope, data.currency)
        if r.get("destination_date"):
            row["end"] = when(r["destination_date"], tz)
        extra = {
            "odometer": [num(r.get("start_odometer"), 0), num(r.get("destination_odometer"), 0)],
            "rated_range_used_mi": num(r.get("rated_used"), 0),
            "top_mph": num(r.get("max_mph"), 0),
            "outside_f": num(r.get("temp_f"), 0),
            "weather": r.get("weather"),
            "note": r.get("note"),
            "highest_ft": num(r["elev_max_m"] * 3.28084, 0) if r.get("elev_max_m") is not None else None,
            "lowest_ft": num(r["elev_min_m"] * 3.28084, 0) if r.get("elev_min_m") is not None else None,
            "cost_is_estimate": True if r.get("cost_estimate") else None,
        }
        return {**row, **{k: v for k, v in extra.items() if v is not None and v != [None, None]}}

    def get_charge(args, scope):
        r = data.charge(scope.vin, args["id"])
        if r is None:
            raise ToolError(f"no charge {args['id']}")
        row = charge_row(r, tz, scope)
        if r.get("session_end_date"):
            row["end"] = when(r["session_end_date"], tz)
        extra = {
            "range_added_mi": num(r.get("range_added"), 0),
            "plan": r.get("rate_plan"),
            "cost_source": r.get("cost_source"),
            "off_peak_kwh": num(r.get("off_peak_kwh")),
            "part_peak_kwh": num(r.get("part_peak_kwh")),
            "peak_kwh": num(r.get("peak_kwh")),
            "solar_kwh": num(r.get("src_solar_kwh")),
            "powerwall_kwh": num(r.get("src_battery_kwh")),
            "grid_kwh": num(r.get("src_grid_kwh")),
        }
        return {**row, "currency": data.currency, **{k: v for k, v in extra.items() if v is not None}}

    def stats(args, scope):
        vin, cars = target(data, scope, args.get("car"))
        lo, hi, label = window({**args, "period": args.get("period") or ("this_month" if not (args.get("since") or args.get("until")) else None)})
        by = args.get("by")
        rows = data.summarize(vin, by or "month", lo, hi)
        t = {k: sum(r[k] for r in rows) for k in ("drives", "miles", "drive_h", "used_kwh", "drive_cost", "charges", "added_kwh", "cost", "priced", "avg_cost")}
        out = {
            "period": label,
            "cars": [c["name"] for c in cars] if vin or len(cars) > 1 else None,
            "drives": t["drives"],
            "miles": num(t["miles"]),
            "driving_hours": num(t["drive_h"]),
            "energy_used_kwh": num(t["used_kwh"]),
            "wh_per_mile": num(t["used_kwh"] * 1000 / t["miles"], 0) if t["miles"] > 1 and t["used_kwh"] > 0 else None,
            "miles_per_kwh": num(t["miles"] / t["used_kwh"], 2) if t["miles"] > 1 and t["used_kwh"] > 0 else None,
            "charges": t["charges"],
            "charged_kwh": num(t["added_kwh"]),
            "charging_cost": num(t["cost"], 2),
            "charging_cost_includes_estimates": True if t["avg_cost"] > 0 else None,
            "charges_without_a_price": t["charges"] - t["priced"] or None,
            "currency": data.currency,
        }
        if by:
            out["by"] = by
            out["buckets"] = [
                {"from": r["period"].isoformat(), "drives": r["drives"], "miles": num(r["miles"]), "charged_kwh": num(r["added_kwh"]), "charging_cost": num(r["cost"], 2)}
                for r in sorted(rows, key=lambda r: r["period"])[-60:]
            ]
        return {k: v for k, v in out.items() if v is not None}

    def battery_health(args, scope):
        _, cars = target(data, scope, args.get("car"))
        out = []
        for c in cars:
            rep = data.lifetime(c["vin"])
            if rep is None:
                out.append({"car": c["name"], "note": "not enough charges to a high level yet to measure"})
                continue
            row = {
                "car": c["name"],
                "range_at_100pct_mi_now": num(rep["now"], 0),
                "first_mi": num(rep["first"], 0),
                "best_mi": num(rep["best"]["full"], 0),
                "charges_used": rep["charges"],
                "from": rep["first_at"].astimezone(tz).date().isoformat(),
                "to": rep["now_at"].astimezone(tz).date().isoformat(),
                "odometer_mi": [num(rep["odo"][0], 0), num(rep["odo"][1], 0)],
            }
            for key in ("best", "first"):
                got = rep["losses"].get(key)
                if got:
                    row[f"lost_since_{key}"] = {"mi": num(got["lost"], 0), "pct": num(got["pct"] * 100)}
            out.append(row)
        return out

    def records(args, scope):
        vin, _ = target(data, scope, args.get("car"))
        lo, hi, label = window(args)
        out = []
        for g in data.records(vin, lo, hi)[:12]:
            r = g["row"]
            what = ends(r, scope) if g["kind"] == "drive" else {"where": place_name(r.get("where"), r.get("where_ref"), r.get("city"), scope)}
            out.append(
                {
                    g["kind"]: r["id"],
                    "car": r.get("car_name"),
                    "on": when(r.get("start_date") or r.get("session_start_date"), tz),
                    **what,
                    "records": [{"record": t["label"], **record_value(t)} for t in g["titles"]],
                }
            )
        return {"period": label, "groups": out}

    def top_routes(args, scope):
        vin, _ = target(data, scope, args.get("car"))
        days = DAYS[args.get("span") or "all"]
        found = data.routes(vin, days)
        rows = []
        for r in found["routes"][: int(args.get("limit") or 10)]:
            a, b = (named(r["a"], scope), named(r["b"], scope))
            rows.append(
                {
                    "route": f"{a} (round trips)" if r["loop"] else f"{a} ⇄ {b}",
                    "drives": r["n"],
                    "typical_minutes": round(r["minutes"]),
                    "wh_per_mile": num(r.get("wh_per_mi"), 0),
                    "last": r["last"].astimezone(tz).date().isoformat(),
                    "tag": r.get("tag"),
                }
            )
        return {"span": args.get("span") or "all", "drives_considered": found["drives"], "routes": [{k: v for k, v in r.items() if v is not None} for r in rows]}

    def top_places(args, scope):
        vin, _ = target(data, scope, args.get("car"))
        found = data.routes(vin, DAYS[args.get("span") or "all"])
        rows = []
        for d in found["destinations"][: int(args.get("limit") or 10)]:
            row = {"place": named(d["place"], scope), "arrivals": d["n"], "last": d["last"].astimezone(tz).date().isoformat()}
            if scope.coords:
                row["at"] = [d["place"].get("latitude"), d["place"].get("longitude")]
            rows.append(row)
        return {"span": args.get("span") or "all", "places": rows}

    def software_versions(args, scope):
        _, cars = target(data, scope, args.get("car"))
        limit = int(args.get("limit") or 8)
        out = []
        for c in cars:
            b = data.software(c)
            versions = [
                {"version": r["version"], "installed": when(r["seen_at"], tz), "days": r.get("days"), "odometer_mi": num(r.get("odometer"), 0), "range_at_100pct_mi": num(r.get("full"), 0)}
                for r in b["rows"][:limit]
            ]
            head = {"car": c["name"], "updates_every_days": num(b["cadence"].get("every"), 0), "days_since_last_update": b["cadence"].get("since")}
            out.append({**{k: v for k, v in head.items() if v is not None}, "versions": [{k: v for k, v in r.items() if v is not None} for r in versions]})
        return out

    tools = [
        Tool("list_cars", "The cars this token can see, by name. Start here to learn the names the other tools take.", {}, list_cars),
        Tool(
            "current_status",
            "Where each car is at now: state (parked, driving, charging), battery %, range, plugged in, charge progress, locked, odometer and the place it is at (a name, or the city). Also answers 'where is the car parked'.",
            {"car": CAR},
            current_status,
        ),
        Tool(
            "list_drives",
            "Drives in a period, newest first, one line each: when, minutes, miles, from and to, kWh, Wh/mile, battery % before and after, cost. To find where a car was at a past moment, ask for one drive with until set to that moment: its 'to' is where the car was parked.",
            {"car": CAR, "period": PERIOD, "since": SINCE, "until": UNTIL, "limit": LIMIT, "offset": OFFSET},
            list_drives,
        ),
        Tool(
            "list_charges",
            "Charges in a period, newest first: when, minutes, where, kind (AC/DC), battery % before and after, kWh added, cost and whether the cost is an estimate.",
            {"car": CAR, "period": PERIOD, "since": SINCE, "until": UNTIL, "limit": LIMIT, "offset": OFFSET},
            list_charges,
        ),
        Tool(
            "get_drive",
            "One drive in full, by the id list_drives gave: the above plus end time, odometer, top speed, weather, elevation and notes.",
            {"id": {"type": "integer", "minimum": 1, "maximum": 2**31 - 1, "description": "The drive's id."}},
            get_drive,
            required=("id",),
        ),
        Tool(
            "get_charge",
            "One charge in full, by the id list_charges gave: the above plus end time, range added, rate plan, the peak/off-peak split and, at home, the sun/Powerwall/grid split.",
            {"id": {"type": "integer", "minimum": 1, "maximum": 2**31 - 1, "description": "The charge's id."}},
            get_charge,
            required=("id",),
        ),
        Tool(
            "stats",
            "Totals for a period (default this month): miles, drives, energy used, efficiency, charged kWh and what charging cost. Add by=day|week|month for a breakdown (the last 60 buckets).",
            {
                "car": CAR,
                "period": PERIOD,
                "since": SINCE,
                "until": UNTIL,
                "by": {"type": "string", "enum": ["day", "week", "month"], "description": "Also return the figures per day, week or month."},
            },
            stats,
        ),
        Tool(
            "battery_health",
            "Battery health from the range at 100 % measured at each charge, over the car's life: what it is now, the first and the best, and the loss since in miles and per cent.",
            {"car": CAR},
            battery_health,
        ),
        Tool(
            "records",
            "The records of a period (default all time): longest drive, top speed, most battery, coldest, most efficient, fastest charge and so on, each with the drive or charge that set it.",
            {"car": CAR, "period": PERIOD, "since": SINCE, "until": UNTIL},
            records,
        ),
        Tool(
            "top_routes",
            "The most-driven routes between places, with how often, the typical minutes and efficiency.",
            {"car": CAR, "span": SPAN, "limit": {"type": "integer", "minimum": 1, "maximum": 25}},
            top_routes,
        ),
        Tool("top_places", "The places drives most often end at, with arrivals and the last visit.", {"car": CAR, "span": SPAN, "limit": {"type": "integer", "minimum": 1, "maximum": 15}}, top_places),
        Tool(
            "software_versions",
            "The software versions each car has run: when each arrived, how many days it stayed, the odometer then and the range at 100 % on it; and how often updates come.",
            {"car": CAR, "limit": {"type": "integer", "minimum": 1, "maximum": 40, "description": "Versions to list, newest first (default 8)."}},
            software_versions,
        ),
    ]
    return {t.name: t for t in tools}


SPAN = {"type": "string", "enum": ["all", "year", "90d"], "description": "all time, the last 12 months or the last 90 days (default all)."}
DAYS = {"all": None, "year": 365, "90d": 90}

# record fmt -> (key, unit) for the value a record carries
_UNITS = {"dist": "mi", "pct": "%", "hours": "h", "speed": "mph", "low": "%", "temp": "F", "elev": "ft", "kw": "kW", "kwh": "kWh", "econ": "Wh/mi"}


def record_value(t: dict) -> dict:
    out = {"value": num(t["value"], 1 if t["fmt"] in ("dist", "hours", "kw", "kwh") else 0), "unit": _UNITS.get(t["fmt"], "")}
    if t.get("note"):
        out["among"] = f"drives of {t['note']:g} mi or more"
    return out


def named(place: dict, scope: Scope) -> str:
    """A route's or destination's place as the token may read it: the owner's name for it, and the site's
    own guess at one ('found' places, often a street) only with coordinates."""
    if place.get("auto") and not scope.coords:
        return "an unnamed spot"
    return place.get("name") or UNNAMED


INSTRUCTIONS = (
    "Car Command's data about this household's electric cars, read-only. Distances are miles, energy kWh, temperatures degrees F, money in the stated currency; "
    "times are local. Start with list_cars for names. Answers are summaries: ask for a period (period, or since/until) rather than everything."
)
