"""A Tesla, live and free: follow a TeslaMate database.

CarConnectivity has no Tesla connector and Tesla's own API now bills per call, so the
Tesla flow that costs nothing is the logger everyone already runs. TeslaMate streams from
the car and keeps positions, charges, states and software updates in its Postgres; this
reads that database the way the TeslaFi poller reads its feed, replays a rolling window
into Car Command's own tables under one import id per car, and writes the newest
position each minute so the Now page is live. Nothing is written to TeslaMate.

TESLAMATE_DB_URL in stack/.env points at it (postgresql://teslamate:…@host:5432/teslamate).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import history_writer
from history import fill_after_import, write_history
from teslafi_import import USABLE_KWH
from teslamate_import import replay_tables, samples_from_tables

LOG = logging.getLogger("carcommand.teslamate")
DEFAULT_TZ = ZoneInfo("America/Los_Angeles")
WINDOW = timedelta(hours=36)

TABLES = {
    "positions": "SELECT * FROM positions WHERE car_id = %s AND date >= %s ORDER BY date",
    "charges": "SELECT c.* FROM charges c JOIN charging_processes p ON p.id = c.charging_process_id WHERE p.car_id = %s AND c.date >= %s ORDER BY c.date",
    "charging_processes": "SELECT * FROM charging_processes WHERE car_id = %s AND (end_date >= %s OR end_date IS NULL) ORDER BY start_date",
    "states": "SELECT * FROM states WHERE car_id = %s AND (end_date >= %s OR end_date IS NULL) ORDER BY start_date",
    "updates": "SELECT * FROM updates WHERE car_id = %s AND start_date >= %s ORDER BY start_date",
}


def import_id(vin: str) -> str:
    return f"teslamate-live:{vin}"


def model_name(car: dict) -> str | None:
    """ "Model 3" from TeslaMate's model letter, with the trim when it knows it."""
    letter = (car.get("model") or "").strip().upper()
    base = {"S": "Model S", "3": "Model 3", "X": "Model X", "Y": "Model Y"}.get(letter)
    trim = (car.get("trim_badging") or "").strip()
    return f"{base} {trim}".strip() if base else None


def window_start(now: datetime, floor: datetime | None, resume: datetime | None) -> datetime:
    """36 hours back, opened further to an hour before the last successful rebuild, never before the floor."""
    start = now - WINDOW
    if resume is not None and resume - timedelta(hours=1) < start:
        start = resume - timedelta(hours=1)
    if floor is not None and start < floor:
        start = floor
    return start


def list_cars(tm) -> list[dict]:
    """TeslaMate's cars: what the poller follows, and what Settings lists when a database is added."""
    return [dict(r) for r in tm.execute("SELECT id, vin, name, model, trim_badging FROM cars WHERE vin IS NOT NULL ORDER BY id").fetchall()]


class TeslaMatePoller:
    def __init__(self, db_url: str | None, connect, connect_teslamate=None, interval_s: int = 60, rebuild_every_s: int = 600, tz: ZoneInfo = DEFAULT_TZ) -> None:
        self.db_url = (db_url or "").strip()
        self.connect = connect
        self.connect_teslamate = connect_teslamate or self._connect_teslamate
        self.interval_s, self.rebuild_every_s, self.tz = interval_s, rebuild_every_s, tz
        self.last_rebuild: dict[str, datetime] = {}
        self.last_pos: dict[str, datetime] = {}
        self.task: asyncio.Task | None = None
        self.vins: list[str] = []  # the cars to follow; empty follows every car in the database
        self.results: dict[str, tuple[datetime | None, str | None]] = {}  # vin -> (last success, last error), for Settings

    def use(self, db_url: str | None, vins: list[str] | None = None) -> None:
        """A database and the cars to follow, from Settings, while running."""
        self.db_url, self.vins = (db_url or "").strip(), list(vins or [])
        if not self.db_url or (self.task and not self.task.done()):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self.task = asyncio.create_task(self.run())

    def followed(self, cars: list[dict]) -> list[dict]:
        return [c for c in cars if not self.vins or c["vin"] in self.vins]

    def _connect_teslamate(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.db_url, row_factory=dict_row, connect_timeout=10)

    # ---- reading TeslaMate
    def cars(self, tm) -> list[dict]:
        return list_cars(tm)

    def tables_since(self, tm, car_id: int, since: datetime) -> dict[str, list[dict]]:
        since_naive = since.astimezone(timezone.utc).replace(tzinfo=None)  # TeslaMate's columns are timestamp without time zone, in UTC
        return {name: [dict(r) for r in tm.execute(sql, (car_id, since_naive)).fetchall()] for name, sql in TABLES.items()}

    def newest_position(self, tm, car_id: int) -> dict | None:
        r = tm.execute("SELECT date, latitude, longitude, odometer, battery_level, speed, drive_id FROM positions WHERE car_id = %s ORDER BY date DESC LIMIT 1", (car_id,)).fetchone()
        return dict(r) if r else None

    def spans(self, vin: str, since: datetime) -> list[tuple[datetime, datetime]]:
        """TeslaMate's own drives and charges for this car since `since`, as (start, end) in
        UTC, one still under way ending now: what a handover to it must not cut."""
        since_naive = since.astimezone(timezone.utc).replace(tzinfo=None)
        now = datetime.now(timezone.utc)

        def aware(t):
            return None if t is None else (t if t.tzinfo else t.replace(tzinfo=timezone.utc))

        with self.connect_teslamate() as tm:
            car = next((c for c in self.cars(tm) if c["vin"] == vin), None)
            if car is None:
                return []
            rows = []
            for table in ("drives", "charging_processes"):
                rows += [dict(r) for r in tm.execute(f"SELECT start_date, end_date FROM {table} WHERE car_id = %s AND (end_date >= %s OR end_date IS NULL)", (car["id"], since_naive)).fetchall()]
        return [(aware(r["start_date"]), aware(r["end_date"]) or now) for r in rows if r["start_date"] is not None]

    # ---- writing ours
    def rebuild(self, vin: str, car: dict, tables: dict, now: datetime, floor: datetime | None) -> dict | None:
        samples = samples_from_tables(tables, vin, car.get("name") or vin)
        if not samples:
            return None
        out = replay_tables(tables, vin, car.get("name") or vin)
        model = model_name(car)
        with self.connect() as conn:
            history_writer.lock(conn, vin)
            if history_writer.writes(conn, vin, "teslamate") is False:
                conn.commit()
                self.last_rebuild[vin] = now
                return None  # another source writes this car's history
            counts = write_history(conn, vin, car.get("name") or vin, "Tesla", model, USABLE_KWH.get(model or ""), out, import_id(vin), "teslamate live", self.tz, rolling=True)
            fill_after_import(conn, vin, self.tz)
            conn.execute("CREATE TABLE IF NOT EXISTS cc_state (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                "INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (f"teslamate_rebuild:{vin}", f"ok {now.isoformat()} positions={counts.get('cc_positions', 0)}"),
            )
            conn.commit()
        self.last_rebuild[vin] = now
        return counts

    def floors(self, vin: str) -> tuple[datetime | None, datetime | None]:
        """(the end of every other import for this car, how far this poller got last time)."""
        with self.connect() as conn:
            other = conn.execute("SELECT MAX(last_at) AS t FROM cc_imports WHERE vin = %s AND id <> %s", (vin, import_id(vin))).fetchone()
            mine = conn.execute("SELECT last_at AS t FROM cc_imports WHERE id = %s", (import_id(vin),)).fetchone()
        return (other["t"] if other else None), (mine["t"] if mine else None)

    def record_position(self, vin: str, p: dict, now: datetime) -> bool:
        """The newest TeslaMate position into cc_positions, once, so Now shows the car where it is."""
        at = p["date"] if p["date"].tzinfo else p["date"].replace(tzinfo=timezone.utc)
        if self.last_pos.get(vin) == at or p.get("latitude") is None:
            return False
        state = "DRIVING" if p.get("drive_id") else "PARKED"
        with self.connect() as conn:
            if history_writer.writes(conn, vin, "teslamate") is False:
                return False  # the position is history: only the car's writer keeps it
            conn.execute(
                "INSERT INTO cc_positions (vin, at, latitude, longitude, level, odometer, state, speed, import_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    vin,
                    at,
                    p["latitude"],
                    p["longitude"],
                    p.get("battery_level"),
                    (p["odometer"] * 0.621371) if p.get("odometer") is not None else None,
                    state,
                    (p["speed"] * 0.621371) if p.get("speed") is not None else None,
                    import_id(vin),
                ),
            )
            conn.commit()
        self.last_pos[vin] = at
        return True

    # ---- the loop
    def tick(self) -> None:
        now = datetime.now(timezone.utc)
        try:
            tm_conn = self.connect_teslamate()
        except Exception:
            for vin in self.vins:
                self.results[vin] = (self.results.get(vin, (None, None))[0], "TeslaMate did not answer")
            raise
        with tm_conn as tm:
            for car in self.followed(self.cars(tm)):
                vin = car["vin"]
                try:
                    p = self.newest_position(tm, car["id"])
                    if p:
                        self.record_position(vin, p, now)
                    self.results[vin] = (now, None)
                except Exception as err:  # noqa: BLE001
                    LOG.warning("teslamate position for %s: %s", vin, err)
                    self.results[vin] = (self.results.get(vin, (None, None))[0], f"{type(err).__name__}: {str(err)[:120]}")
                last = self.last_rebuild.get(vin)
                if last is None or (now - last).total_seconds() >= self.rebuild_every_s:
                    try:
                        floor, resume = self.floors(vin)
                        tables = self.tables_since(tm, car["id"], window_start(now, floor, resume))
                        counts = self.rebuild(vin, car, tables, now, floor)
                        if counts:
                            LOG.info("teslamate rebuild %s: %s trips, %s charges, %s positions", vin, counts.get("trips"), counts.get("charging_sessions"), counts.get("cc_positions"))
                    except Exception as err:  # noqa: BLE001
                        LOG.warning("teslamate rebuild failed for %s: %s", vin, err)

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception as err:  # noqa: BLE001
                LOG.warning("teslamate tick failed: %s", err)
            await asyncio.sleep(self.interval_s)

    def start(self) -> None:
        if not self.db_url:
            LOG.info("no TESLAMATE_DB_URL; no TeslaMate to follow")
            return
        self.task = asyncio.get_event_loop().create_task(self.run())


def check(db_url: str, days: int = 3) -> str:
    """What a rebuild would see, without writing: cars and sample counts. `python3 teslamate_live.py --check`."""
    p = TeslaMatePoller(db_url, connect=None)
    lines = []
    with p.connect_teslamate() as tm:
        for car in p.cars(tm):
            since = datetime.now(timezone.utc) - timedelta(days=days)
            tables = p.tables_since(tm, car["id"], since)
            samples = samples_from_tables(tables, car["vin"], car.get("name") or car["vin"])
            out = replay_tables(tables, car["vin"], car.get("name") or car["vin"]) if samples else {}
            lines.append(
                f"{car.get('name')} ({model_name(car)}): {len(tables['positions'])} positions, {len(tables['charges'])} charge rows, {len(tables['states'])} states → {len(samples)} samples, {len(out.get('trips', []))} trips, {len(out.get('charging_sessions', []))} charges in the last {days} days"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    import os
    import sys

    if "--check" in sys.argv:
        print(check(os.environ["TESLAMATE_DB_URL"], int(sys.argv[sys.argv.index("--check") + 1]) if len(sys.argv) > sys.argv.index("--check") + 1 else 3))
