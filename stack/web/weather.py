"""The weather at the start of each drive, from Open-Meteo, filled in behind the pages.

The car itself gives the outside temperature; what it cannot say is whether it was
raining or how hard the wind blew, and both move the Wh/mi figure more than most people expect. Open-Meteo answers
for any place and hour, past or present, with no key.

None of this touches a page. A worker thread wakes every ten minutes, takes the newest
finished drives that have no weather yet, asks for each one's hour, and writes a row per
drive; the drive page and the Timeline read that row with the same join that already
fetches the tags. What leaves the house: a latitude and longitude rounded to two
decimals (about a kilometre) and a date — no VIN, no name, nothing about the car. The
Settings page has the switch, off on a new install, with that sentence beside it.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

LOG = logging.getLogger("carcommand.weather")

DDL = """
CREATE TABLE IF NOT EXISTS cc_weather (
    vin        VARCHAR NOT NULL,
    start_date TIMESTAMPTZ NOT NULL,
    temp_c     DOUBLE PRECISION,
    precip_mm  DOUBLE PRECISION,
    wind_kmh   DOUBLE PRECISION,
    code       INTEGER,
    fetched_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (vin, start_date)
)
"""

HOURLY = "temperature_2m,precipitation,wind_speed_10m,weather_code"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_LAG = timedelta(days=6)  # the archive runs about five days behind; the forecast API keeps a week of past hours
BACKFILL_DAYS = 90
BATCH = 20
INTERVAL_S = 600
PAUSE_S = 1.0  # between requests; the free tier is generous but not a firehose

# WMO weather interpretation codes, in the words a person would use.
WMO = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "showers",
    81: "showers",
    82: "heavy showers",
    85: "snow showers",
    86: "snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}
WINDY_KMH = 30.0  # about 19 mph: where wind starts to show in the Wh/mi


def describe(code: int | None, wind_kmh: float | None = None, metric: bool = False) -> str | None:
    """ "light rain, windy" — the sky in a word or two, and the wind when it mattered."""
    if code is None:
        return None
    words = WMO.get(int(code), "")
    if wind_kmh is not None and wind_kmh >= WINDY_KMH:
        speed = f"{wind_kmh:.0f} km/h" if metric else f"{wind_kmh / 1.609344:.0f} mph"
        words = f"{words}, {speed} wind" if words else f"{speed} wind"
    return words or None


def endpoint(when: datetime, now: datetime) -> str:
    """The archive for anything older than the forecast API's past week, else the forecast API."""
    return ARCHIVE if when < now - ARCHIVE_LAG else FORECAST


def params(lat: float, lon: float, when: datetime, now: datetime) -> dict:
    day = when.astimezone(timezone.utc).date().isoformat()
    p = {"latitude": round(lat, 2), "longitude": round(lon, 2), "hourly": HOURLY, "timezone": "UTC"}
    if endpoint(when, now) == ARCHIVE:
        p.update(start_date=day, end_date=day)
    else:
        p.update(past_days=7, forecast_days=1)
    return p


def pick(hourly: dict, when: datetime) -> dict | None:
    """The hourly row nearest to `when`, or None when the answer has no hours."""
    times = hourly.get("time") or []
    if not times:
        return None
    target = when.astimezone(timezone.utc).replace(tzinfo=None)
    best = min(range(len(times)), key=lambda i: abs(datetime.fromisoformat(times[i]) - target))
    if abs(datetime.fromisoformat(times[best]) - target) > timedelta(hours=2):
        return None

    def at(key):
        vals = hourly.get(key) or []
        return vals[best] if best < len(vals) else None

    return {"temp_c": at("temperature_2m"), "precip_mm": at("precipitation"), "wind_kmh": at("wind_speed_10m"), "code": at("weather_code")}


PENDING_SQL = """
    SELECT t.vin, t.start_date, t.start_position_latitude AS lat, t.start_position_longitude AS lon
    FROM trips t LEFT JOIN cc_weather w ON w.vin = t.vin AND w.start_date = t.start_date
    WHERE w.vin IS NULL AND t.destination_date IS NOT NULL AND t.destination_date < %s
      AND t.start_date > %s AND t.start_position_latitude IS NOT NULL AND t.start_position_longitude IS NOT NULL
    ORDER BY t.start_date DESC LIMIT %s
"""


class WeatherWorker(threading.Thread):
    """Fills cc_weather for finished drives, a batch every ten minutes, newest first."""

    def __init__(self, connect: Callable, fetch: Callable[[str, dict], dict], enabled: Callable[[], bool], interval_s: int = INTERVAL_S, batch: int = BATCH, pause_s: float = PAUSE_S):
        super().__init__(daemon=True, name="weather")
        self.connect, self.fetch, self.enabled = connect, fetch, enabled
        self.interval_s, self.batch, self.pause_s = interval_s, batch, pause_s
        self.stop = threading.Event()

    def pending(self, conn, now: datetime) -> list[dict]:
        rows = conn.execute(PENDING_SQL, (now - timedelta(hours=1), now - timedelta(days=BACKFILL_DAYS), self.batch)).fetchall()
        return [dict(r) if isinstance(r, dict) else dict(zip(("vin", "start_date", "lat", "lon"), r)) for r in rows]

    def fill_one(self, conn, row: dict, now: datetime) -> bool:
        when = row["start_date"]
        answer = self.fetch(endpoint(when, now), params(row["lat"], row["lon"], when, now))
        got = pick(answer.get("hourly") or {}, when)
        if got is None:
            return False
        conn.execute(
            "INSERT INTO cc_weather (vin, start_date, temp_c, precip_mm, wind_kmh, code, fetched_at) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (vin, start_date) DO NOTHING",
            (row["vin"], when, got["temp_c"], got["precip_mm"], got["wind_kmh"], got["code"], now),
        )
        return True

    def round(self, now: datetime | None = None) -> int:
        """One pass: how many drives got their weather."""
        if not self.enabled():
            return 0
        now = now or datetime.now(timezone.utc)
        done = 0
        with self.connect() as conn:
            conn.execute(DDL)
            conn.commit()
            for i, row in enumerate(self.pending(conn, now)):
                if i:
                    time.sleep(self.pause_s)
                try:
                    if self.fill_one(conn, row, now):
                        done += 1
                        conn.commit()
                except Exception as err:  # noqa: BLE001 - one bad answer must not stop the batch
                    LOG.warning("weather for %s: %s", row["start_date"], err)
                    conn.rollback()
                    break  # the service is unhappy; the rest can wait ten minutes
        return done

    def run(self) -> None:
        while not self.stop.wait(15):  # a short first wait so a fresh install fills quickly
            try:
                n = self.round()
                if n:
                    LOG.info("weather: %d drives filled", n)
            except Exception as err:  # noqa: BLE001
                LOG.warning("weather round failed: %s", err)
            if self.stop.wait(self.interval_s):
                return


def http_fetch(url: str, query: dict) -> dict:
    import httpx

    r = httpx.get(url, params=query, timeout=10.0)
    r.raise_for_status()
    return r.json()
