"""The home plan shaped for Settings › Money. Every plan reads as its newest version's grid
(12 months by 24 hours, one grid when weekdays and weekends agree) shaded by price, with the
prices in force today as chips. A plan the rows editor can hold (typed in, three periods, one
schedule) is edited as dated price rows and schedule rules; one it cannot is edited a price
per version and period. Pure."""

from __future__ import annotations

from datetime import date

import cost


# From and To in a schedule rule: a select of labels, never a number box. 24 ends a day.
def hours_from() -> list[tuple[int, str]]:
    """The hours a rule can start at, worded by the chosen clock (read each time: the clock can change)."""
    return [(h, cost._h(h).replace("am", " am").replace("pm", " pm")) for h in range(24)]


def hours_to() -> list[tuple[int, str]]:
    return hours_from()[1:] + [(24, "midnight")]


def money_context(plan: dict | None, today: date | None = None) -> dict:
    """What Settings › Money needs to show the home plan."""
    plan = plan if cost.valid_plan(plan) else None
    rates, schedule = cost.plan_rows(plan)
    today = today or date.today()
    current = rate_row_now(rates, today)
    return {
        "plan": plan,
        "plan_fits": plan is None or cost.fits_rows(plan),
        "plan_rates": rates,
        "rate_order": list(reversed(range(len(rates)))),
        "rate_shown": shown([i == current for i in reversed(range(len(rates)))]),
        "rate_rows": [{"label": date_label(r["from"]), "now": i == current, "prices": " · ".join(price_text(r[k]) for k in cost.ROLES)} for i, r in enumerate(rates)],
        "schedule": schedule,
        "rules": [rule_view(r) for r in schedule],
        "schedule_text": cost.schedule_text(schedule) if plan else "",
        "plan_grid": grid_view(plan, today),
        "periods": cost.PERIODS,
        "day_kinds": cost.DAYS,
        "month_names": cost.MONTHS,
        "period_options": list(cost.PERIODS.items()),
        "day_options": [(k, v.capitalize()) for k, v in cost.DAYS.items()],
        "hours_from": hours_from(),
        "hours_to": hours_to(),
    }


def shown(now_flags: list[bool]) -> int:
    """How many dated price sets a list shows before "Earlier prices", the list newest first:
    down to the one in force today (the newer ones are coming), at least one."""
    return max(1, next((i + 1 for i, n in enumerate(now_flags) if n), 1))


def rate_row_now(rows: list[dict], today: date) -> int | None:
    """The index of the dated row in force today (the newest one already started), else None."""
    started = [i for i, r in enumerate(rows) if r["from"] <= today.isoformat()]
    return started[-1] if started else None


def grid_view(plan: dict | None, today: date | None = None) -> dict | None:
    """None for no plan."""
    if not plan or not cost.valid_plan(plan):
        return None
    versions = sorted(enumerate(plan["versions"]), key=lambda iv: iv[1]["from"])
    newest = versions[-1][1]
    n = len(newest["periods"])
    rank = sorted(range(n), key=lambda i: (float(newest["periods"][i]["price"]), i))
    heat = {i: (rank.index(i) / (n - 1) if n > 1 else 0.0) for i in range(n)}
    labels = [p.get("label") or p["key"] for p in newest["periods"]]

    def grid(g):
        return [{"month": cost.MONTHS[m], "cells": [{"heat": heat[i], "label": labels[i]} for i in row]} for m, row in enumerate(g)]

    if newest["weekday"] == newest["weekend"]:
        grids = [{"title": "Every day", "rows": grid(newest["weekday"])}]
    else:
        grids = [{"title": "Weekdays", "rows": grid(newest["weekday"])}, {"title": "Weekends", "rows": grid(newest["weekend"])}]
    today = (today or date.today()).isoformat()
    now = next((v for _, v in reversed(versions) if v["from"] <= today), None)
    out = {
        "from": newest["from"],
        "legend": [{"label": labels[i], "heat": heat[i]} for i in rank],
        "grids": grids,
        "shown": 1,  # set below, once the versions are listed
        "versions": [
            {
                "from": v["from"],
                "label": date_label(v["from"]),
                "now": v is now,
                "summary": prices_summary([float(p["price"]) for p in v["periods"]]),
                "periods": [{"field": f"pv{vi}_{pi}", "label": p.get("label") or p["key"], "price": float(p["price"])} for pi, p in enumerate(v["periods"])],
            }
            for vi, v in reversed(versions)
        ],
        # the prices in force today by season (estimated ones last), cheapest first in each, with their shade in the grid
        "now": (
            {
                "label": date_label(now["from"]),
                "since": "" if date.fromisoformat(now["from"]) <= ORIGIN else f"since {date_label(now['from'])}",
                "prices": sorted(
                    (
                        {"label": p.get("label") or p["key"], "price": price_text(float(p["price"])), "heat": heat.get(i, 0.0), "p": float(p["price"]), "est": bool(p.get("est"))}
                        for i, p in enumerate(now["periods"])
                    ),
                    key=lambda c: (c["est"], season_of(c["label"]), c["p"]),
                ),
            }
            if now
            else None
        ),
        "note": newest.get("fixed_note"),
    }
    out["shown"] = shown([v["now"] for v in out["versions"]])
    return out


def season_of(label: str) -> str:
    """The months a period's label names ("Peak, Jun–Sep" -> "Jun–Sep"), so chips group by
    season, the estimated ones last; "" for a label with none."""
    return label.split(", ", 1)[1] if ", " in label else ""


def price_text(value: float) -> str:
    """A $/kWh figure as a bill prints it: two decimals, three when the third one counts."""
    text = f"{value:.3f}"
    return text[:-1] if text.endswith("0") else text


def prices_summary(prices: list[float]) -> str:
    """A version's prices on one line: each of up to three, else the cheapest to the dearest."""
    if len(prices) <= 3:
        return " · ".join(price_text(p) for p in sorted(prices))
    return f"{price_text(min(prices))}–{price_text(max(prices))}"


# The first price row's date when its prices have always applied (cost.LEGACY_RATES, the demo's plan)
ORIGIN = date(2000, 1, 1)


def date_label(iso: str) -> str:
    d = date.fromisoformat(iso)
    if d <= ORIGIN:
        return "Since the start"
    return f"{d:%b} {d.day}, {d.year}"


def months_text(months: list[int] | None) -> str:
    """Month numbers as runs, a run over the new year kept whole: [1..5, 10..12] -> "Oct–May"."""
    if not months:
        return "all year"
    have = set(months)
    start = next((m for m in range(1, 13) if m in have and (m - 2) % 12 + 1 not in have), 1)
    runs, run = [], []
    for k in range(12):
        m = (start - 1 + k) % 12 + 1
        if m in have:
            run.append(m)
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)
    return ", ".join(cost.MONTHS[r[0] - 1] if len(r) == 1 else f"{cost.MONTHS[r[0] - 1]}–{cost.MONTHS[r[-1] - 1]}" for r in runs)


def rule_view(rule: dict) -> dict:
    """A schedule rule's summary line: "Peak · 4 pm–9 pm" and "every day · Oct–May"."""
    hours = dict(hours_from() + hours_to()[-1:])
    return {
        "title": f"{cost.PERIODS[rule['period']]} · {hours[rule['start']]}–{hours[rule['end']]}",
        "when": f"{cost.DAYS[rule['days']].capitalize()} · {months_text(rule['months'])}",
    }


# --- Find my plan: a plan beside the one in use -------------------------------------------

LOWER = 0.85  # the plan in use this far under a tariff reads as a different supplier, not a better deal


def compare(plan: dict, mine: dict | None, home: dict, today: date) -> dict:
    """`plan` and the plan in use priced on the same home charging (`home`, from the profile
    of the last year) at the prices each has in force today: like with like. With no home
    charging every hour counts alike, which is enough to say whether yours is lower."""
    kwh = home.get("kwh") or 0.0
    profile = home["profile"] if kwh > 0 else [1.0] * 576
    theirs = cost.profile_cost(plan, profile, today)
    yours = cost.profile_cost(mine, profile, today) if mine else None
    out = {"mine": (mine or {}).get("name"), "lower": bool(yours is not None and theirs and yours < theirs * LOWER)}
    if kwh > 0:
        first = home.get("first")
        year = not first or (today - first).days > 330
        out.update(
            theirs=theirs,
            yours=yours,
            diff=None if yours is None else theirs - yours,
            kwh=kwh,
            charges=home.get("charges") or 0,
            per="a year" if year else f"since {first:%b} {first.year}",
            span="last 12 months" if year else f"since {date_label(first.isoformat())}",
        )
    return out


def replaces(mine: dict | None, kept: dict | None) -> dict:
    """What "Use this plan" replaces: typed-in prices are kept to go back to; a plan from
    OpenEI is not, but a typed-in one kept before it stays kept."""
    if not mine:
        return {}
    if mine.get("source") == "manual":
        return {"typed": True, "name": mine.get("name") or "Home"}
    return {"name": mine.get("name") or "Home", "back": (kept or {}).get("name")}
