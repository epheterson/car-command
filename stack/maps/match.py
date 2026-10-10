"""A drive's sparse positions to the roads it actually took, on the local Valhalla only.

A car that reports every 15 to 60 s draws its drive as straight lines cutting every corner.
Valhalla's map matcher (trace_attributes, shape_match "map_snap") finds the most likely
road path through those positions. Long drives go in chunks that overlap, each a short call
so the single Valhalla worker is never held for long, and the chunks are joined where their
overlap's middle position was matched. Nothing here ever asks a public service: a drive the
local tiles do not cover, or one Valhalla cannot match, is answered "no match".
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request

MAX_POINTS = 5000  # one drive's positions; a 1 Hz drive never comes here, a 60 s one is far below
CHUNK_POINTS = 100  # per Valhalla call: well under a second of matching each
CHUNK_METERS = 50_000  # Valhalla refuses a trace over 200 km; keep each call far from that
OVERLAP = 6  # positions shared by two chunks; they are joined at the middle one
GPS_ACCURACY_M = 15.0  # a cloud-reported position, not a phone's raw fix
SEARCH_RADIUS_M = 60.0  # how far from a position a road may be and still be a candidate
CALL_TIMEOUT_S = 10.0
BUDGET_S = 45.0  # one drive, all its chunks; the site's client waits longer than this
# The matcher breaks a trace wherever two positions are farther apart than this. Valhalla's
# default (2 km) is a minute at highway speed, exactly the spacing this exists for; the
# setting is not one a request may change, so it is written into the config Valhalla starts with.
BREAKAGE_M = 10_000


def tune_config(config: dict) -> dict:
    """The matcher settings sparse drives need, written into a build's valhalla.json before
    Valhalla starts. Idempotent; builds made before this existed get it at their next start."""
    meili = config.setdefault("meili", {})
    meili.setdefault("default", {})["breakage_distance"] = BREAKAGE_M
    return config


def _meters(a, b) -> float:
    k = 111_320.0
    return math.hypot((a[0] - b[0]) * k, (a[1] - b[1]) * k * math.cos(math.radians((a[0] + b[0]) / 2)))


def clean(points) -> list[list]:
    """[lat, lon] or [lat, lon, epoch seconds] rows, the unusable and the repeats dropped
    (two sources can store one moment twice). Raises ValueError on anything malformed."""
    out: list[list] = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise ValueError("each point is [lat, lon] or [lat, lon, time]")
        try:
            lat, lon = float(p[0]), float(p[1])
            t = float(p[2]) if len(p) > 2 and p[2] is not None else None
        except (TypeError, ValueError) as e:
            raise ValueError("points are numbers") from e
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or math.isnan(lat) or math.isnan(lon):
            raise ValueError("out of range")
        row = [lat, lon] if t is None else [lat, lon, t]
        if out and out[-1][:2] == row[:2]:
            continue  # parked in traffic, or the same reading twice: one position to the matcher
        if out and t is not None and len(out[-1]) > 2 and t <= out[-1][2]:
            continue  # time must move forward or Valhalla refuses the whole trace
        out.append(row)
    return out


def chunks(points: list, size: int = CHUNK_POINTS, overlap: int = OVERLAP, max_m: float = CHUNK_METERS) -> list[tuple[int, int]]:
    """[start, end) index ranges covering every point, each at most `size` points and about
    `max_m` long (never under 2 * overlap + 1 points, so each one moves on), each after the
    first starting `overlap` points before its predecessor ends."""
    n = len(points)
    if n <= 1:
        return [(0, n)] if n else []
    out = []
    start = 0
    while True:
        end, walked = start + 1, 0.0
        while end < n and end - start < size:
            walked += _meters(points[end - 1], points[end])
            if walked > max_m and end - start > overlap * 2:
                break
            end += 1
        out.append((start, end))
        if end >= n:
            return out
        start = max(end - overlap, start + 1)


def body(points: list) -> dict:
    shape = []
    for p in points:
        pt = {"lat": p[0], "lon": p[1]}
        if len(p) > 2:
            pt["time"] = int(p[2])
        shape.append(pt)
    return {
        "shape": shape,
        "costing": "auto",
        "shape_match": "map_snap",
        "use_timestamps": len(points[0]) > 2,
        "trace_options": {"gps_accuracy": GPS_ACCURACY_M, "search_radius": SEARCH_RADIUS_M},
        "filters": {
            "attributes": ["shape", "edge.length", "edge.begin_shape_index", "edge.end_shape_index", "matched.point", "matched.type", "matched.edge_index", "matched.distance_from_trace_point"],
            "action": "include",
        },
    }


def decode(encoded: str, precision: int = 6) -> list[list[float]]:
    """A Google encoded polyline (Valhalla writes precision 6) to [lat, lon] points."""
    out, i, lat, lon, k = [], 0, 0, 0, 10.0**precision
    while i < len(encoded):
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(encoded[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat += vals[0]
        lon += vals[1]
        out.append([lat / k, lon / k])
    return out


def parse(resp: dict) -> dict:
    """Valhalla's answer for one chunk: the line, its length, and each position's match."""
    shape = decode(resp.get("shape") or "")
    edges = resp.get("edges") or []
    matched = []
    for m in resp.get("matched_points") or []:
        ei = m.get("edge_index")
        e = edges[ei] if isinstance(ei, int) and 0 <= ei < len(edges) else None
        matched.append(
            {
                "type": m.get("type"),
                "at": [m.get("lat"), m.get("lon")],
                "off_m": m.get("distance_from_trace_point"),
                "span": (e.get("begin_shape_index", 0), e.get("end_shape_index", len(shape) - 1)) if e else None,
            }
        )
    length_km = sum(e.get("length") or 0 for e in edges)
    return {"shape": shape, "matched": matched, "length_m": length_km * 1000.0}


def _project(a, b, p):
    """The point on segment ab nearest p, and its distance, in a flat local frame."""
    k = math.cos(math.radians(p[0]))
    ax, ay, bx, by, px, py = a[1] * k, a[0], b[1] * k, b[0], p[1] * k, p[0]
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    q = [ay + t * dy, (ax + t * dx) / k]
    return q, _meters(q, p)


def _cut(shape: list, m: dict) -> tuple[int, list]:
    """Where on `shape` a matched position sits: (segment index, the point on it). Searched
    only along the edge it was matched to when that is known, so a drive that passes the same
    corner twice is cut at the right pass."""
    lo, hi = m["span"] if m.get("span") else (0, len(shape) - 1)
    lo, hi = max(0, min(lo, len(shape) - 2)), max(1, min(hi, len(shape) - 1))
    best = (0, shape[0], float("inf"))
    for i in range(lo, hi):
        q, d = _project(shape[i], shape[i + 1], m["at"])
        if d < best[2]:
            best = (i, q, d)
    return best[0], best[1]


def stitch(parts: list[dict], ranges: list[tuple[int, int]]) -> list[list[float]]:
    """One line from the chunks' lines, each pair joined at the middle of their overlap."""
    line = list(parts[0]["shape"])
    base = 0  # line[base + j] is the current chunk's shape[j], for the part of it still kept
    for k in range(1, len(parts)):
        prev_start, prev_end = ranges[k - 1]
        start, _end = ranges[k]
        join = start + (prev_end - start) // 2  # the overlap's middle position
        a = parts[k - 1]["matched"][join - prev_start] if join - prev_start < len(parts[k - 1]["matched"]) else None
        b = parts[k]["matched"][join - start] if join - start < len(parts[k]["matched"]) else None
        nxt = parts[k]["shape"]
        if not a or not b or "unmatched" in (a.get("type"), b.get("type")) or None in a["at"] or None in b["at"]:
            base = len(line)  # no shared position to join at: end to end
            line.extend(nxt)
            continue
        ia, qa = _cut(parts[k - 1]["shape"], a)
        ib, qb = _cut(nxt, b)
        line = line[: base + ia + 1] + [qa]
        if _meters(qa, qb) > 1:
            line.append(qb)
        base = len(line) - (ib + 1)
        line.extend(nxt[ib + 1 :])
    out = []
    for p in line:
        if not out or _meters(out[-1], p) > 0.5:
            out.append([round(p[0], 6), round(p[1], 6)])
    return out


def score(parts: list[dict]) -> dict:
    """How well the positions matched: the share Valhalla placed on a road it chose (not
    'unmatched'), and the median distance each moved to get there."""
    ms = [m for p in parts for m in p["matched"]]
    if not ms:
        return {"confidence": 0.0, "median_off_m": None}
    placed = [m for m in ms if m.get("type") != "unmatched"]
    offs = sorted(m["off_m"] for m in placed if isinstance(m.get("off_m"), (int, float)))
    return {"confidence": round(len(placed) / len(ms), 3), "median_off_m": round(offs[len(offs) // 2], 1) if offs else None}


class Unavailable(Exception):
    """Valhalla is down, slow or busy: worth asking again later, unlike a drive it cannot match."""


class NoMatch(Exception):
    """Valhalla answered and could not match: outside the tiles, or no road path fits."""


def call_valhalla(base: str, payload: dict, timeout: float = CALL_TIMEOUT_S) -> dict:
    req = urllib.request.Request(f"{base}/trace_attributes", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            try:
                detail = json.loads(e.read() or b"{}")
            except ValueError:
                detail = {}
            raise NoMatch(_reason(detail)) from e
        raise Unavailable(f"valhalla {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise Unavailable(type(e).__name__) from e


def _reason(detail: dict) -> str:
    """Valhalla's error in a few words: whether the drive is off the tiles or unmatched."""
    code = detail.get("error_code")
    if code in (170, 171, 172):
        return "outside the built regions"
    if code in (442, 443, 444):
        return "no road path fits"
    return f"valhalla error {code}" if code else "valhalla refused it"


def match(points, call, budget_s: float = BUDGET_S, clock=time.monotonic) -> dict:
    """The matched line for one drive, or a "no match" with why. `call(payload, timeout)` is
    one Valhalla trace_attributes call; it raises Unavailable or NoMatch. The whole drive
    gets `budget_s`: past it the drive is "too long" (a no match, not retried on this
    build), so no one drive can hold the single Valhalla worker for longer."""
    pts = clean(points)
    if len(pts) < 2:
        return {"result": "no match", "reason": "fewer than two positions"}
    ranges = chunks(pts)
    parts = []
    deadline = clock() + budget_s
    for a, b in ranges:
        if b - a < 2:
            continue
        left = deadline - clock()
        if left <= 0:
            return {"result": "no match", "reason": "too long"}
        timeout = min(CALL_TIMEOUT_S, left)
        try:
            parts.append(parse(call(body(pts[a:b]), timeout)))
        except NoMatch as e:
            return {"result": "no match", "reason": str(e)}
        except Unavailable:
            if timeout < CALL_TIMEOUT_S or clock() >= deadline:
                return {"result": "no match", "reason": "too long"}
            raise
        if len(parts[-1]["shape"]) < 2:
            return {"result": "no match", "reason": "valhalla returned no line"}
    line = stitch(parts, [r for r in ranges if r[1] - r[0] >= 2])
    length = sum(_meters(p, q) for p, q in zip(line, line[1:]))
    return {"result": "matched", "points": line, "length_m": round(length, 1), "chunks": len(parts), **score(parts)}
