"""Per-drive and per-charge facts, computed once and stored beside the sessions.

The database plugin's trips table has no battery levels, and charging_sessions
has no integrated energy (the connector never fills meter_*_kwh). Both are
derivable from the interval tables, but deriving them on every page view meant
two index lookups per drive and one query per home charge. With 7,000 drives
and eight years of history that made Statistics a 35-second page. So they are
derived once, here, and every page reads a join.

Filled by the TeslaFi loader after a load and by the recorder in the
background. Both calls are cheap when nothing is missing.
"""

from __future__ import annotations

import json
import logging
import math
from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import datetime

import billed
import driveedits
import placeprice
from cost import session_cost
from places import distance_m, place_for
from regions import region_of
from samples import with_measured_power

LOG = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS cc_trip_facts (
    trip_id     INTEGER PRIMARY KEY,
    level_start DOUBLE PRECISION,
    level_end   DOUBLE PRECISION
);
ALTER TABLE cc_trip_facts ADD COLUMN IF NOT EXISTS temp_f DOUBLE PRECISION;
ALTER TABLE cc_trip_facts ADD COLUMN IF NOT EXISTS range_start DOUBLE PRECISION;
ALTER TABLE cc_trip_facts ADD COLUMN IF NOT EXISTS range_end DOUBLE PRECISION;
ALTER TABLE cc_trip_facts ADD COLUMN IF NOT EXISTS max_mph DOUBLE PRECISION;
-- The highest and lowest ground the drive reached, metres, spikes left out (backfill).
ALTER TABLE cc_trip_facts ADD COLUMN IF NOT EXISTS elev_max_m DOUBLE PRECISION;
ALTER TABLE cc_trip_facts ADD COLUMN IF NOT EXISTS elev_min_m DOUBLE PRECISION;
-- The same drive measured against the downloaded terrain model rather than the car's GPS
-- height (terrain.py): climb and descent turn to turn, and the highest and lowest ground,
-- metres. -1 in the climb: looked, the model does not cover the drive (yet).
ALTER TABLE cc_trip_facts ADD COLUMN IF NOT EXISTS ground_climb_m DOUBLE PRECISION;
ALTER TABLE cc_trip_facts ADD COLUMN IF NOT EXISTS ground_descent_m DOUBLE PRECISION;
ALTER TABLE cc_trip_facts ADD COLUMN IF NOT EXISTS ground_hi_m DOUBLE PRECISION;
ALTER TABLE cc_trip_facts ADD COLUMN IF NOT EXISTS ground_lo_m DOUBLE PRECISION;
CREATE TABLE IF NOT EXISTS cc_vehicle_facts (
    vin              VARCHAR PRIMARY KEY,
    kwh_per_rated_mi DOUBLE PRECISION,
    charges_used     INTEGER,
    updated_at       TIMESTAMPTZ
);
ALTER TABLE cc_vehicle_facts ADD COLUMN IF NOT EXISTS home_rate DOUBLE PRECISION;
-- Whether any home charge behind home_rate was priced on a regional average, so a drive
-- cost worked out from it is shown as an estimate.
ALTER TABLE cc_vehicle_facts ADD COLUMN IF NOT EXISTS home_rate_estimate BOOLEAN;
-- When the per-year figures below were last worked out. Their own stamp, not the
-- all-time one: see fill_facts for what sharing it cost.
ALTER TABLE cc_vehicle_facts ADD COLUMN IF NOT EXISTS years_updated_at TIMESTAMPTZ;
-- What an hour parked costs this car, measured three ways: with nothing running, with
-- Sentry watching, and with the climate on. Rated miles an hour, and the number of
-- parked stretches behind each, because a figure from nine of them is not a figure.
ALTER TABLE cc_vehicle_facts ADD COLUMN IF NOT EXISTS idle_mi_per_h DOUBLE PRECISION;
ALTER TABLE cc_vehicle_facts ADD COLUMN IF NOT EXISTS idle_samples INTEGER;
ALTER TABLE cc_vehicle_facts ADD COLUMN IF NOT EXISTS sentry_mi_per_h DOUBLE PRECISION;
ALTER TABLE cc_vehicle_facts ADD COLUMN IF NOT EXISTS sentry_samples INTEGER;
ALTER TABLE cc_vehicle_facts ADD COLUMN IF NOT EXISTS climate_mi_per_h DOUBLE PRECISION;
ALTER TABLE cc_vehicle_facts ADD COLUMN IF NOT EXISTS climate_samples INTEGER;
ALTER TABLE cc_vehicle_facts ADD COLUMN IF NOT EXISTS parked_updated_at TIMESTAMPTZ;
-- The same measurement again, per year, so a drive is priced at what a rated mile cost
-- the car at the time rather than what it costs now. cc_vehicle_facts keeps the all-time
-- figure, which is still the answer for a car or a year without enough charges of its own.
CREATE TABLE IF NOT EXISTS cc_vehicle_year_facts (
    vin              VARCHAR NOT NULL,
    year             INTEGER NOT NULL,
    kwh_per_rated_mi DOUBLE PRECISION,
    charges_used     INTEGER,
    PRIMARY KEY (vin, year)
);
CREATE TABLE IF NOT EXISTS cc_charge_facts (
    session_id  INTEGER PRIMARY KEY,
    kwh         DOUBLE PRECISION,
    cost        DOUBLE PRECISION,
    plan        VARCHAR,
    at_home     BOOLEAN NOT NULL DEFAULT FALSE
);
ALTER TABLE cc_charge_facts ADD COLUMN IF NOT EXISTS range_added DOUBLE PRECISION;
-- The time-of-use split. session_cost() has always worked these out to price a charge
-- and then discarded them, so the one question a time-of-use customer actually has —
-- how much of my charging lands off-peak — could not be answered on any page.
ALTER TABLE cc_charge_facts ADD COLUMN IF NOT EXISTS off_peak_kwh DOUBLE PRECISION;
ALTER TABLE cc_charge_facts ADD COLUMN IF NOT EXISTS part_peak_kwh DOUBLE PRECISION;
ALTER TABLE cc_charge_facts ADD COLUMN IF NOT EXISTS peak_kwh DOUBLE PRECISION;
-- The three columns above are the split by role (cheapest period, dearest, the rest),
-- which is all a plan with more periods can say in three columns. This is the exact
-- split, period key -> kWh, for the plan version that priced the charge.
ALTER TABLE cc_charge_facts ADD COLUMN IF NOT EXISTS period_kwh JSONB;
-- What the cost came from: 'plan' (the home plan), 'dc' (the fast-charging rate),
-- 'hand' (typed in with the charge), 'average' (the regional average, an estimate).
-- 'none' with a NULL cost: looked at, nothing priced it; NULL: not looked at yet.
ALTER TABLE cc_charge_facts ADD COLUMN IF NOT EXISTS cost_source VARCHAR;
-- The price region the charge's own position is in ('US-IL', 'DE'; '' for none), and
-- for an average-priced charge the month whose figure priced it.
ALTER TABLE cc_charge_facts ADD COLUMN IF NOT EXISTS region VARCHAR;
ALTER TABLE cc_charge_facts ADD COLUMN IF NOT EXISTS rate_month VARCHAR;
-- The charge's highest power, and the pack's range at a full charge as the charge left it (the car's
-- own estimate, else rated range over the level), with that level and the odometer: one
-- point each of the battery's lifetime report, so the report never reads the raw runs.
ALTER TABLE cc_charge_facts ADD COLUMN IF NOT EXISTS peak_kw DOUBLE PRECISION;
ALTER TABLE cc_charge_facts ADD COLUMN IF NOT EXISTS full_range DOUBLE PRECISION;
ALTER TABLE cc_charge_facts ADD COLUMN IF NOT EXISTS full_level DOUBLE PRECISION;
ALTER TABLE cc_charge_facts ADD COLUMN IF NOT EXISTS odometer DOUBLE PRECISION;
-- When the energy and cost were last worked out: a figure taken before the session ended is part of it.
ALTER TABLE cc_charge_facts ADD COLUMN IF NOT EXISTS computed_at TIMESTAMPTZ;
-- What a loader knew about a charge that the sessions table has no column for, keyed
-- like cc_trip_extras so a re-import that renumbers sessions keeps it: the fast-charging
-- network ('supercharger'), where the source said.
CREATE TABLE IF NOT EXISTS cc_charge_extras (
    vin        VARCHAR NOT NULL,
    start_date TIMESTAMPTZ NOT NULL,
    network    VARCHAR,
    PRIMARY KEY (vin, start_date)
);
-- The car's own meter for a live charge (the Lucid connector's charge_session_energy, from
-- 0.4.0): the last reading it gave while charging, since it reads nothing once charging stops.
-- A charge's facts take the larger of it and the power estimate (an export's real_charged outright).
CREATE TABLE IF NOT EXISTS cc_charge_meter (
    session_id INTEGER PRIMARY KEY,
    kwh        DOUBLE PRECISION NOT NULL,
    at         TIMESTAMPTZ NOT NULL
);
-- A charge paused and resumed is one session to the plugin, and the car's meter may restart
-- from zero on resume: `last` is the meter's latest reading, `carried` what it had counted
-- before each restart, and kwh their sum, the charge's whole energy.
ALTER TABLE cc_charge_meter ADD COLUMN IF NOT EXISTS last DOUBLE PRECISION;
ALTER TABLE cc_charge_meter ADD COLUMN IF NOT EXISTS carried DOUBLE PRECISION NOT NULL DEFAULT 0;
"""


def fill_trip_facts(conn, vin: str) -> int:
    """Battery level at the start and end of every finished trip that has no facts yet. One statement."""
    cur = conn.execute(
        """
        INSERT INTO cc_trip_facts (trip_id, level_start, level_end, temp_f, range_start, range_end)
        SELECT t.id, ls.level, le.level, tp.outside_temperature, rs.range, re.range
        FROM trips t
        -- Resolved here rather than inside each lateral: see RATED_MILE_SOURCE for what
        -- the planner does when the drive id is only known inside one.
        LEFT JOIN drives dv ON dv.vin = t.vin
        LEFT JOIN cc_trip_facts f ON f.trip_id = t.id
        LEFT JOIN LATERAL (SELECT r.range FROM drive_ranges r
                           WHERE r.drive_id = dv.id AND r.first_date <= t.start_date ORDER BY r.first_date DESC LIMIT 1) rs ON TRUE
        LEFT JOIN LATERAL (SELECT r.range FROM drive_ranges r
                           WHERE r.drive_id = dv.id AND r.first_date <= t.destination_date ORDER BY r.first_date DESC LIMIT 1) re ON TRUE
        LEFT JOIN LATERAL (SELECT l.level FROM drive_levels l
                           WHERE l.drive_id = dv.id AND l.first_date <= t.start_date ORDER BY l.first_date DESC LIMIT 1) ls ON TRUE
        LEFT JOIN LATERAL (SELECT l.level FROM drive_levels l
                           WHERE l.drive_id = dv.id AND l.first_date <= t.destination_date ORDER BY l.first_date DESC LIMIT 1) le ON TRUE
        LEFT JOIN LATERAL (SELECT o.outside_temperature FROM outside_temperatures o
                           WHERE o.vin = t.vin AND o.first_date <= t.start_date AND o.last_date >= t.start_date - interval '6 hours'
                           ORDER BY o.first_date DESC LIMIT 1) tp ON TRUE
        WHERE t.vin = %s AND t.destination_date IS NOT NULL AND f.trip_id IS NULL
        ON CONFLICT (trip_id) DO NOTHING""",
        (vin,),
    )
    n = cur.rowcount
    backfill(conn, vin)
    return n


BATCH = 300


def backfill(conn, vin: str, batch: int = BATCH) -> dict[str, int]:
    """Columns added after facts were first written, filled a few hundred trips at a time so a
    tick stays short. Sentinels (-999, -1) mark "looked, nothing there" so a row is visited once."""
    done = {}
    done["temp_f"] = conn.execute(
        """
        WITH todo AS (SELECT f.trip_id FROM cc_trip_facts f JOIN trips t ON t.id = f.trip_id WHERE t.vin = %s AND f.temp_f IS NULL LIMIT %s)
        UPDATE cc_trip_facts f SET temp_f = COALESCE((SELECT o.outside_temperature FROM outside_temperatures o
                                                       WHERE o.vin = t.vin AND o.first_date <= t.start_date AND o.last_date >= t.start_date - interval '6 hours'
                                                       ORDER BY o.first_date DESC LIMIT 1), -999)
        FROM todo JOIN trips t ON t.id = todo.trip_id WHERE f.trip_id = todo.trip_id""",
        (vin, batch),
    ).rowcount
    done["range"] = conn.execute(
        """
        WITH todo AS (SELECT f.trip_id FROM cc_trip_facts f JOIN trips t ON t.id = f.trip_id WHERE t.vin = %s AND f.range_start IS NULL LIMIT %s)
        UPDATE cc_trip_facts f SET
            range_start = COALESCE((SELECT r.range FROM drive_ranges r WHERE r.drive_id = d.id AND r.first_date <= t.start_date ORDER BY r.first_date DESC LIMIT 1), -999),
            range_end   = COALESCE((SELECT r.range FROM drive_ranges r WHERE r.drive_id = d.id AND r.first_date <= t.destination_date ORDER BY r.first_date DESC LIMIT 1), -999)
        FROM todo JOIN trips t ON t.id = todo.trip_id JOIN drives d ON d.vin = t.vin WHERE f.trip_id = todo.trip_id""",
        (vin, batch),
    ).rowcount
    done["range_added"] = conn.execute(
        """
        WITH todo AS (SELECT f.session_id FROM cc_charge_facts f JOIN charging_sessions c ON c.id = f.session_id WHERE c.vin = %s AND f.range_added IS NULL AND c.session_end_date IS NOT NULL LIMIT %s)
        UPDATE cc_charge_facts f SET range_added = COALESCE(
            (SELECT r.range FROM drive_ranges r WHERE r.drive_id = d.id AND r.first_date <= c.session_end_date ORDER BY r.first_date DESC LIMIT 1)
          - (SELECT r.range FROM drive_ranges r WHERE r.drive_id = d.id AND r.first_date <= c.session_start_date ORDER BY r.first_date DESC LIMIT 1), -999)
        FROM todo JOIN charging_sessions c ON c.id = todo.session_id JOIN drives d ON d.vin = c.vin WHERE f.session_id = todo.session_id""",
        (vin, batch),
    ).rowcount
    # Where each priced charge's cost came from, and its exact split, for facts written
    # before either was stored. Those were all priced on three periods keyed by role, so
    # the split is the three columns as they are.
    done["cost_source"] = conn.execute(
        """
        WITH todo AS (SELECT f.session_id FROM cc_charge_facts f JOIN charging_sessions c ON c.id = f.session_id
                      WHERE c.vin = %s AND f.cost IS NOT NULL AND f.cost_source IS NULL LIMIT %s)
        UPDATE cc_charge_facts f SET
            cost_source = CASE WHEN f.plan = 'entered by hand' THEN 'hand' WHEN f.plan LIKE 'DC at%%' THEN 'dc' ELSE 'plan' END,
            period_kwh = CASE WHEN f.period_kwh IS NULL AND f.off_peak_kwh IS NOT NULL
                              THEN jsonb_build_object('off_peak', f.off_peak_kwh, 'part_peak', f.part_peak_kwh, 'peak', f.peak_kwh) ELSE f.period_kwh END
        FROM todo WHERE f.session_id = todo.session_id""",
        (vin, batch),
    ).rowcount
    done["max_mph"] = conn.execute(MAX_MPH_SQL, (vin, batch)).rowcount
    done["elevation"] = conn.execute(ELEVATION_SQL, (vin, batch)).rowcount
    done["charge_points"] = conn.execute(CHARGE_POINT_SQL, (vin, batch)).rowcount
    return done


# A drive's top speed from its own fixes, not counting a reading left out by hand (driveedits), or
# from a Tesla's stream where it has one: a feed's fix a minute missed peaks (a drive averaging
# 47 mph topped out at 16), the stream sends speed every second. A top below the drive's own
# average (its odometer over its time) is a peak the samples missed: not known, not shown.
# -1 marks "looked, nothing there" and "not known".
MAX_MPH_SQL = f"""
        WITH todo AS (SELECT f.trip_id FROM cc_trip_facts f JOIN trips t ON t.id = f.trip_id
                      WHERE t.vin = %s AND f.max_mph IS NULL AND t.destination_date IS NOT NULL LIMIT %s),
        seen AS (SELECT todo.trip_id, GREATEST(
            (SELECT MAX(p.speed) FROM cc_positions p WHERE p.vin = t.vin AND p.at BETWEEN t.start_date AND t.destination_date{driveedits.left_out()}),
            (SELECT MAX((e->'value'->>'doubleValue')::float) FROM cc_tesla_raw r, jsonb_array_elements(r.record->'data') e
              WHERE r.vin = t.vin AND r.kind = 'V' AND r.at BETWEEN t.start_date AND t.destination_date
                AND e->>'key' = 'VehicleSpeed' AND e->'value' ? 'doubleValue')) AS top,
            (SELECT (MAX(p.odometer) - MIN(p.odometer)) / NULLIF(EXTRACT(EPOCH FROM t.destination_date - t.start_date) / 3600, 0)
               FROM cc_positions p WHERE p.vin = t.vin AND p.at BETWEEN t.start_date AND t.destination_date) AS avg
          FROM todo JOIN trips t ON t.id = todo.trip_id)
        UPDATE cc_trip_facts f SET max_mph = CASE WHEN seen.top IS NULL OR seen.top < seen.avg THEN -1 ELSE seen.top END
        FROM seen WHERE f.trip_id = seen.trip_id"""

# A drive's highest and lowest ground, from its own fixes. A fix more than SPIKE_M from
# every fix within two either side of it is the receiver losing the sky, not a hill: no
# road climbs that in a fix or two, and a bad one once put a drive four kilometres under
# the sea. Two either side, not one, so the last fix of a drive is not thrown out for
# sitting next to a spike. Anything outside Earth's roads (the Dead Sea shore to the
# highest pass) is dropped the same way.
# -99999 marks "looked, nothing there".
SPIKE_M = 100
ELEVATION_SQL = f"""
        WITH todo AS (SELECT f.trip_id FROM cc_trip_facts f JOIN trips t ON t.id = f.trip_id
                      WHERE t.vin = %s AND f.elev_max_m IS NULL AND t.destination_date IS NOT NULL LIMIT %s)
        UPDATE cc_trip_facts f SET elev_max_m = COALESCE(e.hi, -99999), elev_min_m = COALESCE(e.lo, -99999)
        FROM todo JOIN trips t ON t.id = todo.trip_id
        LEFT JOIN LATERAL (
            SELECT MAX(p.e) AS hi, MIN(p.e) AS lo FROM (
                SELECT elevation AS e, LAG(elevation) OVER w AS a, LEAD(elevation) OVER w AS b,
                       LAG(elevation, 2) OVER w AS a2, LEAD(elevation, 2) OVER w AS b2
                FROM cc_positions q WHERE q.vin = t.vin AND q.at BETWEEN t.start_date AND t.destination_date AND q.elevation BETWEEN -450 AND 6000{driveedits.left_out('q')}
                WINDOW w AS (ORDER BY at)) p
            WHERE COALESCE(p.a, p.b, p.a2, p.b2) IS NULL
               OR ABS(p.e - p.a) <= {SPIKE_M} OR ABS(p.e - p.b) <= {SPIKE_M} OR ABS(p.e - p.a2) <= {SPIKE_M} OR ABS(p.e - p.b2) <= {SPIKE_M}) e ON TRUE
        WHERE f.trip_id = todo.trip_id"""

FT_PER_M = 3.28084


def fill_ground(conn, vin: str, terrain, wanted: set | None = None, batch: int = 100) -> int:
    """Climb, descent and the highest and lowest ground for up to `batch` finished drives not
    yet measured on the terrain model, newest first, with the same turn-to-turn counting the
    pages use on a drive's own heights (day.elevation_profile). A drive the model does not
    cover is marked -1 and the tiles it lacks are added to `wanted`, so they are fetched and
    the drive looked at again. Does nothing while no tile is downloaded."""
    if not terrain:
        return 0
    from day import elevation_profile
    from terrain import ground_track

    todo = conn.execute(
        """SELECT t.id, t.start_date, t.destination_date FROM cc_trip_facts f JOIN trips t ON t.id = f.trip_id
           WHERE t.vin = %s AND f.ground_climb_m IS NULL AND t.destination_date IS NOT NULL ORDER BY t.id DESC LIMIT %s""",
        (vin, batch),
    ).fetchall()
    for t in todo:
        track = conn.execute("SELECT at, latitude, longitude FROM cc_positions WHERE vin = %s AND at BETWEEN %s AND %s ORDER BY at", (vin, t["start_date"], t["destination_date"])).fetchall()
        try:
            ground, ok = ground_track(track, terrain)
            series, climb_ft, descent_ft = elevation_profile(ground) if ok else ([], 0.0, 0.0)
        except Exception as err:  # noqa: BLE001 - one bad drive must not hold the rest back
            LOG.warning("ground facts for trip %s: %s", t["id"], err)
            series, ok = [], True
        if series:
            hs = [h for _t, h in series]
            vals = (climb_ft / FT_PER_M, descent_ft / FT_PER_M, max(hs) / FT_PER_M, min(hs) / FT_PER_M)
        else:
            vals = (-1.0, None, None, None)
            if wanted is not None:
                wanted |= terrain.missing([(p["latitude"], p["longitude"]) for p in track])
        conn.execute("UPDATE cc_trip_facts SET ground_climb_m = %s, ground_descent_m = %s, ground_hi_m = %s, ground_lo_m = %s WHERE trip_id = %s", (*vals, t["id"]))
    return len(todo)


def reset_ground(conn) -> int:
    """Look again at the drives the model did not cover, after new tiles arrive."""
    return conn.execute("UPDATE cc_trip_facts SET ground_climb_m = NULL WHERE ground_climb_m = -1").rowcount


# A charge's peak power and the point it adds to the battery's lifetime report. Only once
# the charge has been over for half an hour: a live session's end keeps moving while it
# charges, and a figure taken partway would stand for good. Power from the power runs,
# else volts times amps (a car that reports only those); range at 100 % from the car's own
# estimate where it gave one around the end, else rated range over the level, which is
# what the estimate is. -1 and -999 mark "looked, nothing there".
CHARGE_POINT_SQL = """
        WITH todo AS (SELECT f.session_id FROM cc_charge_facts f JOIN charging_sessions c ON c.id = f.session_id
                      WHERE c.vin = %s AND f.peak_kw IS NULL AND c.session_end_date < now() - interval '30 minutes' LIMIT %s)
        UPDATE cc_charge_facts f SET
            peak_kw = COALESCE(
                (SELECT MAX(p.power) FROM charging_powers p WHERE p.vin = c.vin AND p.power < 1000
                   AND p.first_date BETWEEN c.session_start_date - interval '12 hours' AND c.session_end_date AND p.last_date >= c.session_start_date),
                (SELECT MAX(x.volts * x.amps) / 1000 FROM cc_charging_electrics x WHERE x.vin = c.vin
                   AND x.first_date BETWEEN c.session_start_date - interval '12 hours' AND c.session_end_date AND x.last_date >= c.session_start_date),
                -1),
            full_level = COALESCE(le.level, -999),
            full_range = COALESCE(
                (SELECT e.range_estimated_full FROM drive_ranges_estimated_full e WHERE e.drive_id = d.id AND e.first_date <= c.session_end_date
                   AND e.last_date >= c.session_end_date - interval '1 hour' ORDER BY e.first_date DESC LIMIT 1),
                CASE WHEN le.level >= 10 THEN re.range * 100 / le.level END,
                -999),
            odometer = COALESCE(c.session_odometer,
                (SELECT p.odometer FROM cc_positions p WHERE p.vin = c.vin AND p.at <= c.session_start_date AND p.odometer IS NOT NULL ORDER BY p.at DESC LIMIT 1),
                -1)
        FROM todo JOIN charging_sessions c ON c.id = todo.session_id JOIN drives d ON d.vin = c.vin
        LEFT JOIN LATERAL (SELECT l.level FROM drive_levels l WHERE l.drive_id = d.id AND l.first_date <= c.session_end_date ORDER BY l.first_date DESC LIMIT 1) le ON TRUE
        LEFT JOIN LATERAL (SELECT r.range FROM drive_ranges r WHERE r.drive_id = d.id AND r.first_date <= c.session_end_date ORDER BY r.first_date DESC LIMIT 1) re ON TRUE
        WHERE f.session_id = todo.session_id"""


# Which charges the measurement is allowed to use, in one place because two queries ask
# it: the all-time figure and the per-year one, which must agree on the population or
# the fallback would not be comparable with the value it falls back from.
# The join to `drives` is hoisted out of the laterals on purpose. Inside them the drive
# id is not known until the join runs, so the planner cannot use the (drive_id,
# first_date) index and hash-joins instead: a sequential scan of all 731,894
# drive_ranges rows, once per session, 1,122 times for one car. That is where 58 seconds
# a car went. Resolved outside, each lateral is one index seek. `drives` holds a row per
# car, so hoisting it changes no row counts.
RATED_MILE_SOURCE = """
        FROM charging_sessions c
        JOIN drives dv ON dv.vin = c.vin
        JOIN LATERAL (SELECT r.range FROM drive_ranges r
                      WHERE r.drive_id = dv.id AND r.first_date <= c.session_start_date ORDER BY r.first_date DESC LIMIT 1) rs ON TRUE
        JOIN LATERAL (SELECT r.range FROM drive_ranges r
                      WHERE r.drive_id = dv.id AND r.first_date <= c.session_end_date ORDER BY r.first_date DESC LIMIT 1) re ON TRUE
        WHERE c.vin = %s AND c.real_charged > 5 AND re.range - rs.range > 20 AND c.charging_type = 'AC'
          AND c.session_end_date - c.session_start_date > interval '30 minutes'"""

# A rated mile outside this band is not a rated mile; something upstream is wrong and a
# wrong constant is worse than none, because every energy and cost figure reads it.
RATED_MILE_BAND = (0.15, 0.6)

# A year with fewer charges than this keeps the all-time figure. The drift being
# corrected is about 3 % end to end over eight years, and a year measured on a handful
# of sessions wobbles by more than that, so below this line the correction would add
# noise rather than remove bias. In practice the years that qualify carry tens to hundreds
# of sessions each, so nothing near the threshold is in play.
YEAR_MIN_CHARGES = 25


def _plausible(k) -> float | None:
    lo, hi = RATED_MILE_BAND
    return float(k) if k is not None and lo < float(k) < hi else None


def fill_vehicle_year_facts(conn, vin: str, tz) -> int:
    """The same measurement per calendar year, so an old drive is priced at what a rated
    mile cost the car then.

    One all-time constant put a 2018 drive on a 2026 figure. Measured on the Model 3 that
    is 0.2475 against the 0.2524 its own 2018 charges show, about 2 %, and the drift is
    not monotonic — 0.2524 in 2018, down to 0.2427 in 2023, back up to 0.2445 in 2026 —
    so it is firmware changing how rated range is worked out at least as much as it is a
    battery ageing. Either way the honest figure for a drive is the one its own year
    measured.

    The year is the local one, matching how every page groups by date; a charge either
    side of midnight on New Year's Eve could land in either year, which moves a yearly
    aggregate of a hundred-odd sessions by nothing that can be seen.
    """
    rows = conn.execute(
        f"""SELECT EXTRACT(year FROM c.session_start_date AT TIME ZONE %s)::int AS yr, COUNT(*) AS n,
                   SUM(c.real_charged) / NULLIF(SUM(re.range - rs.range), 0) AS k
            {RATED_MILE_SOURCE}
            GROUP BY 1 HAVING COUNT(*) >= %s""",
        (getattr(tz, "key", str(tz)), vin, YEAR_MIN_CHARGES),
    ).fetchall()
    kept = 0
    for r in rows:
        yr, n, k = (r["yr"], r["n"], r["k"]) if isinstance(r, dict) else tuple(r)
        k = _plausible(k)
        if k is None:
            continue
        conn.execute(
            """INSERT INTO cc_vehicle_year_facts (vin, year, kwh_per_rated_mi, charges_used) VALUES (%s, %s, %s, %s)
               ON CONFLICT (vin, year) DO UPDATE SET kwh_per_rated_mi = EXCLUDED.kwh_per_rated_mi, charges_used = EXCLUDED.charges_used""",
            (vin, int(yr), k, int(n)),
        )
        kept += 1
    # Stamped whether or not any year qualified, so a car with a few months of history
    # is not remeasured on every tick for an answer that is going to stay empty.
    conn.execute(
        "INSERT INTO cc_vehicle_facts (vin, years_updated_at) VALUES (%s, now()) ON CONFLICT (vin) DO UPDATE SET years_updated_at = now()",
        (vin,),
    )
    return kept


# A parked stretch shorter than this says little and one longer than a day is usually
# two stretches with a gap in the record between them.
PARKED_WINDOW = ("2 hours", "24 hours")

# Below this many stretches the figure is not published. A median of nine nights is a
# number, not a measurement, and it would sit on a page next to two that are.
PARKED_MIN_SAMPLES = 30

PARKED_RATES_SQL = f"""
WITH parked AS (
  SELECT s.id, s.vin, s.first_date AS a, s.last_date AS b,
         EXTRACT(epoch FROM (s.last_date - s.first_date))/3600.0 AS hours
  FROM states s
  WHERE s.vin = %s AND s.state::text IN ('PARKED','OFFLINE')
    AND s.last_date - s.first_date BETWEEN interval '{PARKED_WINDOW[0]}' AND interval '{PARKED_WINDOW[1]}'
    -- A stretch that overlaps a charge is not measuring drain.
    AND NOT EXISTS (SELECT 1 FROM charging_sessions c
                    WHERE c.vin = s.vin AND c.session_start_date < s.last_date AND c.session_end_date > s.first_date)
), modes AS (
  -- One join rather than two correlated lookups per stretch: the correlated form
  -- scanned every mode run this car has before each stretch, 36 s against 13 s.
  SELECT p.id,
    COALESCE(SUM(EXTRACT(epoch FROM (LEAST(x.last_date,p.b) - GREATEST(x.first_date,p.a)))) FILTER (WHERE x.sentry), 0)/3600.0 AS sentry_h,
    COALESCE(SUM(EXTRACT(epoch FROM (LEAST(x.last_date,p.b) - GREATEST(x.first_date,p.a)))) FILTER (WHERE x.climate), 0)/3600.0 AS climate_h
  FROM parked p LEFT JOIN cc_modes x ON x.vin = p.vin AND x.first_date < p.b AND x.last_date > p.a
  GROUP BY p.id
), m AS (
  SELECT p.hours, md.sentry_h, md.climate_h,
    (SELECT r.range FROM drive_ranges r WHERE r.drive_id = (SELECT id FROM drives WHERE vin = p.vin)
      AND r.first_date <= p.a ORDER BY r.first_date DESC LIMIT 1)
    -- The first reading at or AFTER the stretch ends, not the last one before it. A
    -- sleeping car stops reporting, so the reading "at" the end of a quiet night is the
    -- one from bedtime and the night comes out costing nothing. Measured the wrong way
    -- round, half of all quiet stretches showed no loss at all; measured this way, an
    -- eighth do.
    - (SELECT r.range FROM drive_ranges r WHERE r.drive_id = (SELECT id FROM drives WHERE vin = p.vin)
      AND r.first_date >= p.b ORDER BY r.first_date ASC LIMIT 1) AS lost
  FROM parked p JOIN modes md ON md.id = p.id
), ok AS (SELECT * FROM m WHERE lost IS NOT NULL AND lost BETWEEN -1 AND 60 AND hours > 0)
SELECT
  percentile_cont(0.5) WITHIN GROUP (ORDER BY lost/hours) FILTER (WHERE sentry_h < 0.1 AND climate_h < 0.1) AS idle,
  COUNT(*) FILTER (WHERE sentry_h < 0.1 AND climate_h < 0.1) AS idle_n,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY lost/hours) FILTER (WHERE sentry_h > 1) AS sentry,
  COUNT(*) FILTER (WHERE sentry_h > 1) AS sentry_n,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY lost/hours) FILTER (WHERE climate_h > 0.2) AS climate,
  COUNT(*) FILTER (WHERE climate_h > 0.2) AS climate_n
FROM ok"""


def published(value, samples) -> tuple[float | None, int]:
    """A rate and its sample count, or nothing if too few stretches stand behind it."""
    n = int(samples or 0)
    return (float(value) if value is not None and n >= PARKED_MIN_SAMPLES else None, n)


def fill_parked_facts(conn, vin: str) -> None:
    """What an hour parked costs this car, with nothing running, with Sentry, with climate.

    What Sentry costs is a thing about your car rather than about the feature. Measured
    here from the car's own parked stretches: the median rated miles an hour lost, split
    by what was running.

    The medians are medians on purpose. A handful of stretches straddle a firmware
    update or a week at an airport and lose far more than a night should, and a mean
    carries those into the headline figure.
    """
    row = conn.execute(PARKED_RATES_SQL, (vin,)).fetchone()
    if row is None:
        return
    idle, idle_n, sentry, sentry_n, climate, climate_n = tuple(row.values()) if isinstance(row, dict) else tuple(row)
    conn.execute(
        """INSERT INTO cc_vehicle_facts (vin, idle_mi_per_h, idle_samples, sentry_mi_per_h, sentry_samples, climate_mi_per_h, climate_samples, parked_updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (vin) DO UPDATE SET idle_mi_per_h = EXCLUDED.idle_mi_per_h, idle_samples = EXCLUDED.idle_samples,
               sentry_mi_per_h = EXCLUDED.sentry_mi_per_h, sentry_samples = EXCLUDED.sentry_samples,
               climate_mi_per_h = EXCLUDED.climate_mi_per_h, climate_samples = EXCLUDED.climate_samples,
               parked_updated_at = now()""",
        (vin, *published(idle, idle_n), *published(sentry, sentry_n), *published(climate, climate_n)),
    )


def fill_vehicle_facts(conn, vin: str) -> float | None:
    """What a rated mile costs at the wall, from the car's own charges: meter kWh
    over rated range gained, across every AC session over 30 minutes that added
    more than 20 rated miles. Rated-range loggers derive energy per drive this way too
    (rated miles used x a constant); ours is measured on this car.

    This is the all-time figure, and the fallback for a car or a year that has not
    charged enough to measure its own. fill_vehicle_year_facts is the contemporaneous
    one that pages prefer."""
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n, SUM(c.real_charged) / NULLIF(SUM(re.range - rs.range), 0) AS k
        {RATED_MILE_SOURCE}""",
        (vin,),
    ).fetchone()
    n, k = tuple(row.values()) if isinstance(row, dict) else tuple(row)
    k = _plausible(k)
    if not n or k is None:
        return None
    # what a kWh at home has actually cost this car, from the priced sessions (rather than a rate you type in)
    hr = conn.execute(
        """SELECT SUM(f.cost) / NULLIF(SUM(f.kwh), 0) AS rate, COALESCE(bool_or(f.cost_source = 'average'), FALSE) AS est
           FROM cc_charge_facts f JOIN charging_sessions c ON c.id = f.session_id
           WHERE c.vin = %s AND f.at_home AND f.cost IS NOT NULL AND f.kwh > 1""",
        (vin,),
    ).fetchone()
    home_rate, estimated = tuple(hr.values()) if isinstance(hr, dict) else tuple(hr)
    conn.execute(
        """INSERT INTO cc_vehicle_facts (vin, kwh_per_rated_mi, charges_used, home_rate, home_rate_estimate, updated_at) VALUES (%s, %s, %s, %s, %s, now())
                    ON CONFLICT (vin) DO UPDATE SET kwh_per_rated_mi = EXCLUDED.kwh_per_rated_mi, charges_used = EXCLUDED.charges_used,
                    home_rate = EXCLUDED.home_rate, home_rate_estimate = EXCLUDED.home_rate_estimate, updated_at = now()""",
        (vin, k, int(n), float(home_rate) if home_rate is not None else None, bool(estimated)),
    )
    return k


# --- pricing a charge ------------------------------------------------------------------
#
# Each pricer looks at one charge and either prices it, returning a Priced, or passes
# (None). The first that prices it wins, in PRICERS order: what was paid, typed in with
# the charge; what the network billed; the place's own price; the home plan; the
# fast-charging rate away from home; the regional average for a home charge no plan
# covers. A charge none of them prices is stored with no cost.


@dataclass
class Charge:
    """What a pricer may look at: the session, its energy and where it was."""

    session: dict
    kwh: float
    home: bool
    region: str | None
    profile: list  # (first, last, kW) runs inside the session, or one flat run
    tz: object
    dc_price: float | None = None  # fast charging on any network but Tesla's, or an unknown one
    dc_per_min: float | None = None
    averages: object = None  # averages.Averages, or None to price no charge on an average
    sc_price: float | None = None  # fast charging at a Tesla Supercharger
    sc_per_min: float | None = None
    place: dict | None = None  # the named place the charge is at, with its price (placeprice)
    bill: dict | None = None  # what the network billed for it (billed.py), when it says


@dataclass
class Priced:
    cost: float
    source: str  # 'billed', 'hand', 'place', 'plan', 'dc' or 'average', stored as cost_source
    label: str  # stored as plan: the plan's name, "DC at 0.48/kWh", "Illinois average"
    roles: dict | None = None  # kWh by role (off_peak, part_peak, peak), plan only
    split: dict | None = None  # kWh by the plan's own period keys, plan only
    rate_month: str | None = None  # the month whose average priced it, average only


def price_billed(c: Charge) -> Priced | None:
    """What the network actually billed (Tesla's charging history): every fee on the
    session, as charged to the card. Only a price typed in with the charge outranks it."""
    if not c.bill:
        return None
    where = f", {c.bill['site']}" if c.bill.get("site") else ""
    return Priced(float(c.bill["total"]), "billed", f"Billed by Tesla{where}")


def price_hand(c: Charge) -> Priced | None:
    """Typed in with a hand-entered charge: what was actually paid beats any estimate."""
    paid = c.session.get("real_cost")
    return Priced(float(paid), "hand", "entered by hand") if paid is not None else None


def price_plan(c: Charge) -> Priced | None:
    """A home charge on the home plan, by the hour it drew power."""
    if not c.home or c.kwh <= 0:
        return None
    priced = session_cost(c.profile, c.session["session_start_date"], c.session["session_end_date"], c.tz)
    if not priced:
        return None
    # The car's meter is the total; the power profile only says when it was drawn. The
    # same scale that corrects the cost corrects the split, so the periods still sum to
    # the meter reading rather than to the integrated profile.
    scale = (c.kwh / priced["total_kwh"]) if priced["total_kwh"] > 0 else 0.0
    roles = {k: v * scale for k, v in priced["roles"].items()}
    return Priced(priced["cost"] * scale, "plan", priced["plan"], roles=roles, split={k: v * scale for k, v in priced["kwh"].items()})


def price_place(c: Charge) -> Priced | None:
    """A charge at a named place whose owner set a price for it on Places: free, a flat
    price per kWh or per minute (plus a session fee), the home plan, or the regional
    average. A place with no price passes, so Home with none set is priced by the home
    plan below exactly as before places had prices."""
    price = (c.place or {}).get("price")
    if not price or c.kwh <= 0 or not placeprice.valid(price):
        return None
    kind = price["kind"]
    if kind == "home":
        return price_plan(replace(c, home=True))  # the plan's own split and label: it is the plan
    if kind == "average":
        return price_average(replace(c, home=True))  # still marked as an estimate
    if kind == "free":
        return Priced(0.0, "place", placeprice.label(price, c.place["name"]))
    start, end = c.session["session_start_date"], c.session["session_end_date"]
    minutes = (end - start).total_seconds() / 60 if start and end else 0.0
    return Priced(placeprice.cost(price, c.kwh, minutes), "place", placeprice.label(price, c.place["name"]))


def price_dc(c: Charge) -> Priced | None:
    """A fast charge away from home, at the per-kWh price on Settings, plus a per-minute
    rate where the network bills by time: a rate you know you paid. A Supercharger (where
    the source said so) takes the Supercharger prices; any other or unknown network the
    other ones."""
    if c.home or c.kwh <= 0 or (c.session.get("charging_type") or "").upper() != "DC":
        return None
    sc = c.session.get("network") == "supercharger"
    per_kwh, per_min = (c.sc_price, c.sc_per_min) if sc else (c.dc_price, c.dc_per_min)
    if not (per_kwh or per_min):
        return None
    start, end = c.session["session_start_date"], c.session["session_end_date"]
    minutes = ((end - start).total_seconds() / 60) if (start and end and per_min) else 0.0
    price = c.kwh * (per_kwh or 0.0) + minutes * (per_min or 0.0)
    label = " + ".join(x for x in ((f"{per_kwh:.2f}/kWh" if per_kwh else ""), (f"{per_min:.2f}/min" if per_min else "")) if x)
    return Priced(price, "dc", f"{'Supercharger' if sc else 'DC'} at {label}")


def price_average(c: Charge) -> Priced | None:
    """A home charge no plan covers, at the average household price for its region and
    month. Home only: a public charger's price has nothing to do with a household's."""
    if not c.home or c.kwh <= 0 or c.averages is None or not c.region:
        return None
    got = c.averages.price(c.region, "electricity", c.session["session_start_date"].astimezone(c.tz).strftime("%Y-%m"))
    return Priced(c.kwh * got["value"], "average", f"{got['name']} average", rate_month=got["month"]) if got else None


PRICERS = (price_hand, price_billed, price_place, price_plan, price_dc, price_average)


def charge_facts(
    sessions: list[dict],
    powers: list[tuple[datetime, datetime, float]],
    tz,
    is_home,
    dc_price: float | None = None,
    dc_per_min: float | None = None,
    averages=None,
    region_of=region_of,
    sc_price: float | None = None,
    sc_per_min: float | None = None,
    place_of=None,
    bills=(),
) -> list[tuple]:
    """(session_id, kwh, cost, plan, at_home, off_peak, part_peak, peak, period_kwh,
    cost_source, region, rate_month) per session, period_kwh as JSON text. Pure. A charge
    nothing prices has no cost and cost_source 'none'; a charge with no region has ''.

    The region is found from the session's own position and nothing else. `place_of(lat,
    lon)` is the named place there (with its price), the same lookup that labels the
    charge; None prices no charge by its place. `bills` are the network's bills that
    could match these sessions (billed.bills_between); each prices at most one.

    `powers` are (first, last, kW) intervals sorted by first_date; they never
    overlap each other (they are runs), so their last_dates are sorted too and
    the ones touching a session are found by bisect rather than a scan."""
    lasts = [b for _a, b, _kw in powers]
    out = []
    unused = list(bills or ())
    for s in sessions:
        start, end = s["session_start_date"], s["session_end_date"]
        i = bisect_left(lasts, start)
        mine = []
        while i < len(powers) and powers[i][0] <= end:
            a, b, kw = powers[i]
            if kw and kw > 0:
                mine.append((a, b, kw))
            i += 1
        integrated = sum(kw * (min(b, end) - max(a, start)).total_seconds() / 3600 for a, b, kw in mine)
        # An export's meter is the charge's energy. The live car's (live_meter, cc_charge_meter) may
        # be a poll behind the charge's end, so the larger of it and the power estimate stands.
        meter = s.get("real_charged")
        kwh = meter if meter else max(s.get("live_meter") or 0.0, integrated)
        lat, lon = s.get("session_position_latitude"), s.get("session_position_longitude")
        hours = (end - start).total_seconds() / 3600
        profile = mine if integrated > 0 else ([(start, end, kwh / hours)] if hours > 0 and kwh > 0 else [])
        place = place_of(lat, lon) if place_of else None
        # A charge with a price typed in keeps it, so it leaves any bill for another.
        bill = billed.match(s, unused) if unused and s.get("real_cost") is None else None
        if bill is not None:
            unused.remove(bill)
        c = Charge(s, kwh, bool(is_home(lat, lon)), region_of(lat, lon), profile, tz, dc_price, dc_per_min, averages, sc_price, sc_per_min, place, bill)
        p = next((p for p in (pricer(c) for pricer in PRICERS) if p), None)
        if p is None:
            out.append((s["id"], kwh, None, None, c.home, None, None, None, None, "none", c.region or "", None))
            continue
        roles = p.roles or {}
        split = json.dumps(p.split) if p.split is not None else None
        out.append((s["id"], kwh, p.cost, p.label, c.home, roles.get("off_peak"), roles.get("part_peak"), roles.get("peak"), split, p.source, c.region or "", p.rate_month))
    return out


def fill_charge_facts(
    conn,
    vin: str,
    tz,
    is_home,
    batch: int = BATCH,
    dc_price: float | None = None,
    dc_per_min: float | None = None,
    averages=None,
    sc_price: float | None = None,
    sc_per_min: float | None = None,
    place_of=None,
) -> int:
    """Energy and cost for finished charges that have no facts yet: one query for the
    sessions, one for their power intervals.

    A batch at a time, and not for tidiness. The power intervals are read for the span
    from the first session in the batch to the last, so an unbounded batch that happened
    to include a 2018 charge and a 2026 one would read eight years of them into memory
    at once. A backfill that runs longer than expected while holding
    a lock every reader needs takes the site down.
    """
    covered = averages.covered("electricity") if averages is not None else []
    sessions = conn.execute(
        """
        SELECT c.id, c.session_start_date, c.session_end_date, c.session_position_latitude, c.session_position_longitude,
               c.real_charged, c.charging_type::text AS charging_type, c.real_cost, x.network, m.kwh AS live_meter
        FROM charging_sessions c LEFT JOIN cc_charge_facts f ON f.session_id = c.id
        LEFT JOIN cc_charge_extras x ON x.vin = c.vin AND x.start_date = c.session_start_date
        -- the car's own meter for a live charge, where an export has real_charged
        LEFT JOIN cc_charge_meter m ON m.session_id = c.id
        WHERE c.vin = %s AND c.session_start_date IS NOT NULL AND c.session_end_date IS NOT NULL
          -- no facts at all, facts from before the time-of-use split was stored, or a
          -- stored zero on a session whose car metered energy into the battery
          --
          -- That last clause has to be one that stops matching once it is fixed. Written
          -- as a bare "kwh is zero" it never stopped: a session with no meter reading and
          -- no electrics is legitimately zero and was re-picked on every tick, so this
          -- query always returned work, and the expensive per-car measurements that run
          -- "if anything changed" ran every tick too. Against real_charged it converges:
          -- a session with a meter reading gets a non-zero kwh and is not seen again.
          --
          -- A car with no meter (the Lucid) gets its kwh from the power profile instead,
          -- and the connector keeps a live session's end date moving while it charges,
          -- so its facts can be computed off the first few minutes and read zero for
          -- good: one 40 to 90 session once said 0.0 kWh for a week. (No percent signs in
          -- these comments: psycopg reads them as placeholders and the query fails.) A zero on a session
          -- that ended in the last two days is re-read each tick; after that it rests.
          -- (A hand-entered price has no split and never will, so it is not one of those;
          -- nor is one priced on a regional average, which is one flat price.)
          -- (Nor one priced by its place's own flat price, or billed: neither has a split.)
          -- A bill that arrives later clears the price of the sessions it matches
          -- (billed.sessions_for), and the clause for cleared prices below takes them.
          AND (f.session_id IS NULL OR (f.at_home AND f.cost IS NOT NULL AND f.off_peak_kwh IS NULL AND f.plan IS DISTINCT FROM 'entered by hand'
                                        AND COALESCE(f.cost_source, 'plan') NOT IN ('average', 'place', 'billed'))
               OR (COALESCE(f.kwh, 0) = 0 AND (COALESCE(c.real_charged, m.kwh) > 0 OR c.session_end_date > now() - interval '2 days'))
               -- a meter that kept counting after the facts were taken: a streamed session
               -- is first read minutes in (one 24 to 78 charge stored 0.1 of its 36 kWh);
               -- converges, as the stored figure becomes the meter's
               OR COALESCE(c.real_charged, m.kwh) > COALESCE(f.kwh, 0) + 0.05
               -- a fast charge with no price yet, once a rate for its network exists to price
               -- it at; converges as each gets a cost
               OR (f.cost IS NULL AND NOT f.at_home AND c.charging_type = 'DC' AND COALESCE(f.kwh, 0) > 0
                   AND (x.network IS NOT DISTINCT FROM 'supercharger' AND %s OR x.network IS DISTINCT FROM 'supercharger' AND %s))
               -- a home charge with no price that has not been looked at since its region
               -- had an average (looking stores 'none' when there is still nothing); converges
               OR (f.at_home AND f.cost IS NULL AND f.cost_source IS NULL AND f.region = ANY(%s) AND COALESCE(f.kwh, 0) > 0)
               -- a charge whose price was cleared to be worked out again (its place's price
               -- changed, or a bill arrived for it); converges, as looking stores 'none'
               OR (f.cost IS NULL AND f.cost_source IS NULL AND COALESCE(f.kwh, 0) > 0))
        ORDER BY c.session_start_date LIMIT %s""",
        (vin, bool(sc_price or sc_per_min), bool(dc_price or dc_per_min), covered, batch),
    ).fetchall()
    if not sessions:
        return 0
    sessions = [
        (
            dict(
                zip(
                    ("id", "session_start_date", "session_end_date", "session_position_latitude", "session_position_longitude", "real_charged", "charging_type", "real_cost", "network", "live_meter"),
                    s,
                )
            )
            if not isinstance(s, dict)
            else s
        )
        for s in sessions
    ]
    lo, hi = sessions[0]["session_start_date"], max(s["session_end_date"] for s in sessions)
    rows = conn.execute("SELECT first_date, last_date, power FROM charging_powers WHERE vin = %s AND last_date >= %s AND first_date <= %s ORDER BY first_date", (vin, lo, hi)).fetchall()
    powers = [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in rows]
    el = conn.execute("SELECT first_date, last_date, volts, amps FROM cc_charging_electrics WHERE vin = %s AND last_date >= %s AND first_date <= %s ORDER BY first_date", (vin, lo, hi)).fetchall()
    powers = with_measured_power(powers, [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in el])
    bills = billed.bills_between(conn, vin, lo, max(s["session_start_date"] for s in sessions))
    facts = charge_facts(sessions, powers, tz, is_home, dc_price, dc_per_min, averages, sc_price=sc_price, sc_per_min=sc_per_min, place_of=place_of, bills=bills)
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO cc_charge_facts (session_id, kwh, cost, plan, at_home, off_peak_kwh, part_peak_kwh, peak_kwh, period_kwh, cost_source, region, rate_month, computed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, now())
               ON CONFLICT (session_id) DO UPDATE SET off_peak_kwh = EXCLUDED.off_peak_kwh, computed_at = now(),
                   part_peak_kwh = EXCLUDED.part_peak_kwh, peak_kwh = EXCLUDED.peak_kwh, period_kwh = EXCLUDED.period_kwh,
                   region = EXCLUDED.region, at_home = EXCLUDED.at_home,
                   -- Energy and cost too, over the zero or where the new figure is
                   -- larger: a stored figure was taken from as much of the charge as had
                   -- been seen, so a bigger one saw more of it. A price typed in or
                   -- billed is a total, and stands whatever the energy.
                   kwh = CASE WHEN COALESCE(cc_charge_facts.kwh, 0) = 0 OR EXCLUDED.kwh > cc_charge_facts.kwh + 0.05 THEN EXCLUDED.kwh ELSE cc_charge_facts.kwh END,
                   cost = CASE WHEN (COALESCE(cc_charge_facts.kwh, 0) = 0 OR cc_charge_facts.cost IS NULL OR (EXCLUDED.kwh > cc_charge_facts.kwh + 0.05 AND COALESCE(cc_charge_facts.cost_source, 'plan') NOT IN ('hand', 'billed'))) THEN EXCLUDED.cost ELSE cc_charge_facts.cost END,
                   plan = CASE WHEN (COALESCE(cc_charge_facts.kwh, 0) = 0 OR cc_charge_facts.cost IS NULL OR (EXCLUDED.kwh > cc_charge_facts.kwh + 0.05 AND COALESCE(cc_charge_facts.cost_source, 'plan') NOT IN ('hand', 'billed'))) THEN EXCLUDED.plan ELSE cc_charge_facts.plan END,
                   cost_source = CASE WHEN (COALESCE(cc_charge_facts.kwh, 0) = 0 OR cc_charge_facts.cost IS NULL OR (EXCLUDED.kwh > cc_charge_facts.kwh + 0.05 AND COALESCE(cc_charge_facts.cost_source, 'plan') NOT IN ('hand', 'billed'))) THEN EXCLUDED.cost_source ELSE cc_charge_facts.cost_source END,
                   rate_month = CASE WHEN (COALESCE(cc_charge_facts.kwh, 0) = 0 OR cc_charge_facts.cost IS NULL OR (EXCLUDED.kwh > cc_charge_facts.kwh + 0.05 AND COALESCE(cc_charge_facts.cost_source, 'plan') NOT IN ('hand', 'billed'))) THEN EXCLUDED.rate_month ELSE cc_charge_facts.rate_month END
               WHERE cc_charge_facts.off_peak_kwh IS NULL OR COALESCE(cc_charge_facts.kwh, 0) = 0 OR EXCLUDED.kwh > cc_charge_facts.kwh + 0.05
                     OR (cc_charge_facts.cost IS NULL AND (EXCLUDED.cost IS NOT NULL OR cc_charge_facts.cost_source IS NULL))""",
            facts,
        )
    return len(facts)


def fill_charge_regions(conn, vin: str, batch: int = BATCH, region_of=region_of) -> int:
    """The region of charges whose facts were written before regions were stored, a few
    hundred at a time, from each session's own position (never a drive's). '' marks
    "looked, no region" so a row is visited once."""
    rows = conn.execute(
        """SELECT f.session_id, c.session_position_latitude, c.session_position_longitude
           FROM cc_charge_facts f JOIN charging_sessions c ON c.id = f.session_id
           WHERE c.vin = %s AND f.region IS NULL LIMIT %s""",
        (vin, batch),
    ).fetchall()
    rows = [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in rows]
    found = [(region_of(lat, lon) or "", sid) for sid, lat, lon in rows]
    if found:
        with conn.cursor() as cur:
            cur.executemany("UPDATE cc_charge_facts SET region = %s WHERE session_id = %s", found)
    return len(found)


def reopen_average_prices(conn, tz) -> None:
    """After new average figures arrive: home charges that had none get another look, and
    charges priced on an earlier month's figure (their own month was not out yet) are
    priced again. Only the cost columns are cleared, so every page keeps the energy
    meanwhile; fill_charge_facts prices them again a batch at a time."""
    conn.execute("UPDATE cc_charge_facts SET cost_source = NULL WHERE cost_source = 'none' AND at_home")
    conn.execute(
        """UPDATE cc_charge_facts f SET cost = NULL, plan = NULL, cost_source = NULL, rate_month = NULL
           FROM charging_sessions c
           WHERE c.id = f.session_id AND f.cost_source = 'average' AND f.rate_month < to_char(c.session_start_date AT TIME ZONE %s, 'YYYY-MM')""",
        (getattr(tz, "key", str(tz)),),
    )


# What clearing a price forgets: the cost and everything said about it, never the
# energy, so every page keeps showing the kWh while fill_charge_facts prices it again.
CLEAR_PRICE = """UPDATE cc_charge_facts SET cost = NULL, plan = NULL, cost_source = NULL, rate_month = NULL,
       off_peak_kwh = NULL, part_peak_kwh = NULL, peak_kwh = NULL, period_kwh = NULL"""


def clear_prices(conn, session_ids: list[int], batch: int = BATCH) -> int:
    """Forget the price of these charges, a batch at a time, so the next ticks price them
    again. A price typed in with the charge is kept: nothing outranks it but a bill."""
    done = 0
    for i in range(0, len(session_ids), batch):
        done += conn.execute(CLEAR_PRICE + " WHERE session_id = ANY(%s) AND cost_source IS DISTINCT FROM 'hand'", (session_ids[i : i + batch],)).rowcount
    return done


def place_sessions(conn, place: dict, index) -> list[int]:
    """The charges at `place` (id, latitude, longitude, radius_m): those whose position the
    site would label with it, found the way every page finds a charge's place (place_for
    over `index`, the places now). A place no longer in `index` (just removed) takes back
    every charge inside its old circle that no remaining place claims. With `every` set (a
    circle a place moved away from, or one merged into another), every charge inside it:
    any of them may have changed place."""
    lat, lon, r = float(place["latitude"]), float(place["longitude"]), float(place["radius_m"])
    dlat = r / 111_320.0
    dlon = r / max(1.0, 111_320.0 * math.cos(math.radians(lat)))
    rows = conn.execute(
        """SELECT id, session_position_latitude, session_position_longitude FROM charging_sessions
           WHERE session_position_latitude BETWEEN %s AND %s AND session_position_longitude BETWEEN %s AND %s""",
        (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
    ).fetchall()
    out = []
    for row in rows:
        sid, a, b = tuple(row.values()) if isinstance(row, dict) else tuple(row)
        hit = place_for(a, b, index)
        inside = distance_m(a, b, lat, lon) <= r
        if place.get("every") and inside or hit is not None and hit["id"] == place["id"] or hit is None and inside:
            out.append(sid)
    return out


REPRICE_KEY = "reprice_places"  # cc_state: the places whose charges wait to be priced again


def queue_reprice(conn, place: dict, every: bool = False) -> None:
    """Ask for the charges at `place` (id, latitude, longitude, radius_m) to be priced
    again, in the background: the place's own price changed, it became home or stopped
    being, it moved or was resized (`every`: the old circle, whose charges may now be
    another place's or none), or it is being removed. The caller commits; the request
    that changed the place does no more than this."""
    row = conn.execute("SELECT value FROM cc_state WHERE key = %s FOR UPDATE", (REPRICE_KEY,)).fetchone()
    held = json.loads(((row["value"] if isinstance(row, dict) else row[0]) if row else "") or "[]")
    entry = {k: place[k] for k in ("id", "latitude", "longitude", "radius_m")}
    if every:
        entry["every"] = True
    same = lambda p: all(p.get(k) == entry[k] for k in ("id", "latitude", "longitude", "radius_m"))  # noqa: E731
    held = [p for p in held if not same(p)] + [entry]
    conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (REPRICE_KEY, json.dumps(held)))


def drain_reprices(conn, index) -> int:
    """Clear the price of every charge at each queued place, a batch at a time (the
    ticks after this price them again, a batch at a time too), then empty the queue.
    `index` is the places as they are now. Returns the charges cleared."""
    row = conn.execute("SELECT value FROM cc_state WHERE key = %s FOR UPDATE", (REPRICE_KEY,)).fetchone()
    held = json.loads(((row["value"] if isinstance(row, dict) else row[0]) if row else "") or "[]")
    if not held:
        conn.rollback()
        return 0
    cleared = sum(clear_prices(conn, place_sessions(conn, p, index)) for p in held)
    conn.execute("DELETE FROM cc_state WHERE key = %s", (REPRICE_KEY,))
    conn.commit()
    return cleared


def take_meter(conn, vin: str, kwh: float, now: datetime) -> int | None:
    """Keep the car's meter reading for the charge under way: the session the database plugin
    has open for `vin`, and only an open one. A session whose end is set is finished and never
    takes a reading, so a cable back in a few minutes later cannot hand the old charge the new
    one's first tenths; while the new session has no start yet, the reading waits for the next
    tick. A start a little ahead of now (the plugin's clock, or the car's) still counts.

    The stored figure only grows. A reading under the last one is the meter restarting (the
    plugin reopens a session across a pause, the car may count afresh), so the last peak is
    carried and the new reading added to it; a dip of a few hundredths is the meter's rounding
    and is let be. Facts are never lowered here: fill_charge_facts takes the larger of this and
    the estimate. Returns the session, or None. Idempotent: the same reading writes nothing."""
    row = conn.execute(
        """SELECT id FROM charging_sessions WHERE vin = %s AND session_end_date IS NULL
               AND session_start_date IS NOT NULL AND session_start_date <= %s + interval '2 minutes'
           ORDER BY session_start_date DESC LIMIT 1""",
        (vin, now),
    ).fetchone()
    if row is None:
        return None
    sid = row["id"] if isinstance(row, dict) else row[0]
    conn.execute(METER_SQL, (sid, kwh, kwh, now))
    return sid


# m.last is NULL on a row kept before `last` was; its kwh was the last reading then.
METER_SQL = """
INSERT INTO cc_charge_meter AS m (session_id, kwh, last, carried, at) VALUES (%s, %s, %s, 0, %s)
ON CONFLICT (session_id) DO UPDATE SET
    carried = m.carried + CASE WHEN EXCLUDED.last < COALESCE(m.last, m.kwh) - 0.05 THEN COALESCE(m.last, m.kwh) ELSE 0 END,
    last = EXCLUDED.last,
    kwh = m.carried + CASE WHEN EXCLUDED.last < COALESCE(m.last, m.kwh) - 0.05 THEN COALESCE(m.last, m.kwh) ELSE 0 END + EXCLUDED.last,
    at = EXCLUDED.at
WHERE EXCLUDED.last > COALESCE(m.last, m.kwh) OR EXCLUDED.last < COALESCE(m.last, m.kwh) - 0.05"""


def drop_orphans(conn) -> None:
    """Facts whose session was deleted (a re-import replaces sessions under new ids)."""
    conn.execute("DELETE FROM cc_trip_facts f WHERE NOT EXISTS (SELECT 1 FROM trips t WHERE t.id = f.trip_id)")
    conn.execute("DELETE FROM cc_charge_facts f WHERE NOT EXISTS (SELECT 1 FROM charging_sessions c WHERE c.id = f.session_id)")


# A drive's or charge's stored facts set against what its readings say now, a batch of
# sessions (by id, from `since`) at a time. The same lookups fill_trip_facts and the
# range_added backfill make, so a row that differs was worked out from different readings
# or a different end: before rebuilds dropped the facts of a session whose end moved
# (history.moved_ends), a drive first written mid-drive kept its levels from that moment.
STALE_TRIPS_SQL = """
        WITH page AS (SELECT t.id FROM trips t WHERE t.vin = %s AND t.start_date >= %s AND t.id > %s AND t.destination_date IS NOT NULL ORDER BY t.id LIMIT %s)
        SELECT t.id, t.start_date, t.destination_odometer - t.start_odometer AS miles,
               f.level_start, f.level_end, f.range_start, f.range_end,
               ls.level AS now_level_start, le.level AS now_level_end, rs.range AS now_range_start, re.range AS now_range_end
        FROM page JOIN trips t ON t.id = page.id JOIN cc_trip_facts f ON f.trip_id = t.id
        JOIN drives dv ON dv.vin = t.vin
        LEFT JOIN LATERAL (SELECT r.range FROM drive_ranges r WHERE r.drive_id = dv.id AND r.first_date <= t.start_date ORDER BY r.first_date DESC LIMIT 1) rs ON TRUE
        LEFT JOIN LATERAL (SELECT r.range FROM drive_ranges r WHERE r.drive_id = dv.id AND r.first_date <= t.destination_date ORDER BY r.first_date DESC LIMIT 1) re ON TRUE
        LEFT JOIN LATERAL (SELECT l.level FROM drive_levels l WHERE l.drive_id = dv.id AND l.first_date <= t.start_date ORDER BY l.first_date DESC LIMIT 1) ls ON TRUE
        LEFT JOIN LATERAL (SELECT l.level FROM drive_levels l WHERE l.drive_id = dv.id AND l.first_date <= t.destination_date ORDER BY l.first_date DESC LIMIT 1) le ON TRUE
        ORDER BY t.id"""

STALE_CHARGES_SQL = """
        WITH page AS (SELECT c.id FROM charging_sessions c WHERE c.vin = %s AND c.session_start_date >= %s AND c.id > %s AND c.session_end_date IS NOT NULL ORDER BY c.id LIMIT %s)
        SELECT c.id, c.session_start_date AS start_date, f.range_added,
               (SELECT r.range FROM drive_ranges r WHERE r.drive_id = dv.id AND r.first_date <= c.session_end_date ORDER BY r.first_date DESC LIMIT 1)
             - (SELECT r.range FROM drive_ranges r WHERE r.drive_id = dv.id AND r.first_date <= c.session_start_date ORDER BY r.first_date DESC LIMIT 1) AS now_range_added
        FROM page JOIN charging_sessions c ON c.id = page.id JOIN cc_charge_facts f ON f.session_id = c.id
        JOIN drives dv ON dv.vin = c.vin
        ORDER BY c.id"""

TRIP_ENDS = ("level_start", "level_end", "range_start", "range_end")


def _rows(conn, sql: str, args) -> list[dict]:
    cur = conn.execute(sql, args)
    names = [d.name for d in cur.description]
    return [r if isinstance(r, dict) else dict(zip(names, r)) for r in cur.fetchall()]


def _differs(key: str, stored, now) -> bool:
    # a range not filled yet is the backfill's to fill, not wrong; a sentinel (-999:
    # looked, nothing there) agrees with a lookup that finds nothing
    if stored is None and key.startswith("range"):
        return False
    if stored is not None and stored <= -999:
        stored = None
    return (stored is None) != (now is None) or (stored is not None and abs(stored - now) > 0.005)


def stale_ends(conn, vin: str, since: datetime, batch: int = BATCH) -> tuple[list[dict], list[dict]]:
    """The car's drives and charges from `since` whose stored levels, ranges or range added
    disagree with their readings. Read only; a batch of sessions per statement."""
    out = []
    for sql, keys in ((STALE_TRIPS_SQL, TRIP_ENDS), (STALE_CHARGES_SQL, ("range_added",))):
        found, after = [], 0
        while True:
            rows = _rows(conn, sql, (vin, since, after, batch))
            found += [r for r in rows if any(_differs(k, r[k], r["now_" + k]) for k in keys)]
            if len(rows) < batch:
                break
            after = rows[-1]["id"]
        out.append(found)
    return out[0], out[1]


def repair_ends(conn, vin: str, since: datetime, dry_run: bool = True, batch: int = BATCH) -> tuple[list[dict], list[dict]]:
    """Put right what stale_ends finds. A drive's facts go (and its route line, worked out to
    the same wrong end), for fill_trip_facts and the backfills to work out again; a charge's
    range added is cleared for the range_added backfill. Run it again and it finds nothing."""
    trips, charges = stale_ends(conn, vin, since, batch)
    if dry_run:
        return trips, charges
    for i in range(0, len(trips), batch):
        part = trips[i : i + batch]
        driveedits.forget(conn, vin, [t["id"] for t in part], [t["start_date"] for t in part])
        conn.commit()
    for i in range(0, len(charges), batch):
        conn.execute("UPDATE cc_charge_facts SET range_added = NULL WHERE session_id = ANY(%s)", ([c["id"] for c in charges[i : i + batch]],))
        conn.commit()
    return trips, charges


# A charge the connector's database plugin wrote as two rows: a plug reading arrived while the
# car was charging, the plugin opened a second session on it, and the charge's end went there.
# One Lucid charge read "12:16 AM, Home" with no end beside an end-only row. The first row takes
# the end, the level and the unplugging from the end-only row that opened inside it, once that
# plug-in is over and no other charge started in between. The end-only row stays (it has no
# start, so no list counts it): the plugin's rows are its own, this only fills a blank.
HEAL_SPLIT_SQL = """
        WITH pair AS (
            SELECT a.id, b.session_end_date, b.end_level, b.plug_disconnected_date
            FROM charging_sessions a
            JOIN LATERAL (SELECT b.session_end_date, b.end_level, b.plug_disconnected_date FROM charging_sessions b
                          WHERE b.vin = a.vin AND b.session_start_date IS NULL AND b.session_end_date > a.session_start_date
                            AND b.plug_connected_date >= a.session_start_date AND b.plug_disconnected_date IS NOT NULL
                          ORDER BY b.session_end_date LIMIT 1) b ON TRUE
            WHERE a.vin = %s AND a.session_start_date IS NOT NULL AND a.session_end_date IS NULL
              AND NOT EXISTS (SELECT 1 FROM charging_sessions o WHERE o.vin = a.vin AND o.session_start_date > a.session_start_date AND o.session_start_date < b.session_end_date))
        UPDATE charging_sessions c SET session_end_date = p.session_end_date, end_level = COALESCE(c.end_level, p.end_level),
               plug_disconnected_date = COALESCE(c.plug_disconnected_date, p.plug_disconnected_date)
        FROM pair p WHERE c.id = p.id"""


def heal_split_charges(conn, vin: str) -> int:
    return conn.execute(HEAL_SPLIT_SQL, (vin,)).rowcount


# A charge's energy and cost for "finished charging" (vin, since): only facts worked out after the session
# ended. The connector moves a live session's end as it charges, and facts taken partway are its first kWh.
FINISHED_CHARGE_SQL = """SELECT f.kwh, f.cost FROM charging_sessions s JOIN cc_charge_facts f ON f.session_id = s.id
    WHERE s.vin = %s AND COALESCE(s.session_end_date, s.session_start_date) >= %s AND f.computed_at >= s.session_end_date
    ORDER BY s.id DESC LIMIT 1"""


def ensure_tables(conn) -> None:
    """Run once at startup and by the loaders. Never per tick: an ALTER TABLE queues behind any long
    statement and, while it waits, blocks every reader of the table (the whole site)."""
    conn.execute(DDL)
    conn.execute(billed.DDL)
    conn.execute(driveedits.DDL)


def fill_facts(
    conn,
    vin: str,
    tz,
    is_home,
    dc_price: float | None = None,
    dc_per_min: float | None = None,
    averages=None,
    sc_price: float | None = None,
    sc_per_min: float | None = None,
    place_of=None,
) -> tuple[int, int]:
    drop_orphans(conn)
    trips = fill_trip_facts(conn, vin)
    heal_split_charges(conn, vin)
    fill_charge_regions(conn, vin)
    charges = fill_charge_facts(conn, vin, tz, is_home, dc_price=dc_price, dc_per_min=dc_per_min, averages=averages, sc_price=sc_price, sc_per_min=sc_per_min, place_of=place_of)
    row = conn.execute(
        """SELECT updated_at > now() - interval '1 day' AS all_time,
                  years_updated_at > now() - interval '1 day' AS years,
                  parked_updated_at > now() - interval '1 day' AS parked
           FROM cc_vehicle_facts WHERE vin = %s""",
        (vin,),
    ).fetchone()
    fresh_all, fresh_years, fresh_parked = (tuple(row.values()) if isinstance(row, dict) else tuple(row)) if row else (None, None, None)
    if charges or not fresh_all:
        fill_vehicle_facts(conn, vin)
    # The per-year figures carry their own stamp. They were first written sharing the
    # all-time one, and on any install whose all-time figure was already current — which
    # is every install that has ever run, the moment this ships — the year table was
    # never filled at all. No error, no empty page: every drive simply fell back to the
    # all-time constant, exactly as before, and the change looked like it had worked.
    # A dry run against a restored copy of production is what showed the empty table.
    if charges or not fresh_years:
        fill_vehicle_year_facts(conn, vin, tz)
    # Once a day, and on its own stamp. Thirteen seconds a car, which is affordable
    # daily and would not be per tick — and is only safe at all because the schema lock
    # is let go before any of this runs. If the lock were held through the backfill, every
    # reader of the table (the whole site) would wait on it.
    if not fresh_parked:
        fill_parked_facts(conn, vin)
    conn.commit()
    return trips, charges
