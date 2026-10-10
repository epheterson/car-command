"""Download what is queued, one file at a time, verify it, and build a new version beside the
current one; swap only when every step succeeded."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

import placeindex

LOG = logging.getLogger("maps.builder")

MAX_CONSECUTIVE_FAILURES = 3
# Stopped after three failures, the builder tries again by itself after this long, so a
# night of bad luck (a mirror down, a full disk since cleared) does not stop it for good.
STOPPED_RESET = timedelta(hours=24)
# Build inputs that serve nothing once the tiles and the place index exist.
INTERMEDIATES = ("merged.osm.pbf", "places.osm.pbf", "places.geojsonseq")
# A build needs room for the merged extract, the tiles and the place export beside the
# extracts themselves; this much free, or it does not start.
DISK_FACTOR, DISK_SPARE = 3, 1_000_000_000


class RateLimited(Exception):
    pass


def _name(region_id: str) -> str:
    return region_id.replace("/", "__") + ".osm.pbf"


def _text(x) -> str:
    if x is None:
        return ""
    return x if isinstance(x, str) else x.decode(errors="replace")


def _append_log(log: Path, argv: list[str], out, err) -> None:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(argv) + "\n")
        fh.write(_text(out))
        if err:
            fh.write(_text(err))
        fh.write("\n")


def quiet_valhalla_logging(config: dict) -> dict:
    """Valhalla 3.9's generated config does not log request bodies (coordinates) through
    logging.long_request, or through anything else we've found. This is a defensive setting
    only, in case a future Valhalla version adds that behaviour: it raises the top-level
    logging.long_request threshold (seconds) far out, and does the same for loki.logging if
    that block is ever present (it isn't in 3.9's output)."""
    config.setdefault("logging", {})["long_request"] = 1_000_000_000
    loki = config.get("loki")
    if isinstance(loki, dict) and isinstance(loki.get("logging"), dict):
        loki["logging"]["long_request"] = 1_000_000_000
    return config


def _md5_of(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Builder:
    def __init__(self, data: Path, state, fetch, fetch_text, run, now, lock: threading.Lock | None = None) -> None:
        self.data, self.state, self.fetch, self.fetch_text, self.run, self.now = Path(data), state, fetch, fetch_text, run, now
        self.lock = lock or threading.Lock()
        self.free = lambda: shutil.disk_usage(self.data).free

    @property
    def attempt(self) -> int:
        """The backoff exponent, kept on State so reset_backoff() (any user action) can zero it
        along with failures/wait_until."""
        return self.state.attempt

    @attempt.setter
    def attempt(self, value: int) -> None:
        self.state.attempt = value

    def current(self) -> Path | None:
        c = self.data / "current"
        return c.resolve() if c.exists() else None

    def prune(self) -> None:
        """Delete every builds/v<N> but the one `current` points at: older versions and
        half-finished builds alike. The caller closes anything still reading them first,
        or the space is not freed until it does."""
        builds = self.data / "builds"
        if not builds.exists():
            return
        keep = self.current()
        for item in builds.iterdir():
            if item.resolve() == keep:
                continue
            try:
                shutil.rmtree(item) if item.is_dir() and not item.is_symlink() else item.unlink()
            except OSError as e:
                LOG.warning("could not remove old build %s: %s", item, e)

    def _next_version(self) -> int:
        """Find the highest existing builds/v<N> directory and return N+1."""
        builds_dir = self.data / "builds"
        if not builds_dir.exists():
            return 1
        versions = []
        for item in builds_dir.iterdir():
            if item.is_dir() and item.name.startswith("v"):
                try:
                    versions.append(int(item.name[1:]))
                except ValueError:
                    pass
        max_existing = max(versions) if versions else 0
        max_state = self.state.build.get("version") or 0
        return max(max_existing, max_state) + 1

    def _download(self, region: dict) -> None:
        dest_dir = self.data / "pbf"
        dest_dir.mkdir(parents=True, exist_ok=True)
        final = dest_dir / _name(region["id"])
        if region.get("md5") and final.exists() and _md5_of(final) == region["md5"]:
            return  # verified earlier: no need to hit the network again
        part = dest_dir / (_name(region["id"]) + ".part")
        try:
            status = self.fetch(region["pbf"], part)
        except Exception:
            part.unlink(missing_ok=True)
            raise
        if status != 200:
            part.unlink(missing_ok=True)
            raise ValueError(f"HTTP {status}")
        want = self.fetch_text(region["pbf"] + ".md5").split()[0]
        got = _md5_of(part)
        if got != want:
            part.unlink(missing_ok=True)
            raise ValueError(f"checksum mismatch for {region['id']}")
        os.replace(part, final)
        region["md5"] = got

    def step(self) -> str:
        now = self.now()
        with self.lock:
            if self.state.failures >= MAX_CONSECUTIVE_FAILURES:
                since = self.state.stopped_since
                if since is None:
                    # stopped before this was recorded: count the day from now
                    self.state.stopped_since = now.isoformat()
                    self.state.save()
                    return "stopped"
                if now < datetime.fromisoformat(since) + STOPPED_RESET:
                    return "stopped"
                self.state.reset_backoff()
                self.state.save()
            has_work = bool(self.state.queue) or self.state.dirty
            if not has_work:
                return "idle"
            if self.state.wait_until and now < datetime.fromisoformat(self.state.wait_until):
                return "waiting"
            queue_ids = list(self.state.queue)
            all_ids = list(self.state.regions.keys())
            # Queued regions, plus any region whose extract is not on disk: one re-added
            # while a build ran, or left without its file by a removal, fetches it here
            # instead of failing the merge until the builder stops.
            fetch_ids = [rid for rid in all_ids if rid in queue_ids or not (self.data / "pbf" / _name(rid)).exists()]
            regions_snapshot = {rid: dict(self.state.regions[rid]) for rid in fetch_ids}
            prior_version = self.state.build.get("version")
            gen_snapshot = self.state.generation
            self.state.build = {"status": "downloading" if fetch_ids else "building", "version": prior_version, "error": None}
            self.state.save()

        try:
            for rid in fetch_ids:
                region = regions_snapshot[rid]
                self._download(region)
                with self.lock:
                    if rid in self.state.regions:
                        self.state.regions[rid]["md5"] = region.get("md5")
                        self.state.save()
                    else:  # removed while it downloaded: don't leave its extract behind
                        (self.data / "pbf" / _name(rid)).unlink(missing_ok=True)
        except RateLimited:
            with self.lock:
                self.state.backoff(now, self.attempt)
                self.attempt += 1
                self.state.build["status"] = "idle"
                self.state.save()
            return "waiting"
        except ValueError as err:
            return self._fail(now, str(err).split(" for ")[0], prior_version)
        except Exception as err:  # noqa: BLE001 - any other download error backs off and fails the step
            return self._fail(now, f"download: {err.__class__.__name__}", prior_version)

        if not all_ids:
            # nothing left to serve: drop `current` and stop Valhalla (the service's loop does
            # the stopping; here we just clear the symlink and the queued/dirty bookkeeping).
            # A region added while this ran keeps its queue entry and gets the next step.
            with self.lock:
                (self.data / "current").unlink(missing_ok=True)
                self.state.queue = [q for q in self.state.queue if q not in queue_ids]
                self.state.dirty = self.state.generation != gen_snapshot
                self.state.failures = 0
                self.state.build = {"status": "idle", "version": None, "error": None}
                self.state.save()
            return "cleared"

        need = DISK_FACTOR * sum(self._pbf_bytes(rid) for rid in all_ids) + DISK_SPARE
        have = self.free()
        if have < need:
            return self._fail(now, f"not enough disk: need {need / 1e9:.1f} GB, have {have / 1e9:.1f} GB", prior_version)

        self.attempt = 0
        version = self._next_version()
        out = self.data / "builds" / f"v{version}"
        try:
            self._build(out, all_ids)
        except Exception as err:  # noqa: BLE001 - every build exception is a failure (or superseded)
            with self.lock:
                missing = [rid for rid in all_ids if rid not in self.state.regions]
            if missing:
                # a region in this snapshot was removed mid-build (its pbf is gone too): this
                # isn't a real failure, it's stale work. No backoff, no failure count.
                return self._superseded(out)
            return self._fail_build(out, err, prior_version)
        for name in INTERMEDIATES:
            (out / name).unlink(missing_ok=True)

        tmp = self.data / "current.tmp"
        try:
            tmp.unlink(missing_ok=True)
            tmp.symlink_to(out.relative_to(self.data))
            os.replace(tmp, self.data / "current")
        except OSError as err:
            # `current` is untouched (the rename is atomic): fail like a build, with backoff,
            # rather than rebuild from scratch every minute
            tmp.unlink(missing_ok=True)
            return self._fail_build(out, err, prior_version)
        with self.lock:
            stale = self.state.generation != gen_snapshot
            for rid in fetch_ids:
                if rid in self.state.regions:
                    self.state.regions[rid]["refreshed"] = now.isoformat()
            self.state.queue = [q for q in self.state.queue if q not in queue_ids]
            # a removal landed while this build ran: leave dirty so the next step corrects it
            self.state.dirty = stale
            self.state.failures = 0
            self.state.build = {"status": "idle", "version": version, "error": None}
            self.state.save()
        return f"built v{version}"

    def _pbf_bytes(self, rid: str) -> int:
        try:
            return (self.data / "pbf" / _name(rid)).stat().st_size
        except OSError:
            return 0

    def _fail(self, now: datetime, error: str, prior_version: int | None) -> str:
        with self.lock:
            self.state.backoff(now, self.attempt)
            self.attempt += 1
            self.state.failures += 1
            self._note_stopped()
            self.state.build = {"status": "failed", "version": prior_version, "error": error}
            self.state.save()
        return f"failed: {error}"

    def _fail_build(self, out: Path, err: Exception, prior_version: int | None) -> str:
        log = out / "build.log"
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.exists() else []
        if lines:
            LOG.warning("build failed, last lines:\n%s", "\n".join(lines[-20:]))
        try:
            shutil.rmtree(out)
        except OSError as e:
            LOG.warning("could not remove failed build %s: %s", out, e)
        if isinstance(err, subprocess.CalledProcessError):
            last_nonempty = next((ln for ln in reversed(lines) if ln.strip()), "")
            cmd = err.cmd[0] if err.cmd else "?"
            detail = f"{cmd}: {last_nonempty}"[:200]
            result = f"failed: {cmd}"
        else:
            detail = f"{err.__class__.__name__}: {err}"[:200]
            result = f"failed: {err.__class__.__name__}"
        with self.lock:
            self.state.backoff(self.now(), self.attempt)
            self.attempt += 1
            self.state.failures += 1
            self._note_stopped()
            self.state.build = {"status": "failed", "version": prior_version, "error": detail}
            self.state.save()
        return result

    def _note_stopped(self) -> None:
        if self.state.failures >= MAX_CONSECUTIVE_FAILURES and not self.state.stopped_since:
            self.state.stopped_since = self.now().isoformat()

    def _superseded(self, out: Path) -> str:
        """A snapshotted region was removed mid-build: the merge failed on data that no longer
        exists, not because anything is actually broken. No failure, no backoff; dirty (already
        set by remove()) makes the next step() rebuild correctly without it."""
        try:
            shutil.rmtree(out)
        except OSError as e:
            LOG.warning("could not remove superseded build %s: %s", out, e)
        with self.lock:
            self.state.build = {"status": "idle", "version": self.state.build.get("version"), "error": None}
            self.state.dirty = True
            self.state.save()
        return "superseded"

    def _exec(self, argv: list[str], out: Path) -> None:
        log = out / "build.log"
        try:
            result = self.run(argv)
        except Exception as err:  # noqa: BLE001 - log whatever we can, then let step() classify it
            _append_log(log, argv, getattr(err, "stdout", None), getattr(err, "stderr", None))
            raise
        _append_log(log, argv, getattr(result, "stdout", None), getattr(result, "stderr", None))

    def _quiet_logging(self, cfg: Path) -> None:
        try:
            data = json.loads(cfg.read_text())
        except (OSError, ValueError):
            return  # a test fake or a missing config: nothing to quiet
        quiet_valhalla_logging(data)
        cfg.write_text(json.dumps(data))

    def _build(self, out: Path, region_ids: list[str]) -> None:
        out.mkdir(parents=True, exist_ok=True)
        pbfs = [str(self.data / "pbf" / _name(rid)) for rid in region_ids]
        merged = out / "merged.osm.pbf"
        self._exec(["osmium", "merge", *pbfs, "-o", str(merged), "--overwrite"], out)
        cfg = out / "valhalla.json"
        self._exec(["valhalla_build_config", "--mjolnir-tile-dir", str(out / "tiles"), "--mjolnir-tile-extract", str(out / "tiles.tar"), "-o", str(cfg)], out)
        self._quiet_logging(cfg)
        self._exec(["valhalla_build_tiles", "-c", str(cfg), str(merged)], out)
        filtered = out / "places.osm.pbf"
        self._exec(["osmium", "tags-filter", str(merged), "nwr/place", "r/boundary=administrative", "nwr/name", "nwr/addr:housenumber", "-o", str(filtered), "--overwrite"], out)
        seq = out / "places.geojsonseq"
        self._exec(["osmium", "export", str(filtered), "-f", "geojsonseq", "-o", str(seq), "--overwrite"], out)
        if seq.exists() and seq.stat().st_size > 1:
            placeindex.build(seq, out / "places.sqlite")
