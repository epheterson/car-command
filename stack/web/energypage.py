"""What the energy pages say, from plain data: the Powerwall row on Now, the Settings › Energy
status lines, the bar on a charge. Nothing here reads a database or the network."""

from __future__ import annotations

from datetime import datetime

import energy_cloud

IDLE_W = 50.0  # under this a flow is not worth an arrow
SOURCES = {"cloud": "Tesla"}


def kw(watts) -> str:
    return f"{abs(watts) / 1000:.1f} kW" if watts is not None else "—"


def row(reading: dict | None) -> dict | None:
    """The Powerwall row on Now: battery level and four flows, each with the way it moves
    against the house (Grid ↓ is importing, ↑ exporting; Powerwall ↑ is charging, ↓ giving).
    None when there is no fresh reading, so the page shows nothing rather than a stale figure."""
    if not reading:
        return None
    parts = []

    def part(key, label, watts, up, down, title_up, title_down, title_flat):
        arrow, title = "", title_flat
        if watts is not None and watts >= IDLE_W:
            arrow, title = down, title_down
        elif watts is not None and watts <= -IDLE_W:
            arrow, title = up, title_up
        parts.append({"key": key, "label": label, "kw": kw(watts), "arrow": arrow, "title": title, "idle": not arrow})

    solar = reading.get("solar_w")
    parts.append({"key": "solar", "label": "Solar", "kw": kw(solar), "arrow": "", "title": "Solar production", "idle": solar is None or solar < IDLE_W})
    parts.append({"key": "home", "label": "Home", "kw": kw(reading.get("home_w")), "arrow": "", "title": "What the house is drawing", "idle": False})
    part("grid", "Grid", reading.get("grid_w"), "↑", "↓", "Exporting to the grid", "Importing from the grid", "Not exchanging with the grid")
    part("battery", "Powerwall", reading.get("battery_w"), "↑", "↓", "Charging", "Giving to the house", "Idle")
    soc = reading.get("soe_pct")
    return {
        "soc": soc,
        "fill": max(0.0, min(100.0, soc)) if soc is not None else 0.0,
        "parts": parts,
        "source": reading.get("source"),
        "age_s": reading.get("age_s"),
        "source_label": SOURCES.get(reading.get("source"), ""),
    }


def glance(cfg: dict) -> str:
    """The word beside Energy on the Settings list."""
    return "Tesla account" if (cfg.get("cloud") or {}).get("enabled") else "Off"


def ago(seconds) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    return f"{s} s ago" if s < 90 else f"{s // 60} min ago" if s < 5400 else f"{s // 3600} h ago"


def cloud_view(cfg: dict, scope: dict | None, status: dict | None, prog: dict, today) -> dict:
    """The Tesla card: the account's site, whether the token may read it, and the backfill."""
    cloud = cfg.get("cloud") or {}
    first = prog.get("stop") or cloud.get("installed")
    days = energy_cloud.days_between(first, today) if first else 0
    total = prog["total"] or days
    return {
        "connected": scope is not None,
        "granted": bool(scope and scope.get("granted")),
        "enabled": bool(cloud.get("enabled")),
        "site": cloud.get("site_id") or "",
        "name": cloud.get("name") or "",
        "status": status,
        "backfill": {**prog, "total": total, "percent": min(100, round(100 * prog["done"] / total)) if total else 0, "text": backfill_text(prog, total)},
        "note": (
            energy_cloud.cost_note(total) if total else "One request per day of history, newest first. The count and an estimate of the cost show here once Tesla says when the site was installed."
        ),
    }


def backfill_text(prog: dict, total: int) -> str:
    n = f"{prog['done']:,}"
    if prog["state"] == "running":
        return f"{n} of {total:,} days" if total else f"{n} days"
    if prog["state"] == "paused":
        return f"Stopped at {prog['next']} · {n} days done"
    if prog["state"] == "done":
        return f"{n} days read"
    return ""


def backfill_json(prog: dict, total: int) -> dict:
    """What the page's progress bar redraws from, with no reload."""
    return {
        "state": prog["state"],
        "done": prog["done"],
        "total": total,
        "percent": min(100, round(100 * prog["done"] / total)) if total else 0,
        "text": backfill_text(prog, total),
        "error": prog.get("error"),
    }


STEP_TITLES = {"signin": "Signed in", "powerwall": "Powerwall", "history": "History"}


def flow(cfg: dict, scope: dict | None, choices: list[dict] | None, prog: dict, today) -> dict:
    """Setting up the house inside the Tesla flow: three steps, each shown once the one before is
    done, with `current` the one to do now (it gets the primary button). `choices` are the energy
    sites on the account (None: not asked yet), `scope` the token's energy access (None: no
    account).
    """
    cloud = cfg.get("cloud") or {}
    signed, granted = scope is not None, bool(scope and scope.get("granted"))
    have_site = bool(cloud.get("enabled") and cloud.get("site_id"))
    view = cloud_view(cfg, scope, None, prog, today)
    total = view["backfill"]["total"]
    power = {"id": "powerwall", "title": "Your Powerwall", "name": cloud.get("name") or "", "state": "done" if have_site else "next" if signed else "waiting"}
    if signed and not have_site:
        if not granted:
            power["need"] = "scope"
        elif choices is not None and len(choices) == 0:
            power["need"] = "none"
        elif choices and len(choices) > 1:
            power.update(need="pick", choices=choices)
        else:
            power["need"] = "use"
    bf = view["backfill"]
    history = {
        "id": "history",
        "title": "History",
        "state": "done" if prog["state"] == "done" else "next" if have_site else "waiting",
        "days": total,
        "running": prog["state"] == "running",
        "paused": prog["state"] == "paused",
        "errored": prog["state"] == "error",
        "percent": bf["percent"],
        "progress": backfill_text(prog, total),
        "error": prog.get("error"),
        "label": backfill_text(prog, total) if prog["state"] == "done" else f"{total:,} days, about ${total * energy_cloud.PER_REQUEST:.2f}" if total else "One request a day, newest first",
    }
    steps = [{"id": "signin", "title": "Signed in", "state": "done" if signed else "next"}, power, history]
    for n, s in enumerate(steps, 1):
        s["n"] = n
    if not signed:
        for s in steps[1:3]:
            s["state"] = "waiting"
    current = next((s["id"] for s in steps if s["state"] == "next"), None)
    return {"steps": steps, "current": current, "line": " · ".join(f"{s['n']} {STEP_TITLES[s['id']]}" for s in steps), "total": total}


def cars_card(cfg: dict, scope: dict | None) -> dict | None:
    """The Powerwall line on the Tesla account's card in Cars and sources: where it stands, and the
    one action that goes on from there (into the same steps as Connect Tesla). None with no account."""
    if scope is None:
        return None
    cloud = cfg.get("cloud") or {}
    if cloud.get("enabled") and cloud.get("site_id"):
        return {"text": cloud.get("name") or "On this account", "action": "Manage", "todo": False}
    if scope.get("granted"):
        return {"text": "On this account", "action": "Set up", "todo": True}
    return {"text": "Not brought in yet", "action": "Bring it in", "todo": True}


def sun_share(solar, battery, grid, battery_sun_frac) -> float | None:
    """The share of a charge that was the sun's: its direct solar, plus the Powerwall's
    part that was itself solar. None when there is no split."""
    total = (solar or 0.0) + (battery or 0.0) + (grid or 0.0)
    if total <= 0:
        return None
    return ((solar or 0.0) + (battery or 0.0) * (battery_sun_frac or 0.0)) / total


def mix_bar(c: dict) -> dict | None:
    """A home charge's three parts for the bar: kWh, share and cost of sun, Powerwall and
    grid, with ≈ where the figure leans on an estimate. None when the charge has no split."""
    if c.get("src_status") != "ok":
        return None
    kwh = {"sun": c.get("src_solar_kwh") or 0.0, "battery": c.get("src_battery_kwh") or 0.0, "grid": c.get("src_grid_kwh") or 0.0}
    total = sum(kwh.values())
    if total <= 0:
        return None
    approx = bool(c.get("src_approx"))
    costs = {"sun": 0.0, "battery": c.get("src_battery_cost"), "grid": c.get("src_grid_cost")}
    parts = []
    for key, label in (("sun", "Sun"), ("battery", "Powerwall"), ("grid", "Grid")):
        parts.append(
            {
                "key": key,
                "label": label,
                "kwh": kwh[key],
                "percent": 100 * kwh[key] / total,
                "cost": costs[key],
                "approx": (key == "battery" and kwh[key] > 0) or (approx and key != "sun"),
            }
        )
    shown = [p for p in parts if p["kwh"] > 0.005]
    share = sun_share(kwh["sun"], kwh["battery"], kwh["grid"], c.get("src_sun_frac"))
    sun_end = next((p["percent"] for p in parts if p["key"] == "sun"), 0.0)
    battery_end = sun_end + next((p["percent"] for p in parts if p["key"] == "battery"), 0.0)
    return {
        "parts": shown,
        "sun_share": share,
        "approx": approx,
        # Ready for a row, which draws one element: the label for a screen reader, the two stops of its gradient, the note beside it.
        "label": ", ".join(f"{p['label']} {round(p['percent'])}%" for p in shown),
        "grad": f"--s:{sun_end:.1f}%;--b:{battery_end:.1f}%",
        "note": f"{round(share * 100)}% sun" if share is not None else "",
    }


def rate_bar(c: dict) -> dict | None:
    """A home charge's rate mix for the bar: the share of its kWh at the plan's off-peak, part-peak
    and peak rates, from the facts stored beside the charge (nothing is priced here). The note leads
    with the off-peak share ("all off-peak", "82% off-peak") and adds the sun only when there was
    some. None away from home or with no plan's split."""
    if not c.get("at_home") or c.get("off_peak_kwh") is None:
        return None
    kwh = {"off": c.get("off_peak_kwh") or 0.0, "part": c.get("part_peak_kwh") or 0.0, "peak": c.get("peak_kwh") or 0.0}
    total = sum(kwh.values())
    if total <= 0:
        return None
    labels = {"off": "Off-peak", "part": "Part-peak", "peak": "Peak"}
    parts = [{"key": k, "label": labels[k], "kwh": kwh[k], "percent": 100 * kwh[k] / total, "cost": None, "approx": False} for k in kwh if kwh[k] > 0.005]
    off = 100 * kwh["off"] / total
    note = "all off-peak" if off >= 99.5 else f"{round(off)}% off-peak"
    sun = sun_share(c.get("src_solar_kwh"), c.get("src_battery_kwh"), c.get("src_grid_kwh"), c.get("src_sun_frac")) if c.get("src_status") == "ok" else None
    if sun is not None and round(sun * 100) > 0:
        note += f" · {round(sun * 100)}% sun"
    return {
        "parts": parts,
        "label": ", ".join(f"{p['label']} {round(p['percent'])}%" for p in parts),
        "grad": f"--s:{off:.1f}%;--b:{off + 100 * kwh['part'] / total:.1f}%",
        "note": note,
    }


def sun_months(rows) -> list[dict]:
    """Home charging by month, newest first, each with the share that was the sun's (direct
    solar plus the Powerwall's solar part) and the kWh it is out of. `rows` are the month
    sums (ym, solar, battery, grid, battery_sun); a month with nothing split is left out."""
    out = []
    for r in rows:
        total = float(r["solar"] or 0) + float(r["battery"] or 0) + float(r["grid"] or 0)
        if total <= 0:
            continue
        sun = float(r["solar"] or 0) + float(r["battery_sun"] or 0)
        year, month = r["ym"].split("-")
        out.append({"ym": r["ym"], "label": datetime(int(year), int(month), 1).strftime("%b %Y"), "percent": 100 * sun / total, "kwh": total})
    return out
