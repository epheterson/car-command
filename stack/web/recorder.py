"""Position recorder: the one thing the database plugin does not keep.

CarConnectivity's database plugin stores trip endpoints only, so a drive has
no route. This records the car's position from the live tree into our own
table, `cc_positions`, once a minute while it changes. Reads never wake the
car: the REST plugin serves the connector's in-memory tree.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from notify import CHANNELS, URGENT, connector_down, send_discord, send_email, send_pushover, send_telegram, send_webhook

LOG = logging.getLogger("carcommand.recorder")

DDL = """
CREATE TABLE IF NOT EXISTS cc_positions (
    id        BIGSERIAL PRIMARY KEY,
    vin       VARCHAR NOT NULL,
    at        TIMESTAMPTZ NOT NULL,
    latitude  DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    heading   DOUBLE PRECISION,
    level     DOUBLE PRECISION,
    odometer  DOUBLE PRECISION,
    state     VARCHAR
);
ALTER TABLE cc_positions ADD COLUMN IF NOT EXISTS import_id VARCHAR;
ALTER TABLE cc_positions ADD COLUMN IF NOT EXISTS speed DOUBLE PRECISION;
ALTER TABLE cc_positions ADD COLUMN IF NOT EXISTS elevation DOUBLE PRECISION;
-- kW at the wheels at this moment, negative under regeneration. The TeslaFi export has
-- carried it in a column nobody mapped; the Lucid tree does not publish it, so a live
-- fix leaves it null and the chart simply does not appear for that car.
ALTER TABLE cc_positions ADD COLUMN IF NOT EXISTS power_kw DOUBLE PRECISION;
-- The Lucid connector's GPS height as sent (0.4.0's gps_elevation_raw), in a unit nobody knows
-- yet; elevfit.py fits it to metres from the Teslas' heights and the terrain model. Never read as
-- elevation, which is metres.
ALTER TABLE cc_positions ADD COLUMN IF NOT EXISTS elevation_raw DOUBLE PRECISION;
CREATE INDEX IF NOT EXISTS cc_positions_vin_at ON cc_positions (vin, at);
-- Every alert as it went out, or did not: Settings › Alerts shows the newest, so whether one
-- fired is read, not guessed. Kept LOG_DAYS (trim_log, in the background).
CREATE TABLE IF NOT EXISTS cc_alert_log (
    id       BIGSERIAL PRIMARY KEY,
    at       TIMESTAMPTZ NOT NULL,
    kind     VARCHAR NOT NULL,
    vin      VARCHAR,
    text     TEXT NOT NULL,
    outcome  VARCHAR NOT NULL,
    channels VARCHAR
);
CREATE INDEX IF NOT EXISTS cc_alert_log_at ON cc_alert_log (at DESC);
"""
LOG_DAYS = 90


def outcome(results: dict[str, bool], muted: bool) -> tuple[str, str]:
    """(outcome, channels) for the log: sent, failed, partly sent, muted, or no channel; the
    channels it went to, each one that failed marked."""
    if muted:
        return "muted", ""
    if not results:
        return "no channel", ""
    ok = sum(bool(v) for v in results.values())
    word = "sent" if ok == len(results) else "failed" if ok == 0 else "partly sent"
    return word, ", ".join(c if good else f"{c} failed" for c, good in results.items())


# The statuses that mean the car is going somewhere, or about to. "Ready" is
# IGNITION_ON: the first fix of a drive is the one you cannot go back for.
MOVING = ("Driving", "Ready")


def next_interval(status: str | None, idle_s: float, driving_s: float) -> float:
    """How long to wait before sampling again."""
    return driving_s if status in MOVING else idle_s


def should_record(last: dict | None, now: dict) -> bool:
    """Record when the car has moved or the odometer ticked; always record the first fix and every state change."""
    if now.get("latitude") is None or now.get("longitude") is None:
        return False
    if last is None:
        return True
    moved = abs(now["latitude"] - last["latitude"]) > 1e-5 or abs(now["longitude"] - last["longitude"]) > 1e-5
    ticked = now.get("odometer") is not None and last.get("odometer") is not None and abs(now["odometer"] - last["odometer"]) > 0.05
    return moved or ticked or now.get("state") != last.get("state")


class Recorder:
    def __init__(self, fetch_live, connect, interval_s: int = 60, driving_interval_s: int = 20, webhook_url: str | None = None, log_connect=None) -> None:
        self.fetch_live = fetch_live
        self.connect = connect
        # The sent log's connection: alerts go out from sign-in and other requests, which must not wait out
        # a busy or dead database for a log row.
        self.log_connect = log_connect or connect
        self.interval_s = interval_s
        # How often to sample while the car is moving. The connector polling faster does
        # nothing on its own: this is the loop that reads the tree and writes a position,
        # so whatever it sleeps is the real resolution of a drive. A 21-minute drive came
        # out with four fixes while the connector was fetching every 20 seconds.
        self.driving_interval_s = driving_interval_s
        self.webhook_url = webhook_url if webhook_url is not None else os.environ.get("DISCORD_WEBHOOK_URL")
        self.telegram = (os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))
        # Email as a third channel: an SMTP URL with the credentials in it, a recipient, an optional sender.
        self.email = (os.environ.get("SMTP_URL"), os.environ.get("EMAIL_TO"), os.environ.get("EMAIL_FROM"))
        self.pushover = (os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER"))
        # Any JSON receiver, a home hub's /alert for one: a URL and, if it asks for one, a Bearer token.
        self.hook = (os.environ.get("ALERT_WEBHOOK_URL"), os.environ.get("ALERT_WEBHOOK_TOKEN"))
        self.stored: dict[str, dict] = {}  # channels added in Settings (alertchans), by type; .env's win
        self.muted: set[str] = set()  # alert kinds switched off on Settings
        self.routes: dict[str, set[str]] = {}  # kind -> channels, from Settings; a kind not listed goes to every channel
        self.last: dict | None = None
        self.last_car: dict | None = None
        self.clock = lambda: datetime.now(timezone.utc)
        self.answered_at = self.clock()  # the last time the connector answered; the start, until it has
        self.down_told = False
        self.on_snapshot = None  # optional callback(car) on every successful tick
        self.watcher = None  # notify.Watcher: the events, shared with the timer that watches every other car
        self.connector_lost = None  # optional () -> bool: the connector has stopped answering
        self.task: asyncio.Task | None = None

    def ensure_table(self) -> None:
        with self.connect() as conn:
            conn.execute(DDL)
            conn.commit()

    def env_confs(self) -> dict[str, dict]:
        """The channels .env sets up, each as the fields it sends with (alertchans.FIELDS)."""
        out: dict[str, dict] = {}
        if self.webhook_url:
            out["discord"] = {"url": self.webhook_url}
        if all(self.telegram):
            out["telegram"] = {"token": self.telegram[0], "chat_id": self.telegram[1]}
        if self.email[0] and self.email[1]:
            out["email"] = {"smtp_url": self.email[0], "to": self.email[1], "from": self.email[2]}
        if all(self.pushover):
            out["pushover"] = {"token": self.pushover[0], "user": self.pushover[1]}
        if self.hook[0]:
            out["webhook"] = {"url": self.hook[0], "token": self.hook[1]}
        return out

    def confs(self) -> dict[str, dict]:
        """Every channel that is set up: those added in Settings, and .env's, which win."""
        return {**self.stored, **self.env_confs()}

    def channels(self) -> list[str]:
        """The channels that are set up, in the order Settings shows them."""
        have = self.confs()
        return [c for c in CHANNELS if c in have]

    def notify(self, text: str, kind: str = "other", vin: str | None = None) -> None:
        to = self.routes.get(kind)
        muted = kind in self.muted or to == set()
        results: dict[str, bool] = {}
        if muted:
            LOG.info("event (muted %s): %s", kind, text)
        else:
            LOG.info("event: %s", text)
            for c in self.channels():
                if to is None or c in to:
                    results[c] = self.send(c, text, kind)
        self.log(kind, vin, text, *outcome(results, muted))

    def send(self, channel: str, text: str, kind: str, conf: dict | None = None) -> bool:
        """One message down one channel; `conf` tries fields that are not saved yet (a test-send)."""
        c = conf if conf is not None else self.confs().get(channel, {})
        if channel == "discord":
            return send_discord(c.get("url"), text)
        if channel == "telegram":
            return send_telegram(c.get("token"), c.get("chat_id"), text)
        if channel == "email":
            return send_email(c.get("smtp_url"), c.get("to"), c.get("from"), text.split("\n", 1)[0][:120], text)
        if channel == "pushover":
            return send_pushover(c.get("token"), c.get("user"), text, priority=1 if kind in URGENT else 0)
        return send_webhook(c.get("url"), c.get("token"), text, kind)

    def log(self, kind: str, vin: str | None, text: str, result: str, channels: str) -> None:
        """One row of the sent log. Never in the way of the message itself."""
        if self.log_connect is None:
            return
        try:
            with self.log_connect() as conn:
                conn.execute(
                    "INSERT INTO cc_alert_log (at, kind, vin, text, outcome, channels) VALUES (%s, %s, %s, %s, %s, %s)",
                    (datetime.now(timezone.utc), kind, vin, text, result, channels),
                )
                conn.commit()
        except Exception as err:  # noqa: BLE001
            LOG.warning("alert log: %s", err)

    def trim_log(self, days: int = LOG_DAYS) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM cc_alert_log WHERE at < now() - make_interval(days => %s)", (days,))
            conn.commit()

    def lost(self) -> None:
        if not self.down_told and connector_down(self.answered_at, self.clock()):
            self.notify("Car Command has not heard from CarConnectivity for 10 minutes.", "connector")
            self.down_told = True

    def answered(self) -> None:
        if self.down_told:
            self.notify("CarConnectivity is back.", "connector")
            self.down_told = False
        self.answered_at = self.clock()

    def tick(self) -> bool:
        try:
            car = self.fetch_live()
        except Exception:
            self.lost()
            raise
        # The site reads history when the connector stops answering rather than failing, so a
        # fetch that worked is not a connector that answered: `connector_lost` says which.
        if self.connector_lost and self.connector_lost():
            self.lost()
        else:
            self.answered()
        # Only the connector's own car: a car read from history is the timer's (Watcher, keyed
        # by VIN). Diffing whichever car came back against the last one compared two cars.
        if self.watcher and car.get("live") is not False:
            for kind, text in self.watcher.step(car):
                self.notify(text, kind, car.get("vin"))
        self.last_car = car
        if self.on_snapshot:
            try:
                self.on_snapshot(car)
            except Exception as err:  # noqa: BLE001
                LOG.warning("snapshot hook failed: %s", err)
        # Only the connector's own readings. With the connector away, fetch_live falls back to
        # a car's stored history; written back as a fix stamped now, it outdated the TeslaFi feed's
        # newest row, and Now lost a drive under way (nowpage.feed_status) and showed the car asleep.
        if car.get("live") is False:
            return False
        now = {
            "vin": car["vin"],
            "latitude": car.get("lat"),
            "longitude": car.get("lon"),
            "heading": car.get("heading"),
            "level": car.get("soc"),
            "odometer": car.get("odometer_mi"),
            "state": car.get("status"),
            "speed": car.get("speed_mph"),
            "elevation_raw": car.get("elevation_raw"),
        }
        if not should_record(self.last, now):
            return False
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO cc_positions (vin, at, latitude, longitude, heading, level, odometer, state, speed, elevation_raw) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (now["vin"], datetime.now(timezone.utc), now["latitude"], now["longitude"], now["heading"], now["level"], now["odometer"], now["state"], now["speed"], now["elevation_raw"]),
            )
            conn.commit()
        self.last = now
        return True

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception as err:  # noqa: BLE001 - keep recording through connector or DB blips
                LOG.warning("recorder tick failed: %s", err)
            await asyncio.sleep(next_interval(self.last_car and self.last_car.get("status"), self.interval_s, self.driving_interval_s))

    def start(self) -> None:
        self.ensure_table()
        self.task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
