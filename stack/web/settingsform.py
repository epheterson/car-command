"""What each Settings section owns, and how its form becomes stored values.

Settings was one form and one save that wrote every key, defaulting whatever the form did not
carry; split into pages, that save would reset every section but the one on screen. Here each
section owns a fixed set of keys, and parse() returns only those, and of those only the ones
whose field was on the page: a field that was not shown keeps its value. Figures shown rounded
or in the install's own units come back unchanged when the page is saved unchanged. Pure: the
route brings the form, the settings in force and the alert channels."""

from __future__ import annotations

import re
from datetime import time as dtime

import charge_hold
import clockfmt
import cost
import settings as settings_mod

SECTIONS: tuple[tuple[str, str], ...] = (
    ("cars", "Cars and sources"),
    ("money", "Money"),
    ("energy", "Powerwall"),
    ("alerts", "Alerts"),
    ("commands", "Commands"),
    ("look", "Units and look"),
    ("maps", "Maps"),
    ("data", "Data"),
    ("access", "Sign-in and API"),
)
LABELS = dict(SECTIONS)
# The Settings landing: every section once, in groups, each row with a glance at its value.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Your cars", ("cars", "money", "energy", "alerts", "commands")),
    ("This site", ("look", "maps", "data")),
    ("Access", ("access",)),
)
CHANNEL_NAMES = {"discord": "Discord", "telegram": "Telegram", "email": "Email", "pushover": "Pushover", "webhook": "Webhook"}


def hidden_rows(st: dict, *, tesla: bool, commandable: bool, energy_on: bool = False) -> set[str]:
    """Settings sections kept off the landing and the sidebar. Powerwall always: it belongs to
    the Tesla account and opens from that account's row in Cars and sources (no
    Powerwall at the top of Settings). Commands needs a Tesla or a connector car. Both stay
    open at their URL."""
    out = {"energy"}
    if not (tesla or commandable or st.get("commands_on")):
        out.add("commands")
    return out


def index(st: dict, cars: int, channels, sign_in: bool, energy: str = "", hide=()) -> list[dict]:
    """The Settings landing's groups of rows. Pure: the route brings what each row previews."""
    plan = st.get("rate_plan") if cost.valid_plan(st.get("rate_plan")) else None
    preview = {
        "cars": f"{cars} car{'' if cars == 1 else 's'}" if cars else "None yet",
        "money": plan["name"] if plan else "No plan",
        "energy": energy,
        "alerts": ", ".join(CHANNEL_NAMES.get(c, c) for c in channels) or "Off",
        "commands": "On" if st.get("commands_on") else "Off",
        "look": f"{'km' if st.get('units') == 'metric' else 'mi'} · {st.get('clock') or '12'}-hour",
        "maps": "",
        "data": "",
        "access": "Sign-in on" if sign_in else "Sign-in off",
    }
    return [{"title": title, "rows": [{"key": k, "label": LABELS[k], "value": preview[k]} for k in keys if k not in hide]} for title, keys in GROUPS]


OWNS: dict[str, tuple[str, ...]] = {
    "cars": ("car_names",),  # renames have their own route; pack sizes live on the drives table, beside the car
    "money": ("rate_plan", "manual_plan", "dc_price", "dc_per_min", "sc_price", "sc_per_min", "gas_mpg", "gas_price"),
    "energy": (),  # the Tesla site and the backfill have their own forms and state
    "alerts": ("alert_routes", "alert_routed", "tire_low_psi", "tire_high_psi", "plug_reminder_at", "plug_reminder_below", "summary_at"),
    "look": ("currency", "units", "theme", "week_start", "clock", "date_order", "timezone"),
    "commands": ("commands_on", "command_tiers", "charge_hold", "fleet_credit", "fleet_rates", "fleet_budget_on"),
    "maps": ("public_tiles", "public_geocoder", "satellite", "terrain", "maps_offer"),  # maps_offer: the one-time offer after the first import, see mapsoffer.py
    "data": ("min_drive_mi", "min_charge_kwh", "weather"),
    "access": (),  # the password and the API tokens have their own tables
}
# settings.py owns the defaults; settingsform re-exports the name the tests and this
# module's docstring expect.
DEFAULTS = settings_mod.DEFAULTS
# Stored but no longer edited: muted_alerts predates alert routing and is read only as a
# fallback; plan, rates and schedule predate plans and are read once, to make the first one.
RETIRED = ("muted_alerts", "plan", "rates", "schedule")

# Fast charging away from home: every other network per kWh and per minute, then Superchargers.
FAST_KEYS = ("dc_price", "dc_per_min", "sc_price", "sc_per_min")

KM = 1.609344
LITRES = 3.78541  # per US gallon
L100 = 235.215  # mpg <-> L/100 km
PSI_PER_BAR = 14.5037738


def hhmm(value) -> dtime | None:
    """A time of day as typed on the page: "21:00", "9:30 pm", "9pm" (clockfmt.parse_tod)."""
    got = clockfmt.parse_tod(value)
    return dtime(*got) if got else None


def number(text) -> float | None:
    try:
        return float(str(text).strip()) if text not in (None, "") else None
    except ValueError:
        return None


def bounded(text, lo: float, hi: float) -> float | None:
    v = number(text)
    return v if v is not None and lo <= v <= hi else None


def converted(st: dict) -> dict[str, str]:
    """Fields a metric install sees in its own units, exactly as the page shows them. An
    imperial install sees the stored figures, so this is empty for it."""
    if st.get("units") != "metric":
        return {}
    mi = st.get("min_drive_mi")
    return {
        "gas_l100": f"{L100 / st['gas_mpg']:.1f}" if st.get("gas_mpg") else "",
        "gas_ppl": f"{st['gas_price'] / LITRES:.2f}" if st.get("gas_price") else "",
        "tire_low_bar": f"{st['tire_low_psi'] / PSI_PER_BAR:.2f}" if st.get("tire_low_psi") else "",
        "tire_high_bar": f"{st['tire_high_psi'] / PSI_PER_BAR:.2f}" if st.get("tire_high_psi") else "",
        "min_drive_km": f"{(0.1 if mi is None else mi) * KM:.1f}",
    }


def parse(section: str, form, was: dict, channels=(), kinds=()) -> dict:
    """The stored values this section's form sets. `form` is the posted form, `was` the
    settings in force, `channels` the alert channels set up, `kinds` the alert kinds."""
    if section not in OWNS:
        raise KeyError(section)
    out: dict = {}
    shown = converted(was)

    def on_page(field: str) -> bool:
        return field in form

    def changed(field: str) -> bool:
        # a converted field that comes back exactly as shown was not touched
        return on_page(field) and (form.get(field) or "").strip() != shown.get(field)

    if section == "money":
        plan = money_plan(form, was.get("rate_plan"))
        if plan is not None:
            out["rate_plan"] = plan
        for key in FAST_KEYS:
            if on_page(key):
                out[key] = bounded(form.get(key), 0.01, 5.0)
        # Fuel is kept in mpg and per gallon; a metric install's page shows L/100 km and per litre.
        # The field names say which unit was typed, so a page left open across a units switch
        # is read by them, not by the units now in force.
        if changed("gas_l100"):
            l100 = number(form.get("gas_l100"))
            out["gas_mpg"] = bounded(f"{L100 / l100:.3f}", 5.0, 150.0) if l100 else None
        if changed("gas_ppl"):
            ppl = number(form.get("gas_ppl"))
            out["gas_price"] = bounded(f"{ppl * LITRES:.4f}", 0.10, 50.0) if ppl else None
        for key, lo, hi in (("gas_mpg", 5.0, 150.0), ("gas_price", 0.10, 50.0)):
            if on_page(key):
                out[key] = bounded(form.get(key), lo, hi)
    elif section == "alerts":
        if channels and on_page("routes_shown"):  # the routing table is on the page only when a channel is set up; unticked boxes post nothing, so the table sends a marker
            listed = set(form["routes_kinds"].split(",")) if "routes_kinds" in form else None  # a kind the page left out keeps its routing
            kept = was.get("alert_routes") if isinstance(was.get("alert_routes"), dict) else {}
            out["alert_routes"] = {k: [c for c in channels if form.get(f"route_{k}_{c}") == "on"] for k in kinds if listed is None or k in listed}
            out["alert_routes"].update({k: v for k, v in kept.items() if listed is not None and k in kinds and k not in listed})
            out["alert_routed"] = sorted(set(was.get("alert_routed") or []) | set(channels))  # every channel on the page was seen, so chosen
        # Kept in psi; a metric install's page shows bar, and the field name says which was typed.
        for key in ("tire_low", "tire_high"):
            if changed(f"{key}_bar"):
                bar = number(form.get(f"{key}_bar"))
                out[f"{key}_psi"] = bounded(f"{bar * PSI_PER_BAR:.2f}", 10, 90) if bar else None
            if on_page(f"{key}_psi"):
                out[f"{key}_psi"] = bounded(form.get(f"{key}_psi"), 10, 90)
        if on_page("plug_reminder_at"):
            at = hhmm(form.get("plug_reminder_at"))
            out["plug_reminder_at"] = at.strftime("%H:%M") if at else None
        if on_page("plug_reminder_below"):
            out["plug_reminder_below"] = bounded(form.get("plug_reminder_below"), 1.0, 100.0)
        if on_page("summary_at"):
            sat = hhmm(form.get("summary_at"))
            out["summary_at"] = sat.strftime("%H:%M") if sat else "07:00"
    elif section == "commands":
        out.update(commands_form(form, was))
    elif section == "look":
        if on_page("currency"):
            out["currency"] = (form.get("currency") or "$").strip()[:4] or "$"
        if on_page("units"):
            out["units"] = "metric" if form.get("units") == "metric" else "imperial"
        if on_page("theme"):
            out["theme"] = form.get("theme") if form.get("theme") in ("light", "dark") else "system"
        if on_page("week_start"):
            out["week_start"] = "sunday" if form.get("week_start") == "sunday" else "monday"
        if on_page("clock"):
            out["clock"] = "24" if form.get("clock") == "24" else "12"
        if on_page("date_order"):
            out["date_order"] = "dmy" if form.get("date_order") == "dmy" else "mdy"
        if on_page("timezone"):
            # the page shows the zone in force; one left as the page showed it (timezone_was) stays blank, following the container
            zone = settings_mod.valid_zone(form.get("timezone"))
            out["timezone"] = None if zone and zone == settings_mod.valid_zone(form.get("timezone_was")) else zone
    elif section == "data":
        if on_page("min_drive_mi"):
            v = bounded(form.get("min_drive_mi"), 0.0, 50.0)
            out["min_drive_mi"] = 0.1 if v is None else v
        if changed("min_drive_km"):
            km = bounded(form.get("min_drive_km"), 0.0, 50.0 * KM)
            out["min_drive_mi"] = 0.1 if km is None else round(km / KM, 4)
        if on_page("min_charge_kwh"):
            v = bounded(form.get("min_charge_kwh"), 0.0, 50.0)
            out["min_charge_kwh"] = 0.0 if v is None else v
        if on_page("weather_shown"):  # an unticked box posts nothing, so the page sends a marker beside it
            out["weather"] = form.get("weather") == "on"
    elif section == "maps":
        # An unticked box posts nothing, so each switch sends a marker beside it.
        if on_page("public_tiles_shown"):
            out["public_tiles"] = form.get("public_tiles") == "on"
        if on_page("public_geocoder_shown"):
            out["public_geocoder"] = form.get("public_geocoder") == "on"
        if on_page("satellite_shown"):
            out["satellite"] = form.get("satellite") == "on"
        if on_page("terrain_shown"):
            out["terrain"] = form.get("terrain") == "on"
    return out


REFRESH = ("monthly", "quarterly", "off")


def maps_service(form) -> dict | None:
    """The local maps add-on's own settings from Settings › Maps, or None when the page did not
    show them or they came back as shown (auto_was, refresh_was): a save that changes only a
    public switch makes no call to the add-on."""
    if "maps_service_shown" not in form:
        return None
    kw: dict = {"auto": form.get("auto") == "on"}
    if form.get("refresh") in REFRESH:
        kw["refresh"] = form.get("refresh")
    was = {"auto": form.get("auto_was") == "1", "refresh": form.get("refresh_was")}
    return None if all(kw[k] == was[k] for k in kw) else kw


def capacity_changes(form, current: dict[str, float | None], fallback: dict[str, float | None] | None = None) -> dict[str, float | None]:
    """vin -> the pack size to store, for each known car whose field came back different from
    how the page showed it (one decimal, blank when unknown). Blank hands it back to the source:
    the figure in `fallback` (what the car reports, or its model's), else nothing."""
    fallback = fallback or {}
    out: dict[str, float | None] = {}
    for vin, cap in current.items():
        field = f"capacity_{vin}"
        if field not in form:
            continue
        posted = (form.get(field) or "").strip()
        if posted == (f"{cap:.1f}" if cap else ""):
            continue
        out[vin] = bounded(posted, 5, 400) if posted else fallback.get(vin)
    return out


MAX_ROWS = 48  # rows read back per table; a page never shows near this many


def _indexes(form, prefix: str) -> list[int]:
    """The row numbers posted under prefix{n}, in order. Read from the form rather than
    counted to a fixed limit: a plan's rows come back from its grid, and overlapping rows
    can come back as more than were typed, so a fixed count dropped the ones past it."""
    out = set()
    for key in form:
        rest = key[len(prefix) :] if key.startswith(prefix) else ""
        if rest.isdigit() and int(rest) < MAX_ROWS:
            out.add(int(rest))
    return sorted(out)


def money_plan(form, was: dict | None) -> dict | None:
    """The home plan the Money form describes, or None to keep the saved one.

    The rows editor (a plan that fits it, or no plan yet) sends dated rate rows and schedule
    rows, which compile to a manual plan; a blank rate table keeps the saved plan. A row or
    rule ticked Remove (del{n}, sdel{n}) is left out, as a blanked date or hour always was. A plan
    shown as a grid sends a price per version and period instead (pv{version}_{period}).
    The name is the label on every cost, so a rename alone is saved too."""
    name = (form.get("plan") or "").strip()[:40] if "plan" in form else None
    if "from0" in form:
        rows = [
            {"from": form.get(f"from{i}", ""), "off_peak": form.get(f"off{i}", ""), "part_peak": form.get(f"part{i}", ""), "peak": form.get(f"peak{i}", "")}
            for i in _indexes(form, "from")
            if form.get(f"del{i}") != "on"
        ]
        rates = settings_mod.parse_rate_rows(rows)
        if rates:
            shown = _indexes(form, "sp")
            sched_rows = [
                {
                    "period": form.get(f"sp{i}"),
                    "days": form.get(f"sd{i}"),
                    "start": form.get(f"ss{i}"),
                    "end": form.get(f"se{i}"),
                    "months": [m for m in range(1, 13) if form.get(f"sm{i}_{m}") == "on"],
                }
                for i in shown
                if form.get(f"sdel{i}") != "on"
            ]
            # rules on the page, even all of them removed, are the schedule; none on the page keeps the saved one
            schedule = cost.clean_schedule(sched_rows) if shown else cost.plan_rows(was)[1]
            plan = cost.compile_rows(name or (was or {}).get("name") or "Home", rates, schedule, utility=(was or {}).get("utility"))
            return None if plan == was else plan
    if not was:
        return None
    plan = was
    prices = {}
    for vi, v in enumerate(was.get("versions") or []):
        for pi, p in enumerate(v.get("periods") or []):
            price = bounded(form.get(f"pv{vi}_{pi}"), 0.0, 9.999) if f"pv{vi}_{pi}" in form else None
            if price is not None and price != float(p.get("price") or 0):
                prices[(vi, pi)] = price
    if prices:
        plan = cost.with_prices(was, prices)
    if name and name != was.get("name"):
        plan = {**plan, "name": name}
    return plan if plan is not was else None


def reprices(values: dict, was: dict) -> bool:
    """A change to the home plan's prices or hours re-prices every home charge; a rename does not."""
    return "rate_plan" in values and cost.prices_differ(values["rate_plan"], was.get("rate_plan"))


def fast_rate_changed(values: dict, was: dict) -> bool:
    return any(k in values and values[k] != was.get(k) for k in FAST_KEYS)


def fast_rates(st: dict) -> dict[str, float | None]:
    """The four fast-charging prices as numbers (None for blank), as the pricers take them."""
    return {k: (float(st[k]) if st.get(k) else None) for k in FAST_KEYS}


VIN_FIELD = re.compile(r"^[A-Za-z0-9]{1,32}$")


def money_field(value) -> float | None:
    """A non-negative amount of dollars, or None for blank or anything else."""
    try:
        v = float(str(value).strip().lstrip("$"))
    except ValueError:
        return None
    return round(v, 6) if 0 <= v < 100000 else None


def commands_form(form, was: dict) -> dict:
    """Settings › Commands: the switch, each car's tier and each car's charging hold. Each part
    only when its marker was on the page (an unticked box posts nothing); cars are named by
    the fields' suffixes, and a suffix that is not a VIN's shape is ignored."""
    out: dict = {}
    if "commands_shown" in form:
        out["commands_on"] = form.get("commands_on") == "on"
    if "tiers_shown" in form:
        tiers = {}
        for field in form:
            vin = field[5:] if field.startswith("tier_") else ""
            if VIN_FIELD.match(vin) and str(form.get(field)) in ("2", "3"):
                tiers[vin] = int(form.get(field))
        out["command_tiers"] = tiers
    if "hold_shown" in form:
        holds = {}
        for field in form:
            vin = field[5:] if field.startswith("hold_") and not field.startswith(("hold_from_", "hold_until_", "hold_shown")) else ""
            if VIN_FIELD.match(vin) and form.get(field) == "on":
                start, end = hhmm(form.get(f"hold_from_{vin}")), hhmm(form.get(f"hold_until_{vin}"))
                holds[vin] = {"from": start.strftime("%H:%M") if start else charge_hold.DEFAULTS["from"], "until": end.strftime("%H:%M") if end else "00:00"}
        out["charge_hold"] = holds
    if "fleet_shown" in form:
        out["fleet_budget_on"] = form.get("fleet_budget_on") == "on"
        credit = money_field(form.get("fleet_credit"))
        if credit is not None:
            out["fleet_credit"] = credit
        rates = dict(was.get("fleet_rates") or DEFAULTS["fleet_rates"])
        for k in DEFAULTS["fleet_rates"]:
            v = money_field(form.get(f"fleet_rate_{k}"))
            if v is not None:
                rates[k] = v / 1000 if k == "signals" else v  # signals are priced and shown per thousand
        out["fleet_rates"] = rates
    return out
