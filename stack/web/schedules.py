"""A Tesla's charging and departure schedules: which API a car's firmware takes, what a
vehicle_data answer says of them, and the lines Now says.

Pure: dicts in, dicts and strings out. Coordinates in an answer (where each schedule applies, which
is the owner's home) are dropped here, so nothing that is kept holds one."""

from __future__ import annotations

import re

import clockfmt
from placeicons import icon_for_name
from places import distance_m, is_home

NEAR_M = 150  # a schedule within this of a place is at that place

FIRST_MODERN = (2024, 26)  # from here Tesla's add_charge_schedule and add_precondition_schedule replace the legacy pair


def modern(version: str | None) -> bool:
    """Whether the car takes the current schedule API. A version that cannot be read counts as
    current: the cars this serves are kept up to date."""
    m = re.match(r"^\s*(\d{4})\.(\d+)", str(version or ""))
    return True if not m else (int(m.group(1)), int(m.group(2))) >= FIRST_MODERN


def _minutes(v) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1439 else None


def _point(v) -> tuple[float, float] | None:
    """(lat, lon) from a {"latitude", "longitude"} (or lat, lon) dict; None for anything else, and for 0, 0, which is no place."""
    if not isinstance(v, dict):
        return None
    lat, lon = (v.get(a) if v.get(a) is not None else v.get(b) for a, b in (("latitude", "lat"), ("longitude", "lon")))
    ok = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (lat, lon))
    return (float(lat), float(lon)) if ok and (lat or lon) and abs(lat) <= 90 and abs(lon) <= 180 else None


def parse(body: dict | None, locate=None) -> dict:
    """What a vehicle_data answer says: {"firmware", "tesla", "charge": [{start, end, at}], "precondition": [{time, at}]},
    enabled schedules only. `at` is the NAME of the place a schedule applies to (Tesla's Home or Work, else
    whatever `locate(lat, lon)` names, else "elsewhere"): the coordinates of a schedule are looked up and dropped.
    "tesla" is the car's own Home and Work, {"Home": (lat, lon)}, when charge_state carries them: the only
    positions kept, in memory, to send a new schedule to the place the car calls home."""
    r = (body or {}).get("response") or {}
    out: dict = {"firmware": None, "tesla": {}, "charge": [], "precondition": []}
    version = (r.get("vehicle_state") or {}).get("car_version")
    if isinstance(version, str) and version.strip():
        out["firmware"] = version.split(" ")[0]
    state = r.get("charge_state") or {}
    for name, key in (("Home", "home_location"), ("Work", "work_location")):
        if _point(state.get(key)):
            out["tesla"][name] = _point(state.get(key))

    def where(s) -> str | None:
        p = _point({"latitude": s.get("latitude"), "longitude": s.get("longitude")})
        if p is None:
            return None
        for name, q in out["tesla"].items():
            if distance_m(*p, *q) <= NEAR_M:
                return name
        return (locate(*p) if locate else None) or "elsewhere"

    for s in (r.get("charge_schedule_data") or {}).get("charge_schedules") or []:
        if isinstance(s, dict) and s.get("enabled"):
            start = _minutes(s.get("start_time")) if s.get("start_enabled") else None
            end = _minutes(s.get("end_time")) if s.get("end_enabled") else None
            if start is not None or end is not None:
                out["charge"].append({"start": start, "end": end, "at": where(s)})
    for s in (r.get("preconditioning_schedule_data") or {}).get("precondition_schedules") or []:
        if isinstance(s, dict) and s.get("enabled") and _minutes(s.get("precondition_time")) is not None:
            out["precondition"].append({"time": _minutes(s["precondition_time"]), "at": where(s)})
    return out


def _tod(m: int) -> str:
    return clockfmt.tod(f"{m // 60}:{m % 60:02d}")


def _at(name: str | None) -> str:
    return f" ({name})" if name else ""


def lines(read: dict | None, stream: dict | None = None) -> list[str]:
    """What Now says of the schedule, one short phrase each, with the place it applies to by name. The car's
    own schedules from vehicle_data when they were read; else the legacy ones the stream carries."""
    out = []
    if read and (read["charge"] or read["precondition"]):
        for c in read["charge"]:
            if c["start"] is not None:
                out.append(f"charges at {_tod(c['start'])}{_at(c['at'])}")
            if c["end"] is not None:
                out.append(f"charged by {_tod(c['end'])}{_at(c['at'])}")
        out += [f"warm by {_tod(p['time'])}{_at(p['at'])}" for p in read["precondition"]]
        return out
    st = stream or {}
    if st.get("sched_mode") == "StartAt" and st.get("sched_charge_min") is not None:
        out.append(f"charges at {_tod(st['sched_charge_min'])}")
    if st.get("sched_mode") == "DepartBy" and st.get("sched_depart_min") is not None:
        out.append(f"leave by {_tod(st['sched_depart_min'])}")
        if st.get("preconditioning"):
            out.append("warms up")
    return out


_HEAD = re.compile(r"^(charges at|charges from|charged by|leave by|warm by|charges) (.+?)(?: \((.+)\))?$")
_HEAD_WORDS = {"charges at": "starts charging", "charges from": "starts charging", "charged by": "charged by", "leave by": "leave by", "warm by": "warm by", "charges": "starts charging"}


def head(lines: list[str] | None) -> tuple[str, str]:
    """A schedule's tile: the time alone in bold, what happens then and where under it, then the rest. A tile
    holds about twelve characters of bold, so "charges at 12:00 AM" read "charges at 12:…"."""
    if not lines:
        return "", ""
    m = _HEAD.match(lines[0])
    if not m:
        return lines[0][:1].upper() + lines[0][1:], " · ".join(lines[1:])
    verb, when, where = m.groups()
    word = _HEAD_WORDS[verb]
    if verb == "charges" and " to " in when:  # a window: the start in bold, the end under it
        when, end = when.split(" to ", 1)
        word = f"charges until {end}"
    said = word + (f" at {where}" if where else "")
    return when, " · ".join([said] + lines[1:])


def lucid_lines(on, start: str | None, end: str | None, waiting: str | None = None) -> list[str]:
    """What Now says of a Lucid's charging schedule, from the connector's `charging_schedule_*` and
    `scheduled_charge`: one window with no place. Empty while the connector has not published one
    (an older connector, or the car has never held a schedule)."""
    if on is None or (start is None and end is None):
        return []

    def tod(clock: str) -> str:
        return clockfmt.tod(clock) if re.match(r"^\d{1,2}:\d{2}$", clock or "") else str(clock)

    if not on:
        return ["off"]
    window = " to ".join(tod(x) for x in (start, end) if x)
    return [f"charges {window}" if start and end else f"charges from {tod(start)}" if start else f"charged by {tod(end)}"] + (["waiting for it"] if waiting == "scheduled" else [])


# ---- where a new schedule applies --------------------------------------------------------------------------


def is_work(p: dict) -> bool:
    icon = p.get("icon")
    return icon == "work" if icon is not None else (not p.get("auto") and icon_for_name(p.get("name")) == "work")


def choices(read: dict | None, rows: list[dict], here: tuple[float, float] | None) -> list[dict]:
    """The places a schedule may apply to, in the order they are offered: [{id, name, group}].
    Tesla's own Home and Work (when the car says them), Car Command's Home and Work places, where the car is
    now, then every other named place. An id names a place and never carries a position."""
    out, tesla = [], (read or {}).get("tesla") or {}
    for name, p in tesla.items():
        out.append({"id": f"tesla:{name.lower()}", "name": name, "group": "From the car"})
    live = [r for r in rows if not r.get("merged_into") and r.get("latitude") is not None and r.get("longitude") is not None]
    # a place of ours that is the very spot the car calls Home or Work is the same choice
    same = lambda r: any(distance_m(r["latitude"], r["longitude"], *q) <= NEAR_M for q in tesla.values())
    tagged = [r for r in live if (is_home(r) or is_work(r)) and not same(r)]
    out += [{"id": f"place:{r['id']}", "name": r["name"], "group": "Car Command"} for r in sorted(tagged, key=lambda r: (not is_home(r), str(r["name"])))]
    if here:
        out.append({"id": "car", "name": "Where the car is now", "group": "Here"})
    rest = [r for r in live if r not in tagged and not same(r) and not r.get("auto")]
    out += [{"id": f"place:{r['id']}", "name": r["name"], "group": "Other places"} for r in sorted(rest, key=lambda r: str(r["name"]))]
    return out


def default(options: list[dict]) -> str | None:
    """Tesla's Home when the car has one, else where the car is, else none: a schedule with no pick and no default is refused."""
    ids = [o["id"] for o in options]
    return "tesla:home" if "tesla:home" in ids else "car" if "car" in ids else None


def resolve(choice: str | None, read: dict | None, rows: list[dict], here: tuple[float, float] | None) -> tuple[float, float] | None:
    """The position for a choice made from `choices`, looked up here and never taken from the request."""
    if choice == "car":
        return here
    kind, _, name = str(choice or "").partition(":")
    if kind == "tesla":
        return ((read or {}).get("tesla") or {}).get(name.capitalize())
    if kind == "place":
        for r in rows:
            if str(r.get("id")) == name and not r.get("merged_into") and r.get("latitude") is not None:
                return (float(r["latitude"]), float(r["longitude"]))
    return None
