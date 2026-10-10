"""What Tesla actually billed for each Supercharger session, from the Fleet API's charging
history, kept in cc_billed_charges and matched to our own sessions by car and start time.

A charge with a bill is priced at the bill (facts.price_billed, first in the chain). The
history is read by BilledWorker in the background, never on a page load, at most every
MIN_GAP, and only when Connect Tesla holds a token granted the charging scope. What goes
to Tesla is the token (in a header), the VIN and the page; nothing about where the car
has been. The bill's own invoice PDF is fetched only when someone opens it, through the
site (/dx/charging/invoice/{id}), so the token never reaches a browser.

Tesla's shape (GET /api/1/dx/charging/history), one item per session:
  {"sessionId": 123, "vin": "...", "siteLocationName": "Springfield, IL",
   "chargeStartDateTime": "2026-09-20T13:02:11-05:00", "chargeStopDateTime": "...",
   "countryCode": "US", "billingType": "IMMEDIATE",
   "fees": [{"feeType": "CHARGING", "currencyCode": "USD", "pricingType": "PAYMENT",
             "rateBase": 0.48, "usageBase": 40, "uom": "kwh", "totalDue": 19.2, ...},
            {"feeType": "PARKING", "totalDue": 0, "uom": "min", ...}],
   "invoices": [{"fileName": "....pdf", "contentId": "abc-123", "invoiceType": "IMMEDIATE"}]}
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from places import distance_m

LOG = logging.getLogger("carcommand.billed")

DDL = """
CREATE TABLE IF NOT EXISTS cc_billed_charges (
    id           VARCHAR PRIMARY KEY,
    vin          VARCHAR NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    stopped_at   TIMESTAMPTZ,
    site         VARCHAR,
    currency     VARCHAR,
    total        DOUBLE PRECISION NOT NULL,
    kwh          DOUBLE PRECISION,
    invoice_id   VARCHAR,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cc_billed_charges_vin_start ON cc_billed_charges (vin, started_at);
"""

WINDOW = timedelta(minutes=10)  # a bill's start and our session's start agree to within this
NEAR_M = 1000.0  # and, when both carry a position, are this close (Tesla's history carries none today)
MIN_GAP = timedelta(hours=6)  # between reads of the history
PAGE = 50
MAX_PAGES = 100
OVERLAP = timedelta(days=2)  # each read starts this far before the newest bill held, for late ones
# cc_state keys
TRIED = "billed_tried_at"  # when a read last began, finished or not: the MIN_GAP is from this
STAMP = "billed_read_at"  # when a read last finished for every car
BASE_KEY = "billed_page_base"  # the number of the history's first page, 0 or 1, once learned
FROM_KEY = "billed_from:"  # + VIN: where the next read starts, moved only when a read finishes


def _time(text) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _num(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_item(item: dict) -> dict | None:
    """One history item as a row, or None when it has no id, car, start or amount. The
    total is every fee (charging and idle), what the card was charged; kWh is the charging
    fee's usage where it is metered by the kWh."""
    if not isinstance(item, dict):
        return None
    sid, vin, start = item.get("sessionId"), item.get("vin"), _time(item.get("chargeStartDateTime"))
    fees = [f for f in item.get("fees") or [] if isinstance(f, dict)]
    dues = [_num(f.get("totalDue")) for f in fees]
    if sid in (None, "") or not vin or start is None or not fees or all(d is None for d in dues):
        return None
    charging = next((f for f in fees if f.get("feeType") == "CHARGING"), fees[0])
    kwh = _num(charging.get("usageBase")) if (charging.get("uom") or "").lower() == "kwh" else None
    invoice = next((i.get("contentId") for i in item.get("invoices") or [] if isinstance(i, dict) and i.get("contentId")), None)
    return {
        "id": str(sid),
        "vin": vin,
        "started_at": start,
        "stopped_at": _time(item.get("chargeStopDateTime")),
        "site": (item.get("siteLocationName") or "").strip() or None,
        "currency": charging.get("currencyCode") or None,
        "total": round(sum(d for d in dues if d is not None), 2),
        "kwh": kwh,
        "invoice_id": invoice,
    }


def parse_page(body) -> tuple[list[dict], bool]:
    """(rows, whether there is another page) from one page of history."""
    body = body if isinstance(body, dict) else {}
    data = body.get("data") if isinstance(body.get("data"), list) else []
    rows = [r for r in (parse_item(i) for i in data) if r]
    more = body.get("hasMoreData")
    if more is None:  # not said: a full page may have more behind it
        more = len(data) >= PAGE and len(data) > 0
    return rows, bool(more) and bool(data)


def match(session: dict, bills: list[dict]) -> dict | None:
    """The bill for one of our sessions: the same car, the start within WINDOW (nearest
    first), and within NEAR_M when both have a position. Pure."""
    start = session.get("session_start_date")
    if start is None:
        return None
    best = None
    for b in bills:
        gap = abs((b["started_at"] - start).total_seconds())
        if gap > WINDOW.total_seconds() or (session.get("vin") and b.get("vin") and b["vin"] != session["vin"]):
            continue
        a, o = session.get("session_position_latitude"), session.get("session_position_longitude")
        if None not in (a, o, b.get("latitude"), b.get("longitude")) and distance_m(a, o, b["latitude"], b["longitude"]) > NEAR_M:
            continue
        if best is None or gap < best[0]:
            best = (gap, b)
    return best[1] if best else None


def bills_between(conn, vin: str, lo: datetime, hi: datetime) -> list[dict]:
    """The bills that could match a session starting between lo and hi."""
    rows = conn.execute(
        "SELECT id, vin, started_at, site, currency, total, invoice_id FROM cc_billed_charges WHERE vin = %s AND started_at BETWEEN %s AND %s ORDER BY started_at",
        (vin, lo - WINDOW, hi + WINDOW),
    ).fetchall()
    keys = ("id", "vin", "started_at", "site", "currency", "total", "invoice_id")
    return [dict(r) if isinstance(r, dict) else dict(zip(keys, r)) for r in rows]


def invoice_for(conn, vin: str, start: datetime) -> dict | None:
    """The bill behind a billed charge, for its page's invoice link."""
    return match({"vin": vin, "session_start_date": start}, bills_between(conn, vin, start, start))


def store(conn, rows: list[dict]) -> list[str]:
    """Insert or refresh bills; the ids of those that are new or whose amount changed."""
    changed = []
    for r in rows:
        got = conn.execute(
            """INSERT INTO cc_billed_charges (id, vin, started_at, stopped_at, site, currency, total, kwh, invoice_id, fetched_at)
               VALUES (%(id)s, %(vin)s, %(started_at)s, %(stopped_at)s, %(site)s, %(currency)s, %(total)s, %(kwh)s, %(invoice_id)s, now())
               ON CONFLICT (id) DO UPDATE SET total = EXCLUDED.total, kwh = EXCLUDED.kwh, invoice_id = COALESCE(EXCLUDED.invoice_id, cc_billed_charges.invoice_id),
                   currency = EXCLUDED.currency, site = EXCLUDED.site, stopped_at = EXCLUDED.stopped_at, fetched_at = now()
               WHERE cc_billed_charges.total IS DISTINCT FROM EXCLUDED.total OR cc_billed_charges.invoice_id IS NULL AND EXCLUDED.invoice_id IS NOT NULL
               RETURNING id""",
            r,
        ).fetchone()
        if got:
            changed.append(r["id"])
    return changed


def sessions_for(conn, bill_ids: list[str]) -> list[int]:
    """Our sessions within WINDOW of these bills' starts that a bill has not priced yet."""
    if not bill_ids:
        return []
    rows = conn.execute(
        """SELECT DISTINCT c.id FROM cc_billed_charges b
           JOIN charging_sessions c ON c.vin = b.vin AND c.session_start_date BETWEEN b.started_at - interval '10 minutes' AND b.started_at + interval '10 minutes'
           LEFT JOIN cc_charge_facts f ON f.session_id = c.id
           WHERE b.id = ANY(%s) AND f.cost_source IS DISTINCT FROM 'billed'""",
        (bill_ids,),
    ).fetchall()
    return [r["id"] if isinstance(r, dict) else r[0] for r in rows]


def _state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM cc_state WHERE key = %s", (key,)).fetchone()
    return (row["value"] if isinstance(row, dict) else row[0]) if row else None


def _set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))


def _first_id(body) -> str | None:
    data = (body or {}).get("data") if isinstance(body, dict) else None
    return str(data[0].get("sessionId")) if data and isinstance(data[0], dict) else None


class BilledWorker(threading.Thread):
    """Reads the charging history in the background and stores the bills.

    `access()` -> (token, API base) or None, refreshed as the site does it; `fleet()` the
    Fleet client or None; `vins(conn)` the cars to ask about; `clear(conn, ids)` puts our
    sessions a new bill matches back in the queue to be priced (facts.clear_prices).

    Pages are asked for oldest first. Whether the first page is numbered 0 or 1 is learned
    once, the first time there is history to look at, and kept. A read that stops partway
    (a refusal, a 429) keeps the bills it got but moves nothing on: the next read, after
    MIN_GAP, starts each unfinished car from where this one started."""

    page_size = PAGE

    def __init__(self, connect: Callable, access: Callable, fleet: Callable, vins: Callable, clear: Callable, interval_s: int = 3600) -> None:
        super().__init__(daemon=True, name="tesla-bills")
        self.connect, self.access, self.fleet, self.vins, self.clear = connect, access, fleet, vins, clear
        self.interval_s = interval_s
        self.stop = threading.Event()

    def due(self, conn, now: datetime) -> bool:
        last = _time(_state(conn, TRIED))
        return last is None or now - last >= MIN_GAP

    def page_base(self, fleet, token, api, vin, start) -> int | None:
        """0 or 1, from asking for both: a server numbered from 1 answers page 0 with its
        first page (or refuses it); one numbered from 0 answers with two different pages.
        None while there is nothing to tell them apart by."""
        from tesla_fleet import TeslaError

        try:
            zero = fleet.charging_history(token, api, vin, start=start, page=0, size=self.page_size, order="ASC")
        except TeslaError as err:
            if err.status in (400, 404, 422):
                return 1
            raise
        one = fleet.charging_history(token, api, vin, start=start, page=1, size=self.page_size, order="ASC")
        first0, first1 = _first_id(zero), _first_id(one)
        if first0 is None:
            return 1 if first1 is not None else None
        if first1 is None or first1 != first0:
            return 0
        return 1

    def read(self, fleet, token, api, vin, start, base, rows: list[dict]) -> list[dict]:
        """Every page for one car into `rows`, which keeps what came before a failure."""
        for page in range(base, base + MAX_PAGES):
            got, more = parse_page(fleet.charging_history(token, api, vin, start=start, page=page, size=self.page_size, order="ASC"))
            rows += [r for r in got if r["vin"] == vin]
            if not more:
                break
        return rows

    def round(self, now: datetime | None = None) -> str | None:
        """One pass: None when nothing was due or allowed, else what it did."""
        now = now or datetime.now(timezone.utc)
        fleet = self.fleet()
        if fleet is None:
            return None
        with self.connect() as conn:
            if not self.due(conn, now):
                return None
            vins = self.vins(conn)
            starts = {vin: _state(conn, FROM_KEY + vin) for vin in vins}
            base = _state(conn, BASE_KEY)
            conn.commit()
        base = int(base) if base in ("0", "1") else None
        got = self.access()
        if not got:
            return None
        token, api = got
        from tesla_fleet import BILLING_SCOPE, granted_scopes  # the scope gate, read from the token itself

        if BILLING_SCOPE not in granted_scopes(token):
            return "no scope"
        with self.connect() as conn:
            _set(conn, TRIED, now.isoformat())  # a read that fails still waits MIN_GAP for the next
            conn.commit()
        # No transaction across the reads from Tesla: each car's pages, then a short write.
        fetched: list[dict] = []
        finished: dict[str, str] = {}
        learned = base is None
        failed = None
        try:
            for vin in vins:
                start = starts.get(vin)
                if base is None:
                    base = self.page_base(fleet, token, api, vin, start)
                    if base is None:
                        finished[vin] = start or ""  # no history yet: nothing to read
                        continue
                rows = self.read(fleet, token, api, vin, start, base, fetched)
                newest = max((r["started_at"] for r in rows if r["vin"] == vin), default=None)
                finished[vin] = (newest - OVERLAP).isoformat() if newest else (start or "")
        except Exception as err:  # noqa: BLE001 - keep what was read; the next read starts where this one did
            failed = str(err)[:200]
            LOG.warning("tesla bills: %s", failed)
        with self.connect() as conn:
            changed = store(conn, fetched)
            self.clear(conn, sessions_for(conn, changed))
            for vin, start in finished.items():
                if start:
                    _set(conn, FROM_KEY + vin, start)
            if learned and base is not None:
                _set(conn, BASE_KEY, str(base))
            if not failed:
                _set(conn, STAMP, now.isoformat())
            conn.commit()
        LOG.info("tesla bills: %d read, %d new or changed", len(fetched), len(changed))
        return "failed" if failed else f"{len(changed)} new"

    def run(self) -> None:
        self.stop.wait(180)  # let startup settle
        while not self.stop.is_set():
            try:
                self.round()
            except Exception as err:  # noqa: BLE001 - a bad pass waits for the next one
                LOG.warning("tesla bills: %s", err)
            self.stop.wait(self.interval_s)
