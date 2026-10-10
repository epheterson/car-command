"""TeslaFi's history API: the drives and charges it has summarised, by date range.

    https://www.teslafi.com/history.php?command=drives&dateFrom=YYYY-MM-DD&dateTo=YYYY-MM-DD
    https://www.teslafi.com/history.php?command=charges&dateFrom=...&dateTo=...
    with  Authorization: Bearer <token>

The feed (`teslafi_live`) only ever hands over the current reading, so a car whose token
was added late, or whose poller was down, has a hole between its last import and the
first feed row. This fills it from the summaries: one row per drive (start, end,
odometer, positions, miles, max speed, temperature) and per charge (start, end, levels,
energy, position, wall volts and amps, software version). No minute-by-minute readings,
so no route, no power curve, no battery line inside a drive; the full TeslaFi export
(`make import`) is still the way to get those. Rows go through `history.write_history`
like every other source, under `teslafi-history:{vin}`, rolling, so a later fill for a
later gap keeps what an earlier one wrote.

The poller runs this once per car per start when it finds a gap of more than 36 hours
between the newest thing any import wrote and now; `make teslafi-history` runs it by hand.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from history import fill_after_import, write_history
from teslafi_import import USABLE_KWH, model_from_vin

LOG = logging.getLogger("carcommand.teslafi_history")
HISTORY = os.environ.get("TESLAFI_HISTORY_URL", "https://www.teslafi.com/history.php")
GAP = timedelta(hours=36)  # the feed's own rolling window covers anything shorter
DC_TYPES = ("Tesla", "MCSingle", "Combo", "CCS", "CHAdeMO")
DEFAULT_TZ = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))


def import_id(vin: str) -> str:
    return f"teslafi-history:{vin}"


def _f(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _at(text: str | None) -> datetime | None:
    """TeslaFi's `date` is the start, in UTC, as 'YYYY-MM-DD HH:MM:SS'."""
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def trip_from(d: dict) -> dict | None:
    """A drive summary as the trips row the writer takes, with the figures that have
    nowhere else to go (max speed, temperature) beside it for the facts."""
    start = _at(d.get("date"))
    if start is None:
        return None
    minutes = _i(d.get("dayDriveHours")) * 60 + _i(d.get("dayDriveMinutes"))
    end = start + timedelta(minutes=max(1, minutes))
    return {
        "start_date": start,
        "destination_date": end,
        "start_odometer": _f(d.get("startOdo")),
        "destination_odometer": _f(d.get("endOdo")),
        "start_position_latitude": _f(d.get("latitude")),
        "start_position_longitude": _f(d.get("longitude")),
        "destination_position_latitude": _f(d.get("endLatitude")),
        "destination_position_longitude": _f(d.get("endLongitude")),
        "max_mph": _f(d.get("speedMAX")),
        "temp_f": (_f(d.get("temperatureAVG")) or None) if d.get("noOutsideTemperatureReported") not in (1, "1", True) else None,
        "tag": (d.get("tag") or "").strip() or None,
    }


def charge_from(d: dict) -> dict | None:
    start = _at(d.get("date"))
    if start is None:
        return None
    minutes = _i(d.get("totalMinutes")) or (_i(d.get("hours")) * 60 + _i(d.get("minutes")))
    end = start + timedelta(minutes=max(1, minutes))
    # fastChargerType names the connector, AC ones included ("ACSingleWi..." at home);
    # fast charging is the Supercharger flag or a DC connector name
    kind = (d.get("fastChargerType") or "").strip()
    fast = _i(d.get("superChargerFlag")) == 1 or any(kind.startswith(x) for x in DC_TYPES)
    return {
        "session_start_date": start,
        "session_end_date": end,
        "start_level": _f(d.get("startPercent")),
        "end_level": _f(d.get("chargePercent")) or _f(d.get("lastDataBatteryLevel")),
        "session_position_latitude": _f(d.get("latitude")),
        "session_position_longitude": _f(d.get("longitude")),
        "session_odometer": _f(d.get("odometer")),
        "real_charged": _f(d.get("totalEnergyAdded")) or _f(d.get("energyAdded")),
        "charging_type": "DC" if fast else "AC",
        "network": "supercharger" if _i(d.get("superChargerFlag")) == 1 or kind == "Tesla" else None,
        "volts": _f(d.get("avgVoltage")),
        "amps": _f(d.get("avgCurrent")),
        "power_kw": _f(d.get("avgChargerPower")),
        "software": (d.get("fw") or "").strip() or None,
    }


def build(drives: list[dict], charges: list[dict], vin: str, name: str | None) -> dict | None:
    """The writer's shape from TeslaFi's two lists; see build_rows."""
    return build_rows([t for t in (trip_from(d) for d in drives) if t], [c for c in (charge_from(d) for d in charges) if c], vin, name)


def build_rows(trips: list[dict], sessions: list[dict], vin: str, name: str | None) -> dict | None:
    """The writer's shape from trip and session rows. Interval tables carry one row per
    drive (driving) and per charge (charging, with the wall's volts and amps and, where
    the charger reported it, its power), levels at each end of a charge, and the
    temperature of each drive; software from the version each charge saw."""
    trips = _one_per_start(sorted(trips, key=lambda t: t["start_date"]), "start_date")
    sessions = _one_per_start(sorted(sessions, key=lambda c: c["session_start_date"]), "session_start_date")
    if not trips and not sessions:
        return None
    # every interval table is unique on (vin, first_date): a drive that ends the second a
    # charge starts, or a one-minute charge, would collide, so each list keeps one row per start
    states = _rows([(t["start_date"], t["destination_date"], "DRIVING") for t in trips] + [(c["session_start_date"], c["session_end_date"], "PARKED") for c in sessions])
    charging_states = _rows([(c["session_start_date"], c["session_end_date"], "CHARGING") for c in sessions])
    levels = []
    for c in sessions:
        if c["start_level"] is not None:
            levels.append((c["session_start_date"], c["session_start_date"], c["start_level"]))
        if c["end_level"] is not None:
            levels.append((c["session_end_date"], c["session_end_date"], c["end_level"]))
    levels = _rows(levels)
    temps = _rows([(t["start_date"], t["destination_date"], t["temp_f"]) for t in trips if t["temp_f"] is not None])
    powers = _rows([(c["session_start_date"], c["session_end_date"], c["power_kw"]) for c in sessions if c["power_kw"]])
    electrics = _rows([(c["session_start_date"], c["session_end_date"], (c["volts"], c["amps"])) for c in sessions if c["volts"] and c["amps"]])
    software = _rows([(c["session_start_date"], c["session_start_date"], c["software"]) for c in sessions if c["software"]])
    stamps = [t["start_date"] for t in trips] + [t["destination_date"] for t in trips] + [c["session_start_date"] for c in sessions] + [c["session_end_date"] for c in sessions]
    return {
        "first": min(stamps),
        "last": max(stamps),
        "rows": len(trips) + len(sessions),
        "vehicle": {"vin": vin, "name": name, "model": model_from_vin(vin)},
        "states": states,
        "connection_states": [],
        "charging_states": charging_states,
        "outside_temperatures": temps,
        "charging_powers": powers,
        "drive_levels": levels,
        "drive_ranges": [],
        "trips": trips,
        "charging_sessions": sessions,
        "positions": [],
        "software": software,
        "inside_temperatures": [],
        "modes": [],
        "charging_electrics": electrics,
    }


def _one_per_start(rows: list[dict], key: str) -> list[dict]:
    out, seen = [], set()
    for r in rows:
        if r[key] in seen:
            continue  # TeslaFi lists the same session twice now and then
        seen.add(r[key])
        out.append(r)
    return out


def _rows(rows: list[tuple]) -> list[tuple]:
    """Interval rows sorted, one per first_date, the longer one winning a tie."""
    best: dict = {}
    for a, b, v in rows:
        if a not in best or b > best[a][1]:
            best[a] = (a, b, v)
    return sorted(best.values())


def catchup_window(newest_before_feed: datetime | None, feed_first: datetime | None, now: datetime, gap: timedelta = GAP) -> tuple[date, date] | None:
    """The dates to ask for: from the newest thing any other import wrote (the export, a
    TeslaMate load, an earlier fill) to the feed's first row, when that hole is wider than
    the feed's own window; None when there is nothing to fill. The feed's own import is
    left out of "newest" on purpose: it is always current, and hid a 24-day hole on
    2026-09-28. A car with no history at all gets the last year."""
    end = feed_first or now
    start = newest_before_feed if newest_before_feed is not None else end - timedelta(days=365)
    if end - start <= gap:
        return None
    return start.date(), end.date()


def http_fetch(token: str, command: str, date_from: date, date_to: date) -> list[dict]:
    import httpx

    r = httpx.get(HISTORY, params={"command": command, "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()}, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    r.raise_for_status()
    body = r.json()
    if not isinstance(body, dict) or "results" not in body:
        raise RuntimeError(f"unexpected answer from TeslaFi history: {str(body)[:120]}")
    return body["results"] or []


def inside(rows: list[dict], key: str, after: datetime | None, before: datetime | None) -> list[dict]:
    """Only the summaries that fall in the hole: TeslaFi is asked by date, the hole has a
    time at each end, and a row the export or the feed already holds must not come twice."""
    return [r for r in rows if (after is None or r[key] > after) and (before is None or r[key] < before)]


def backfill(
    connect, vin: str, token: str, date_from: date, date_to: date, tz: ZoneInfo = DEFAULT_TZ, fetch=http_fetch, name: str | None = None, after: datetime | None = None, before: datetime | None = None
) -> dict | None:
    """Fill a car's history from TeslaFi's summaries between two dates, keeping only the
    rows that start after `after` and before `before` (the hole's own edges). Returns the
    writer's counts, or None when TeslaFi had nothing there."""
    drives = fetch(token, "drives", date_from, date_to)
    charges = fetch(token, "charges", date_from, date_to)
    trips = inside([t for t in (trip_from(d) for d in drives) if t], "start_date", after, before)
    sessions = inside([c for c in (charge_from(d) for d in charges) if c], "session_start_date", after, before)
    out = build_rows(trips, sessions, vin, name)
    if out is None:
        return None
    with connect() as conn:
        veh = out["vehicle"]
        model = veh.get("model")
        counts = write_history(conn, vin, veh.get("name") or vin, "Tesla", model, USABLE_KWH.get(model or ""), out, import_id(vin), "teslafi history", tz, rolling=True)
        fill_after_import(conn, vin, tz)
        # the figures the facts cannot work out without readings: the API's own max speed and temperature
        for t in out["trips"]:
            row = conn.execute("SELECT id FROM trips WHERE vin = %s AND start_date = %s", (vin, t["start_date"])).fetchone()
            if row is None:
                continue
            trip_id = row["id"] if isinstance(row, dict) else row[0]
            conn.execute(
                "INSERT INTO cc_trip_facts (trip_id, max_mph, temp_f) VALUES (%s, %s, %s) ON CONFLICT (trip_id) DO UPDATE SET max_mph = COALESCE(EXCLUDED.max_mph, cc_trip_facts.max_mph), temp_f = COALESCE(EXCLUDED.temp_f, cc_trip_facts.temp_f)",
                (trip_id, t["max_mph"], t["temp_f"]),
            )
            if t.get("tag"):
                conn.execute("INSERT INTO cc_drive_tags (trip_id, tag) VALUES (%s, %s) ON CONFLICT (trip_id) DO NOTHING", (trip_id, t["tag"]))
        conn.commit()
    counts["drives_seen"], counts["charges_seen"] = len(drives), len(charges)
    return counts


def edges(conn, vin: str) -> tuple[datetime | None, datetime | None]:
    """(newest last_at of every import but the feed's, the feed's first_at) for one car."""
    feed = f"teslafi-live:{vin}"
    # neither the feed nor an earlier fill counts as "before": a fill run again must see the same hole
    # aliased: a dict row with two columns both called max keeps only one of them
    row = conn.execute(
        "SELECT MAX(last_at) FILTER (WHERE id NOT IN (%s, %s)) AS before_feed, MAX(first_at) FILTER (WHERE id = %s) AS feed_first FROM cc_imports WHERE vin = %s", (feed, import_id(vin), feed, vin)
    ).fetchone()
    if row is None:
        return None, None
    return (row["before_feed"], row["feed_first"]) if isinstance(row, dict) else (row[0], row[1])


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="fill a car's history from TeslaFi's drive and charge summaries")
    ap.add_argument("vin")
    ap.add_argument("date_from")
    ap.add_argument("date_to", nargs="?", default=None)
    ap.add_argument("--everything", action="store_true", help="take every row in the range, even where the export or the feed already has it")
    args = ap.parse_args(argv)
    import app

    tokens = [t.strip() for t in os.environ.get("TESLAFI_TOKENS", "").split(",") if t.strip()]
    token = None
    for t in tokens:
        raw, _sep, tok = t.rpartition(":")
        if raw == args.vin:
            token = tok
            break
    if token is None:
        # learn which token is the car's from the feed's own row
        from teslafi_live import TeslaFiPoller

        p = TeslaFiPoller(tokens, connect=app.db, tz=app.TZ)
        for tok in p.tokens:
            try:
                row = p.fetch(tok, "lastGood")
            except Exception as err:  # noqa: BLE001
                print(f"token could not be read: {err}", file=sys.stderr)
                continue
            if (row.get("vin") or "").strip() == args.vin:
                token = tok
                break
    if token is None:
        print("no TeslaFi token answers for that vin", file=sys.stderr)
        return 1
    d0 = date.fromisoformat(args.date_from)
    d1 = date.fromisoformat(args.date_to) if args.date_to else datetime.now(app.TZ).date()
    # by default only the hole between the last other import and the feed's first row is
    # touched, so a fill can never replace rows the export or the feed wrote in full
    with app.db() as conn:
        after, before = (None, None) if args.everything else edges(conn, args.vin)
    counts = backfill(app.db, args.vin, token, d0, d1, app.TZ, after=after, before=before)
    print("nothing in that range" if counts is None else ", ".join(f"{v} {k}" for k, v in counts.items() if v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
