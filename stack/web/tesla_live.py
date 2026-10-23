"""The cars' own stream, from the receiver into history.

The receiver (the `tesla` compose profile) publishes every record a car sends on a ZMQ
socket, one message per record: the topic `tesla_telemetry_<kind>` (V for data,
connectivity, alerts, errors) and the record as JSON. This subscribes in a background
thread, keeps each record raw in cc_tesla_raw (written in batches every few seconds,
kept 120 days) and, once an hour per car, replays the last day and a half of raw records
through tesla_stream into history.write_history, replacing what the previous replay
wrote: the TeslaFi feed's pattern, so a fix to the working-out reaches back by itself.

Only cars chosen on Connect Tesla are kept; a record from any other VIN is dropped and
counted. Nothing here runs on a page: the page reads `results`, `state` and `dropped`.

Silence: the receiver writes a DISCONNECTED record only when a car's socket closes. When
a car's records stop for any other reason (the receiver restarted, a message was lost),
the replay puts a disconnect after the last record (with_silence), so a drive cannot run on
and a car the receiver lost sight of still falls asleep."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import groupby

import fleet_usage
import history_writer
import identity
import tesla_nav
import tesla_stream
from history import _t, fill_after_import, write_history
from recorder import should_record
from teslafi_import import DEFAULT_TZ, USABLE_KWH, model_from_vin, replay

LOG = logging.getLogger("carcommand.tesla")
NAMESPACE = "tesla_telemetry"
KINDS = ("V", "connectivity", "alerts", "errors")
WINDOW = timedelta(hours=36)
SILENCE = tesla_stream.DRIVE_END  # driving, quiet this long and the car is gone: the grace T2a's drive rule uses
QUIET = timedelta(minutes=30)  # not driving, quiet this long and the car is gone
KEEP = timedelta(days=120)
LIVE = timedelta(minutes=2)  # a message this recent and the car is streaming now
FLUSH_S = 5
BATCH = 300
HELD_MAX = 50_000  # raw records held while the database is away; past this the oldest go
# What the car's live position is made of, carried from record to record; and Sentry, for Now's guard dot
LIVE_FIELDS = (
    "Location",
    "Odometer",
    "BatteryLevel",
    "VehicleSpeed",
    "Gear",
    "GpsHeading",
    "PackVoltage",
    "PackCurrent",
    "SentryMode",
    # locked, doors and windows, for Now and the API
    "Locked",
    "DoorState",
    "FdWindow",
    "FpWindow",
    "RdWindow",
    "RpWindow",
    # charging and plugged, and the charge limit, for Now's command row and the charging hold
    "DetailedChargeState",
    "ChargeState",
    "ChargeLimitSoc",
    # the cabin and outside, for Now and the API while the car is awake
    "InsideTemp",
    "OutsideTemp",
    # range, charging power and energy, climate, the port, a waiting update, the driver: the rest of Now
    "RatedRange",
    "EstBatteryRange",
    "ACChargingPower",
    "DCChargingPower",
    "ChargerVoltage",
    "ChargeAmps",
    "ACChargingEnergyIn",
    "DCChargingEnergyIn",
    "HvacPower",
    "ClimateKeeperMode",
    "DefrostMode",
    "HvacLeftTemperatureRequest",
    "PreconditioningEnabled",
    "ChargePortDoorOpen",
    "ChargePortLatch",
    "ChargeRateMilePerHour",
    "TimeToFullCharge",
    "ChargeCurrentRequest",
    "DriverSeatOccupied",
    "SoftwareUpdateVersion",
    "Version",
)
RESTORE = timedelta(days=3)  # how far back a restart reads the stream to know each car's last word

DDL = """
CREATE TABLE IF NOT EXISTS cc_tesla_raw (
    vin    VARCHAR NOT NULL,
    at     TIMESTAMPTZ NOT NULL,
    kind   VARCHAR NOT NULL,
    record JSONB NOT NULL,
    PRIMARY KEY (vin, at, kind)
);
"""


def import_id(vin: str) -> str:
    return f"tesla-stream:{vin}"


def parse(frames: list[bytes], now: datetime, namespace: str = NAMESPACE) -> tuple[str, str, datetime, dict] | None:
    """One ZMQ message as (kind, vin, time the car made it, record), or None when it is not a car's record."""
    if len(frames) < 2:
        return None
    kind = frames[0].decode("utf-8", "replace").removeprefix(namespace + "_")
    if kind not in KINDS:
        return None
    try:
        record = json.loads(frames[-1])
    except ValueError:
        return None
    if not isinstance(record, dict) or not record.get("vin"):
        return None
    stamp = record.get("createdAt") or record.get("created_at")
    try:
        at = tesla_stream.when(stamp) if stamp else now
    except ValueError:
        at = now
    return kind, record["vin"], at, record


def with_silence(recs: list[tuple[datetime, str, dict]], until: datetime) -> list[tuple[datetime, str, dict]]:
    """The records with a disconnect added where a car went quiet and none came: SILENCE
    after its last record while it was driving (a moving car sends every second), QUIET
    after it otherwise (a parked car, or one on a slow charge, can go minutes with nothing
    new to say). The end of the window counts as the next record."""
    out = []
    for vin, group in groupby(sorted(recs, key=lambda r: (r[1] or "", r[0])), key=lambda r: r[1]):
        group, gear, speed = list(group), None, 0.0
        for i, (t, _vin, fields) in enumerate(group):
            out.append((t, vin, fields))
            if "Gear" in fields:
                gear = fields["Gear"] if isinstance(fields["Gear"], str) else None
            if "VehicleSpeed" in fields:
                speed = fields["VehicleSpeed"] if isinstance(fields["VehicleSpeed"], (int, float)) else 0.0
            if fields.get("_connected") is False:
                continue
            grace = SILENCE if gear in tesla_stream.DRIVING_GEARS or speed > tesla_stream.MOVING_MPH else QUIET
            nxt = group[i + 1][0] if i + 1 < len(group) else until
            if nxt - t > grace:
                out.append((t + grace, vin, {"_connected": False}))
    return out


def window_start(samples: list[dict], now: datetime, floor: datetime | None, resume: datetime | None = None) -> datetime | None:
    """Where a rebuild starts: the last moment asleep before now-36h, so no drive or charge
    is cut, but never before `floor`. `resume` (how far the last good rebuild got) opens the
    window back to an hour before it after a run of failed rebuilds."""
    if not samples:
        return None
    nominal = now - WINDOW
    if resume is not None and resume - timedelta(hours=1) < nominal:
        nominal = resume - timedelta(hours=1)
    start = next((s["time"] for s in reversed(samples) if s["time"] <= nominal and s["asleep"]), max(nominal, samples[0]["time"]))
    return floor if floor is not None and start < floor else start


def zmq_socket(url: str):
    """A SUB socket on the receiver, subscribed to everything. ZMQ reconnects by itself,
    from one second apart up to a minute, whenever the receiver goes away."""
    import zmq

    s = zmq.Context.instance().socket(zmq.SUB)
    s.setsockopt(zmq.RCVHWM, 100_000)  # a rebuild takes seconds; records wait here meanwhile
    s.setsockopt(zmq.RECONNECT_IVL, 1000)
    s.setsockopt(zmq.RECONNECT_IVL_MAX, 60_000)
    s.setsockopt(zmq.LINGER, 0)
    s.connect(url)
    s.setsockopt(zmq.SUBSCRIBE, b"")
    return _Sub(s)


class _Sub:
    def __init__(self, s):
        self.s = s

    def recv(self, timeout_ms: int) -> list[bytes] | None:
        return self.s.recv_multipart() if self.s.poll(timeout_ms) else None

    def close(self) -> None:
        self.s.close(0)


class TeslaStream:
    def __init__(self, connect, url: str | None = None, tz=DEFAULT_TZ, socket_factory=zmq_socket, rebuild_every_s: int = 3600, clock=None) -> None:
        self.connect, self.url, self.tz = connect, url, tz
        self.socket_factory = socket_factory
        self.rebuild_every_s = rebuild_every_s
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.vins: set[str] = set()
        self.results: dict[str, tuple[datetime | None, str | None]] = {}  # vin -> (last good rebuild, last error), for Settings
        self.state: dict[str, dict] = {}  # vin -> {"at": last message, "connected": bool}
        self.car_errors: dict[str, list[dict]] = {}  # vin -> the last errors the car itself reported
        self.dropped: Counter = Counter()  # records from cars not chosen, by VIN
        self.error: str | None = None  # the subscriber's last trouble, for Settings
        self.held: list[tuple] = []
        self.lost = 0  # records dropped because the database was away longer than HELD_MAX lasts
        self.backoff = 1
        self.last_rebuild: dict[str, datetime] = {}
        self.last_prune: datetime | None = None
        # The live position, for a car whose history the stream writes: the newest of each
        # LIVE_FIELDS value, written with each flush when the car has moved (Now stays fresh
        # between the hourly replays, which rewrite these rows with the rest)
        self.carried: dict[str, dict] = {}
        self.identity: dict[str, dict] = {}  # vin -> the newest CarType, Trim, ExteriorColor... (identity.Worker keeps them)
        self.moved: set[str] = set()
        self.writing: set[str] = set()
        self.last_pos: dict[str, dict] = {}
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None

    # ---- owned like the feed poller: Settings hands it the cars, it starts when there are any
    def use(self, vins, url: str | None = None) -> None:
        self.vins = {v for v in vins or () if v}
        if url is not None:
            self.url = url
        if self.url and self.vins and not (self.thread and self.thread.is_alive()):
            self.stopped.clear()
            self.thread = threading.Thread(target=self.run, daemon=True, name="tesla-stream")
            self.thread.start()

    def stop(self) -> None:
        self.stopped.set()

    def live(self, vin: str, now: datetime | None = None) -> bool:
        s = self.state.get(vin)
        return bool(s and s["connected"] and (now or self.clock()) - s["at"] <= LIVE)

    # ---- one record in
    def handle(self, frames: list[bytes]) -> bool:
        now = self.clock()
        got = parse(frames, now)
        if got is None:
            return False
        kind, vin, at, record = got
        if kind == "V":
            fleet_usage.METER.signals(vin, len(record.get("data") or []))  # billed per signal, a car not chosen here too
        if vin not in self.vins:
            if not self.dropped[vin]:
                LOG.info("tesla stream: records from a car not chosen on Connect Tesla are dropped")
            self.dropped[vin] += 1
            return False
        connected = str(record.get("status", "")).upper() != "DISCONNECTED" if kind == "connectivity" else True
        self.state[vin] = {"at": now, "connected": connected}
        if kind == "errors":
            self.car_errors[vin] = [{"name": e.get("name") or "", "body": str(e.get("body") or "")[:300], "at": at} for e in record.get("errors") or [] if isinstance(e, dict)][:5]
        self.held.append((vin, at, kind, json.dumps(record)))
        if kind == "V":
            self.carry(vin, at, record)
        if len(self.held) > HELD_MAX:
            over = len(self.held) - HELD_MAX
            del self.held[:over]
            self.lost += over
        return True

    def carry(self, vin: str, at: datetime, record: dict) -> None:
        c = self.carried.setdefault(vin, {})
        if c.get("_at") is not None and at < c["_at"]:
            return  # a resent record from before what is already carried
        for d in record.get("data") or []:
            k = d.get("key") if isinstance(d, dict) else None
            if k in identity.STREAM_KEYS:
                v = tesla_stream.value_of(d.get("value") or {})
                if isinstance(v, float) and v.is_integer():
                    v = str(int(v))  # a trim of digits alone ("74") arrives as a number
                if isinstance(v, str) and v:
                    self.identity.setdefault(vin, {})[k] = v
                continue
            if k not in LIVE_FIELDS and k not in tesla_nav.LIVE_KEYS:
                continue
            v = tesla_stream.value_of(d.get("value") or {})
            if v is tesla_stream.INVALID:
                c.pop(k, None)
            elif v is not None:
                c[k] = v
                if k == "Location":
                    self.moved.add(vin)
        c["_at"] = at

    def route(self, vin: str, now: datetime | None = None) -> dict | None:
        """The route a car is driving now, if any ({name, minutes, miles}): from memory, for Now."""
        c = self.carried.get(vin) or {}
        return tesla_nav.live_route(c, c.get("_at"), now or self.clock())

    def positions(self) -> list[tuple]:
        """cc_positions rows for the cars the stream writes, where each is now, if it moved."""
        rows = []
        for vin in sorted(self.moved & self.writing):
            c = self.carried.get(vin) or {}
            if not c.get("Location"):
                continue
            speed = c.get("VehicleSpeed")
            driving = c.get("Gear") in tesla_stream.DRIVING_GEARS or (speed or 0) > tesla_stream.MOVING_MPH
            pos = {"latitude": c["Location"][0], "longitude": c["Location"][1], "odometer": c.get("Odometer"), "state": "DRIVING" if driving else "PARKED"}
            if not should_record(self.last_pos.get(vin), pos):
                continue
            volts, amps = c.get("PackVoltage"), c.get("PackCurrent")
            power = round(tesla_stream.PACK_SIGN * volts * amps / 1000, 3) if volts is not None and amps is not None else None
            rows.append((vin, c["_at"], pos["latitude"], pos["longitude"], c.get("GpsHeading"), c.get("BatteryLevel"), pos["odometer"], speed, pos["state"], power, import_id(vin)))
            self.last_pos[vin] = pos
        self.moved.clear()
        return rows

    def flush(self) -> int:
        """The held records into cc_tesla_raw, BATCH rows a statement; a resent record is kept once."""
        if not self.held:
            return 0
        rows, n = list(self.held), 0
        with self.connect() as conn:
            cur = conn.cursor()
            for i in range(0, len(rows), BATCH):
                cur.executemany("INSERT INTO cc_tesla_raw (vin, at, kind, record) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", rows[i : i + BATCH])
                n += len(rows[i : i + BATCH])
            for p in self.positions():
                # the writer may have changed since the last tick: asked under the car's lock, so
                # a handover running now finishes first and its choice is what is read
                history_writer.lock(conn, p[0])
                if history_writer.writes(conn, p[0], "tesla") is False:
                    self.writing.discard(p[0])
                    continue
                conn.execute(
                    "INSERT INTO cc_positions (vin, at, latitude, longitude, heading, level, odometer, speed, state, power_kw, import_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", p
                )
            conn.commit()
        del self.held[: len(rows)]
        return n

    @staticmethod
    def watching(conn, vin: str, now: datetime) -> bool:
        """Whether another source writes this car: the chosen writer (history_writer) decides;
        with none chosen, another live source (the TeslaFi feed, TeslaMate) that wrote it in
        the last day and a half owns its history and the stream counts what it would write."""
        mine = history_writer.writes(conn, vin, "tesla")
        if mine is not None:
            return not mine
        other_live = _t(conn.execute("SELECT MAX(last_at) FROM cc_imports WHERE vin = %s AND (id LIKE 'teslafi-live:%%' OR id LIKE 'teslamate-live:%%')", (vin,)).fetchone())[0]
        return other_live is not None and other_live > now - WINDOW

    # ---- the hourly replay
    def rebuild(self, vin: str, now: datetime) -> dict | None:
        with self.connect() as conn:
            history_writer.lock(conn, vin)
            watching = self.watching(conn, vin, now)
            floor = _t(conn.execute("SELECT MAX(last_at) FROM cc_imports WHERE vin = %s AND id <> %s", (vin, import_id(vin))).fetchone())[0]
            done = _t(conn.execute("SELECT last_at FROM cc_imports WHERE id = %s", (import_id(vin),)).fetchone())
            resume = done[0] if done else None
            since = min(now - 3 * WINDOW, (resume - timedelta(hours=1) - WINDOW) if resume else now)
            rows = conn.execute("SELECT record FROM cc_tesla_raw WHERE vin = %s AND at >= %s AND kind IN ('V', 'connectivity') ORDER BY at", (vin, since)).fetchall()
            payloads = [r["record"] if isinstance(r, dict) else r[0] for r in rows]
            recs = with_silence(tesla_stream.records([p if isinstance(p, dict) else json.loads(p) for p in payloads]), until=now)
            samples = tesla_stream.samples(recs, until=now)
            # where each drive navigated to, kept whoever writes the car's history
            tesla_nav.save(conn, tesla_nav.per_drive(recs, samples))
            start = window_start(samples, now, None if watching else floor, resume)
            samples = [s for s in samples if start is not None and s["time"] >= start]
            if not samples:
                return None
            out = replay(samples)
            if watching:
                counts = {"trips": len(out.get("trips") or []), "charging_sessions": len(out.get("charging_sessions") or [])}
                self.note(conn, vin, f"watching {now.isoformat()} trips={counts['trips']} charges={counts['charging_sessions']} (another source writes this car)")
                conn.commit()
                return None
            veh = out["vehicle"]
            model = veh.get("model") or model_from_vin(vin)
            counts = write_history(conn, vin, veh.get("name") or vin, "Tesla", model, USABLE_KWH.get(model or ""), out, import_id(vin), "tesla stream", self.tz, rolling=True)
            fill_after_import(conn, vin, self.tz)
            self.note(conn, vin, f"ok {now.isoformat()} trips={counts['trips']} from={start.isoformat()}")
            conn.commit()
        return counts

    @staticmethod
    def note(conn, vin: str, text: str) -> None:
        """The last rebuild's outcome, kept where Settings and a later session can read it."""
        conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f"stream_rebuild:{vin}", text[:300]))

    def prune(self, now: datetime) -> int:
        """Raw records past KEEP, BATCH at a time, so no statement holds the table for long."""
        n = 0
        with self.connect() as conn:
            for _ in range(1000):
                cur = conn.execute("DELETE FROM cc_tesla_raw WHERE ctid IN (SELECT ctid FROM cc_tesla_raw WHERE at < %s LIMIT %s)", (now - KEEP, BATCH))
                conn.commit()
                n += cur.rowcount or 0
                if (cur.rowcount or 0) < BATCH:
                    break
        self.last_prune = now
        return n

    def tick(self) -> None:
        """What the thread does between records: which cars it writes (for their live
        position), the hourly rebuilds and the daily prune."""
        now = self.clock()
        try:
            with self.connect() as conn:
                self.writing = {vin for vin in self.vins if not self.watching(conn, vin, now)}
        except Exception as err:  # noqa: BLE001 - the rebuilds below say the database's trouble
            LOG.warning("tesla stream: which cars it writes: %s", err)
        for vin in sorted(self.vins):
            last = self.last_rebuild.get(vin)
            if last is not None and (now - last).total_seconds() < self.rebuild_every_s:
                continue
            self.last_rebuild[vin] = now  # tried, whatever comes of it: the next try is an hour on
            try:
                counts = self.rebuild(vin, now)
                self.results[vin] = (now, None)
                if counts:
                    LOG.info("tesla stream rebuild %s: %s trips, %s charges", vin, counts.get("trips"), counts.get("charging_sessions"))
            except Exception as err:  # noqa: BLE001 - one car's failure must not stop the others
                LOG.warning("tesla stream rebuild failed for %s: %s", vin, err)
                self.results[vin] = (self.results.get(vin, (None, None))[0], f"{type(err).__name__}: {err}"[:160])
                try:
                    with self.connect() as conn:
                        self.note(conn, vin, f"error {now.isoformat()} {str(err)[:200]}")
                        conn.commit()
                except Exception:  # noqa: BLE001
                    pass
        if self.last_prune is None or now - self.last_prune >= timedelta(days=1):
            try:
                self.prune(now)
            except Exception as err:  # noqa: BLE001
                LOG.warning("tesla stream prune: %s", err)
                self.last_prune = now

    # ---- the thread
    def serve(self) -> None:
        """Records in until stopped; raises when the socket fails, and run() starts over after a
        pause. A database that is away is not the socket's trouble: the records wait in `held`
        and the next flush tries again, so nothing the cars send meanwhile is missed."""
        sock = self.socket_factory(self.url)
        try:
            flushed, ticked = time.monotonic(), None
            while not self.stopped.is_set():
                frames = sock.recv(1000)
                if frames and self.handle(frames):
                    self.backoff, self.error = 1, None
                if time.monotonic() - flushed >= FLUSH_S or len(self.held) >= BATCH:
                    self.safe_flush()
                    flushed = time.monotonic()
                if ticked is None or time.monotonic() - ticked >= 60:
                    if self.safe_flush():  # a rebuild reads what has just arrived
                        self.tick()
                    ticked = time.monotonic()
        finally:
            sock.close()

    def safe_flush(self) -> bool:
        """flush(), with a failure said and the records kept for the next try."""
        try:
            self.flush()
        except Exception as err:  # noqa: BLE001 - the database's trouble; the socket stays open
            self.error = f"writing records: {type(err).__name__}: {err}"[:160]
            LOG.warning("tesla stream: %s; %d records held", self.error, len(self.held))
            return False
        self.backoff = 1
        return True

    def restore(self, now: datetime | None = None) -> int:
        """Each car's last word from the stored stream, so a restart does not forget a sleeping car's
        lock, doors, windows and charge limit until it next wakes. Writes nothing."""
        since, n = (now or self.clock()) - RESTORE, 0
        with self.connect() as conn:
            for vin in sorted(self.vins):
                for at, record in (tuple(_t(r)) for r in conn.execute("SELECT at, record FROM cc_tesla_raw WHERE vin = %s AND kind = 'V' AND at >= %s ORDER BY at", (vin, since)).fetchall()):
                    self.carry(vin, at, record if isinstance(record, dict) else json.loads(record))
                    n += 1
        self.moved.clear()  # nothing restored is a new position to write
        return n

    def run(self) -> None:
        self.backoff = 1
        try:
            LOG.info("tesla stream: %d stored records restore what each car last said", self.restore())
        except Exception as err:  # noqa: BLE001 - a fresh install has no table yet; the cars say it again when they wake
            LOG.warning("tesla stream: restore: %s", err)
        while not self.stopped.is_set():
            try:
                self.serve()
            except Exception as err:  # noqa: BLE001 - say what failed, then start over
                self.error = f"{type(err).__name__}: {err}"[:160]
                LOG.warning("tesla stream: %s; again in %s s", self.error, self.backoff)
                self.stopped.wait(self.backoff)
                self.backoff = min(self.backoff * 2, 60)
        try:
            self.flush()
        except Exception as err:  # noqa: BLE001
            LOG.warning("tesla stream: %d records not written at stop: %s", len(self.held), err)
