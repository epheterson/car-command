"""The records board: the longest, fastest, coldest, highest drive and the most powerful
charge of a period, each one the real drive or charge rather than a number.

Pure. The drives come in as the Stats page already has them (drives_light: the facts,
kWh worked out), the charges as (id, peak kW) pairs; out comes one pick a record, in the
board's order, each with the row it belongs to. A record nothing in the period can answer
(no elevation logged, no charge with a power reading) is left off, never shown as zero."""

from __future__ import annotations

# Wh per mile from rated range is only as fine as one rated mile: about 250 Wh, which on a
# three-mile hop to the shop is 80 Wh/mi either way. The efficiency records count drives
# of at least this many miles; when fewer than EFF_MIN_DRIVES are that long (a month of
# short commutes), the line steps down once to EFF_FALLBACK_MI, and says so.
EFF_MIN_MI = 10.0
EFF_FALLBACK_MI = 5.0
EFF_MIN_DRIVES = 3

FT_PER_M = 3.28084

# A drive under this share of its car's median Wh per mile in the period used less battery
# than its miles allow: its end level was read before the drive was over, not a record.
# One 115-mile drive stored 91 to 82 % (really 40 %) and held Most efficient at 47 Wh/mi.
EFF_PLAUSIBLE = 0.4


def _battery(d: dict) -> float | None:
    a, b = d.get("level_start"), d.get("level_end")
    return (a - b) if a is not None and b is not None and a > b else None


def _hours(d: dict) -> float | None:
    t = d.get("duration")
    return t.total_seconds() / 3600 if t is not None and t.total_seconds() > 0 else None


def _wh(d: dict) -> float | None:
    kwh, mi = d.get("kwh_used"), d.get("miles")
    return kwh * 1000 / mi if kwh and mi and mi > 0 else None


# The outside sensor's "no reading" is -40, the one value where °F and °C agree; a drive
# that started on it is not the coldest drive.
NO_READING_F = -39.5


def _temp(d: dict) -> float | None:
    t = d.get("temp_f")
    return t if t is not None and t > NO_READING_F else None


def _ft(key: str):
    return lambda d: d[key] * FT_PER_M if d.get(key) is not None else None


# key, label, how a drive answers it, highest (True) or lowest wins, what the value is
DRIVE_RECORDS = (
    ("distance", "Longest drive", lambda d: d.get("miles"), True, "dist"),
    ("battery", "Most battery in one drive", _battery, True, "pct"),
    ("time", "Longest time driving", _hours, True, "hours"),
    ("speed", "Top speed", lambda d: d.get("max_mph"), True, "speed"),
    ("lowest", "Lowest battery", lambda d: d.get("level_end"), False, "low"),
    ("cold", "Coldest drive", _temp, False, "temp"),
    ("hot", "Hottest drive", _temp, True, "temp"),
    ("high", "Highest point", _ft("elev_max_m"), True, "elev"),
    ("low", "Lowest point", _ft("elev_min_m"), False, "elev"),
)


def _pick(rows: list[dict], value, highest: bool) -> tuple[dict, float] | None:
    """The row with the highest (or lowest) value; on a tie the earlier one, which set it first."""
    best, best_s, best_v = None, None, None
    for r in rows:
        v = value(r)
        if v is None:
            continue
        s = v if highest else -v
        if best is None or s > best_s or (s == best_s and r["start_date"] < best["start_date"]):
            best, best_s, best_v = r, s, v
    return (best, best_v) if best is not None else None


def plausible(drives: list[dict]) -> list[dict]:
    """The drives whose Wh per mile is at least EFF_PLAUSIBLE of their own car's median."""
    by_car: dict = {}
    for d in drives:
        w = _wh(d)
        if w is not None:
            by_car.setdefault(d.get("vin"), []).append(w)
    floor = {vin: sorted(ws)[len(ws) // 2] * EFF_PLAUSIBLE for vin, ws in by_car.items()}
    return [d for d in drives if _wh(d) is not None and _wh(d) >= floor[d.get("vin")]]


def efficiency_floor(drives: list[dict]) -> float | None:
    """The shortest drive, in miles, the efficiency records count: EFF_MIN_MI, or
    EFF_FALLBACK_MI when too few are that long, or None when too few are even that."""
    for floor in (EFF_MIN_MI, EFF_FALLBACK_MI):
        if sum(1 for d in drives if (d.get("miles") or 0) >= floor and _wh(d) is not None) >= EFF_MIN_DRIVES:
            return floor
    return None


def board(drives: list[dict], peak_charge: tuple[dict, float] | None = None, big_charge: tuple[dict, float] | None = None) -> list[dict]:
    """[{key, label, value, fmt, kind, row, note}] in the board's order. `drives` are the
    period's finished drives; `peak_charge` is (charge row, kW) for the period's most
    powerful charge and `big_charge` (charge row, kWh) for the one that added the most, or
    None. `note` qualifies a record ("drives of 10 mi or more")."""
    out = []
    for key, label, value, highest, fmt in DRIVE_RECORDS:
        got = _pick(drives, value, highest)
        if got:
            out.append({"key": key, "label": label, "value": got[1], "fmt": fmt, "kind": "drive", "row": got[0], "note": None})
    for key, label, fmt, got in (("peak_kw", "Fastest charge", "kw", peak_charge), ("most_kwh", "Most energy in one charge", "kwh", big_charge)):
        if got and got[1]:
            out.append({"key": key, "label": label, "value": got[1], "fmt": fmt, "kind": "charge", "row": got[0], "note": None})
    floor = efficiency_floor(drives)
    if floor is not None:
        long = plausible([d for d in drives if (d.get("miles") or 0) >= floor])
        for key, label, highest in (("best_wh", "Most efficient", False), ("worst_wh", "Least efficient", True)):
            got = _pick(long, _wh, highest)
            if got:
                out.append({"key": key, "label": label, "value": got[1], "fmt": "econ", "kind": "drive", "row": got[0], "note": floor})
    return out


# The records that lead the board, large, in this order; the rest are one line each.
LEADS = ("distance", "speed", "peak_kw")


def fold(board: list[dict]) -> list[dict]:
    """The board with each drive or charge once, holding every record it set: [{kind, row,
    titles (the board's entries, in order), keys, lead}]. Leads first (the groups holding a
    LEADS record, in that order, at most three), then the rest in the board's order."""
    groups: dict[tuple, dict] = {}
    for b in board:
        g = groups.setdefault((b["kind"], b["row"]["id"]), {"kind": b["kind"], "row": b["row"], "titles": [], "keys": set()})
        g["titles"].append(b)
        g["keys"].add(b["key"])
    order = list(groups.values())
    leads = []
    for k in LEADS:
        g = next((g for g in order if k in g["keys"]), None)
        if g is not None and g not in leads:
            leads.append(g)
    for g in order:
        g["lead"] = g in leads
        # a lead's own record goes first on its card
        if g["lead"]:
            g["titles"].sort(key=lambda t: LEADS.index(t["key"]) if t["key"] in LEADS else len(LEADS))
    return leads + [g for g in order if g not in leads]
