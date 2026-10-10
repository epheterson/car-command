"""Average household electricity and petrol prices by region and month.

The figures come from one file, rates/averages.json (built by
scripts/build-rate-averages.py from US EIA, Eurostat and the EU Weekly Oil Bulletin).
The image carries the copy current when it was built; RatesWorker replaces it in the
background with the published one at RATES_URL when a home charge is in a region the
copy lacks, or the copy is more than MAX_AGE_DAYS old, at most once a day, and keeps the
downloaded copy in the database (cc_rate_averages) so a restart does not lose it.

The download is the whole file from a fixed URL with no parameters: nothing about where
any car has been is sent. A failed download keeps the copy in use and is shown on
Settings › Money.

A month with no figure yet (EIA runs about two months behind, Eurostat half a year)
takes the newest earlier one, and says which; an earlier figure more than MAX_LAG_MONTHS
old is not used at all.
"""

from __future__ import annotations

import json
import logging
import threading
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

LOG = logging.getLogger("carcommand.averages")

# Where every install fetches the published file. One constant, so a release (or a fork,
# or someone hosting their own copy) changes one line; CC_RATES_URL overrides it.
RATES_URL = "https://raw.githubusercontent.com/epheterson/car-command/rates-data/rates/averages.json"
BUNDLED = Path(__file__).resolve().parent / "rates" / "averages.json"
MAX_AGE_DAYS = 45
MIN_GAP = timedelta(days=1)  # between automatic downloads
MAX_LAG_MONTHS = 24
MAX_BYTES = 8_000_000  # a published file is under 1 MB; anything this big is not one

LITRES_PER_GALLON = 3.78541

DDL = """
CREATE TABLE IF NOT EXISTS cc_rate_averages (
    id         INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    data       JSONB,
    fetched_at TIMESTAMPTZ,
    tried_at   TIMESTAMPTZ,
    error      TEXT
);
"""

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def month_label(ym: str | None) -> str:
    """'2026-07' -> 'Jul 2026'."""
    try:
        y, m = ym.split("-")
        return f"{MONTHS[int(m) - 1]} {y}"
    except (AttributeError, ValueError, IndexError):
        return ym or ""


def _months_between(a: str, b: str) -> int:
    (ya, ma), (yb, mb) = (map(int, a.split("-")), map(int, b.split("-")))
    return (yb - ya) * 12 + (mb - ma)


def valid(data) -> bool:
    """Whether a file has the shape pricing reads: regions with dated series of numbers."""
    try:
        if data.get("version") != 1 or not isinstance(data.get("regions"), dict) or len(data["regions"]) < 5:
            return False
        for r in data["regions"].values():
            for fuel in ("electricity", "petrol"):
                s = r.get(fuel)
                if s is None:
                    continue
                if not s["values"] or not all(isinstance(k, str) and len(k) == 7 and isinstance(v, (int, float)) and v > 0 for k, v in s["values"].items()):
                    return False
                str(s["source"]), str(s["unit"]), str(s["area"])
    except (AttributeError, KeyError, TypeError):
        return False
    return True


class Averages:
    """One copy of the file and where it came from ('bundled' or 'downloaded')."""

    def __init__(self, data: dict, origin: str = "bundled", fetched_at: datetime | None = None) -> None:
        self.data, self.origin, self.fetched_at = data, origin, fetched_at
        self.regions: dict = data.get("regions") or {}
        self._keys: dict[tuple[str, str], list[str]] = {}

    @property
    def built(self) -> date | None:
        try:
            return date.fromisoformat(self.data.get("built"))
        except (TypeError, ValueError):
            return None

    def age_days(self, now: datetime) -> float | None:
        """How old the copy is: since it was downloaded, or since the bundled one was built."""
        if self.fetched_at:
            return (now - self.fetched_at).total_seconds() / 86400
        if self.built:
            return (now.date() - self.built).days
        return None

    def covered(self, fuel: str = "electricity") -> list[str]:
        return sorted(code for code, r in self.regions.items() if r.get(fuel))

    def name(self, code: str | None) -> str | None:
        return (self.regions.get(code) or {}).get("name") if code else None

    def price(self, region: str | None, fuel: str, ym: str) -> dict | None:
        """The figure for `region` in month `ym` ('YYYY-MM'), or the newest earlier one
        within MAX_LAG_MONTHS: {value, month, region, name, area, source, unit, currency}."""
        s = (self.regions.get(region) or {}).get(fuel) if region else None
        if not s:
            return None
        keys = self._keys.get((region, fuel))
        if keys is None:
            keys = self._keys[(region, fuel)] = sorted(s["values"])
        i = bisect_right(keys, ym)
        if not i:
            return None
        used = keys[i - 1]
        if _months_between(used, ym) > MAX_LAG_MONTHS:
            return None
        return {
            "value": float(s["values"][used]),
            "month": used,
            "wanted": ym,
            "region": region,
            "name": self.regions[region].get("name") or region,
            "area": s.get("area") or region,
            "source": s["source"],
            "unit": s["unit"],
            "currency": s.get("currency"),
        }

    def why_not(self, region: str | None, fuel: str, ym: str) -> str:
        """Why price() said nothing, in a few words for a page."""
        if not region:
            return "no position to place it in a region"
        s = (self.regions.get(region) or {}).get(fuel)
        if not s:
            return f"no {fuel} average for {self.name(region) or region}"
        first, last = min(s["values"]), max(s["values"])
        if ym < first:
            return f"the {s['area']} figures start in {month_label(first)}"
        return f"the newest {s['area']} figure is from {month_label(last)}"

    def summary(self) -> list[dict]:
        """One line per source: what it is and the newest month it has, for Settings."""
        seen: dict[str, str] = {}
        for r in self.regions.values():
            for fuel in ("electricity", "petrol"):
                s = r.get(fuel)
                if s and s.get("as_of", "") > seen.get(s["source"], ""):
                    seen[s["source"]] = s["as_of"]
        return [{"source": src, "as_of": ym, "label": month_label(ym)} for src, ym in sorted(seen.items())]


def bundled() -> Averages:
    try:
        data = json.loads(BUNDLED.read_text())
    except (OSError, ValueError) as err:
        LOG.warning("bundled rate averages unreadable: %s", err)
        data = {}
    return Averages(data if valid(data) else {"regions": {}}, "bundled")


def newer(a: Averages, b: Averages | None) -> Averages:
    """The copy to price with: the later-built of the two, the downloaded one on a tie
    (an image upgrade can bring a bundled copy newer than an old download)."""
    if b is None or not b.regions:
        return a
    if not a.regions:
        return b
    ab, bb = a.built or date.min, b.built or date.min
    if ab == bb:
        return a if a.origin == "downloaded" else b
    return a if ab > bb else b


_ACTIVE: Averages | None = None


def active() -> Averages:
    """The copy in force; the bundled one until the database's is read or a download lands."""
    global _ACTIVE  # noqa: PLW0603 - the one place the copy is set
    if _ACTIVE is None:
        _ACTIVE = bundled()
    return _ACTIVE


def use(copy: Averages) -> None:
    global _ACTIVE  # noqa: PLW0603
    _ACTIVE = copy


def due(copy: Averages, now: datetime, tried_at: datetime | None, missing: list[str], fetched_at: datetime | None = None) -> str | None:
    """Why a download is due now, or None. At most once a day. A region the copy lacks
    asks only until one download has landed (`fetched_at`): the published file lacked it
    then and will not have it tomorrow either, so after that only age counts."""
    if tried_at and now - tried_at < MIN_GAP:
        return None
    age = copy.age_days(now)
    if age is None or age > MAX_AGE_DAYS:
        return "older than 45 days"
    if missing and not fetched_at and not copy.fetched_at:
        return "a home charge is in a region the bundled copy lacks"
    return None


def status_row(conn) -> dict:
    """When the last download landed and was last tried, and how the last try went."""
    row = conn.execute("SELECT fetched_at, tried_at, error FROM cc_rate_averages WHERE id = 1").fetchone()
    if not row:
        return {}
    return dict(row) if isinstance(row, dict) else dict(zip(("fetched_at", "tried_at", "error"), row))


def stored(conn) -> Averages | None:
    """The downloaded copy kept in the database, if there is a good one."""
    row = conn.execute("SELECT data, fetched_at FROM cc_rate_averages WHERE id = 1 AND data IS NOT NULL").fetchone()
    if not row:
        return None
    data, fetched_at = (row["data"], row["fetched_at"]) if isinstance(row, dict) else (row[0], row[1])
    if isinstance(data, str):
        data = json.loads(data)
    return Averages(data, "downloaded", fetched_at) if data and valid(data) else None


def settings_view(copy: Averages, status: dict, homes: list, now: datetime, url: str, tz=timezone.utc) -> dict:
    """What Settings › Money shows about the averages: the copy in force and where it came
    from, each source's newest month, the last download's result, and for each region home
    charges have been in, this month's figures (or why there are none)."""
    ym = now.astimezone(tz).strftime("%Y-%m")
    out_homes = []
    for h in homes:
        code, n, est = (h["region"], h["n"], h["est"]) if isinstance(h, dict) else tuple(h)
        if not code:
            continue
        out_homes.append(
            {
                "code": code,
                "name": copy.name(code) or code,
                "charges": n,
                "estimated": est,
                "price": copy.price(code, "electricity", ym),
                "why": copy.why_not(code, "electricity", ym),
                "petrol": copy.price(code, "petrol", ym),
            }
        )
    return {
        "origin": copy.origin,
        "built": copy.built,
        "fetched_at": status.get("fetched_at") or copy.fetched_at,
        "tried_at": status.get("tried_at"),
        "error": status.get("error"),
        "sources": copy.summary(),
        "homes": out_homes,
        "url": url,
    }


def missing_regions(conn, copy: Averages) -> list[str]:
    """Regions of home charges the copy has no electricity figure for."""
    rows = conn.execute("SELECT DISTINCT region FROM cc_charge_facts WHERE at_home AND region IS NOT NULL AND region <> ''").fetchall()
    have = set(copy.covered("electricity"))
    return sorted(code for code in ((r["region"] if isinstance(r, dict) else r[0]) for r in rows) if code not in have)


def http_get(url: str) -> bytes:
    import httpx

    with httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": "car-command"}) as client:
        r = client.get(url)
        r.raise_for_status()
        if len(r.content) > MAX_BYTES:
            raise ValueError(f"{len(r.content)} bytes is not a rate file")
        return r.content


class RatesWorker(threading.Thread):
    """Keeps the copy in force current, in the background; a page never waits on it.

    `fetch(url) -> bytes`; `after_change(conn)` runs once a new copy is in force (it puts
    the charges the new figures can price back in the queue); `missing(conn)` lists the
    regions of home charges the copy has no electricity figure for."""

    def __init__(self, connect: Callable, fetch: Callable[[str], bytes], url: str, after_change: Callable, missing: Callable, interval_s: int = 3600) -> None:
        super().__init__(daemon=True, name="rate-averages")
        self.connect, self.fetch, self.url, self.after_change, self.missing = connect, fetch, url, after_change, missing
        self.interval_s = interval_s
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.forced = False

    def request(self) -> None:
        """Download on the next pass whatever the schedule says (Settings' "Update now")."""
        self.forced = True
        self.wake.set()

    def load(self) -> None:
        """Put the database's copy in force if it is newer than the bundled one."""
        with self.connect() as conn:
            use(newer(bundled(), stored(conn)))

    def round(self, now: datetime | None = None) -> str | None:
        """One pass: 'updated', 'unchanged', 'failed', or None when nothing was due.

        Three steps, and no transaction across the download: a short look at whether one
        is due, the download with nothing open, then a short write of the result."""
        now = now or datetime.now(timezone.utc)
        forced, self.forced = self.forced, False
        copy = active()
        if forced:
            why = "asked for on Settings"
        else:
            with self.connect() as conn:
                status = status_row(conn)
                why = due(copy, now, status.get("tried_at"), self.missing(conn, copy), status.get("fetched_at"))
                conn.commit()
        if not why:
            return None
        LOG.info("rate averages: downloading (%s)", why)
        try:
            data = json.loads(self.fetch(self.url))
            if not valid(data):
                raise ValueError("the file is not a rate averages file")
        except Exception as err:  # noqa: BLE001 - any failure keeps the copy in force
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO cc_rate_averages (id, tried_at, error) VALUES (1, %s, %s) ON CONFLICT (id) DO UPDATE SET tried_at = EXCLUDED.tried_at, error = EXCLUDED.error",
                    (now, (str(err).splitlines() or [""])[0][:300] or type(err).__name__),
                )
                conn.commit()
            LOG.warning("rate averages: %s", err)
            return "failed"
        changed = data.get("regions") != copy.regions
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO cc_rate_averages (id, data, fetched_at, tried_at, error) VALUES (1, %s, %s, %s, NULL)
                   ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, fetched_at = EXCLUDED.fetched_at, tried_at = EXCLUDED.tried_at, error = NULL""",
                (json.dumps(data), now, now),
            )
            use(newer(bundled(), Averages(data, "downloaded", now)))
            if changed:
                self.after_change(conn)
            conn.commit()
        return "updated" if changed else "unchanged"

    def run(self) -> None:
        try:
            self.load()
        except Exception as err:  # noqa: BLE001
            LOG.warning("rate averages: reading the stored copy: %s", err)
        self.wake.wait(120)  # let startup settle before the first look
        while not self.stop.is_set():
            self.wake.clear()
            try:
                self.round()
            except Exception as err:  # noqa: BLE001
                LOG.warning("rate averages round failed: %s", err)
            self.wake.wait(self.interval_s)
