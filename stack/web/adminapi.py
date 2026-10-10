"""What the admin API answers with: the facts the Settings pages already gather, cut down to
plain JSON. Every view names the fields it passes on, so a token, key, password or database
address added to a source row or a Tesla answer later cannot ride along by accident.

Pure: the routes in app.py bring the facts, the settings and the rows.
"""

from __future__ import annotations

from datetime import date, datetime

import placeprice
from places import posted_address
from sources import scrub


class BadRequest(ValueError):
    """A request the API answers with 400 and this message."""


def plain(value):
    """JSON-ready: datetimes and dates as ISO text, tuples as lists, the rest as it is."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


# ---- Connect Tesla ---------------------------------------------------------


def tesla(facts: dict, steps: list[dict] | None, domain: str, key_domain: str) -> dict:
    """The Connect Tesla checklist as data: each step's state, the registered domain, the
    stream, and per car whether it holds the key, whether it took its config, when it last
    sent a message and what errors it reported."""
    per = facts.get("stream_cars") or {}
    cars = []
    for c in facts.get("cars") or []:
        s = per.get(c["vin"]) or {}
        known = bool(s.get("config_known"))
        config = s.get("config") or {}
        cars.append(
            {
                "vin": c["vin"],
                "name": c.get("name"),
                "known_as": c.get("known_as"),
                "paired": bool(c.get("paired")),
                "firmware": c.get("firmware"),
                "streaming": bool(s.get("on")),
                "config_held": (s.get("config") is not None) if known else None,
                "config_synced": bool(config.get("synced")) if known and config else None,
                "config_limit_reached": bool(config.get("limit_reached")) if known and config else None,
                "live": bool(s.get("live")),
                "last_message_at": s.get("last_at"),
                "errors": [{"name": e.get("name"), "body": e.get("body"), "at": e.get("at")} for e in s.get("errors") or []],
            }
        )
    receiver = facts.get("receiver") or {}
    return plain(
        {
            "steps": [{"id": st["id"], "title": st["title"], "state": st["state"], "detail": st["detail"]} for st in steps or []],
            "domain": domain or None,
            "key_domain": key_domain or None,
            "app_ok": bool(facts.get("app_ok")),
            "app_error": facts.get("app_error"),
            "key": bool(facts.get("key")),
            "public_ok": bool(facts.get("public_ok")),
            "public_error": facts.get("public_error"),
            "registered": bool(facts.get("registered")),
            "register_error": facts.get("register_error"),
            "account": bool(facts.get("account")),
            "account_error": facts.get("account_error"),
            "stream": {
                "host": facts.get("stream_host"),
                "host_ok": bool(facts.get("stream_host_ok")),
                "registered": facts.get("stream_registered"),
                "register_error": facts.get("stream_register_error"),
                "public_error": facts.get("stream_public_error"),
                "receiver_ok": receiver.get("ok"),
                "receiver_problem": receiver.get("problem"),
                "receiver_error": receiver.get("error"),
                "receiver_expires_at": receiver.get("expires_at"),
                "dropped": facts.get("stream_dropped"),
                "error": facts.get("stream_error"),
            },
            "cars": cars,
        }
    )


def vin_of(body) -> str:
    """The car a stream start or stop is for; a 400 without one."""
    vin = str((body or {}).get("vin") or "").strip() if isinstance(body, dict) else ""
    if not vin:
        raise BadRequest('Say which car: {"vin": "..."}.')
    return vin


# ---- sources ---------------------------------------------------------------


def status(result) -> dict | None:
    """A poller's (last success, error) as {ok_at, error}; None before its first try."""
    if not result:
        return None
    ok_at, error = result
    return {"ok_at": ok_at, "error": error}


def source(row: dict, car: str, kinds: dict, live=None) -> dict:
    """One kept source, its secret left behind. Errors are masked with that secret, as the
    page's are, in case an answer ever quoted it back."""
    secret = row.get("plain")
    config = row.get("config") or {}
    mask = (lambda text: scrub(str(text), secret)) if secret else str
    now = status(live)
    if now and now["error"]:
        now["error"] = mask(now["error"])
    out = {
        "id": row["id"],
        "kind": row["kind"],
        "kind_label": kinds.get(row["kind"], row["kind"]),
        "vin": row.get("vin"),
        "car": car,
        "label": row.get("label"),
        "enabled": bool(row.get("enabled", True)),
        "readable": secret is not None,
        "last_ok_at": row.get("last_ok_at"),
        "last_error": mask(row["last_error"]) if row.get("last_error") else None,
        "status": now,
    }
    if row["kind"] == "teslamate":
        out["following"] = list(config.get("vins") or [])
    elif row["kind"] == "tesla":
        out["streaming"] = list(config.get("stream") or [])
    elif row["kind"] == "lucid":
        out["pending"] = bool(config.get("pending"))
    return plain(out)


def tested(checked: dict) -> dict:
    """A source test's result: whether it answered, why not, and the cars it found."""
    out = {"ok": bool(checked.get("ok")), "error": checked.get("error") or None}
    for key in ("vin", "name", "model"):
        if checked.get(key):
            out[key] = checked[key]
    if checked.get("cars") is not None:
        out["cars"] = [{"vin": c.get("vin"), "name": c.get("name") or ""} for c in checked["cars"]]
    return plain(out)


# ---- places ----------------------------------------------------------------

PLACE_FIELDS = ("name", "icon", "price", "address")


def place(row: dict, home: bool = False, currency: str = "$", visits: int | None = None) -> dict:
    """`visits`: placespage.visits, the count every page shows."""
    price = row.get("price") or None
    return plain(
        {
            "id": row["id"],
            "name": row["name"],
            "latitude": row.get("latitude"),
            "longitude": row.get("longitude"),
            "radius_m": row.get("radius_m"),
            "auto": bool(row.get("auto")),
            "visits": visits,
            "address": row.get("user_address") or row.get("address"),
            "own_address": bool(row.get("user_address")),
            "icon": row.get("icon"),
            "price": price,
            "price_text": placeprice.describe(price, home=home, currency=currency),
        }
    )


def place_patch(body, valid_icon) -> dict:
    """A PATCH body as the changes to make, checked as the Places forms check theirs:
    name (1–40 characters), icon (a known icon, "none", or null for the default), price
    (placeprice's stored shape, or null for not set) and address (the owner's own; null or
    blank goes back to the looked-up one). `valid_icon` is the forms' own check; it raises
    for an icon it does not know. BadRequest for anything else wrong."""
    if not isinstance(body, dict) or not body:
        raise BadRequest(f"Send a JSON object with any of: {', '.join(PLACE_FIELDS)}.")
    unknown = sorted(set(body) - set(PLACE_FIELDS))
    if unknown:
        raise BadRequest(f"Not a field of a place: {', '.join(unknown)}.")
    out: dict = {}
    if "name" in body:
        name = " ".join(str(body["name"] or "").split())[:40] if isinstance(body["name"], str) else ""
        if not name:
            raise BadRequest("A name is some text, up to 40 characters.")
        out["name"] = name
    if "icon" in body:
        if body["icon"] is not None and not isinstance(body["icon"], str):
            raise BadRequest("An icon is one of the icon names, \"none\", or null.")
        out["icon"] = valid_icon(body["icon"])
    if "price" in body:
        price = body["price"]
        if price is not None and not isinstance(price, dict):
            raise BadRequest('A price is {"kind": ..., "rate"?: ..., "fee"?: ...} or null.')
        try:
            out["price"] = (True, placeprice.clean((price or {}).get("kind"), (price or {}).get("rate"), (price or {}).get("fee")))
        except placeprice.BadPrice as err:
            raise BadRequest(str(err)) from None
    if "address" in body:
        address = body["address"]
        if address is not None and not isinstance(address, str):
            raise BadRequest("An address is some text, or null for the looked-up one.")
        given = posted_address(address, None)
        out["address"] = given if given[0] else (True, None)
    return out


# ---- Money -----------------------------------------------------------------

FAST = ("dc_price", "dc_per_min", "sc_price", "sc_per_min")


def plan_summary(plan: dict | None, schedule_text: str = "") -> dict | None:
    """The home plan in force: its name, where it came from, and the newest prices."""
    if not plan:
        return None
    versions = sorted(plan.get("versions") or [], key=lambda v: v.get("from") or "")
    newest = versions[-1] if versions else {}
    return {
        "name": plan.get("name"),
        "utility": plan.get("utility"),
        "source": plan.get("source"),
        "versions": len(versions),
        "from": newest.get("from"),
        "periods": [{"key": p.get("key"), "label": p.get("label") or p.get("key"), "price": float(p.get("price") or 0)} for p in newest.get("periods") or []],
        "schedule": schedule_text or None,
    }


def money(st: dict, plan: dict | None, schedule_text: str, rates: dict) -> dict:
    """Settings › Money as data: the home plan, fast-charging prices, the petrol comparison
    and the regional averages' state."""
    return plain(
        {
            "currency": st.get("currency") or "$",
            "plan": plan_summary(plan, schedule_text),
            "fast": {k: st.get(k) for k in FAST},
            "petrol": {"mpg": st.get("gas_mpg"), "price": st.get("gas_price")},
            "averages": {
                "origin": rates.get("origin"),
                "built": rates.get("built"),
                "fetched_at": rates.get("fetched_at"),
                "tried_at": rates.get("tried_at"),
                "error": rates.get("error"),
                "sources": rates.get("sources") or [],
                "homes": rates.get("homes") or [],
            },
        }
    )
