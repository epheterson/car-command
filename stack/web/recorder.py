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

from notify import connector_down, event_kinds, send_discord, send_email, send_pushover, send_telegram

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
CREATE INDEX IF NOT EXISTS cc_positions_vin_at ON cc_positions (vin, at);
"""


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
    def __init__(self, fetch_live, connect, interval_s: int = 60, driving_interval_s: int = 20, webhook_url: str | None = None) -> None:
        self.fetch_live = fetch_live
        self.connect = connect
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
        self.muted: set[str] = set()  # alert kinds switched off on Settings
        self.routes: dict[str, set[str]] = {}  # kind -> channels, from Settings; a kind not listed goes to every channel
        self.last: dict | None = None
        self.last_car: dict | None = None
        self.failures = 0
        self.on_snapshot = None  # optional callback(car) on every successful tick
        self.task: asyncio.Task | None = None

    def ensure_table(self) -> None:
        with self.connect() as conn:
            conn.execute(DDL)
            conn.commit()

    def channels(self) -> list[str]:
        """The channels that are set up, in the order Settings shows them."""
        out = []
        if self.webhook_url:
            out.append("discord")
        if all(self.telegram):
            out.append("telegram")
        if self.email[0] and self.email[1]:
            out.append("email")
        if all(self.pushover):
            out.append("pushover")
        return out

    def notify(self, text: str, kind: str = "other") -> None:
        to = self.routes.get(kind)
        if kind in self.muted or to == set():
            LOG.info("event (muted %s): %s", kind, text)
            return
        LOG.info("event: %s", text)
        if to is None or "discord" in to:
            send_discord(self.webhook_url, text)
        if to is None or "telegram" in to:
            send_telegram(self.telegram[0], self.telegram[1], text)
        if to is None or "email" in to:
            send_email(self.email[0], self.email[1], self.email[2], text.split("\n", 1)[0][:120], text)
        if to is None or "pushover" in to:
            send_pushover(self.pushover[0], self.pushover[1], text)

    def tick(self) -> bool:
        try:
            car = self.fetch_live()
        except Exception:
            self.failures += 1
            if connector_down(self.failures):
                self.notify("Car Command has not heard from CarConnectivity for 10 minutes.", "connector")
            raise
        if self.failures >= 10:
            self.notify("CarConnectivity is back.", "connector")
        self.failures = 0
        for kind, text in event_kinds(self.last_car, car):
            self.notify(text, kind)
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
        }
        if not should_record(self.last, now):
            return False
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO cc_positions (vin, at, latitude, longitude, heading, level, odometer, state, speed) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (now["vin"], datetime.now(timezone.utc), now["latitude"], now["longitude"], now["heading"], now["level"], now["odometer"], now["state"], now["speed"]),
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
