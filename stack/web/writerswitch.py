"""Switching a car's writer from the pages: the choices a car has, the switch run as a
background job (history_writer.switch, under the car's lock), and the trouble a writer can
be in that the owner should see on the car's card and on Connect Tesla.

A switch reads days of records, so the page that asks for it only records the request and
returns; the card and step 6 show "switching…" until the job says how it went."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

import history_writer

LOG = logging.getLogger("carcommand.writer")
QUIET = timedelta(minutes=30)  # tesla_live.QUIET: a connected car silent this long has been lost, not parked


def options(writer: dict[str, str], stream: set[str], feed: set[str], mate: set[str]) -> dict[str, list[dict]]:
    """Per car (by app.source_of's word for its writer now): the sources that can write its history."""
    return {
        vin: history_writer.options(history_writer.KIND_OF_SOURCE.get(kind), {"tesla": vin in stream, "teslafi": vin in feed, "teslamate": vin in mate})
        for vin, kind in writer.items()
        if kind != "live"
    }


def silent(kind: str | None, heard: dict | None, others: list[str], now: datetime) -> str | None:
    """What to tell the owner when the stream writes a car and has gone silent without the
    car saying goodbye (a sleeping car says it): since when, and how to fill the stretch."""
    if kind != "tesla" or not heard or not heard.get("connected") or now - heard["at"] <= QUIET:
        return None
    fill = f" {' and '.join(others)} still {'has' if len(others) == 1 else 'have'} its raw records from then: switch back to fill the gap." if others else ""
    return f"The stream has been silent since {heard['at']:%b %-d %H:%M} UTC without the car saying goodbye, so nothing since is written.{fill}"


class Switcher:
    """One switch per car at a time, each in a thread; how it went is kept for the pages."""

    def __init__(self, connect, tz, after=None, spawn=None, clock=None) -> None:
        self.connect, self.tz, self.after = connect, tz, after
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True, name="writer-switch").start())
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def request(self, vin: str, new: str, current: str | None, last_ok: datetime | None = None, teslamate_spans=None) -> bool:
        """Start a switch; False when one is already running for this car."""
        with self.lock:
            if (self.jobs.get(vin) or {}).get("state") == "switching":
                return False
            self.jobs[vin] = {"state": "switching", "to": new, "name": history_writer.NAMES.get(new, new), "at": self.clock(), "boundary": None, "problem": None}
        self.spawn(lambda: self.run(vin, new, current, last_ok, teslamate_spans))
        return True

    def run(self, vin: str, new: str, current: str | None, last_ok, teslamate_spans) -> None:
        b = problem = None
        try:
            with self.connect() as conn:
                b, problem = history_writer.switch(conn, vin, new, current, self.clock(), self.tz, last_ok, teslamate_spans)
        except Exception as err:  # noqa: BLE001 - said on the page; the lock went with the rollback
            LOG.warning("switching %s to %s: %s", vin, new, err)
            problem = f"The switch failed: {type(err).__name__}: {err}"[:240]
        if not problem and self.after:
            try:
                self.after(vin, new)
            except Exception as err:  # noqa: BLE001
                LOG.warning("after switching %s: %s", vin, err)
        with self.lock:
            self.jobs[vin] = dict(self.jobs.get(vin) or {}, state="refused" if problem else "done", boundary=b, problem=problem, at=self.clock())

    def status(self, vin: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(vin)
            return dict(job) if job else None
