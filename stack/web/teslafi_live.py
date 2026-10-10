"""Keep the Teslas current from TeslaFi's feed API until Car Command talks to
Tesla itself.

TeslaFi exposes the same poll row the export is made of at
    https://www.teslafi.com/feed.php   with   Authorization: Bearer <token>
one row per call, one token per vehicle. This polls each token once a minute, keeps every distinct
row raw (cc_teslafi_rows, unique per vin and Date), writes the position
straight away so Now is fresh, and once an hour replays the last day and a
half of raw rows through the same code the export went through, replacing
what the previous replay wrote. The window starts at a sleep, so no drive or
charge is ever split, and never reaches back into the full-history import.

Tokens: TeslaFi → Settings → Advanced → API, one per vehicle, into
TESLAFI_TOKENS in stack/.env on the host, comma separated, optionally as
VIN:token. A sleeping car's row carries no vin, so without the prefix the
poller asks for `lastGood` (the last full row) once to learn it.
"""

from __future__ import annotations

import asyncio
import sys
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import history_writer
from history import _t, fill_after_import, write_history
from recorder import should_record
from teslafi_import import USABLE_KWH, DEFAULT_TZ, model_from_vin, replay, sample_from_row, vehicle_state

LOG = logging.getLogger("carcommand.teslafi")
FEED = os.environ.get("TESLAFI_FEED_URL", "https://www.teslafi.com/feed.php")
WINDOW = timedelta(hours=36)

DDL = """
CREATE TABLE IF NOT EXISTS cc_teslafi_rows (
    vin  VARCHAR NOT NULL,
    at   TIMESTAMPTZ NOT NULL,
    data JSONB NOT NULL,
    PRIMARY KEY (vin, at)
);
"""


def import_id(vin: str) -> str:
    return f"teslafi-live:{vin}"


def window_start(raw: list[tuple[datetime, dict]], now: datetime, floor: datetime | None, tz=DEFAULT_TZ, resume: datetime | None = None) -> datetime:
    """Where a rebuild starts: the last sleeping row before now-36h, but never before `floor`
    (the end of the full-history import, which owns everything earlier).

    `resume` is how far the last successful rebuild got. When that is older than the
    36-hour window the window opens back to an hour before it, so a stretch of failed
    rebuilds is caught up by the first one that works. Without this, a car's drives
    of Sep 14 and 15 2026 sat in the raw rows and were never written: the rebuilds of
    those days failed, the ones from Sep 16 on looked back 36 hours, and the drives fell
    in between. The raw rows are kept for good, so a replay can reach back any distance.
    """
    nominal = now - WINDOW
    if resume is not None and resume - timedelta(hours=1) < nominal:
        nominal = resume - timedelta(hours=1)
    start = nominal
    for at, row in reversed(raw):
        if at <= nominal:
            s = sample_from_row(row, tz, when=at)
            if s and s["asleep"]:
                start = at
                break
            if at < nominal - WINDOW:
                break
    if floor is not None and start < floor:
        start = floor
    return start


# A car that stops driving or charging has its history rewritten on the next poll, not on the hour.
ACTIVE_STATES = ("Driving", "Charging")


class TeslaFiPoller:
    def __init__(self, tokens: list[str], connect, interval_s: int = 60, rebuild_every_s: int = 3600, tz: ZoneInfo = DEFAULT_TZ) -> None:
        self.tokens: list[str] = []
        self.vin_for_token: dict[str, str] = {}
        self._parse(tokens)
        self.results: dict[str, tuple[datetime | None, str | None]] = {}  # vin -> (last success, last error), for Settings
        self.connect = connect
        self.interval_s = interval_s
        self.rebuild_every_s = rebuild_every_s
        self.tz = tz
        self.last_pos: dict[str, dict] = {}
        self.last_rebuild: dict[str, datetime] = {}
        self.caught_up: set[str] = set()  # cars whose history hole was filled this start
        self.active: dict[str, bool] = {}  # vin -> driving or charging at the last poll
        self.ended: set[str] = set()  # cars whose drive or charge just ended: rebuild now, not on the hour
        self.task: asyncio.Task | None = None

    def _parse(self, tokens: list[str]) -> None:
        parsed, vins = [], {}
        for t in tokens:
            t = (t or "").strip()
            if not t:
                continue
            if ":" in t:
                vin, t = t.rsplit(":", 1)
                vins[t] = vin.strip()
            parsed.append(t)
        self.tokens, self.vin_for_token = parsed, vins

    def use(self, tokens: list[str]) -> None:
        """New tokens, from Settings, while running. The loop reads self.tokens every tick."""
        self._parse(tokens)
        self._ensure_running()

    def _ensure_running(self) -> None:
        if not self.tokens or (self.task and not self.task.done()):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # not started yet; start() will pick the tokens up
        self.task = asyncio.create_task(self.run())

    # ---- one poll
    def fetch(self, token: str, command: str | None = None) -> dict:
        import httpx

        # bearer header, TeslaFi's recommended form: the token never appears in a URL or an access log
        r = httpx.get(FEED, params={"command": command} if command else None, timeout=20, headers={"User-Agent": "carcommand/0.1", "Authorization": f"Bearer {token}"})
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, dict):
            raise ValueError(f"unexpected feed body: {str(body)[:80]}")
        return body

    def ingest(self, token: str, row: dict, now: datetime) -> bool:
        """Store one feed row. Returns True when it was new."""
        vin = row.get("vin") or self.vin_for_token.get(token)
        if not vin:
            # asleep: the row is blank. Ask once for the last full row, which names the car.
            try:
                full = self.fetch(token, "lastGood")
                vin = full.get("vin")
                if vin:
                    self.vin_for_token[token] = vin
                    self.ingest(token, full, now)
            except Exception as err:  # noqa: BLE001
                LOG.warning("lastGood lookup failed: %s", err)
        if not vin:
            LOG.warning("feed row without a vin and no vin known for this token yet")
            return False
        self.vin_for_token[token] = vin
        sample = sample_from_row(row, self.tz, when=now)
        if sample is None:
            return False
        at = sample["time"]
        with self.connect() as conn:
            conn.execute(DDL)
            cur = conn.execute("INSERT INTO cc_teslafi_rows (vin, at, data) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (vin, at, json.dumps(row)))
            new = cur.rowcount == 1
            # the position is history: only the car's writer keeps it (history_writer)
            if new and sample["latitude"] is not None and history_writer.writes(conn, vin, "teslafi") is not False:
                pos = {
                    "vin": vin,
                    "latitude": sample["latitude"],
                    "longitude": sample["longitude"],
                    "heading": sample["heading"],
                    "level": sample["level"],
                    "odometer": sample["odometer"],
                    "state": vehicle_state(sample),
                }
                if should_record(self.last_pos.get(vin), pos):
                    conn.execute(
                        "INSERT INTO cc_positions (vin, at, latitude, longitude, heading, level, odometer, speed, state, power_kw, import_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (vin, at, pos["latitude"], pos["longitude"], pos["heading"], pos["level"], pos["odometer"], sample["speed"], pos["state"], sample.get("power_kw"), import_id(vin)),
                    )
                    self.last_pos[vin] = pos
            conn.commit()
        return new

    # ---- hourly replay of the recent window
    def rebuild(self, vin: str, now: datetime) -> dict | None:
        with self.connect() as conn:
            history_writer.lock(conn, vin)
            if history_writer.writes(conn, vin, "teslafi") is False:
                # another source writes this car: the raw rows are kept, nothing is replayed
                self.note(conn, vin, f"watching {now.isoformat()} (another source writes this car)")
                conn.commit()
                self.last_rebuild[vin] = now
                return None
            floor = _t(conn.execute("SELECT MAX(last_at) FROM cc_imports WHERE vin = %s AND id <> %s", (vin, import_id(vin))).fetchone())[0]
            done = _t(conn.execute("SELECT last_at FROM cc_imports WHERE id = %s", (import_id(vin),)).fetchone())
            resume = done[0] if done else None
            since = min(now - 3 * WINDOW, (resume - timedelta(hours=1) - WINDOW) if resume else now)
            raw = [tuple(_t(r)) for r in conn.execute("SELECT at, data FROM cc_teslafi_rows WHERE vin = %s AND at >= %s ORDER BY at", (vin, since)).fetchall()]
            start = window_start(raw, now, floor, self.tz, resume=resume)
            samples = [s for s in (sample_from_row(row, self.tz, when=at) for at, row in raw if at >= start) if s]
            if not samples:
                return None
            out = replay(samples)
            veh = out["vehicle"]
            model = veh.get("model") or model_from_vin(vin)
            counts = write_history(conn, vin, veh.get("name") or vin, "Tesla", model, USABLE_KWH.get(model or ""), out, import_id(vin), "teslafi feed", self.tz, rolling=True)
            fill_after_import(conn, vin, self.tz)
            # the raw rows stay for good: they are what a replay (`make replay`) reads, see raw.py
            self.note(conn, vin, f"ok {now.isoformat()} trips={counts['trips']} from={start.isoformat()}")
            conn.commit()
        self.last_rebuild[vin] = now
        return counts

    @staticmethod
    def note(conn, vin: str, text: str, key: str = "feed_rebuild") -> None:
        """The last rebuild's outcome, in cc_state: the container's log is gone the next
        time the container is rebuilt, and three days of failed rebuilds went unseen that way."""
        conn.execute("CREATE TABLE IF NOT EXISTS cc_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f"{key}:{vin}", text))

    def tick(self) -> None:
        now = datetime.now(timezone.utc)
        for token in list(self.tokens):
            vin = self.vin_for_token.get(token)
            try:
                row = self.fetch(token)
                self.ingest(token, row, now)
                vin = vin or row.get("vin") or self.vin_for_token.get(token)
                if vin:
                    self.results[vin] = (now, None)
                    active = row.get("carState") in ACTIVE_STATES
                    if self.active.get(vin) and not active:
                        self.ended.add(vin)
                    self.active[vin] = active
            except Exception as err:  # noqa: BLE001 - one bad poll must not stop the others
                LOG.warning("teslafi poll failed: %s", err)
                if vin:
                    self.results[vin] = (self.results.get(vin, (None, None))[0], f"{type(err).__name__}: {err}".replace(token, "***")[:120])  # masked, then cut
        for token, vin in list(self.vin_for_token.items()):
            if vin not in self.caught_up:
                self.caught_up.add(vin)
                try:
                    self.catch_up(vin, token, now)
                except Exception as err:  # noqa: BLE001
                    LOG.warning("teslafi history catch-up failed for %s: %s", vin, err)
        for vin in set(self.vin_for_token.values()):
            last = self.last_rebuild.get(vin)
            if last is None or vin in self.ended or (now - last).total_seconds() >= self.rebuild_every_s:
                self.ended.discard(vin)
                try:
                    counts = self.rebuild(vin, now)
                    if counts:
                        LOG.info("teslafi rebuild %s: %s trips, %s charges, %s positions", vin, counts["trips"], counts["charging_sessions"], counts["cc_positions"])
                except Exception as err:  # noqa: BLE001
                    LOG.warning("teslafi rebuild failed for %s: %s", vin, err)
                    try:
                        with self.connect() as conn:
                            self.note(conn, vin, f"error {now.isoformat()} {str(err)[:200]}")
                            conn.commit()
                    except Exception:  # noqa: BLE001
                        pass

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception as err:  # noqa: BLE001
                LOG.warning("teslafi tick failed: %s", err)
            await asyncio.sleep(self.interval_s)

    def catch_up(self, vin: str, token: str, now: datetime) -> dict | None:
        """Fill the hole between the newest thing any import wrote for this car and now
        from TeslaFi's history summaries, once per car per start. The feed only ever
        hands over the current reading, so a token added weeks after the last export
        (one car, 2026-09-28: 24 days) would otherwise leave those weeks empty."""
        import teslafi_history

        with self.connect() as conn:
            if history_writer.writes(conn, vin, "teslafi") is False:
                return None  # another source writes this car's history; its holes are its own
            newest, feed_first = teslafi_history.edges(conn, vin)
            done = conn.execute("SELECT value FROM cc_state WHERE key = %s", (f"history_catchup:{vin}",)).fetchone()
        window = teslafi_history.catchup_window(newest, feed_first, now)
        if window is None:
            return None
        stamp = f"{window[0]}..{window[1]}"
        if done and stamp in str(done["value"] if isinstance(done, dict) else done[0]):
            return None  # this hole was filled on an earlier start; TeslaFi had what it had
        counts = teslafi_history.backfill(self.connect, vin, token, window[0], window[1], self.tz, after=newest, before=feed_first)
        with self.connect() as conn:
            self.note(
                conn,
                vin,
                f"ok {now.isoformat()} history {window[0]}..{window[1]} " + ("nothing" if counts is None else f"trips={counts['trips']} charges={counts['charging_sessions']}"),
                key="history_catchup",
            )
            conn.commit()
        LOG.info("teslafi history for %s %s..%s: %s", vin, window[0], window[1], counts)
        return counts

    def start(self) -> None:
        if not self.tokens:
            LOG.info("no TESLAFI_TOKENS; the Teslas stay as imported")
            return
        self._ensure_running()

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()


def replay_from(poller: "TeslaFiPoller", vin: str, since: datetime, now: datetime | None = None) -> dict | None:
    """Rebuild a car's history from its raw feed rows from `since` on: what `make replay`
    runs after a fix to the working-out, and what brought one car's weeks back. It moves
    the feed's own "how far did I get" mark back to `since`, so the next rebuild opens
    there and the rolling clear (history.replace_span) takes only what it rewrites."""
    now = now or datetime.now(timezone.utc)
    with poller.connect() as conn:
        conn.execute("UPDATE cc_imports SET last_at = %s WHERE id = %s AND last_at > %s", (since, import_id(vin), since))
        conn.commit()
    return poller.rebuild(vin, now)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="replay a car's TeslaFi feed rows into its history from a date")
    ap.add_argument("vin")
    ap.add_argument("since", help="ISO date or time; the replay opens at the last sleep before it")
    args = ap.parse_args(argv)
    import app

    since = datetime.fromisoformat(args.since)
    if since.tzinfo is None:
        since = since.replace(tzinfo=app.TZ)
    poller = TeslaFiPoller([], connect=app.db, tz=app.TZ)
    counts = replay_from(poller, args.vin, since)
    print("nothing to replay" if counts is None else ", ".join(f"{v} {k}" for k, v in counts.items() if v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
