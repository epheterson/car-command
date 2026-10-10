"""Find a US utility's rate plan in OpenEI's Utility Rate Database and turn it into a home plan.

Only what the user typed is ever sent: a utility's name or a ZIP code, or the id of a plan
picked from the results. Nothing about the cars, where they are or where they charge. The
API key goes to OpenEI as its `api_key` parameter and nowhere else: not in an error, not in
a log line, not in the cache key. With CC_URDB_URL set, requests go to that relay instead,
which holds a key of its own, with the same parameters and no key.

Every request goes through one `call(url, params, timeout) -> (status, body)`, httpx by
default, so tests answer from fixtures and nothing here touches the network on its own. All the
requests one button press makes share one deadline (DEADLINE_S). Answers are kept in
cc_urdb_cache for a day, keyed by the parameters without the key.

A utility is picked from a bundled list (rates/utilities.json, built from EIA Form 861 by
scripts/build-utilities.py) and asked for by its EIA id, so nobody types OpenEI's spelling.
Matching what is typed against that list happens on the page and here, never over the wire.

A URDB rate is one dated version of a tariff; superseded versions stay listed with their
start and end dates. A plan here is every version of one tariff: the same utility and name
across start dates, plus any the rate names as the one it `supersedes` (a tariff renamed
between versions). Each version's energy charges become periods (tier 1's rate plus its
adjustment, $/kWh), its weekday and weekend schedules the 12x24 grids, which in URDB are
already indexes into those periods. Higher tiers, fixed, minimum and demand charges are
written in the version's note and not priced.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import cost

BASE = "https://api.openei.org/utility_rates"
SIGNUP = "https://openei.org/services/api/signup/"
VERSION = 7
DEADLINE_S = 20  # every request of one Search, Preview or Use, together
PAGE = 500  # the most OpenEI returns per request
MAX_PAGES = 3  # a large utility lists ~600 residential rates; newest first, so a cut drops the oldest
MAX_CHAIN = 40  # versions followed back through `supersedes`
CACHE_S = 86400
KEY_STATE = "openei_key"
SHOWN = 150  # plans listed at most; a filter narrows a long list

ZIP = re.compile(r"^\d{5}(?:-\d{4})?$")
LABEL = re.compile(r"^[0-9a-f]{24}$")
KEY = re.compile(r"^[A-Za-z0-9]{20,80}$")

DDL = """
CREATE TABLE IF NOT EXISTS cc_urdb_cache (
    key         VARCHAR PRIMARY KEY,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    items       JSONB NOT NULL
);
"""

# What the parse and the list read from a rate; the rest (descriptions, demand schedules,
# revision lists) is dropped before anything is cached.
KEEP = (
    "label",
    "name",
    "utility",
    "startdate",
    "enddate",
    "supersedes",
    "uri",
    "eiaid",
    "country",
    "energyratestructure",
    "energyweekdayschedule",
    "energyweekendschedule",
    "fixedchargefirstmeter",
    "fixedchargeunits",
    "mincharge",
    "minchargeunits",
)
DEMAND = ("demandratestructure", "flatdemandstructure", "coincidentratestructure")


class _NoKey(logging.Filter):
    """httpx logs every request's URL at INFO, and an OpenEI URL carries the key."""

    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        if "api_key=" in text:
            record.msg, record.args = re.sub(r"api_key=[^&\s\"']+", "api_key=***", text), ()
        return True


logging.getLogger("httpx").addFilter(_NoKey())


class URDBError(Exception):
    """Something to tell the user, already free of the key."""


# --- requests: only typed input ---------------------------------------------------------


def query(text: str | None = None, eia=None) -> dict:
    """The search parameter: a utility picked from the list is its EIA id; else a ZIP code
    is an address, and anything else a utility's name as OpenEI lists it."""
    if eia not in (None, ""):
        try:
            eia = int(eia)
        except (TypeError, ValueError):
            eia = 0
        if not 0 < eia < 10_000_000:
            raise URDBError("That is not a utility from the list.")
        return {"eia": eia}
    text = " ".join(str(text or "").split())
    if not text:
        raise URDBError("Type your utility's name or your ZIP code.")
    if len(text) > 120:
        raise URDBError("That is too long for a utility's name.")
    return {"address": text} if ZIP.match(text) else {"ratesforutility": text}


def search_params(text: str | None = None, eia=None) -> dict:
    return {"version": VERSION, "format": "json", "sector": "Residential", "approved": "true", "detail": "full", "limit": PAGE, "orderby": "startdate", "direction": "desc", **query(text, eia)}


def page_params(label: str) -> dict:
    label = str(label or "").strip()
    if not LABEL.match(label):
        raise URDBError("That is not an OpenEI plan id.")
    return {"version": VERSION, "format": "json", "detail": "full", "getpage": label}


def cache_key(params: dict) -> str:
    return urlencode(sorted((k, str(v)) for k, v in params.items() if k != "api_key"))


def _httpx(url: str, params: dict, timeout: float = DEADLINE_S):
    import httpx

    r = httpx.get(url, params=params, headers={"User-Agent": "carcommand/0.1"}, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body


def scrub(text: str, key: str | None) -> str:
    return text.replace(key, "***") if key else text


class Client:
    """OpenEI with the user's key, or a relay (CC_URDB_URL) that holds one."""

    def __init__(self, key: str | None = None, relay: str | None = None, call=None, clock=time.monotonic):
        self.key = (key or "").strip() or None
        self.relay = (relay or "").strip() or None
        self.base = self.relay or BASE
        self.call = call or _httpx
        self.clock = clock
        self.until = clock() + DEADLINE_S  # one client per button press

    @property
    def ready(self) -> bool:
        return bool(self.relay or self.key)

    def get(self, params: dict) -> list[dict]:
        """One request's items. Raises URDBError with nothing of the key in it."""
        import httpx

        if not self.ready:
            raise URDBError("Plan search needs an OpenEI API key.")
        send = dict(params) if self.relay else {**params, "api_key": self.key}
        late = URDBError(f"OpenEI did not answer within {DEADLINE_S} seconds. Try again in a minute.")
        left = self.until - self.clock()
        if left <= 0.5:
            raise late
        try:
            status, body = self.call(self.base, send, timeout=left)
        except httpx.TimeoutException:
            raise late from None
        except httpx.HTTPError as err:
            raise URDBError(f"Could not reach {'the plan relay' if self.relay else 'OpenEI'} ({type(err).__name__}).") from None
        err = body.get("error") if isinstance(body, dict) else None
        if err:
            code = err.get("code") if isinstance(err, dict) else None
            if code in ("API_KEY_INVALID", "API_KEY_MISSING", "API_KEY_DISABLED", "API_KEY_UNAUTHORIZED"):
                raise URDBError("OpenEI refused the API key. Check it under Find my plan, or get a new one.")
            if code == "OVER_RATE_LIMIT":
                raise URDBError("OpenEI says this key is over its hourly limit. Try again in an hour.")
            message = err.get("message") if isinstance(err, dict) else err
            raise URDBError(scrub(f"OpenEI answered: {message}", self.key)[:200])
        if status >= 400 or not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise URDBError(f"OpenEI answered {status} without a list of plans.")
        return [x for x in body["items"] if isinstance(x, dict)]

    def all(self, params: dict) -> "Found":
        """Every page of a search, up to MAX_PAGES, trimmed; `.more` when the cap cut it."""
        out = Found()
        for n in range(MAX_PAGES):
            got = self.get({**params, "offset": n * PAGE})
            out += [trim(x) for x in got]
            if len(got) < PAGE:
                return out
        out.more = True
        return out


class Found(list):
    """A search's rates; `more` when OpenEI had more than MAX_PAGES pages of them."""

    more = False


def as_found(items, more=False) -> Found:
    out = Found(items or [])
    out.more = bool(more)
    return out


def trim(item: dict) -> dict:
    out = {k: item[k] for k in KEEP if k in item}
    out["demand"] = any(item.get(k) for k in DEMAND)
    return out


# --- the day's cache --------------------------------------------------------------------


def cached(conn, key: str) -> Found | None:
    row = conn.execute("SELECT items FROM cc_urdb_cache WHERE key = %s AND fetched_at > now() - make_interval(secs => %s)", (key, CACHE_S)).fetchone()
    if not row:
        return None
    kept = json.loads(row["items"]) if isinstance(row["items"], str) else row["items"]
    return as_found(kept.get("items"), kept.get("more")) if isinstance(kept, dict) else as_found(kept)


def keep(conn, key: str, items: Found) -> None:
    conn.execute("DELETE FROM cc_urdb_cache WHERE fetched_at < now() - make_interval(secs => %s)", (CACHE_S,))
    conn.execute(
        "INSERT INTO cc_urdb_cache (key, items) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET items = EXCLUDED.items, fetched_at = now()",
        (key, json.dumps({"items": list(items), "more": getattr(items, "more", False)})),
    )
    conn.commit()


def fetch(connect, client: Client, params: dict, cache_only: bool = False) -> Found | None:
    """A search's items from the day's cache, or OpenEI. No database connection is held
    while OpenEI answers. With cache_only, None when nothing is cached."""
    key = cache_key(params)
    with connect() as conn:
        hit = cached(conn, key)
    if hit is not None or cache_only:
        return hit
    items = client.all(params) if "getpage" not in params else as_found([trim(x) for x in client.get(params)])
    with connect() as conn:
        keep(conn, key, items)
    return items


# --- the key, sealed like every other secret kept in the app -----------------------------


def check_key(text: str) -> str:
    text = (text or "").strip()
    if not KEY.match(text):
        raise URDBError("That does not look like an OpenEI API key: letters and digits, about 40 of them.")
    return text


def save_key(conn, vault, text: str) -> None:
    from sources import last4

    value = json.dumps({"sealed": vault.seal(text, "state", KEY_STATE).decode(), "last4": last4(text)})
    conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (KEY_STATE, value))


def forget_key(conn) -> None:
    conn.execute("DELETE FROM cc_state WHERE key = %s", (KEY_STATE,))


def _stored(conn) -> dict | None:
    row = conn.execute("SELECT value FROM cc_state WHERE key = %s", (KEY_STATE,)).fetchone()
    try:
        return json.loads(row["value"]) if row else None
    except (TypeError, ValueError):
        return None


def key_hint(conn) -> str | None:
    """ "…1234" for a saved key, read without opening it; None for none."""
    kept = _stored(conn)
    return (kept.get("last4") or "…") if kept else None


def load_key(conn, vault) -> str | None:
    kept = _stored(conn)
    return vault.open(kept["sealed"].encode(), "state", KEY_STATE, legacy=True) if kept and kept.get("sealed") else None


# --- a rate as a plan version -------------------------------------------------------------


def _day(epoch) -> date | None:
    """The day a rate starts or ends. OpenEI keeps a US utility's local midnight, 04:00 to
    10:00 UTC, so the UTC date is that day whatever zone this install is in."""
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _money(x: float) -> str:
    """Dollars to the cent, or to as many places as a price per kWh needs (at least two)."""
    whole, _, frac = f"{x:,.5f}".partition(".")
    return f"${whole}.{frac.rstrip('0').ljust(2, '0')}"


def _months_text(months: set[int]) -> str:
    """Months as a span where they run on (wrapping the year end), else listed."""
    if not months or len(months) == 12:
        return ""
    ms = sorted(months)
    for start in ms:  # a run that wraps: Oct–May
        run = [(start - 1 + i) % 12 + 1 for i in range(len(ms))]
        if set(run) == months:
            a, b = cost.MONTHS[run[0] - 1], cost.MONTHS[run[-1] - 1]
            return a if a == b else f"{a}–{b}"
    return ", ".join(cost.MONTHS[m - 1] for m in ms)


def _grid(g, n: int, what: str, name: str) -> list[list[int]]:
    try:
        rows = [[int(i) for i in row] for row in g]
    except (TypeError, ValueError):
        rows = []
    if len(rows) != 12 or any(len(r) != 24 or any(not 0 <= i < n for i in r) for r in rows):
        raise URDBError(f"OpenEI's {what} schedule for {name} is not a 12-month, 24-hour grid of its periods.")
    return rows


def _rank_name(rank: int, k: int) -> str:
    """The name of the rank-th cheapest of k prices in force in a month."""
    if k == 1:
        return "All hours"
    if rank == k - 1:
        return "Peak"
    if k >= 4 and rank == 0:
        return "Super off-peak"
    return "Off-peak" if rank == (1 if k >= 4 else 0) else "Part-peak"


def _labels(prices: list[float], weekday, weekend) -> list[str]:
    """A name per period from where its price sits among those of the months it is used in:
    the dearest is peak, the cheapest off-peak (super off-peak when a month has four prices
    or more), anything between part-peak; a month with one price is all hours. Periods
    that share a name get their months, then a number."""
    used: dict[int, set[int]] = {i: set() for i in range(len(prices))}
    roles: dict[int, list[str]] = {i: [] for i in range(len(prices))}
    for m in range(12):
        idx = set(weekday[m]) | set(weekend[m])
        here = sorted({prices[i] for i in idx})
        for i in idx:
            used[i].add(m + 1)
            roles[i].append(_rank_name(here.index(prices[i]), len(here)))
    names = [max(sorted(set(r)), key=r.count) if r else "Not used" for r in roles.values()]
    names = [(f"{n}, {_months_text(used[i])}" if names.count(n) > 1 and _months_text(used[i]) else n) for i, n in enumerate(names)]
    return [n if names.count(n) == 1 else f"{n} ({i + 1})" for i, n in enumerate(names)]


def version_of(item: dict) -> dict:
    """One URDB rate as a plan version. Raises URDBError when it cannot price a kWh."""
    name = item.get("name") or "this plan"
    start = _day(item.get("startdate"))
    if start is None:
        raise URDBError(f"OpenEI gives no start date for {name}.")
    structure = item.get("energyratestructure")
    if not isinstance(structure, list) or not structure:
        raise URDBError(f"{name} has no energy charges in OpenEI, so it cannot price a charge.")
    prices, higher = [], []
    for tiers in structure:
        if not isinstance(tiers, list) or not tiers or not isinstance(tiers[0], dict):
            raise URDBError(f"{name} has a period with no price in OpenEI.")
        for t in tiers:
            unit = str(t.get("unit") or "kWh")
            if not unit.lower().startswith("kwh"):
                raise URDBError(f"{name} charges energy per {unit}, not per kWh, so it cannot be used here.")
        try:
            each = [float(t.get("rate") or 0) + float(t.get("adj") or 0) for t in tiers]
        except (TypeError, ValueError):
            raise URDBError(f"{name} has a price OpenEI does not give as a number.") from None
        if each[0] < 0:
            raise URDBError(f"{name} has a negative price for energy, which cannot be used here.")
        prices.append(round(each[0], 5))
        higher.append((tiers[0].get("max"), str(tiers[0].get("unit") or "kWh"), each[1:]))
    n = len(prices)
    weekday = _grid(item.get("energyweekdayschedule"), n, "weekday", name)
    weekend = _grid(item.get("energyweekendschedule"), n, "weekend", name)
    labels = _labels(prices, weekday, weekend)
    notes = []
    tiered = [(labels[i], cap, unit, rest) for i, (cap, unit, rest) in enumerate(higher) if rest]
    if tiered:
        bits = []
        for label, cap, unit, rest in tiered:
            over = f" over {cap:g} {unit}" if isinstance(cap, (int, float)) else ""
            bits.append(f"{label} {', then '.join(_money(x) for x in rest)}{over}")
        notes.append("Tiered by use: each period is priced at its first tier. Higher tiers, not priced: " + "; ".join(bits) + ".")
    for amount, unit, what in ((item.get("fixedchargefirstmeter"), item.get("fixedchargeunits"), "Fixed charge"), (item.get("mincharge"), item.get("minchargeunits"), "Minimum charge")):
        if isinstance(amount, (int, float)) and amount > 0:
            notes.append(f"{what} {_money(float(amount))}{(unit or '').replace('$', '')}, not priced.")
    if item.get("demand"):
        notes.append("Demand charges, not priced.")
    return {
        "from": start.isoformat(),
        "periods": [{"key": f"p{i}", "label": labels[i], "price": prices[i]} for i in range(n)],
        "weekday": weekday,
        "weekend": weekend,
        "fixed_note": " ".join(notes) or None,
    }


# --- versions of one tariff -------------------------------------------------------------


def _norm(text) -> str:
    return " ".join(str(text or "").split()).casefold()


def groups(items: list[dict]) -> list[list[dict]]:
    """The rates as tariffs: same utility and name, joined across a rename by `supersedes`;
    each oldest first."""
    parent: dict[str, str] = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_label = {x["label"]: x for x in items if x.get("label")}
    by_name: dict[tuple, str] = {}
    for label, x in by_label.items():
        parent[label] = label
        first = by_name.setdefault((_norm(x.get("utility")), _norm(x.get("name"))), label)
        parent[find(label)] = find(first)
    for label, x in by_label.items():
        prev = x.get("supersedes")
        if prev in by_label and _norm(by_label[prev].get("utility")) == _norm(x.get("utility")):
            parent[find(prev)] = find(label)
    out: dict[str, list[dict]] = {}
    for label in by_label:
        out.setdefault(find(label), []).append(by_label[label])
    return [sorted(g, key=lambda x: (int(x.get("startdate") or 0), x["label"])) for g in out.values()]


def chain(page, label: str) -> list[dict]:
    """`label`'s rate and the ones it supersedes, newest first, through `page(label) -> rate
    or None` (the list in hand, else OpenEI). Stops at a gap, a loop or MAX_CHAIN."""
    out, seen = [], set()
    while label and label not in seen and len(out) < MAX_CHAIN:
        seen.add(label)
        x = page(label)
        if not x:
            break
        out.append(x)
        label = x.get("supersedes")
    return out


def versions_of(label: str, items: list[dict]) -> list[dict]:
    """Every version of the tariff `label` is one of, oldest first; one per start date.
    Two on the same date: the one picked, then one it supersedes or is superseded by,
    then the later-listed."""
    by_label = {x["label"]: x for x in items if x.get("label")}
    picked = {x["label"] for x in chain(by_label.get, label)}
    newer = label
    while True:  # the ones superseding the pick count as its line too
        nxt = next((x["label"] for x in items if x.get("supersedes") == newer and x.get("label") not in picked), None)
        if not nxt:
            break
        picked.add(nxt)
        newer = nxt
    for g in groups(items):
        if any(x["label"] == label for x in g):
            by_start: dict[int, dict] = {}
            for x in g:
                start, have = int(x.get("startdate") or 0), by_start.get(int(x.get("startdate") or 0))
                if have is None or (x["label"] == label) or (x["label"] in picked and have["label"] != label):
                    by_start[start] = x
            return [by_start[k] for k in sorted(by_start)]
    return []


def to_plan(versions: list[dict]) -> dict:
    """A home plan from a tariff's versions, oldest first. Versions that cannot price a
    kWh are left out while one can; with none, the newest one's reason is raised."""
    out, last_err = [], None
    for x in versions:
        try:
            v = version_of(x)
        except URDBError as err:
            last_err = err
            continue
        out = [o for o in out if o["from"] != v["from"]] + [v]
    if not out:
        raise last_err or URDBError("OpenEI has no version of that plan.")
    newest = versions[-1]
    plan = {"name": " ".join(str(newest.get("name") or "Home").split())[:40], "utility": newest.get("utility"), "source": f"urdb:{newest['label']}", "versions": out}
    if str(newest.get("uri") or "").startswith("https://"):
        plan["link"] = newest["uri"]
    if not cost.valid_plan(plan):
        raise URDBError("OpenEI's plan did not come out as a usable grid.")
    return plan


# --- the list ---------------------------------------------------------------------------


def kind(v: dict) -> str:
    """'Time-of-use' when a day has more than one price, 'Seasonal' when only the months
    differ, else 'Flat'."""
    prices = [p["price"] for p in v["periods"]]
    months = [{prices[i] for i in v["weekday"][m]} | {prices[i] for i in v["weekend"][m]} for m in range(12)]
    days = [{prices[i] for i in row} for g in (v["weekday"], v["weekend"]) for row in g]
    if any(len(d) > 1 for d in days):
        return "Time-of-use"
    return "Seasonal" if len({frozenset(m) for m in months}) > 1 else "Flat"


def summary(group: list[dict], today: date) -> dict:
    """One tariff as a row of the list: its newest version's name, kind and fixed charge."""
    newest = group[-1]
    end = _day(newest.get("enddate"))
    try:
        v = version_of(newest)
        what, tiered, usable = kind(v), "Tiered" in (v["fixed_note"] or ""), True
    except URDBError:
        what, tiered, usable = "No energy price", False, False
    fixed = newest.get("fixedchargefirstmeter")
    return {
        "label": newest["label"],
        "name": " ".join(str(newest.get("name") or "").split()),
        "utility": newest.get("utility") or "",
        "first": _day(group[0].get("startdate")),
        "start": _day(newest.get("startdate")),
        "end": end,
        "current": end is None or end >= today,
        "versions": len({x.get("startdate") for x in group}),
        "kind": what,
        "tiered": tiered,
        "fixed": f"{_money(float(fixed))}{(newest.get('fixedchargeunits') or '').replace('$', '')}" if isinstance(fixed, (int, float)) and fixed > 0 else None,
        "usable": usable,
    }


def listing(items: list[dict], today: date, contains: str = "") -> dict:
    """The search as Settings shows it: a row per tariff, current ones first, then by name;
    `contains` narrows by name. At most SHOWN rows, and how many there were."""
    rows = [summary(g, today) for g in groups(items)]
    want = _norm(contains)
    if want:
        rows = [r for r in rows if want in _norm(r["name"])]
    rows.sort(key=lambda r: (not r["current"], not r["usable"], _norm(r["name"])))
    return {"rows": rows[:SHOWN], "total": len(rows), "more": bool(getattr(items, "more", False)), "utilities": sorted({r["utility"] for r in rows if r["utility"]})}


def preview(plan: dict) -> dict:
    """What the page shows before "Use this plan": the plan, its version dates newest first,
    and its newest version's note."""
    vs = plan["versions"]
    return {"plan": plan, "label": plan["source"].split(":", 1)[1], "dates": [v["from"] for v in reversed(vs)], "note": vs[-1].get("fixed_note"), "kind": kind(vs[-1])}


# --- the bundled utility list -----------------------------------------------------------

UTILITIES = Path(__file__).parent / "rates" / "utilities.json"
_UTILITIES: dict | None = None


def _compact(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


def _initials(name: str) -> str:
    return "".join(w[0] for w in re.findall(r"[a-z0-9]+", name.casefold()))


def utilities() -> dict:
    """The list as built, read once: {"utilities": [[eia, name, states, customers], ...],
    "aliases": {...}}. Empty when the file is missing."""
    global _UTILITIES  # noqa: PLW0603 - read once, on the first search
    if _UTILITIES is None:
        try:
            _UTILITIES = json.loads(UTILITIES.read_text())
        except (OSError, ValueError):
            _UTILITIES = {"utilities": [], "aliases": {}}
    return _UTILITIES


def utilities_meta() -> dict:
    u = utilities()
    return {"year": u.get("year"), "source": u.get("source"), "count": len(u.get("utilities") or [])}


def suggest(text: str, n: int = 8) -> list[dict]:
    """Utilities for what is typed, best first: an alias, a name or its initials starting
    with it, a name containing it; then the most customers. The page does the same in
    the browser as you type; nothing is sent."""
    want = _compact(text)
    if not want:
        return []
    u = utilities()
    alias = {_compact(k): v for k, v in (u.get("aliases") or {}).items()}
    hits = []
    for i, (eia, name, states, _customers) in enumerate(u.get("utilities") or []):
        c = _compact(name)
        if alias.get(want) == eia:
            rank = 0
        elif c == want:
            rank = 1
        elif c.startswith(want) or _initials(name).startswith(want):
            rank = 2
        elif want in c:
            rank = 3
        else:
            continue
        hits.append((rank, i, {"eia": eia, "name": name, "states": states}))
    return [h[2] for h in sorted(hits)[:n]]


def utility_for(text: str) -> dict | None:
    """The utility `text` names outright: its alias or its whole name. A partial name is
    only a suggestion, so it is not taken as a pick."""
    want = _compact(text)
    if not want:
        return None
    u = utilities()
    eia = {_compact(k): v for k, v in (u.get("aliases") or {}).items()}.get(want)
    for e, name, states, _customers in u.get("utilities") or []:
        if e == eia or _compact(name) == want:
            return {"eia": e, "name": name, "states": states}
    return None
