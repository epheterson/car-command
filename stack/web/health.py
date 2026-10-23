"""What watches the data itself.

Every source has a marker that says it is fine: the feed's rebuild writes `ok`, the
connector answers, the recorder ticks. None of them notice when a car's hourly
replay erases weeks of drives an hour at a time: the
rows keep arriving and the rebuild kept succeeding, on a window that no longer held the
drives. These checks read the data, not the markers: does what arrived get logged, and
do the miles the odometer saw show up as drives.

A worker runs them once an hour per car, keeps the answer with its evidence in
`cc_state` (`health:{vin}`), and sends each new finding once a day as an alert of kind
`data`. Pages read the stored answer; nothing here runs on a page load.
"""

from __future__ import annotations

import driveedits
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

LOG = logging.getLogger("carcommand.health")

STALE_AFTER = timedelta(days=1)  # readings a day newer than anything logged from them
QUIET_AFTER = timedelta(hours=24)  # a source that has said nothing for this long
REBUILD_AFTER = timedelta(hours=3)  # the feed rebuilds hourly; three misses is a problem
MILES_SLACK = 5.0  # a short trip below the drive threshold is not a finding
MILES_SHARE = 0.2  # ...nor is a fifth of the week; more than that is missing drives
WEEK = timedelta(days=7)
INTERVAL_S = 3600

# Each source by its plain name (carspage.WRITERS). A car's own `source_name` wins: a car on
# the connector is named by its maker ("Lucid (CarConnectivity)"), which only the caller knows.
SOURCES = {"live": "CarConnectivity", "feed": "TeslaFi (API)", "teslamate": "TeslaMate", "tesla": "Tesla (direct)", "history": "imported history only"}


def _age(now: datetime, then: datetime | None) -> str:
    if then is None:
        return "never"
    s = (now - then).total_seconds()
    if s < 3600:
        return f"{int(s // 60)} min"
    if s < 2 * 86400:
        return f"{s / 3600:.0f} h"
    return f"{s / 86400:.0f} days"


def check(car: dict, now: datetime) -> list[dict]:
    """Findings for one car, from the plain facts `gather` collects.

    car: vin, name, source (live | feed | teslamate | history), raw_newest (the newest
    reading from the source), derived_newest (the newest drive, charge or state written
    from it), active_since_derived (a reading after derived_newest showed the car driving
    or charging), odo_7d and trips_7d (miles the odometer moved this week, and miles the
    drives add up to), rebuild (the feed's marker text, if any).
    Each finding: kind, level (warn | bad), text, and the evidence it rests on.
    """
    out: list[dict] = []
    name, source = car["name"], car.get("source") or "history"
    raw, derived = car.get("raw_newest"), car.get("derived_newest")
    if source == "history":
        return out  # nothing is expected to arrive; the Data section says so, no alert
    if raw is None or now - raw > QUIET_AFTER:
        said = car.get("source_name") or SOURCES.get(source, source)
        text = f"Nothing has ever arrived from {said} for {name}." if raw is None else f"Nothing from {said} for {name} in {_age(now, raw)}."
        out.append({"kind": "quiet", "level": "bad", "text": text, "raw_newest": raw})
        return out  # the rest would only restate it
    if source in ("feed", "teslamate") and derived is not None and raw - derived > STALE_AFTER and car.get("active_since_derived"):
        out.append(
            {
                "kind": "stale",
                "level": "bad",
                "text": f"{name}'s readings arrive but nothing has been logged from them since {derived.astimezone().strftime('%b %-d')} ({_age(now, derived)} ago).",
                "raw_newest": raw,
                "derived_newest": derived,
            }
        )
    odo, trips = car.get("odo_7d"), car.get("trips_7d")
    if odo is not None and trips is not None:
        gap = odo - trips
        if gap > MILES_SLACK and gap > MILES_SHARE * odo:
            out.append({"kind": "miles", "level": "warn", "text": f"{gap:.0f} of the {odo:.0f} mi {name} drove this week are not in any drive.", "odo_7d": odo, "trips_7d": trips})
    marker = car.get("rebuild") or ""
    if source == "feed":
        if marker.startswith("error"):
            out.append({"kind": "rebuild", "level": "bad", "text": f"{name}'s feed rebuild failed: {marker[6:].split(' ', 1)[-1][:120]}", "rebuild": marker})
        elif marker.startswith("ok "):
            try:
                at = datetime.fromisoformat(marker.split(" ")[1])
            except ValueError:
                at = None
            if at is not None and now - at > REBUILD_AFTER:
                out.append({"kind": "rebuild", "level": "warn", "text": f"{name}'s feed has not been rebuilt for {_age(now, at)}.", "rebuild": marker})
    return out


# ---- the facts, from the database


def _t(row):
    if row is None:
        return None
    return tuple(row.values()) if isinstance(row, dict) else tuple(row)


def _one(conn, sql: str, params: tuple):
    r = _t(conn.execute(sql, params).fetchone())
    return r[0] if r else None


def gather(conn, vin: str, name: str, source: str, now: datetime) -> dict:
    """The plain facts `check` reads, for one car. Six small queries an hour; the feed's
    odometer comes out of its raw JSON rows, which are kept for exactly this."""
    week = now - WEEK
    car: dict = {"vin": vin, "name": name, "source": source, "checked_at": now}
    # Both sums cover the same stretch: from the start of the first whole drive this week to
    # the end of the newest one. A drive that began before the week's edge, or is still under
    # way (written when it ends), would otherwise count in the odometer and not in the drives.
    first_start = _one(conn, "SELECT MIN(start_date) FROM trips t WHERE vin = %s AND start_date > %s" + driveedits.ABSORBED, (vin, week))
    since = first_start or week
    until = _one(conn, "SELECT MAX(destination_date) FROM trips WHERE vin = %s AND start_date >= %s", (vin, since)) or now
    if source == "feed":
        car["raw_newest"] = _one(conn, "SELECT MAX(at) FROM cc_teslafi_rows WHERE vin = %s", (vin,))
        car["odo_7d"] = _one(
            conn,
            "SELECT MAX((data->>'odometer')::float) - MIN((data->>'odometer')::float) FROM cc_teslafi_rows WHERE vin = %s AND at BETWEEN %s AND %s AND data->>'odometer' <> ''",
            (vin, since, until),
        )
        car["rebuild"] = _one(conn, "SELECT value FROM cc_state WHERE key = %s", (f"feed_rebuild:{vin}",))
    elif source in ("live", "teslamate"):
        car["raw_newest"] = _one(conn, "SELECT MAX(last_date) FROM states WHERE vin = %s", (vin,))
        car["odo_7d"] = _one(conn, "SELECT MAX(odometer) - MIN(odometer) FROM cc_positions WHERE vin = %s AND at BETWEEN %s AND %s", (vin, since, until))
    car["derived_newest"] = _one(
        conn,
        "SELECT GREATEST((SELECT MAX(destination_date) FROM trips WHERE vin = %s), (SELECT MAX(session_end_date) FROM charging_sessions WHERE vin = %s))",
        (vin, vin),
    )
    car["trips_7d"] = _one(conn, "SELECT COALESCE(SUM(destination_odometer - start_odometer), 0) FROM trips t WHERE vin = %s AND start_date >= %s" + driveedits.ABSORBED, (vin, since))
    if source == "feed" and car["derived_newest"] is not None:
        car["active_since_derived"] = bool(
            _one(conn, "SELECT 1 FROM cc_teslafi_rows WHERE vin = %s AND at > %s AND data->>'carState' IN ('Driving', 'Charging') LIMIT 1", (vin, car["derived_newest"]))
        )
    return car


def encode(car: dict, findings: list[dict]) -> str:
    def plain(v):
        return v.isoformat() if isinstance(v, datetime) else v

    return json.dumps({"car": {k: plain(v) for k, v in car.items()}, "findings": [{k: plain(v) for k, v in f.items()} for f in findings]})


def decode(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


class HealthWorker(threading.Thread):
    """Runs `gather` and `check` for every car once an hour."""

    def __init__(self, connect: Callable, cars: Callable[[], list[dict]], notify: Callable[[str, str], None], interval_s: int = INTERVAL_S) -> None:
        super().__init__(daemon=True, name="health")
        self.connect, self.cars, self.notify, self.interval_s = connect, cars, notify, interval_s
        self.stop = threading.Event()

    def round(self, now: datetime | None = None) -> int:
        """One pass: how many findings there are across the garage."""
        now = now or datetime.now(timezone.utc)
        total = 0
        with self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS cc_state (key TEXT PRIMARY KEY, value TEXT)")
            for c in self.cars():
                car = gather(conn, c["vin"], c["name"], c["source"], now)
                findings = check(car, now)
                total += len(findings)
                conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f"health:{c['vin']}", encode(car, findings)))
                for f in findings:
                    key = f"health_sent:{c['vin']}:{f['kind']}"
                    if _one(conn, "SELECT value FROM cc_state WHERE key = %s", (key,)) == now.date().isoformat():
                        continue
                    self.notify(f["text"], "data")
                    conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, now.date().isoformat()))
                conn.commit()
        return total

    def run(self) -> None:
        while not self.stop.wait(90):  # after the pollers have had a first go
            try:
                n = self.round()
                if n:
                    LOG.warning("data check: %d finding(s)", n)
            except Exception as err:  # noqa: BLE001
                LOG.warning("data check failed: %s", err)
            if self.stop.wait(self.interval_s):
                return
