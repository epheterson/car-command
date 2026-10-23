"""Ground height from a terrain model kept on this box.

The model is Terrain Tiles (Mapzen/Tilezen) on AWS Open Data: a blend of SRTM, USGS 3DEP,
EU-DEM and other national models, whose attributions are listed in the README credits. It is
fetched as one-degree "skadi" tiles and shrunk at download to a 3-arc-second grid (about 90 m)
in a file of plain little-endian int16s, so a lookup is a four-value bilinear read from
memory: no GDAL, no server, nothing about a drive ever leaves. Only a tile's name (the
one-degree square, about 100 km) is requested, and AWS sees those names.

A tile file is `N37W122.cct`: b"CCT1", the samples per side (uint16), a spare uint16, then
n*n int16 rows from the north edge down, columns west to east, the first and last row and
column sitting on the tile's own edges so neighbouring tiles agree at the seam. -32768 is a
void, and so is anything under -450 m (the archive carries sea-floor depths, which no road has).
A square the archive does not have (a 404) is `N37W122.none`, empty, so it is asked for once."""

from __future__ import annotations

import gzip
import http.client
import json
import math
import errno
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from array import array
from pathlib import Path

MAGIC = b"CCT1"
HEADER = 8
VOID = -32768
FACTOR = 3  # the archive's 1" (3601 per side) averaged down to 3" (1201): 2.9 MB a tile instead of 26
FLOOR_M = -450  # the Dead Sea shore is the lowest road on Earth; anything under is sea floor or a bad cell
SOURCE = "https://elevation-tiles-prod.s3.amazonaws.com/skadi/{band}/{name}.hgt.gz"
TILE_MB = 10  # what one land tile weighs to download; coast and mountain tiles run from 1 to 14
STORED_MB = 3  # and on disk after FACTOR
FETCH_TIMEOUT_S = 60  # per socket read
FETCH_DEADLINE_S = 300  # for the whole tile
MIN_FIXES = 3  # readings in a square before it is worth downloading
BACKOFF_S = (3600, 6 * 3600, 24 * 3600)  # after the first, second and third failure; then it is given up


def tile_name(lat: float, lon: float) -> str:
    """'N37W122' for any point in the square whose south-west corner is 37 N, 122 W."""
    la, lo = math.floor(lat), math.floor(lon)
    return f"{'N' if la >= 0 else 'S'}{abs(la):02d}{'E' if lo >= 0 else 'W'}{abs(lo):03d}"


def tile_corner(name: str) -> tuple[int, int]:
    """(south, west) degrees of a tile name."""
    la, lo = int(name[1:3]), int(name[4:7])
    return (la if name[0] == "N" else -la), (lo if name[3] == "E" else -lo)


def tiles_for(points, k: int = 1) -> set[str]:
    """The tile names holding at least k of the (lat, lon, ...) points, None coordinates skipped;
    k keeps one stray fix from asking for a square nobody has driven in."""
    counts: dict[str, int] = {}
    for p in points:
        if p[0] is not None and p[1] is not None and -90 <= p[0] < 90 and -180 <= p[1] < 180:
            n = tile_name(p[0], p[1])
            counts[n] = counts.get(n, 0) + 1
    return {n for n, c in counts.items() if c >= k}


def convert(raw: bytes, factor: int | None = None) -> bytes:
    """A skadi .hgt (big-endian int16, 3601 per side; 1201 is kept as it is) as a tile file, each
    output sample the mean of the factor x factor block of readings centred on it, voids and
    sea-floor depths left out of the mean (and a void only where the whole block is void)."""
    side = math.isqrt(len(raw) // 2)
    if factor is None:
        factor = {3601: FACTOR, 1201: 1}.get(side) or 0
        if not factor:
            raise ValueError("not a 1 or 3 arc-second tile")
    if side * side * 2 != len(raw) or side < factor + 1 or (side - 1) % factor:
        raise ValueError("not an SRTM tile")
    src = array("h")
    src.frombytes(raw)
    if sys.byteorder == "little":
        src.byteswap()
    n = (side - 1) // factor + 1
    half = factor // 2
    out = array("h")
    cols = [(max(0, j * factor - half), min(side, j * factor + half + 1)) for j in range(n)]
    for i in range(n):
        r0, r1 = max(0, i * factor - half), min(side, i * factor + half + 1)
        # one value per column: the sum and count of the block's rows, voids skipped
        total = [0] * side
        count = [0] * side
        for r in range(r0, r1):
            row = src[r * side : (r + 1) * side]
            for c, v in enumerate(row):
                if v >= FLOOR_M:
                    total[c] += v
                    count[c] += 1
        for c0, c1 in cols:
            k = sum(count[c0:c1])
            out.append(round(sum(total[c0:c1]) / k) if k else VOID)
    if sys.byteorder == "big":
        out.byteswap()
    return MAGIC + struct.pack("<HH", n, 0) + out.tobytes()


class Tile:
    def __init__(self, data: bytes) -> None:
        if data[:4] != MAGIC:
            raise ValueError("not a terrain tile")
        self.n = struct.unpack_from("<H", data, 4)[0]
        if self.n < 2 or len(data) != HEADER + 2 * self.n * self.n:
            raise ValueError("a tile of the wrong size")
        self.cells = array("h")
        self.cells.frombytes(data[HEADER : HEADER + self.n * self.n * 2])
        if sys.byteorder == "big":
            self.cells.byteswap()

    def height(self, fx: float, fy: float) -> float | None:
        """Bilinear height at fx, fy in 0..1 from the west and south edges, None over a void."""
        n = self.n
        x, y = fx * (n - 1), (1 - fy) * (n - 1)
        i, j = min(int(y), n - 2), min(int(x), n - 2)
        dy, dx = y - i, x - j
        c = self.cells
        a, b, d, e = c[i * n + j], c[i * n + j + 1], c[(i + 1) * n + j], c[(i + 1) * n + j + 1]
        if VOID in (a, b, d, e):
            known = [v for v in (a, b, d, e) if v != VOID]
            return sum(known) / len(known) if known else None
        return (a * (1 - dx) + b * dx) * (1 - dy) + (d * (1 - dx) + e * dx) * dy


class Terrain:
    """The downloaded tiles in a folder, read on demand with the most recent few kept in memory."""

    def __init__(self, folder, keep: int = 6) -> None:
        self.folder = Path(folder) if folder else None
        self.keep = keep
        self._tiles: dict[str, Tile | None] = {}
        self._lock = threading.Lock()  # pages, the facts backfill and the downloader all read
        self._have: set[str] | None = None  # tiles on disk, listed once and again only after forget()

    def names(self) -> set[str]:
        """Tiles on disk (land) and squares known to be sea."""
        if not self.folder or not self.folder.is_dir():
            return set()
        return {p.stem for p in self.folder.iterdir() if p.suffix in (".cct", ".none")}

    def have(self) -> set[str]:
        """Tiles on disk. Listed once, then kept until forget(): pages ask often."""
        have = self._have
        if have is None:
            have = {p.stem for p in self.folder.glob("*.cct")} if self.folder and self.folder.is_dir() else set()
            self._have = have
        return have

    def warm(self, names) -> None:
        """Read the named tiles into memory, from the worker, so a page finds them there."""
        for n in list(names)[: self.keep]:
            if n in self.have():
                self._tile(n)

    def size_bytes(self) -> int:
        if not self.folder or not self.folder.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.folder.glob("*.cct"))

    def _tile(self, name: str) -> Tile | None:
        with self._lock:
            return self._tile_locked(name)

    def _tile_locked(self, name: str) -> Tile | None:
        if name in self._tiles:
            t = self._tiles.pop(name)  # most recent last
            self._tiles[name] = t
            return t
        t = None
        path = self.folder / f"{name}.cct" if self.folder else None
        if path and path.is_file():
            try:
                t = Tile(path.read_bytes())
            except (OSError, ValueError, struct.error):
                t = None
        self._tiles[name] = t
        while len(self._tiles) > self.keep:
            self._tiles.pop(next(iter(self._tiles)))
        return t

    def forget(self) -> None:
        """Drop what is held in memory, after tiles are added or removed."""
        with self._lock:
            self._tiles.clear()
            self._have = None

    def height(self, lat, lon) -> float | None:
        """Metres above sea level at a point, None with no tile there or over a void."""
        if lat is None or lon is None:
            return None
        try:
            la, lo = float(lat), float(lon)
        except (TypeError, ValueError):
            return None
        if not (-90 <= la < 90 and -180 <= lo < 180):
            return None
        t = self._tile(tile_name(la, lo))
        return t.height(lo - math.floor(lo), la - math.floor(la)) if t else None

    def missing(self, points, k: int = MIN_FIXES) -> set[str]:
        """Tiles holding at least k of the points that are neither downloaded nor known absent."""
        return tiles_for(points, k) - self.names()

    def __bool__(self) -> bool:
        return bool(self.have())


# A drive counts as on the ground model only when this share of its readings has a height:
# mixing two sources inside one drive would draw a step where they meet.
COVERED = 0.9
MAX_GRADE = 0.15  # roads rarely pass 12 %


def _grade_ok(prev, lat, lon, h) -> bool:
    """Whether the ground at this fix could be what a road runs on, from the last kept fix:
    no more than MAX_GRADE over the distance between them, taken as at least 50 m so two
    close fixes may differ by a few metres. A bridge over a gorge or a tunnel under a ridge
    reads the ground far below or above the road and fails this."""
    pla, plo, ph = prev
    dist = math.hypot((lat - pla) * 111_320, (lon - plo) * 111_320 * math.cos(math.radians(lat)))
    return abs(h - ph) <= MAX_GRADE * max(dist, 50.0)


def ground_track(track: list[dict], terrain: Terrain | None) -> tuple[list[dict], bool]:
    """(track with each reading's `elevation` replaced by the ground height under it, True)
    when the model covers the drive; else (track unchanged, False). A reading with no ground
    under it, or whose ground a road could not follow from the last kept one (a bridge deck,
    a tunnel), is left without a height. Cheap to call without a model: it returns at once."""
    if not terrain or not track:
        return track, False
    heights: list[float | None] = []
    prev = None
    for p in track:
        la, lo = p.get("latitude"), p.get("longitude")
        h = terrain.height(la, lo)
        if h is not None and prev is not None and not _grade_ok(prev, la, lo, h):
            h = None
        if h is not None:
            prev = (la, lo, h)
        heights.append(h)
    if sum(h is not None for h in heights) < COVERED * len(track):
        return track, False
    return [{**p, "elevation": h} for p, h in zip(track, heights)], True


def fetch(name: str, folder, get=None, factor: int | None = None) -> str:
    """Download one tile into folder: 'ok' or 'sea' (the archive has no such square). Raises
    on a network failure, or a body that is not a tile. `get(url)` returns the response body,
    or None for 404."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    url = SOURCE.format(band=name[:3], name=name)
    body = (get or _get)(url)
    if body is None:
        (folder / f"{name}.none").write_bytes(b"")
        return "sea"
    data = convert(gzip.decompress(body), factor)
    tmp = folder / f"{name}.part"
    tmp.write_bytes(data)
    tmp.replace(folder / f"{name}.cct")
    return "ok"


def _get(url: str) -> bytes | None:
    """The body at url, None for a 404. Read in pieces against FETCH_DEADLINE_S, so a slow
    trickle cannot hold the worker for longer than that."""
    req = urllib.request.Request(url, headers={"User-Agent": "car-command"})
    end = time.monotonic() + FETCH_DEADLINE_S
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
            parts = []
            while chunk := r.read(1 << 20):
                parts.append(chunk)
                if time.monotonic() > end:
                    raise TimeoutError("terrain download too slow")
            return b"".join(parts)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise


NETWORK = (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException)
GIVE_UP = len(BACKOFF_S)  # failed this many times: not asked for again


def _due(fails: dict, name: str, now: float) -> bool:
    f = fails.get(name)
    return not f or (f["n"] < GIVE_UP and now - f["at"] >= BACKOFF_S[f["n"] - 1])


def download_pass(terrain: Terrain, wanted, get=None, limit: int = 1, free_bytes=None, reserve: int = 2_000_000_000, fails: dict | None = None, now=time.time, factor: int | None = None) -> dict:
    """Fetch up to `limit` of the wanted tiles that are not here yet (in name order, so a pass
    is repeatable). Stops at a network failure (the same tile is asked for again next pass),
    or when the disk is nearly full or refuses the write ('disk'). A tile that arrives but
    cannot be used is recorded in `fails` ({name: {n, at, error}}) and left alone for an
    hour, then six, then a day, and given up after the third. {"ok", "sea", "left",
    "failed", "stopped"}: left counts tiles still to fetch, failed those given up."""
    fails = {} if fails is None else fails
    t = now()
    todo = sorted(set(wanted) - terrain.names())
    out = {"ok": 0, "sea": 0, "left": 0, "failed": sum(1 for n in todo if fails.get(n, {}).get("n", 0) >= GIVE_UP), "stopped": None}
    due = [n for n in todo if _due(fails, n, t)]
    if due and terrain.folder:
        # A folder that cannot be written is found out here, before 10 MB is fetched for nothing.
        try:
            terrain.folder.mkdir(parents=True, exist_ok=True)
            probe = terrain.folder / ".probe"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as err:
            out.update(stopped="disk", error=f"Can't write the terrain folder ({err.strerror or type(err).__name__})")
            due = []
    for name in due[:limit]:
        if free_bytes is not None and free_bytes() - STORED_MB * 1_000_000 < reserve:
            out.update(stopped="disk", error="Not enough free disk space")
            break
        try:
            r = fetch(name, terrain.folder, get, factor)
        except NETWORK:
            out["stopped"] = "network"
            break
        except OSError as err:
            if isinstance(err, PermissionError) or err.errno in (errno.ENOSPC, errno.EROFS, errno.EACCES, errno.EPERM):
                out.update(stopped="disk", error=f"Can't write the terrain folder ({err.strerror or type(err).__name__})")
                break
            f = fails.setdefault(name, {"n": 0})
            f.update(n=f["n"] + 1, at=t, error=f"{type(err).__name__}: {err}"[:200])
            continue
        except Exception as err:  # noqa: BLE001  a body that is not a tile: gzip, size, format
            f = fails.setdefault(name, {"n": 0})
            f.update(n=f["n"] + 1, at=t, error=f"{type(err).__name__}: {err}"[:200])
            continue
        fails.pop(name, None)
        out[r] += 1
    out["failed"] = sum(1 for n in todo if fails.get(n, {}).get("n", 0) >= GIVE_UP)
    out["left"] = len(set(wanted) - terrain.names()) - out["failed"]
    if out["ok"] or out["sea"]:
        terrain.forget()
    return out


def region_names(tiles, region_of, name_of) -> list[str]:
    """The regions (states, else countries) the tiles' centres fall in, most tiles first, by name."""
    seen: dict[str, int] = {}
    for t in tiles:
        s, w = tile_corner(t)
        code = region_of(s + 0.5, w + 0.5)
        if code:
            seen[code] = seen.get(code, 0) + 1
    return [name_of(c) or c for c, _n in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))]


def status(terrain: Terrain, need, on: bool, region_of=None, name_of=None, fails: dict | None = None, disk_error: str | None = None) -> dict:
    """What Settings › Maps shows: whether it is on, how many of the needed squares are
    here, what is left to download (tile count and megabytes, an upper bound), what the
    downloaded tiles take on disk, and the regions the needed squares lie in."""
    need = set(need)
    here = terrain.names()
    left = need - here
    bad = {n: f for n, f in (fails or {}).items() if n in left}
    gave_up = [n for n, f in bad.items() if f["n"] >= GIVE_UP]
    return {
        "on": on,
        "need": len(need),
        "have": len(need & terrain.have()),
        "left": len(left),
        "mb_left": (len(left) - len(gave_up)) * TILE_MB,
        "disk_mb": round(terrain.size_bytes() / 1e6),
        "regions": region_names(need, region_of, name_of) if region_of else [],
        "error": disk_error or next((f["error"] for f in bad.values()), None),
        "disk": bool(disk_error),
        "gave_up": len(gave_up),
        "state": "ready" if need and not left else ("waiting" if on else ("paused" if need and terrain.have() else "off")),
    }


class Worker:
    """The background pass: work out which squares the cars need, fetch one if the owner has
    switched downloads on, fill ground figures for drives, and look again at drives the model
    did not cover once the downloads are done. Nothing in it touches the network unless `on()`
    says so, and the need is worked out from the cars' own positions either way, so Settings
    can show the size and the regions before anything is switched on.

    points() -> (lat, lon, when) in time order; backfill() -> drives filled this call;
    reset() -> drives marked for another look; state has get(key), set(key, value), delete(key)."""

    NEED, FAIL, RESET, WANTED_KEY, DISK = "terrain_need", "terrain_fail", "terrain_reset", "terrain_wanted", "terrain_disk"
    NEED_EVERY_S = 3600

    def __init__(self, terrain: Terrain, on, points, state, backfill, reset, wanted: set, get=None, free_bytes=None, now=time.time) -> None:
        self.terrain, self.on, self.points, self.state = terrain, on, points, state
        self.backfill, self.reset, self.wanted = backfill, reset, wanted
        self.get, self.free_bytes, self.now = get, free_bytes, now
        self.need_at = 0.0
        self.home: list[str] = []

    def _stored(self, key: str) -> set[str]:
        return {n for n in (self.state.get(key) or "").split(",") if n}

    def refresh_need(self) -> set[str]:
        """Squares holding at least MIN_FIXES of the cars' positions, plus those a drive passed
        through, kept with what was needed before. Local only; at most once an hour."""
        stored = self._stored(self.NEED) | self._stored(self.WANTED_KEY)
        if self.now() - self.need_at < self.NEED_EVERY_S and stored:
            return stored | self.wanted
        pts = self.points()
        need = tiles_for(pts, MIN_FIXES) | stored | self.wanted
        self.state.set(self.NEED, ",".join(sorted(need)))
        self.home = list(dict.fromkeys(tile_name(p[0], p[1]) for p in reversed(pts) if p[0] is not None and p[1] is not None))
        self.need_at = self.now()
        return need

    def tick(self) -> dict:
        need = self.refresh_need()
        if self.wanted:
            self.state.set(self.WANTED_KEY, ",".join(sorted(self._stored(self.WANTED_KEY) | self.wanted)))
        out: dict = {"idle": not self.on()}
        if self.on():
            try:
                fails = json.loads(self.state.get(self.FAIL) or "{}")
            except ValueError:
                fails = {}
            out.update(download_pass(self.terrain, need, get=self.get, free_bytes=self.free_bytes, fails=fails, now=self.now))
            self.state.set(self.FAIL, json.dumps(fails))
            if out.get("error"):
                self.state.set(self.DISK, out["error"])
            else:
                self.state.delete(self.DISK)
            if out["ok"]:
                self.state.set(self.RESET, "1")
            if out["left"] == 0 and out["stopped"] is None and self.state.get(self.RESET) == "1":
                self.reset()  # once, when the last square is in, not at every square
                self.state.delete(self.RESET)
        if self.terrain:
            self.terrain.warm(self.home)
            out["filled"] = self.backfill()
        return out
