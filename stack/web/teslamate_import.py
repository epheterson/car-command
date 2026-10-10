"""TeslaMate → Car Command: the weeks a car was logged by TeslaMate (a
Lucid through a bridge into TeslaMate, for one) as samples the TeslaFi replay
already understands. TeslaMate stores naive UTC, and through the bridge the
units are raw km and °C; the database plugin stores miles and °F.

Six CSV dumps from TeslaMate's Postgres (see `make import-teslamate`):
positions, drives, charging_processes, charges, states, updates. Each row
becomes one sample; drives and charging processes supply the session ids;
state rows contribute a sample at each end so a sleep spans its interval.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from teslafi_import import _clean, _f, _i, replay

KM_TO_MI = 0.621371


def _t(v) -> datetime | None:
    """TeslaMate stores naive UTC; a CSV gives the text, Postgres the datetime. Both become aware UTC."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = _clean(v)
    if not s:
        return None
    return datetime.fromisoformat(s.replace(" ", "T")).replace(tzinfo=timezone.utc)


def _c_to_f(c):
    return None if c is None else c * 9 / 5 + 32


def _km(v):
    return None if v is None else v * KM_TO_MI


def _rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def blank(t: datetime, vin: str, **kw) -> dict:
    s = {
        "time": t,
        "vin": vin,
        "name": None,
        "model": None,
        "software": None,
        "odometer": None,
        "level": None,
        "range": None,
        "outside_f": None,
        "driving": False,
        "asleep": False,
        "charging": False,
        "charging_state": None,
        "power": 0.0,
        "energy_added": None,
        "fast_charger": False,
        "supercharger": False,
        "latitude": None,
        "longitude": None,
        "heading": None,
        "speed": None,
        "elevation": None,
        "inside_f": None,
        "sentry_raw": "",
        "climate_raw": "",
        "fan_on": False,
        "volts": None,
        "amps": None,
        "drive_no": 0,
        "charge_no": 0,
        "blank": False,
    }
    s.update(kw)
    return s


def tables_from_dir(d: Path) -> dict[str, list[dict]]:
    """The six CSV dumps as {table: rows}; a missing file is an empty table."""
    out = {}
    for t in ("states", "positions", "charges", "charging_processes", "updates", "drives"):
        f = d / f"{t}.csv"
        out[t] = _rows(f) if f.exists() else []
    return out


def samples_from_dir(d: Path, vin: str, name: str) -> list[dict]:
    return samples_from_tables(tables_from_dir(d), vin, name)


def samples_from_tables(tables: dict[str, list[dict]], vin: str, name: str) -> list[dict]:
    """Samples from TeslaMate rows, whether they came through a CSV dump (strings) or
    straight from its Postgres (native types): the converters take either."""
    out: list[dict] = []
    for r in tables.get("states", []):
        a, b = _t(r.get("start_date")), _t(r.get("end_date"))
        asleep = (_clean(r.get("state")) or "").lower() in ("asleep", "offline")
        if a:
            out.append(blank(a, vin, asleep=asleep))
        if b:
            out.append(blank(b, vin, asleep=asleep))
    for r in tables.get("positions", []):
        t = _t(r.get("date"))
        if not t:
            continue
        out.append(
            blank(
                t,
                vin,
                odometer=_km(_f(r.get("odometer"))),
                level=_f(r.get("battery_level")),
                range=_km(_f(r.get("rated_battery_range_km"))),
                outside_f=_c_to_f(_f(r.get("outside_temp"))),
                driving=bool(_i(r.get("drive_id"))),
                drive_no=_i(r.get("drive_id")) or 0,
                latitude=_f(r.get("latitude")),
                longitude=_f(r.get("longitude")),
                speed=_km(_f(r.get("speed"))),
                elevation=_f(r.get("elevation")),
                inside_f=_c_to_f(_f(r.get("inside_temp"))),
                climate_raw="true" if (_clean(r.get("is_climate_on")) or "").lower() in ("t", "true", "1") else "false",
            )
        )
    for r in tables.get("charges", []):
        t = _t(r.get("date"))
        if not t:
            continue
        power = _f(r.get("charger_power")) or 0.0
        out.append(
            blank(
                t,
                vin,
                charging=True,
                charging_state="Charging",
                power=power,
                level=_f(r.get("battery_level")),
                range=_km(_f(r.get("rated_battery_range_km"))),
                outside_f=_c_to_f(_f(r.get("outside_temp"))),
                energy_added=_f(r.get("charge_energy_added")),
                charge_no=_i(r.get("charging_process_id")) or 0,
                volts=_f(r.get("charger_voltage")),
                amps=_f(r.get("charger_actual_current")),
                fast_charger=(_clean(r.get("fast_charger_present")) or "").lower() in ("t", "true", "1"),
                supercharger="Tesla" in (_clean(r.get("fast_charger_type")), _clean(r.get("fast_charger_brand"))),
            )
        )
    for r in tables.get("charging_processes", []):
        # the session's place, from its position row, rides on the first charge sample of the process
        pass
    if out:
        out[0]["name"] = name
    out.sort(key=lambda s: s["time"])
    # a software update rides on the first sample at or after it: a sample of its own would
    # assert a vehicle state (PARKED) at a moment nothing else was observed
    from bisect import bisect_left

    times = [s["time"] for s in out]
    for r in tables.get("updates", []):
        t, v = _t(r.get("start_date")), _clean(r.get("version"))
        if t and v and out:
            out[min(bisect_left(times, t), len(out) - 1)]["software"] = v
    return out


def attach_charge_positions(samples: list[dict], d) -> None:
    """Charge rows carry no coordinates; take them from the process's position row.
    `d` is a directory of CSVs or a dict of tables; ids are compared as strings, since a
    CSV gives "12" where Postgres gives 12."""
    tables = d if isinstance(d, dict) else tables_from_dir(d)
    pos = {str(r["id"]): r for r in tables.get("positions", [])}
    procs = {str(r["id"]): r for r in tables.get("charging_processes", [])}
    for s in samples:
        if s["charge_no"] and s["latitude"] is None:
            p = pos.get(str((procs.get(str(s["charge_no"])) or {}).get("position_id", "")))
            if p:
                s["latitude"], s["longitude"] = _f(p.get("latitude")), _f(p.get("longitude"))
                if s["odometer"] is None:
                    s["odometer"] = _km(_f(p.get("odometer")))


def replay_dir(d: Path, vin: str, name: str) -> dict:
    return replay_tables(tables_from_dir(d), vin, name)


def replay_tables(tables: dict[str, list[dict]], vin: str, name: str) -> dict:
    samples = samples_from_tables(tables, vin, name)
    attach_charge_positions(samples, tables)
    out = replay(samples)
    out["vehicle"] = {"vin": vin, "name": name}
    return out
