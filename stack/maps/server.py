"""The local maps service: regions, builds, and place lookups, on the internal network only."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import match as matcher
from builder import Builder, RateLimited, _name
from placeindex import Places
from regions import REGION_CAP_BYTES, Index, pick
from reverse import ROAD_REACH_M, answer
from state import State

LOG = logging.getLogger("maps")
GEOFABRIK_INDEX = "https://download.geofabrik.de/index-v1.json"
UA = {"User-Agent": "carcommand-maps/0.1 (self-hosted EV logger)"}
VALHALLA = "http://127.0.0.1:8002"
REFRESH_DAYS = {"monthly": 30, "quarterly": 90}
MAX_BODY = 2 << 20  # a drive of MAX_POINTS positions is well under this


def _now():
    return datetime.now(timezone.utc)


# A download that stalls or meets a busy mirror is tried again at once, on a new connection,
# before the step fails: one failed step costs an hour's backoff, and Geofabrik's servers drop
# or stall single requests when they are loaded (seen 2026-10-06) while the next one answers.
TRIES, TRY_PAUSE_S = 3, 10


def _transient(err: Exception) -> bool:
    if isinstance(err, urllib.error.HTTPError):
        return err.code >= 500
    return isinstance(err, (TimeoutError, urllib.error.URLError, ConnectionError))


def _tried(call, sleep=time.sleep):
    for i in range(TRIES):
        try:
            return call()
        except Exception as err:  # noqa: BLE001 - only the transient ones are tried again
            if i == TRIES - 1 or not _transient(err):
                raise
            LOG.warning("download: %s, trying again", type(err).__name__)
            sleep(TRY_PAUSE_S * (i + 1))


def _fetch(url: str, dest: Path) -> int:
    def once() -> int:
        req = urllib.request.Request(url, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as fh:
                shutil.copyfileobj(r, fh, 1 << 20)
                return r.status
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited(url) from e
            raise

    return _tried(once)


def _fetch_text(url: str) -> str:
    def once() -> str:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            return r.read().decode()

    return _tried(once)


def _size(url: str) -> int | None:
    """The extract's size, or None. Tried again like a download: a stalled HEAD is an unknown
    size, and an unknown size passes the region over for a different one."""

    def once() -> int | None:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA, method="HEAD"), timeout=30) as r:
            return int(r.headers.get("Content-Length") or 0) or None

    try:
        return _tried(once)
    except Exception:  # noqa: BLE001
        return None


def _run(argv: list[str]):
    if argv[0] == "valhalla_build_config" and "-o" in argv:
        i = argv.index("-o")
        out = Path(argv[i + 1])
        res = subprocess.run(argv[:i] + argv[i + 2 :], check=True, capture_output=True, text=True)
        out.write_text(res.stdout)
        return res
    return subprocess.run(argv, check=True, capture_output=True, text=True)


def _disk_usage(path: Path) -> int:
    """Sum file sizes under path, tolerating anything (a file, or a whole directory) vanishing
    mid-walk -- a build swapping `current` out from under us must not break /status."""
    total = 0
    try:
        it = path.rglob("*")
    except OSError:
        return 0
    while True:
        try:
            f = next(it)
        except StopIteration:
            break
        except OSError:
            continue  # a directory disappeared while we were listing it
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue  # the file itself vanished between listing and stat
    return total


def _locate(lat: float, lon: float) -> list:
    # radius returns every edge within reach, not just the nearest; costing auto drops footways
    body = json.dumps({"locations": [{"lat": lat, "lon": lon, "radius": ROAD_REACH_M}], "costing": "auto", "verbose": True}).encode()
    req = urllib.request.Request(f"{VALHALLA}/locate", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1.5) as r:
        return json.loads(r.read())


def _trace(payload: dict, timeout: float = matcher.CALL_TIMEOUT_S) -> dict:
    return matcher.call_valhalla(VALHALLA, payload, timeout)


class Service:
    def __init__(self, data: Path | str, index: Index | None = None, sizes: dict | None = None, locate=_locate, popen=subprocess.Popen, trace=_trace) -> None:
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.state = State.load(self.data / "state.json")
        self._index = index
        self._index_injected = index is not None
        self._index_lock = threading.Lock()
        if sizes:
            self.state.sizes.update(sizes)
        self.locate = locate
        self.trace = trace
        # One drive matched at a time: Valhalla runs one worker, and the place lookups the
        # site makes on every new stop share it. A second /match is told "busy", not queued.
        self._matching = threading.Lock()
        self._popen = popen
        self.places: Places | None = None
        self.lock = threading.Lock()
        self.builder = Builder(self.data, self.state, _fetch, _fetch_text, _run, _now, lock=self.lock)
        self.valhalla: subprocess.Popen | None = None
        self.reopen()
        # Nothing is building yet: anything `current` does not point at is an old version
        # or a build the last run never finished.
        self.builder.prune()
        self._disk = _disk_usage(self.data)

    @property
    def sizes(self) -> dict:
        return self.state.sizes

    def index(self) -> Index:
        if self._index_injected:
            return self._index
        path = self.data / "geofabrik-index.json"
        with self._index_lock:
            stale = not path.exists() or time.time() - path.stat().st_mtime > 30 * 86400
            if self._index is None or stale:
                if stale:
                    part = path.with_suffix(".part")
                    _fetch(GEOFABRIK_INDEX, part)
                    os.replace(part, path)
                self._index = Index(json.loads(path.read_text())["features"])
        return self._index

    def reopen(self) -> None:
        """Open the current build's place index and close the previous one, so a pruned
        build's files are really freed."""
        cur = self.builder.current()
        old = self.places
        self.places = Places(cur / "places.sqlite") if cur and (cur / "places.sqlite").exists() else None
        if old is not None and hasattr(old, "close"):
            old.close()

    def ensure(self, lat: float, lon: float, reason: str) -> dict:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("out of range")
        chain = self.index().chain(lat, lon)
        # From the largest region down, stopping at the first that fits the cap: that is the
        # one pick() takes, and the smaller areas the point is in are never asked about.
        # A continent is never a candidate, so it is never asked about either.
        for r in reversed(chain):
            if not r.get("parent"):
                continue
            size = self.state.sizes.get(r["id"])
            if size is None:
                size = _size(r["pbf"])  # a failed HEAD is never cached: retry next time
                if size is not None:
                    with self.lock:
                        self.state.sizes[r["id"]] = size
                        self.state.save()
            if size and size <= REGION_CAP_BYTES:
                break
        region = pick(chain, self.state.sizes)
        if region is None:
            return {"result": "nowhere", "region": None}
        with self.lock:
            result = self.state.ensure(region, self.state.sizes.get(region["id"], 0), reason, _now())
            self.state.save()
        return {"result": result, "region": region["id"]}

    def status(self) -> dict:
        disk = self._disk  # walked after each build step, not on every call
        with self.lock:
            s = self.state
            return {
                "version": s.build.get("version"),
                "regions": list(s.regions.values()),
                "queue": list(s.queue),
                "building": s.build.get("status"),
                "error": s.build.get("error"),
                "wait_until": s.wait_until,
                "auto": s.auto,
                "refresh": s.refresh,
                "disk_bytes": disk,
            }

    def reverse(self, lat: float, lon: float) -> dict:
        if self.builder.current() is None:
            return {"error": "Unable to geocode"}
        try:
            loc = self.locate(lat, lon)
        except Exception:  # noqa: BLE001 - Valhalla down or slow: answer from the index alone
            loc = []
        return answer(lat, lon, loc, self.places) or {"error": "Unable to geocode"}

    def candidates(self, lat: float, lon: float, limit: int) -> dict:
        places = self.places  # bind once: a build swap must not change it mid-call
        if places is None:
            return {"candidates": [], "address": None}
        r = self.reverse(lat, lon).get("address") or {}
        addr = {k: r[k] for k in ("house_number", "road") if k in r}
        town = r.get("city") or r.get("town") or r.get("village")
        if town:
            addr["city"] = town
        return {"candidates": places.candidates(lat, lon, limit), "address": addr or None}

    def search(self, q: str, lat: float | None, lon: float | None, limit: int) -> dict:
        """Typed text to places in the index (Places.search); none before the first build."""
        places = self.places  # bind once: a build swap must not change it mid-call
        if places is None:
            return {"results": [], "built": False}
        return {"results": places.search(q, lat, lon, limit), "built": True}

    def match(self, points: list) -> dict:
        """A drive's positions to the roads it took (match.match), or why not. "busy" and
        "unavailable" are worth asking again; "no match" is the answer for this build, and
        that includes input it cannot use, so a caller never retries one drive forever."""
        version = self.state.build.get("version")
        if not isinstance(points, list):
            return {"result": "no match", "reason": "points is not a list", "version": version}
        if len(points) > matcher.MAX_POINTS:
            return {"result": "no match", "reason": "too many points", "version": version}
        try:
            pts = matcher.clean(points)
        except ValueError as err:
            return {"result": "no match", "reason": f"bad points: {err}", "version": version}
        if self.builder.current() is None:
            return {"result": "no match", "reason": "no local maps built yet", "version": None}
        if not self._matching.acquire(blocking=False):
            return {"result": "busy"}
        try:
            out = matcher.match(pts, self.trace)
        except matcher.Unavailable:
            return {"result": "unavailable"}
        finally:
            self._matching.release()
        out["version"] = self.state.build.get("version")
        return out

    def _tune(self, cfg: Path) -> None:
        try:
            data = json.loads(cfg.read_text())
        except (OSError, ValueError):
            return
        tuned = matcher.tune_config(json.loads(json.dumps(data)))
        if tuned != data:
            try:
                cfg.write_text(json.dumps(tuned))
            except OSError as err:
                LOG.warning("valhalla config: %s", type(err).__name__)

    def start_valhalla(self) -> None:
        cur = self.builder.current()
        if self.valhalla:
            self.valhalla.terminate()
            try:
                self.valhalla.wait(10)
            except subprocess.TimeoutExpired:
                self.valhalla.kill()
                self.valhalla.wait()
            self.valhalla = None
        if cur and (cur / "valhalla.json").exists():
            self._tune(cur / "valhalla.json")
            self.valhalla = self._popen(["valhalla_service", str(cur / "valhalla.json"), "1"])

    def _apply_refresh_cadence(self, now: datetime) -> None:
        days = REFRESH_DAYS.get(self.state.refresh)
        if not days:
            return  # "off" never re-queues
        cutoff = now - timedelta(days=days)
        with self.lock:
            changed = False
            for rid, r in self.state.regions.items():
                if rid in self.state.queue:
                    continue
                refreshed = r.get("refreshed")
                if refreshed and datetime.fromisoformat(refreshed) < cutoff:
                    self.state.queue.append(rid)
                    r["reason"] = "refresh"
                    r["md5"] = None  # force a real re-download: the old pbf may be stale
                    changed = True
            if changed:
                self.state.save()

    def tick(self) -> str:
        """One loop iteration: restart Valhalla if it died, apply the refresh cadence, and run
        one builder step. Factored out so the loop body itself is testable without copying its
        conditions into a test."""
        if self.valhalla is None or self.valhalla.poll() is not None:
            if self.valhalla is not None:
                LOG.warning("valhalla_service exited (code %s); restarting", self.valhalla.poll())
            self.start_valhalla()
        self._apply_refresh_cadence(_now())
        result = self.builder.step()
        if result.startswith("built") or result == "cleared":
            # "cleared" (the last region was removed): reopen() drops self.places to None
            # (builder.current() is now None) and start_valhalla() stops the old process
            # without starting a new one (no valhalla.json left to point it at).
            self.reopen()
            self.start_valhalla()
            # Both have let go of the old version: now its space can come back.
            self.builder.prune()
            LOG.info(result)
        elif result.startswith("failed"):
            LOG.warning(result)  # otherwise only /status says why, and only until the next step
        self._disk = _disk_usage(self.data)
        return result

    def loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as err:  # noqa: BLE001 - the build loop must never die
                LOG.warning("build loop: %s", err)
            time.sleep(60)


def _coords(q: dict) -> tuple[float, float]:
    lat, lon = float(q["lat"][0]), float(q["lon"][0])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("out of range")
    return lat, lon


def make_server(port: int = 8090, data: Path | str = "/data", service: Service | None = None) -> ThreadingHTTPServer:
    svc = service or Service(data)

    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, body) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("too large")
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):  # noqa: N802
            try:
                u = urlparse(self.path)
                q = parse_qs(u.query)
                try:
                    if u.path == "/status":
                        return self._json(200, svc.status())
                    if u.path == "/reverse":
                        return self._json(200, svc.reverse(*_coords(q)))
                    if u.path == "/candidates":
                        try:
                            limit = max(1, min(int((q.get("limit") or ["10"])[0]), 20))
                        except ValueError:
                            return self._json(400, {"error": "limit must be an integer"})
                        return self._json(200, svc.candidates(*_coords(q), limit))
                    if u.path == "/search":
                        # the typed text: answered from the local index, never logged
                        text = (q.get("q") or [""])[0][:100]
                        near = _coords(q) if "lat" in q and "lon" in q else (None, None)
                        limit = max(1, min(int((q.get("limit") or ["8"])[0]), 20))
                        return self._json(200, svc.search(text, *near, limit))
                except (KeyError, ValueError):
                    return self._json(400, {"error": "lat and lon are required numbers"})
                return self._json(404, {"error": "not found"})
            except Exception as err:  # noqa: BLE001 - never drop the socket on an unexpected error
                LOG.warning("handler error: %s", type(err).__name__)
                return self._json(503, {"error": "unavailable"})

        def do_POST(self):  # noqa: N802
            try:
                u = urlparse(self.path)
                try:
                    b = self._body()
                    if u.path == "/regions/ensure":
                        return self._json(200, svc.ensure(float(b["lat"]), float(b["lon"]), str(b.get("reason") or "")[:80]))
                    if u.path == "/regions/add":
                        r = svc.index().by_id[b["id"]]
                        size = svc.state.sizes.get(r["id"])
                        if size is None:
                            size = _size(r["pbf"])  # stays None when the HEAD fails: never 0
                        with svc.lock:
                            if size is not None:
                                svc.state.sizes[r["id"]] = size
                            svc.state.add(r, size, "added by hand", _now())
                            svc.state.reset_backoff()
                            svc.state.save()
                        return self._json(200, {"result": "queued", "region": r["id"]})
                    if u.path == "/settings":
                        with svc.lock:
                            if "auto" in b:
                                svc.state.auto = bool(b["auto"])
                            if b.get("refresh") in ("monthly", "quarterly", "off"):
                                svc.state.refresh = b["refresh"]
                            svc.state.save()
                        return self._json(200, svc.status())
                    if u.path == "/match":
                        return self._json(200, svc.match(b.get("points") if isinstance(b, dict) else None))
                    if u.path == "/retry":
                        with svc.lock:
                            svc.state.reset_backoff()
                            svc.state.save()
                        return self._json(200, svc.status())
                except (KeyError, ValueError, json.JSONDecodeError):
                    return self._json(400, {"error": "bad request"})
                return self._json(404, {"error": "not found"})
            except Exception as err:  # noqa: BLE001
                LOG.warning("handler error: %s", type(err).__name__)
                return self._json(503, {"error": "unavailable"})

        def do_DELETE(self):  # noqa: N802
            try:
                u = urlparse(self.path)
                if u.path.startswith("/regions/"):
                    rid = unquote(u.path[len("/regions/") :])
                    with svc.lock:
                        if rid not in svc.state.regions:
                            return self._json(404, {"error": "not found"})
                        svc.state.remove(rid)
                        svc.state.dirty = True
                        svc.state.reset_backoff()
                        svc.state.save()
                    (svc.data / "pbf" / _name(rid)).unlink(missing_ok=True)
                    return self._json(200, svc.status())
                return self._json(404, {"error": "not found"})
            except Exception as err:  # noqa: BLE001
                LOG.warning("handler error: %s", type(err).__name__)
                return self._json(503, {"error": "unavailable"})

        def log_message(self, *a):  # quiet: positions arrive in query strings
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.service = svc
    return srv


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = make_server(int(os.environ.get("MAPS_PORT", "8090")), os.environ.get("MAPS_DATA", "/data"))
    threading.Thread(target=s.service.loop, daemon=True).start()
    s.serve_forever()
