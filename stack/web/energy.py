"""The house's energy beside the cars: cc_energy rows, and for every home charge how much of it
came from the sun, the Powerwall and the grid.

Brand-neutral on purpose. A row is (site, at, solar_w, home_w, grid_w, battery_w, soe_pct,
source) with the signs of a Powerwall: grid and battery positive when power comes *from* them
(import, discharge), solar and home never negative. Where the readings come from is the
source's business (energy_cloud.py); everything here reads rows. The source column is there for
another source to come: today every row is Tesla's cloud, one sample per five minutes, kept as it
came and held for the five minutes from its time (`minute_samples`). Five minutes is the
resolution the whole split is worked at, and the only one there is.

The split of a charge is worked out once, after the charge ends, and stored in
cc_charge_sources beside the charge facts; pages read a join and never integrate anything.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import cost

DDL = """
CREATE TABLE IF NOT EXISTS cc_energy (
    site       VARCHAR NOT NULL,
    at         TIMESTAMPTZ NOT NULL,
    solar_w    REAL,
    home_w     REAL,
    grid_w     REAL,
    battery_w  REAL,
    soe_pct    REAL,
    source     VARCHAR NOT NULL DEFAULT 'cloud',
    PRIMARY KEY (site, at)
);
-- When the row was written or last replaced: a charge split earlier from a gap in the readings is
-- looked at again when something newer lands in its window (a backfill reaching it).
ALTER TABLE cc_energy ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
-- Where a home charge's energy came from. status: 'ok' (split worked out) or 'none' (looked, no
-- house readings for it; looked at again after a day). The kWh are the charge's own, split three
-- ways; battery_sun_frac is the Powerwall's solar share over the week before; the costs are in
-- the install's currency, NULL with no home plan. approx: a gap in the readings, or no
-- battery history to read the Powerwall's mix from, behind it.
CREATE TABLE IF NOT EXISTS cc_charge_sources (
    session_id        INTEGER PRIMARY KEY,
    status            VARCHAR NOT NULL,
    solar_kwh         DOUBLE PRECISION,
    battery_kwh       DOUBLE PRECISION,
    grid_kwh          DOUBLE PRECISION,
    battery_sun_frac  DOUBLE PRECISION,
    grid_cost         DOUBLE PRECISION,
    battery_cost      DOUBLE PRECISION,
    approx            BOOLEAN NOT NULL DEFAULT FALSE,
    coverage          DOUBLE PRECISION,
    checked_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

SITE = "home"
CONFIG_KEY = "energy_config"  # cc_state: JSON, nothing secret in it
LEGACY_PASSWORD_KEY = "energy_gateway_password"  # a local gateway was once read; its sealed password is deleted at startup
CLOUD_S = 300  # seconds one row stands for
LIVE_MAX_AGE_S = 900  # a live reading is five minutes apart, so Now shows one for fifteen
WINDOW_DAYS = 7  # how far back the Powerwall's own mix is read
MIN_COVERAGE = 0.5  # less of a charge covered by readings than this: no split
SETTLE = timedelta(minutes=5)  # a charge is split this long after it ends, once the last minutes are in
RETRY = timedelta(days=1)  # a charge with no readings is looked at again after this
FIELDS = ("solar_w", "home_w", "grid_w", "battery_w", "soe_pct")


# ---- config: where the house is read from


def default_config() -> dict:
    return {"cloud": {"enabled": False, "site_id": "", "name": "", "installed": ""}}


def load_config(get) -> dict:
    """The stored config over its defaults; a damaged value reads as none stored."""
    out = default_config()
    try:
        stored = json.loads(get(CONFIG_KEY) or "{}")
    except ValueError:
        stored = {}
    for part in out:
        if isinstance(stored.get(part), dict):
            out[part].update({k: v for k, v in stored[part].items() if k in out[part]})
    return out


def save_config(put, cfg: dict) -> None:
    put(CONFIG_KEY, json.dumps(cfg))


def drop_gateway(get, put, delete, conn=None) -> bool:
    """A local gateway was once read here and no longer is. What it left in cc_state goes at
    startup: its sealed password, and the `local` part of the config (address, pins). Cheap and
    safe to repeat; True when anything was there. With a connection, the rows a local gateway wrote
    (source 'local', one a minute) go too: they would be read as five minutes wide."""
    found = False
    if conn is not None:
        conn.execute("DELETE FROM cc_energy WHERE source = 'local'")
    if get(LEGACY_PASSWORD_KEY) is not None:
        delete(LEGACY_PASSWORD_KEY)
        found = True
    try:
        stored = json.loads(get(CONFIG_KEY) or "{}")
    except ValueError:
        stored = {}
    if isinstance(stored, dict) and "local" in stored:
        stored.pop("local")
        put(CONFIG_KEY, json.dumps(stored))
        found = True
    return found


def home_from(solar, battery, grid) -> float | None:
    """What the house drew when only the sources are known: they add up to it."""
    if solar is None or battery is None or grid is None:
        return None
    return max(0.0, solar + battery + grid)


class Live:
    """The newest reading, in memory: what Now and /api/energy read. Never a query."""

    def __init__(self):
        self.reading: dict | None = None
        self.source: str | None = None
        self.at: datetime | None = None

    def set(self, reading: dict, source: str, at: datetime) -> None:
        self.reading, self.source, self.at = dict(reading), source, at

    def get(self, now: datetime, max_age_s: float = LIVE_MAX_AGE_S) -> dict | None:
        """The reading, with its source and age, or None when there is none or it is old."""
        if self.reading is None or self.at is None:
            return None
        age = (now - self.at).total_seconds()
        if age > max_age_s:
            return None
        return {**self.reading, "source": self.source, "age_s": max(0, int(age))}


# ---- storing and reading rows


def store(conn, rows: list[dict], source: str = "cloud", site: str = SITE) -> int:
    """Rows kept by (site, at); a later row for the same time replaces the earlier."""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO cc_energy (site, at, solar_w, home_w, grid_w, battery_w, soe_pct, source) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (site, at) DO UPDATE SET solar_w = EXCLUDED.solar_w, home_w = EXCLUDED.home_w, grid_w = EXCLUDED.grid_w, "
            "battery_w = EXCLUDED.battery_w, soe_pct = EXCLUDED.soe_pct, source = EXCLUDED.source, updated_at = now()",
            [(site, r["at"], r.get("solar_w"), r.get("home_w"), r.get("grid_w"), r.get("battery_w"), r.get("soe_pct"), source) for r in rows],
        )
    return len(rows)


# Rows covering [lo, hi), the one before it included (it stands for five minutes).
SERIES_SQL = """
SELECT e.at, e.solar_w, e.home_w, e.grid_w, e.battery_w, e.soe_pct, e.source
FROM cc_energy e
WHERE e.site = %s AND e.at >= %s AND e.at < %s
ORDER BY e.at"""

# The hours one row stands for, in SQL: five minutes.
ROW_HOURS = "5.0 / 60"


def series(conn, lo: datetime, hi: datetime, site: str = SITE) -> list[dict]:
    return conn.execute(SERIES_SQL, (site, lo - timedelta(seconds=CLOUD_S), hi)).fetchall()


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def minute_samples(rows: list[dict]) -> dict[datetime, dict]:
    """minute -> the reading that stands for it: a row holds the five minutes from its time, and
    where two overlap the later one (nearer in time) is the one read."""
    out: dict[datetime, dict] = {}
    for r in sorted(rows, key=lambda r: r["at"]):
        for i in range(CLOUD_S // 60):
            out[_minute(r["at"]) + timedelta(minutes=i)] = r
    return out


# ---- one charge, split


def car_mix(s: dict, car_w: float) -> tuple[float, float, float] | None:
    """(sun, battery, grid) shares of what the car drew in one minute, judged by what plugging it
    in drew: the house's own load goes first (it was there before the car), solar serves that
    first, and the car gets whatever solar is left over, which would otherwise have gone to the
    Powerwall or the grid. What the car draws beyond that comes from the battery and the grid in
    the proportion they were giving (the house's own share of them is taken at the same
    proportion, so it does not change the split). None when the house or the car drew nothing
    readable.

    The car's draw is a minute; the house's reading is five minutes' average, so a car can look
    larger than the whole house. It is held to what the house drew: it cannot have had more, and
    sun that was exported is not the house's."""
    home = s.get("home_w")
    if home is None or home <= 0 or car_w <= 0:
        return None
    car = min(car_w, home)
    base = home - car
    sun = min(car, max(max(s.get("solar_w") or 0.0, 0.0) - base, 0.0))
    rest = car - sun
    battery, grid = max(s.get("battery_w") or 0.0, 0.0), max(s.get("grid_w") or 0.0, 0.0)
    if rest <= 0:
        return 1.0, 0.0, 0.0
    if battery + grid <= 0:
        return sun / car, 0.0, rest / car  # nothing reported giving it: the grid, as the safe reading
    return sun / car, rest * battery / (battery + grid) / car, rest * grid / (battery + grid) / car


def car_minutes(powers: list[tuple[datetime, datetime, float]], start: datetime, end: datetime) -> dict[datetime, float]:
    """minute -> kWh the car drew in it, from power intervals (first, last, kW)."""
    out: dict[datetime, float] = {}
    for a, b, kw in powers:
        if kw is None or kw <= 0:
            continue
        t, stop = max(a, start), min(b, end)
        while t < stop:
            m = _minute(t)
            seg = min(stop, m + timedelta(minutes=1))
            out[m] = out.get(m, 0.0) + kw * (seg - t).total_seconds() / 3600
            t = seg
    return out


def split_charge(powers, start: datetime, end: datetime, rows: list[dict]) -> dict:
    """Where one charge's energy came from, minute by minute against the house's readings:
    {solar, battery, grid} kWh over the minutes that have readings, `total` kWh of the whole
    charge, `covered` the share of it that had readings, and `grid_by_minute` for pricing."""
    draw, samples = car_minutes(powers, start, end), minute_samples(rows)  # kWh the car drew in each minute
    total = sum(draw.values())
    got = {"solar": 0.0, "battery": 0.0, "grid": 0.0}
    covered, grid_by_minute = 0.0, []
    for m, kwh in draw.items():
        sample = samples.get(m)
        mix = car_mix(sample, kwh * 60_000) if sample else None
        if mix is None:
            continue
        covered += kwh
        got["solar"] += kwh * mix[0]
        got["battery"] += kwh * mix[1]
        got["grid"] += kwh * mix[2]
        grid_by_minute.append((m, kwh * mix[2]))
    return {**got, "total": total, "covered": (covered / total) if total > 0 else 0.0, "grid_by_minute": grid_by_minute}


# ---- a change in how a split is worked out redoes every stored one, once

MODEL_KEY = "charge_sources_model"  # cc_state
MODEL = 2  # 1: the car got the whole house's mix. 2: the car got what plugging it in drew (car_mix)


def reset_sources(get, put, conn) -> bool:
    """At startup: if the stored splits were made by an older model, delete them so the recorder
    pass works every one out again, and note the model. Runs its delete once per model; True when it did."""
    if get(MODEL_KEY) == str(MODEL):
        return False
    conn.execute("DELETE FROM cc_charge_sources")
    conn.commit()  # the note is kept only once the delete has happened
    put(MODEL_KEY, str(MODEL))
    return True


# ---- the Powerwall's own mix, and prices

BATTERY_HOURS_SQL = f"""
SELECT date_trunc('hour', timezone(%s, e.at)) AS hour,
       SUM(LEAST(GREATEST(-COALESCE(e.battery_w, 0), 0), GREATEST(COALESCE(e.solar_w, 0) - COALESCE(e.home_w, 0), 0)) * {ROW_HOURS}) / 1000 AS from_sun_kwh,
       SUM(GREATEST(-COALESCE(e.battery_w, 0), 0) * {ROW_HOURS}) / 1000 AS in_kwh
FROM cc_energy e
WHERE e.site = %s AND e.at >= %s AND e.at < %s
GROUP BY 1 ORDER BY 1"""


def rate_at(local: datetime) -> float | None:
    """The home plan's price at a local time, or None with no plan covering it."""
    version = cost.version_at(local)
    if version is None:
        return None
    return float(version["periods"][cost.period_index(version, local)]["price"])


def battery_mix(hours: list[dict]) -> dict:
    """What went into the Powerwall over a window, from its hourly sums: the solar share
    (None when nothing went in) and the mean price the grid's part was bought at (None with
    no plan, or no grid part)."""
    total = sum(float(h["in_kwh"] or 0) for h in hours)
    sun = sum(min(float(h["from_sun_kwh"] or 0), float(h["in_kwh"] or 0)) for h in hours)
    if total <= 0:
        return {"sun_frac": None, "grid_rate": None, "in_kwh": 0.0}
    bought, priced = 0.0, 0.0
    for h in hours:
        grid_part = max(float(h["in_kwh"] or 0) - float(h["from_sun_kwh"] or 0), 0.0)
        rate = rate_at(h["hour"])
        if grid_part > 0 and rate is not None:
            bought += grid_part
            priced += grid_part * rate
    return {"sun_frac": sun / total, "grid_rate": (priced / bought) if bought > 0 else None, "in_kwh": total}


def price_split(split: dict, mix: dict, tz) -> dict:
    """Costs for a split: sunshine free, the grid's part at the plan's price for each minute
    it was drawn, and the Powerwall's grid-charged part at the price its grid energy went in at.
    None for a cost with no plan behind it."""
    grid_cost = 0.0
    for m, kwh in split["grid_by_minute"]:
        rate = rate_at(m.astimezone(tz))
        if rate is None:
            grid_cost = None
            break
        grid_cost += kwh * rate
    frac = mix["sun_frac"] if mix["sun_frac"] is not None else 0.0
    from_grid = split["battery"] * (1.0 - frac)
    if from_grid <= 1e-9:
        battery_cost = 0.0
    else:
        battery_cost = from_grid * mix["grid_rate"] if mix["grid_rate"] is not None else None
    return {"grid_cost": grid_cost, "battery_cost": battery_cost, "sun_frac": mix["sun_frac"]}


def provenance(split: dict, mix: dict, tz, kwh: float | None) -> dict:
    """The row for cc_charge_sources, or the 'none' row when too little of the charge had
    readings. The split is scaled to the charge's own kWh (the car's meter), so the three
    parts add up to the figure on the page."""
    if split["total"] <= 0 or split["covered"] < MIN_COVERAGE:
        return {"status": "none", "coverage": split["covered"]}
    known = split["solar"] + split["battery"] + split["grid"]
    scale = (kwh / known) if kwh and kwh > 0 and known > 0 else 1.0
    priced = price_split(split, mix, tz)
    return {
        "status": "ok",
        "solar_kwh": split["solar"] * scale,
        "battery_kwh": split["battery"] * scale,
        "grid_kwh": split["grid"] * scale,
        "battery_sun_frac": mix["sun_frac"],
        "grid_cost": None if priced["grid_cost"] is None else priced["grid_cost"] * scale,
        "battery_cost": None if priced["battery_cost"] is None else priced["battery_cost"] * scale,
        "approx": bool(split["covered"] < 0.95 or mix["sun_frac"] is None),  # five-minute readings are the resolution there is, not a reason to doubt it
        "coverage": split["covered"],
    }


# ---- stored beside the charge facts

BATCH = 50  # charges a pass; each reads its own minutes and a week of the battery's hours

CANDIDATES_SQL = """
SELECT c.id, c.session_start_date AS start, c.session_end_date AS end, f.kwh
FROM charging_sessions c
JOIN cc_charge_facts f ON f.session_id = c.id AND f.at_home AND COALESCE(f.kwh, 0) > 0
LEFT JOIN cc_charge_sources s ON s.session_id = c.id
WHERE c.vin = %s AND c.session_start_date IS NOT NULL AND c.session_end_date IS NOT NULL
  AND c.session_end_date < now() - interval '5 minutes'
  AND c.session_end_date >= (SELECT MIN(e0.at) FROM cc_energy e0 WHERE e0.site = %s)
  AND (s.session_id IS NULL
       OR (s.status = 'none' AND (s.checked_at < now() - interval '1 day'
           OR EXISTS (SELECT 1 FROM cc_energy e1 WHERE e1.site = %s AND e1.at >= c.session_start_date - interval '5 minutes' AND e1.at < c.session_end_date AND e1.updated_at > s.checked_at)))
       -- a charge whose kWh grew after it was split (a streamed session's meter catching up):
       -- an ok split is scaled to the kWh, so its parts stop adding up to it
       OR (s.status = 'ok' AND COALESCE(s.solar_kwh, 0) + COALESCE(s.battery_kwh, 0) + COALESCE(s.grid_kwh, 0) > 0
           AND ABS(COALESCE(s.solar_kwh, 0) + COALESCE(s.battery_kwh, 0) + COALESCE(s.grid_kwh, 0) - f.kwh) > 0.05)
       -- a split made across a gap, once readings have landed in its window since
       OR (s.status = 'ok' AND s.approx
           AND EXISTS (SELECT 1 FROM cc_energy e1 WHERE e1.site = %s AND e1.at >= c.session_start_date - interval '5 minutes' AND e1.at < c.session_end_date AND e1.updated_at > s.checked_at)))
  -- Readings exist for it. A charge from before any were kept gets no row at all, so it is
  -- never visited, and is picked up the day a backfill reaches it.
  AND EXISTS (SELECT 1 FROM cc_energy e WHERE e.site = %s AND e.at >= c.session_start_date - interval '5 minutes' AND e.at < c.session_end_date)
ORDER BY c.session_start_date DESC LIMIT %s"""

UPSERT_SQL = """
INSERT INTO cc_charge_sources (session_id, status, solar_kwh, battery_kwh, grid_kwh, battery_sun_frac, grid_cost, battery_cost, approx, coverage, checked_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, FALSE), %s, now())
ON CONFLICT (session_id) DO UPDATE SET status = EXCLUDED.status, solar_kwh = EXCLUDED.solar_kwh, battery_kwh = EXCLUDED.battery_kwh,
    grid_kwh = EXCLUDED.grid_kwh, battery_sun_frac = EXCLUDED.battery_sun_frac, grid_cost = EXCLUDED.grid_cost,
    battery_cost = EXCLUDED.battery_cost, approx = EXCLUDED.approx, coverage = EXCLUDED.coverage, checked_at = now()"""


def drop_orphan_sources(conn) -> None:
    conn.execute("DELETE FROM cc_charge_sources WHERE session_id NOT IN (SELECT id FROM charging_sessions)")


def fill_charge_sources(conn, vin: str, tz, batch: int = BATCH, site: str = SITE) -> int:
    """The split of finished home charges that have none: sun, Powerwall and grid kWh with
    their costs, newest first, a batch at a time. Run after a charge's facts exist and its
    last minutes of readings are in; cheap when nothing is waiting (one query)."""
    from samples import with_measured_power

    sessions = conn.execute(CANDIDATES_SQL, (vin, site, site, site, site, batch)).fetchall()
    if not sessions:
        return 0
    out = []
    for s in sessions:
        lo, hi = s["start"], s["end"]
        powers = [
            (r["first_date"], r["last_date"], r["power"])
            for r in conn.execute("SELECT first_date, last_date, power FROM charging_powers WHERE vin = %s AND last_date >= %s AND first_date <= %s ORDER BY first_date", (vin, lo, hi)).fetchall()
        ]
        el = conn.execute("SELECT first_date, last_date, volts, amps FROM cc_charging_electrics WHERE vin = %s AND last_date >= %s AND first_date <= %s ORDER BY first_date", (vin, lo, hi)).fetchall()
        powers = with_measured_power(powers, [(r["first_date"], r["last_date"], r["volts"], r["amps"]) for r in el])
        split = split_charge(powers, lo, hi, series(conn, lo, hi, site))
        mix = (
            battery_mix(conn.execute(BATTERY_HOURS_SQL, (tz.key, site, lo - timedelta(days=WINDOW_DAYS), lo)).fetchall())
            if split["covered"] >= MIN_COVERAGE
            else {"sun_frac": None, "grid_rate": None, "in_kwh": 0.0}
        )
        p = provenance(split, mix, tz, s["kwh"])
        out.append(
            (
                s["id"],
                p["status"],
                p.get("solar_kwh"),
                p.get("battery_kwh"),
                p.get("grid_kwh"),
                p.get("battery_sun_frac"),
                p.get("grid_cost"),
                p.get("battery_cost"),
                bool(p.get("approx")),
                p["coverage"],
            )
        )
    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, out)
    return len(out)


# ---- the day so far

KWH_KEYS = ("solar_kwh", "home_kwh", "grid_in_kwh", "grid_out_kwh", "battery_in_kwh", "battery_out_kwh")
KWH_SUMS = """SUM(GREATEST(COALESCE(solar_w, 0), 0) * h) / 1000 AS solar_kwh,
       SUM(COALESCE(home_w, 0) * h) / 1000 AS home_kwh,
       SUM(GREATEST(COALESCE(grid_w, 0), 0) * h) / 1000 AS grid_in_kwh,
       SUM(GREATEST(-COALESCE(grid_w, 0), 0) * h) / 1000 AS grid_out_kwh,
       SUM(GREATEST(-COALESCE(battery_w, 0), 0) * h) / 1000 AS battery_in_kwh,
       SUM(GREATEST(COALESCE(battery_w, 0), 0) * h) / 1000 AS battery_out_kwh,
       COUNT(*) AS rows"""
ROWS_IN = f"(SELECT e.*, {ROW_HOURS} AS h FROM cc_energy e WHERE e.site = %s AND e.at >= %s AND e.at < %s) q"
TOTALS_SQL = f"SELECT {KWH_SUMS}\nFROM {ROWS_IN}"
# By UTC hour: every hour of a day the clocks change is its own, and a whole hour is the same
# hour in any zone with whole-hour offsets. The reader puts it on its own clock.
HOURS_SQL = f"SELECT date_trunc('hour', at AT TIME ZONE 'UTC') AS hour_utc, {KWH_SUMS}\nFROM {ROWS_IN}\nGROUP BY 1 ORDER BY 1"
HOUR_ROWS = 3600 // CLOUD_S  # rows in a whole hour


def totals(conn, lo: datetime, hi: datetime, site: str = SITE) -> dict | None:
    """kWh in [lo, hi) from the stored rows (five minutes a row), or
    None when there are none. What /api/energy calls today's totals."""
    row = conn.execute(TOTALS_SQL, (site, lo, hi)).fetchone()
    if not row or not row["rows"]:
        return None
    return {k: round(float(row[k] or 0), 3) for k in KWH_KEYS}


def hours(conn, lo: datetime, hi: datetime, site: str = SITE) -> list[dict]:
    """kWh per clock hour in [lo, hi), oldest first: the hour's UTC start, its kWh, and how
    much of it the readings cover (1.0 is all twelve rows). An hour with no rows is left out,
    so a gap reads as a gap and not as a house that used nothing. What /api/energy/hours
    answers, for a reader that prices the house by the hour (the energy dashboard's bill)."""
    out = []
    for r in conn.execute(HOURS_SQL, (site, lo, hi)).fetchall():
        hour = r["hour_utc"].replace(tzinfo=timezone.utc)
        # + 0.0: a sum of negated zeros is -0.0, which JSON would print as such
        out.append({"hour": hour, **{k: round(float(r[k] or 0), 3) + 0.0 for k in KWH_KEYS}, "coverage": round(min(r["rows"] / HOUR_ROWS, 1.0), 3)})
    return out
