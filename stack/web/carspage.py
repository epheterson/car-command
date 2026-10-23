"""Settings › Cars and sources: one card per car and one row per account, from what the
page already loads (the vehicles, their sources, the pollers' last results). Nothing here
reads the database or the network."""

from __future__ import annotations

import re

from teslafi_import import USABLE_KWH

# What a car's history is written from (app.source_of), in words a person would use. These are
# the names every page uses for a source (Data's checks and their alerts too): one plain name
# per source, never "the connector" or "the feed".
WRITERS = {"feed": "TeslaFi (API)", "teslamate": "TeslaMate", "tesla": "Tesla (direct)", "history": "imported history"}
# What each kind of account feeds a car, named as on the car's card.
FEEDS = {"teslafi": "TeslaFi (API)", "teslamate": "TeslaMate", "tesla": "Tesla (direct)", "lucid": "Lucid (CarConnectivity)"}
WHAT = {"teslafi": "TeslaFi token", "teslamate": "TeslaMate database", "tesla": "Tesla account", "lucid": "Lucid account"}


def is_lucid(make: str | None) -> bool:
    return (make or "").strip().lower() == "lucid"


def live_name(make: str | None) -> str:
    """The connector's cars are named by their maker and the CarConnectivity connector that
    reads them: "Lucid (CarConnectivity)", "Volkswagen (CarConnectivity)"."""
    if is_lucid(make):
        return FEEDS["lucid"]
    return f"{make.strip()} (CarConnectivity)" if (make or "").strip() else "CarConnectivity"


def writer_name(kind: str, make: str | None = None) -> str:
    return live_name(make) if kind == "live" else WRITERS.get(kind, kind)


def model_figure(make: str | None, model: str | None) -> float | None:
    """The usable pack the model is known for; Teslas only, the one table there is."""
    if (make or "Tesla").strip().lower() != "tesla":
        return None
    return USABLE_KWH.get((model or "").strip())


def _same(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) < 0.05


def pack(stored: float | None, reported: float | None, reported_by: str | None, model_kwh: float | None) -> dict:
    """The pack as a fact with where it came from. What the car's source reports wins over the
    model's figure; a stored number that is neither was typed here (or given to an import)."""
    figure, figure_by = (reported, f"{reported_by}'s figure") if reported else (model_kwh, "the model's figure") if model_kwh else (None, None)
    if not stored:
        origin, text = None, "unknown"
    elif _same(stored, reported):
        origin, text = "reported", f"reported by {reported_by}"
    elif _same(stored, model_kwh):
        origin, text = "model", "the model's figure"
    else:
        origin, text = "you", "set by you"
    return {"kwh": stored, "origin": origin, "text": text, "figure": figure, "figure_by": figure_by}


def plain(text: str | None) -> str | None:
    """A stored failure in words a person can act on: the sign-in note an older connector wrote named
    the Python class that raised."""
    m = re.match(r"Lucid did not sign in \(\w+\)\.", text or "")
    return "Lucid did not accept the sign-in. Check the e-mail and password, then sign in again." if m else text


def row_status(kind: str, *, pending=False, last_error=None, readable=True, status=None, last_ok_at=None) -> dict:
    """An account's state as a dot and a sentence: ok, bad, wait or none, and when."""
    last_error = plain(last_error)
    if kind == "lucid":
        # the row's own sign-in result first, then what the connector reports
        if pending:
            return {"state": "wait", "text": "Signing in…", "at": None}
        if last_error:
            return {"state": "bad", "text": last_error, "at": None}
        if readable and status:
            return {"state": "bad", "text": status[1], "at": None} if status[1] else {"state": "ok", "text": "Working", "at": status[0]}
        if readable and last_ok_at:
            return {"state": "ok", "text": "Signed in", "at": last_ok_at}
    if not readable:
        return {"state": "bad", "text": "Saved with another key: remove it and add it again", "at": None}
    if status:
        return {"state": "bad", "text": status[1], "at": None} if status[1] else {"state": "ok", "text": "Working", "at": status[0]}
    if last_error:
        return {"state": "bad", "text": last_error, "at": None}
    if last_ok_at:
        return {"state": "ok", "text": "Worked", "at": last_ok_at}
    return {"state": "none", "text": "Not checked yet", "at": None}


def cc_status(config: dict, last_error=None, readable=True, status=None, has_cars=False, last_ok_at=None) -> dict:
    """Another brand's account, as the connector container reports it back: installing,
    starting, removing, why it failed, or what the connector says of its cars."""
    state = config.get("state")
    last_error = plain(last_error)
    if state == "remove":
        return {"state": "wait", "text": "Removing: CarConnectivity takes it out within a few seconds", "at": None}
    if last_error:
        return {"state": "bad", "text": last_error, "at": None}
    if not readable:
        return {"state": "bad", "text": "Saved with another key: remove it and add it again", "at": None}
    if state == "install":
        return {"state": "wait", "text": f"Installing {config.get('package', 'its plugin')} {config.get('version', '')}…".replace(" …", "…"), "at": None}
    if state == "ready":
        return {"state": "wait", "text": "Starting CarConnectivity with it…", "at": None}
    if has_cars and status:
        return {"state": "bad", "text": status[1], "at": None} if status[1] else {"state": "ok", "text": "Working", "at": status[0]}
    return {"state": "wait", "text": "Running; no car from it has reported yet", "at": last_ok_at}


def account_vins(kind: str, vin: str | None, config: dict | None, lucid_vins: list[str], tesla_vins: list[str], live_makes: dict | None = None) -> list[str]:
    """The cars an account answers for, as far as the page knows without asking anyone."""
    config = config or {}
    if kind == "carconnectivity":
        import connectors

        return [v for v, make in (live_makes or {}).items() if connectors.claims(config, make)]
    if kind == "teslafi":
        return [vin] if vin else []
    if kind == "teslamate":
        return list(config.get("vins") or [c["vin"] for c in config.get("cars") or []])
    if kind == "lucid":
        return list(lucid_vins)
    if kind == "tesla":
        return list(tesla_vins)
    return [vin] if vin else []


def accounts(
    rows: list[dict],
    env_rows: list[dict],
    token_file: bool,
    names: dict[str, str],
    lucid_vins: list[str],
    tesla_vins: list[str],
    connector: tuple | None,
    env_teslamate_vins: list[str] = (),
    live_makes: dict | None = None,
) -> list[dict]:
    """One row per account: the app's own, then the token file, then what is still in .env."""
    out = []
    for r in rows:
        config = r.get("config") or {}
        vins = account_vins(r["kind"], r.get("vin"), config, lucid_vins, tesla_vins, live_makes)
        if r["kind"] == "carconnectivity":
            st = cc_status(config, r.get("last_error"), r.get("readable", True), r.get("status"), bool(vins), r.get("last_ok_at"))
            what = f"{config.get('brand') or r.get('label') or 'Another brand'} (CarConnectivity)"
        else:
            st = row_status(r["kind"], pending=bool(config.get("pending")), last_error=r.get("last_error"), readable=r.get("readable", True), status=r.get("status"), last_ok_at=r.get("last_ok_at"))
            what = WHAT.get(r["kind"], r["kind"].title())
        cars = [names.get(v) or (r.get("label") if r["kind"] == "teslafi" else None) or v[-6:] for v in vins]
        kept = "kept in the app"
        if r["kind"] == "lucid":  # only the token Lucid hands back is kept; a refused sign-in keeps nothing
            kept = "nothing kept" if st["state"] == "bad" and r.get("last_error") else "its sign-in token is kept, never the password"
        elif r["kind"] == "carconnectivity" and st["state"] == "bad":  # left out of the running connector; its settings stay to retry
            kept = "kept here, not running"
        out.append({**r, "what": what, "vins": vins, "cars": cars, "kept": kept, "where": "app", "retry": st["state"] == "bad" and r["kind"] in ("lucid", "carconnectivity"), **st})
    if token_file:
        st = row_status("env", status=connector)
        out.append(
            {
                "kind": "lucid",
                "what": "Lucid account",
                "vins": list(lucid_vins),
                "cars": [names.get(v, v) for v in lucid_vins],
                "kept": "from a sign-in file",
                "kept_detail": "secrets/lucid-token.json",
                "where": "file",
                **st,
            }
        )
    for s in env_rows:
        vins = account_vins(s["kind"], s.get("vin"), {}, lucid_vins, tesla_vins)
        if s["kind"] == "teslamate":
            vins = list(env_teslamate_vins)  # .env names no cars: the ones TeslaMate writes
        st = row_status("env", status=s.get("status"))
        kept = "from .env" + ("" if s.get("used", True) else ", no longer read")
        cars = [names.get(v, v) for v in vins] or ([s["car"]] if s.get("car") and s["kind"] != "teslamate" else [])
        detail = {"teslafi": "TESLAFI_TOKENS", "teslamate": "TESLAMATE_DB_URL"}.get(s["kind"], s["kind"]) + " in stack/.env"
        out.append({**s, "what": WHAT.get(s["kind"], s["kind"]), "vins": vins if s.get("used", True) else [], "cars": cars, "kept": kept, "kept_detail": detail, "where": "env", **st})
    return out


def cards(
    vehicles: list[dict], makes: dict[str, tuple], writer: dict[str, str], writer_status: dict[str, tuple | None], newest: dict, packs: dict[str, dict], accts: list[dict], choices: dict | None = None
) -> list[dict]:
    """One card per car: its name and model, where its data comes from (the source that
    writes its history first), how new the newest reading is, its pack, and the sources
    that could write its history instead (history_writer.options)."""
    out = []
    for v in vehicles:
        vin = v["vin"]
        make, model = makes.get(vin) or (None, None)
        kind = writer.get(vin, "history")
        first = {"name": writer_name(kind, make), "writes": True, **(row_status("env", status=writer_status.get(vin)) if kind != "history" else {"state": None, "text": "", "at": None})}
        srcs = [first]
        for a in accts:
            if vin not in a.get("vins", []):
                continue
            name = FEEDS.get(a["kind"], a["kind"])
            if a["kind"] in ("lucid", "carconnectivity"):
                name = live_name(make)
            if all(s["name"] != name for s in srcs):
                srcs.append({"name": name, "writes": False, "state": a["state"], "text": a["text"], "at": a["at"]})
        label = " ".join(x for x in (make, model) if x) if model else ""
        out.append(
            {
                "vin": vin,
                "name": v["name"],
                "source_name": v.get("source_name") or v["name"],
                "model": label,
                "sources": srcs,
                "newest": newest.get(vin),
                "pack": packs.get(vin) or pack(None, None, None, None),
                "choices": (choices or {}).get(vin, []),
            }
        )
    return out
