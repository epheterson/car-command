"""What regions this install has, why, what is queued, and when it may try again."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

BACKOFF = (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))


class State:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.regions: dict[str, dict] = {}
        self.queue: list[str] = []
        self.wait_until: str | None = None
        self.build = {"status": "idle", "version": None, "error": None}
        self.auto = True
        self.refresh = "monthly"
        self.failures = 0
        self.sizes: dict[str, int] = {}
        self.dirty = False
        self.generation = 0
        self.attempt = 0
        self.stopped_since: str | None = None  # when failures reached the stop limit

    _FIELDS = ("regions", "queue", "wait_until", "build", "auto", "refresh", "failures", "sizes", "dirty", "generation", "attempt", "stopped_since")

    @classmethod
    def load(cls, path: Path) -> "State":
        s = cls(path)
        if s.path.exists():
            d = json.loads(s.path.read_text())
            for k in s._FIELDS:
                if k in d:
                    setattr(s, k, d[k])
        return s

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: getattr(self, k) for k in self._FIELDS}, indent=1))
        os.replace(tmp, self.path)

    def _record(self, region: dict, size: int, reason: str, now: datetime) -> None:
        self.regions.setdefault(
            region["id"],
            {"id": region["id"], "name": region.get("name", region["id"]), "pbf": region["pbf"], "bytes": size, "reason": reason, "added": now.isoformat(), "refreshed": None, "md5": None},
        )
        if region["id"] not in self.queue:
            self.queue.append(region["id"])

    def ensure(self, region: dict, size: int, reason: str, now: datetime) -> str:
        have = self.regions.get(region["id"])
        if have and have.get("refreshed") and region["id"] not in self.queue:
            return "covered"
        if region["id"] in self.queue:
            return "already queued"
        if not self.auto:
            return "off"
        self._record(region, size, reason, now)
        return "queued"

    def add(self, region: dict, size: int, reason: str, now: datetime) -> None:
        self._record(region, size, reason, now)

    def remove(self, region_id: str) -> None:
        self.regions.pop(region_id, None)
        self.queue = [q for q in self.queue if q != region_id]
        self.generation += 1  # lets a build in progress detect it's working on stale data

    def backoff(self, now: datetime, attempt: int) -> None:
        self.wait_until = (now + BACKOFF[min(attempt, len(BACKOFF) - 1)]).isoformat()

    def ready(self, now: datetime) -> bool:
        return bool(self.queue) and (self.wait_until is None or now >= datetime.fromisoformat(self.wait_until))

    def reset_backoff(self) -> None:
        """A user action (add, remove, explicit retry) gives the builder a fresh start."""
        self.failures = 0
        self.wait_until = None
        self.attempt = 0
        self.stopped_since = None
