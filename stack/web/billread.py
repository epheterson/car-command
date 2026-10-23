"""Read a home electricity bill for Find my plan: the rate schedule it is billed on, the
utility, the community-choice provider that sells the generation (when one does), and what
each kWh cost in each time-of-use period, delivery and generation together.

A PDF is someone else's file, so it is read in a child process (`parse`): ten seconds, half
a gigabyte, pypdf's own limits lowered, the text cut to 200,000 characters in lines of at
most 2,000, and every pattern below bounded so none can backtrack without end. Whatever
goes wrong in there, the person is told the bill could not be read. Nothing of the file is
kept: the reading travels in the finder's links until a plan is made from it.

A bill lists each period's energy as "Peak 123.4 kWh @ $0.52012 $64.18", once under the
utility's delivery charges and, with a community-choice provider, again under its
generation charges. A period's price is the two added up, plus the per-kWh share of the
lines billed as totals on the delivery side (the generation credit, the power charge
indifference adjustment, the franchise fee surcharge), spread over all of the bill's kWh.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import threading
from datetime import date
from pathlib import Path

try:
    import resource  # imported here, never between fork and exec
except ImportError:  # pragma: no cover - not on Windows
    resource = None

HERE = Path(__file__).resolve().parent
MAX_BYTES = 10_000_000
MAX_PAGES = 12
MAX_TEXT = 200_000
MAX_LINE = 2_000
TIMEOUT_S = 10
MEMORY = 512 * 1024 * 1024
UNREADABLE = "Couldn't read this bill. Download it again from your utility's site as a PDF, or pick your utility instead."
BUSY = "Another bill is being read. Try again in a moment."
UTILITY_CHARS = 20_000  # the utility's name is on the first page
_SLOTS = threading.BoundedSemaphore(2)  # bills read at once: each may take 512 MB for 10 s

SCHEDULE = re.compile(r"\b(?:Rate Schedule|Rate Plan|Schedule|Rate)[ \t]{0,3}[:#]?[ \t]{0,3}([A-Z][A-Z0-9]{0,11}(?:[-‑][A-Z0-9]{1,6}){0,3})\b")
PERIOD = re.compile(
    r"^[ \t]{0,8}(?P<name>Super[ \t-]{0,2}Off[ \t-]{0,2}Peak|Off[ \t-]{0,2}Peak|Part(?:ial)?[ \t-]{0,2}Peak|Mid[ \t-]{0,2}Peak|On[ \t-]{0,2}Peak|Peak)\b[^\n]{0,60}?"
    r"(?P<kwh>\d[\d,]{0,9}(?:\.\d{1,8})?)[ \t]{0,3}kWh[ \t]{0,3}(?:@|at|x|×)[ \t]{0,3}\$?[ \t]{0,2}(?P<rate>\d{0,2}\.\d{1,8})",
    re.I | re.M,
)
TOTALS = re.compile(r"(Generation Credit|Power Charge Indifference Adjustment|Franchise Fee Surcharge)[^\n$\d-]{0,40}(-?)[ \t]{0,3}\$?[ \t]{0,3}(-?)([\d,]{0,12}\.\d\d)", re.I)
GENERATION = re.compile(r"generation", re.I)
DATES = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})[ \t]{0,3}[-–][ \t]{0,3}(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
ROLE = {"superoffpeak": "super_off_peak", "offpeak": "off_peak", "partpeak": "part_peak", "partialpeak": "part_peak", "midpeak": "part_peak", "onpeak": "peak", "peak": "peak"}
ORDER = ("super_off_peak", "off_peak", "part_peak", "peak")
GENERIC = {"SCHEDULE", "PLAN", "RATE", "NUMBER", "TYPE"}
CCA = HERE / "rates" / "cca.json"
_CCA: dict | None = None


class BillError(Exception):
    """Something to tell the user about the file."""


class BillBusy(BillError):
    """Two bills are being read already."""


# --- the child process: pypdf and the patterns, fenced in -------------------------------------


def _limits() -> None:
    """The child's ceilings, set by the child itself before it reads a byte of the bill."""
    if resource is None:  # pragma: no cover - not on Windows
        return
    for what, value in ((resource.RLIMIT_AS, MEMORY), (resource.RLIMIT_CPU, TIMEOUT_S + 2)):
        try:
            resource.setrlimit(what, (value, value))
        except (ValueError, OSError):
            pass  # macOS refuses RLIMIT_AS; the timeout still holds


def check(data: bytes) -> None:
    """What can be told from the bytes alone."""
    if not data:
        raise BillError("Choose your bill's PDF.")
    if len(data) > MAX_BYTES:
        raise BillError("That file is over 10 MB; a bill's PDF is far smaller.")
    if data.lstrip()[:5] != b"%PDF-":
        raise BillError("That is not a PDF. Download the bill from your utility's site as a PDF.")


def parse(data: bytes) -> dict:
    """{"bill": read(...), "utility": urdb.utility_in(...)} for a PDF's bytes, worked out in a
    child process with a time and memory limit. Raises BillError, never anything else."""
    check(data)
    if not _SLOTS.acquire(blocking=False):
        raise BillBusy(BUSY)
    try:
        done = subprocess.run(  # noqa: S603 - this interpreter, this file, no shell; the child sets its own limits
            [sys.executable, str(HERE / "billread.py")],
            input=data,
            capture_output=True,
            timeout=TIMEOUT_S,
            cwd=HERE,
            check=False,
        )
        out = json.loads(done.stdout or b"{}") if done.returncode == 0 else {}
    except (subprocess.TimeoutExpired, OSError, ValueError):
        out = {}
    finally:
        _SLOTS.release()
    if "error" in out:
        raise BillError(out["error"])
    if not isinstance(out.get("bill"), dict):
        raise BillError(UNREADABLE)
    return out


def clip(text: str) -> str:
    """At most MAX_TEXT characters, in lines of at most MAX_LINE."""
    return "\n".join(line[:MAX_LINE] for line in text[: MAX_TEXT * 2].splitlines())[:MAX_TEXT]


def text_of(data: bytes) -> str:
    """The bill's text, page by page, clipped. pypdf's limits are lowered to what a bill
    needs; anything it raises is a bill that cannot be read."""
    check(data)
    import pypdf

    pypdf.overwrite_configuration(
        maximum_declared_stream_length=8_000_000,
        array_based_stream_maximum_output_length=8_000_000,
        zlib_maximum_output_length=8_000_000,
        lzw_maximum_output_length=8_000_000,
        run_length_maximum_output_length=8_000_000,
        stream_decoding_work_maximum_length=40_000_000,
        image_maximum_buffer_size=8_000_000,
        page_tree_maximum_entries=200,
        xform_maximum_invocations_per_extraction=200,
    )
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise BillError("That PDF is locked with a password.")
        parts, size = [], 0
        for page in reader.pages[:MAX_PAGES]:
            text = page.extract_text() or ""
            parts.append(text)
            size += len(text)
            if size > MAX_TEXT:
                break
        return clip("\n".join(parts))
    except BillError:
        raise
    except Exception:  # noqa: BLE001 - pypdf's errors (PyPdfError and its kin) and the rest alike
        raise BillError(UNREADABLE) from None


def _child() -> None:  # pragma: no cover - the child process's whole life
    _limits()
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    try:
        text = text_of(data)
        import urdb

        out = {"bill": read(text), "utility": urdb.utility_in(text[:UTILITY_CHARS])}
    except BillError as err:
        out = {"error": str(err)}
    except Exception:  # noqa: BLE001 - whatever it was, the person hears the same
        out = {"error": UNREADABLE}
    sys.stdout.write(json.dumps(out, default=str))


# --- what the text says ----------------------------------------------------------------------


def providers() -> list[dict]:
    """The California community-choice providers (rates/cca.json), read once."""
    global _CCA  # noqa: PLW0603 - read once
    if _CCA is None:
        try:
            _CCA = json.loads(CCA.read_text())
        except (OSError, ValueError):
            _CCA = {"providers": []}
    return _CCA.get("providers") or []


def providers_for(eia) -> list[dict]:
    """The community-choice providers in a utility's territory: {"name", "label"} each, the
    label what the provider calls itself on a bill ("MCE", "SVCE")."""
    try:
        eia = int(eia)
    except (TypeError, ValueError):
        return []
    return [{"name": p["name"], "label": p.get("label") or p["name"]} for p in providers() if eia in p.get("utilities", [])]


def _role(name: str) -> str:
    return ROLE.get(re.sub(r"[^a-z]", "", name.lower()), "peak")


def provider(text: str) -> str | None:
    """The community-choice provider the bill names, from rates/cca.json only: a full name,
    or a short one that cannot be mistaken for anything else (four letters or more, or MCE,
    3CE)."""
    low = text.lower()
    keys = []
    for p in providers():
        keys.append((p["name"], p["name"]))
        keys += [(s, p["name"]) for s in p.get("short", []) if len(s) >= 4 or s in ("MCE", "3CE")]
    for key, name in sorted(keys, key=lambda k: -len(k[0])):
        if re.search(rf"(?<![a-z0-9]){re.escape(key.lower())}(?![a-z0-9])", low):
            return name
    return None  # a provider the list does not know is not named: a guess could take in an address


def schedule(text: str) -> str | None:
    """The rate schedule the bill names ("EV2A", "E-TOU-C"): a code after "Rate Schedule",
    "Rate" or "Schedule", with a digit or a dash in it or all in capitals."""
    for m in SCHEDULE.finditer(text):
        code = m.group(1).replace("‑", "-")
        if code not in GENERIC and len(code) >= 2 and (any(c.isdigit() for c in code) or "-" in code or code.isupper()):
            return code
    return None


def period_of(text: str) -> tuple[date, date] | None:
    """The billing period, from the first "from - to" dates."""
    m = DATES.search(text)
    if not m:
        return None
    try:
        y = lambda s: int(s) + (2000 if len(s) <= 2 else 0)  # noqa: E731
        a = date(y(m.group(3)), int(m.group(1)), int(m.group(2)))
        b = date(y(m.group(6)), int(m.group(4)), int(m.group(5)))
    except ValueError:
        return None
    return (a, b) if a <= b else None


def read(text: str) -> dict:
    """What the bill says: {"schedule", "provider", "month", "start", "prices": {role:
    $/kWh}, "kwh"}. `month` is where most of the billing period falls, `start` its first
    day. Prices are each period's delivery and generation rates added up, plus the per-kWh
    share of the totals-only lines; a period billed twice in one section (a rate change
    mid-period) is weighted by its kWh."""
    text = clip(text)
    lines = text.splitlines()
    gen_at = None
    # where the generation section starts: the first line naming generation with period
    # lines after it (a heading such as "MCE Electric Generation Charges")
    offset, starts = 0, []
    for line in lines:
        starts.append(offset)
        offset += len(line) + 1
    for i, line in enumerate(lines):
        if i and GENERATION.search(line) and "credit" not in line.lower() and any(PERIOD.match(rest) for rest in lines[i + 1 : i + 30]):
            gen_at = starts[i]
            break
    sections: dict[str, dict[str, list[tuple[float, float]]]] = {"delivery": {}, "generation": {}}
    for m in PERIOD.finditer(text):
        part = "generation" if gen_at is not None and m.start() > gen_at else "delivery"
        try:
            kwh, rate = float(m.group("kwh").replace(",", "")), float(m.group("rate"))
        except ValueError:
            continue
        if 0 < rate < 5:
            sections[part].setdefault(_role(m.group("name")), []).append((kwh, rate))

    def mean(rows):
        k = sum(x for x, _ in rows)
        return sum(x * r for x, r in rows) / k if k else sum(r for _, r in rows) / len(rows)

    delivery = {role: mean(rows) for role, rows in sections["delivery"].items()}
    generation = {role: mean(rows) for role, rows in sections["generation"].items()}
    kwh = sum(x for rows in sections["delivery"].values() for x, _ in rows) or sum(x for rows in sections["generation"].values() for x, _ in rows)
    extra = 0.0
    for m in TOTALS.finditer(text):
        try:
            amount = float(m.group(4).replace(",", ""))
        except ValueError:
            continue
        sign = -1 if (m.group(2) or m.group(3) or "credit" in m.group(1).lower()) else 1
        extra += sign * amount
    per = extra / kwh if kwh else 0.0
    roles = set(delivery) | set(generation)
    prices = {r: round(delivery.get(r, 0.0) + generation.get(r, 0.0) + per, 5) for r in ORDER if r in roles}
    span = period_of(text)
    mid = span[0] + (span[1] - span[0]) / 2 if span else None
    return {
        "schedule": schedule(text),
        "provider": provider(text),
        "month": mid.month if mid else None,
        "start": span[0].isoformat() if span else None,
        "prices": {r: p for r, p in prices.items() if p > 0},
        "kwh": round(kwh, 1),
    }


# --- the reading as it travels in the finder's links -----------------------------------------


def to_query(bill: dict) -> dict:
    """The reading as link parameters: the schedule, the provider, the period and the prices."""
    out = {"bill": bill.get("schedule") or "?", "bp": ",".join(f"{r}:{p:g}" for r, p in (bill.get("prices") or {}).items())}
    if bill.get("provider"):
        out["cca"] = bill["provider"]
    if bill.get("month"):
        out["bm"] = str(bill["month"])
    if bill.get("start"):
        out["bs"] = bill["start"]
    return out


def from_query(q) -> dict | None:
    """A reading back from link parameters; None when there is none or it does not parse."""
    code = (q.get("bill") or "").strip()[:20]
    if not code:
        return None
    prices = {}
    for bit in (q.get("bp") or "").split(",")[:8]:
        role, _, price = bit.partition(":")
        try:
            p = float(price)
        except ValueError:
            continue
        if role in ORDER and 0 < p < 5:
            prices[role] = p
    try:
        m = int(q.get("bm") or 0)
    except ValueError:
        m = 0
    try:
        start = date.fromisoformat((q.get("bs") or "")[:10]).isoformat()
    except ValueError:
        start = None
    return {"schedule": None if code == "?" else code, "provider": (q.get("cca") or "").strip()[:60] or None, "month": m if 1 <= m <= 12 else None, "start": start, "prices": prices}


def query_keys() -> tuple[str, ...]:
    return ("bill", "bp", "cca", "bm", "bs")


# --- the plan the bill names, priced as the bill was ----------------------------------------


def names(bill: dict | None, plan_name: str) -> bool:
    """Whether the bill names this plan, strictly: a code the same in both, or the bill's
    with one option letter after the plan's number (EV2A on the bill, "EV2 (Sch)" in
    OpenEI). E-TOU-C does not name E-TOU, nor E-TOU-C3."""
    import urdb

    if not (bill and bill.get("schedule")):
        return False
    mine, theirs = urdb.codes(bill["schedule"]), urdb.codes(plan_name)
    if mine & theirs:
        return True
    return any(len(a) == len(b) + 1 and a.startswith(b) and b[-1].isdigit() and a[-1].isalpha() for a in mine for b in theirs if len(b) >= 2)


def apply(plan: dict, bill: dict, today: date, before: dict | None = None) -> dict:
    """The plan the bill names (`plan`, from OpenEI) at the bill's prices, from the bill's
    first day on; charges before it keep the plan in use (`before`). The tariff's newest
    hours, each period's price scaled by how the bill's price for it compares with the
    tariff's in the bill's month: cheapest to cheapest when the bill lists as many as the
    month uses, else by role. Periods the bill's month does not use (the other season) are
    scaled the same way and marked `est`; a second bill for that season, applied over a plan
    from the first, sets them and keeps the first bill's."""
    import cost

    prices = bill.get("prices") or {}
    if not prices:
        raise BillError("The bill shows no price per kWh by time of use.")
    v = max(plan["versions"], key=lambda x: x["from"])
    m = (bill.get("month") or today.month) - 1
    tariff = [float(p["price"]) for p in v["periods"]]
    used = sorted({i for i in v["weekday"][m] + v["weekend"][m]}, key=lambda i: (tariff[i], i))
    billed = [prices[r] for r in ORDER if r in prices]
    ratio: dict[int, float] = {}
    if len(billed) == len(used):
        ratio = {i: b / tariff[i] for i, b in zip(used, sorted(billed)) if tariff[i] > 0}
    roles = cost.roles(v)
    by_role = {"off_peak": prices.get("off_peak") or prices.get("super_off_peak"), "part_peak": prices.get("part_peak"), "peak": prices.get("peak")}
    in_month: dict[str, list[float]] = {}
    for i in used:
        in_month.setdefault(roles[i], []).append(tariff[i])
    role_ratio = {r: by_role[r] / (sum(xs) / len(xs)) for r, xs in in_month.items() if by_role.get(r) and sum(xs) > 0}
    fallback = sum(role_ratio.values()) / len(role_ratio) if role_ratio else (sum(ratio.values()) / len(ratio) if ratio else 1.0)
    # a period of the other season takes the ratio of the bill month's period at its rank
    # (cheapest, next, dearest) among the periods of its own months
    for i in range(len(tariff)):
        if i in used or i in ratio:
            continue
        months = [mm for mm in range(12) if i in v["weekday"][mm] + v["weekend"][mm]]
        if not months:
            continue
        own = sorted({j for j in v["weekday"][months[0]] + v["weekend"][months[0]]}, key=lambda j: (tariff[j], j))
        if len(own) == len(used) and used[own.index(i)] in ratio:
            ratio[i] = ratio[used[own.index(i)]]
    periods = []
    for i, p in enumerate(v["periods"]):
        new = {k: val for k, val in p.items() if k != "est"}
        new["price"] = round(tariff[i] * ratio.get(i, role_ratio.get(roles[i], fallback)), 5)
        if i not in used:
            new["est"] = True
        periods.append(new)
    source = f"bill:{plan['source'].split(':', 1)[-1]}"
    start = bill.get("start") or date(today.year, m + 1, 1).isoformat()
    earlier = []
    if before and cost.valid_plan(before) and before.get("source") == source:
        # a second bill for the same plan: its month's periods set, the first bill's kept
        kept = max(before["versions"], key=lambda x: x["from"])
        if len(kept["periods"]) == len(periods):
            periods = [new if i in used or old.get("est") else old for i, (new, old) in enumerate(zip(periods, kept["periods"]))]
        start = min(start, kept["from"])
        earlier = [x for x in before["versions"] if x["from"] < start]
    elif before and cost.valid_plan(before):
        earlier = [x for x in before["versions"] if x["from"] < start]
    name = " · ".join(x for x in (bill.get("schedule") or plan["name"], bill.get("provider")) if x)
    out = {
        "name": f"{name} (your bill)"[:40],
        "utility": plan.get("utility"),
        "source": source,
        "versions": [*earlier, {**v, "from": start, "periods": periods, "fixed_note": "Prices from your bill, delivery and generation together."}],
    }
    if plan.get("link"):
        out["link"] = plan["link"]
    if not cost.valid_plan(out):
        raise BillError("The bill's prices did not fit that plan's hours.")
    return out


if __name__ == "__main__":  # pragma: no cover - parse() runs this file as the child
    sys.path.insert(0, str(HERE))
    _child()
