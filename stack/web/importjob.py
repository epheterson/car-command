"""History import from the browser: TeslaFi exports uploaded to the site, or a TeslaMate database.

One job at a time, run on a thread of the web process and never on a request. The loaders are
the command-line ones (teslafi_load.parse/store, teslamate_load.write_car), so a file loads the
same either way and a second run replaces what the first wrote (the import id is the file's
hash, or `tm-<vin>` for TeslaMate) instead of doubling it.

State is one small JSON file beside the uploads, written on every change and read once at
start. A job found "running" at start was cut off by a restart: it is marked interrupted, and a
TeslaFi job can resume from the files still on disk. The TeslaMate address is held in memory
only, so that one is started again from the form.

Uploads stream to disk under CC_IMPORT_DIR (the data volume) in chunks, with a size cap; nothing
here holds a whole file in memory.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger("carcommand.importjob")

DEFAULT_DIR = "/data/imports"
MAX_FILE = int(float(os.environ.get("CC_IMPORT_MAX_MB", "1024")) * (1 << 20))  # per file, as uploaded
MAX_FILES = 8
MAX_STAGED = 4 * MAX_FILE
CHUNK = 1 << 20
NAME_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ \-]{0,118}\.(csv|csv\.gz|gz)", re.IGNORECASE)
# Columns a TeslaFi export always has; the first row of data must also carry a date we can read.
REQUIRED = ("Date", "battery_level")
STATES = ("running", "done", "failed", "interrupted")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_name(raw: str | None) -> str | None:
    """The file name as kept: no path, no odd characters, a .csv, .csv.gz or .gz ending. None when it is not one."""
    name = os.path.basename((raw or "").replace("\\", "/")).strip()
    return name if NAME_OK.fullmatch(name) else None


def validate_teslafi(path: Path) -> tuple[bool, str]:
    """Is this a TeslaFi export? Reads the first 64 KB only (gunzipped, if it is gzip), so a
    wrong file is turned away before an import begins and a gzip bomb costs nothing."""
    try:
        with open(path, "rb") as fh:
            gz = fh.read(2) == b"\x1f\x8b"
        opener = gzip.open if gz else open
        with opener(path, "rb") as fh:
            head = fh.read(1 << 16)
    except (OSError, EOFError, gzip.BadGzipFile):
        return False, "Not a readable CSV or gzip file."
    text = head.decode("utf-8", errors="replace")
    if "\x00" in text:
        return False, "Not a text export."
    lines = text.splitlines()
    if len(lines) < 2:
        return False, "No data rows."
    import csv

    header = next(csv.reader([lines[0].lstrip("﻿")]), [])
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        return False, "Not a TeslaFi export: no " + ", ".join(missing) + " column."
    from teslafi_import import sample_from_row

    rows = csv.DictReader(io.StringIO("\n".join(lines[:60])))
    if not any(sample_from_row(r) is not None for r in rows):
        return False, "No row with a date this importer reads."
    return True, ""


class Upload:
    """One file being received: chunks in, a size cap, and a rename only once it is whole and valid."""

    def __init__(self, store: "Store", name: str, declared: int | None) -> None:
        self.store, self.name = store, name
        self.path = store.staged_dir / (name + ".part")
        self.size = 0
        self.fh = open(self.path, "wb")

    def write(self, chunk: bytes) -> str | None:
        """None while it fits; the reason once it does not (the caller aborts)."""
        self.size += len(chunk)
        if self.size > MAX_FILE:
            return f"Larger than the {MAX_FILE >> 20} MB limit per file."
        self.fh.write(chunk)
        return None

    def abort(self) -> None:
        self.fh.close()
        self.path.unlink(missing_ok=True)

    def finish(self) -> str | None:
        """None when kept; the reason when the file is turned away."""
        self.fh.close()
        if self.size == 0:
            self.path.unlink(missing_ok=True)
            return "The file is empty."
        ok, why = validate_teslafi(self.path)
        if not ok:
            self.path.unlink(missing_ok=True)
            return why
        return self.store.keep(self.name, self.path, self.size)


class Store:
    """The staged files, the one job, and the thread that runs it.

    `loader(path, progress) -> dict` loads one TeslaFi file; `mate(url, progress, item_done)`
    runs a TeslaMate import; `after()` names the places at the end. All three are injected, so
    the state machine is testable without a database."""

    def __init__(self, root: str | os.PathLike | None, loader=None, mate=None, after=None, threaded: bool = True) -> None:
        self.root = Path(root or os.environ.get("CC_IMPORT_DIR") or DEFAULT_DIR)
        self.staged_dir, self.job_dir = self.root / "staged", self.root / "job"
        self.loader, self.mate, self.after, self.threaded = loader, mate, after, threaded
        self.lock = threading.RLock()
        self.secret: str | None = None  # a TeslaMate address, in memory only
        self.typed: str | None = None  # the address of a refused attempt, handed back to the form once, never saved
        self.saved_at = 0.0
        self.state: dict = {"staged": [], "job": None, "notice": None}
        self.thread: threading.Thread | None = None

    # ---- the state file
    @property
    def file(self) -> Path:
        return self.root / "state.json"

    def recover(self) -> None:
        """At start: read the file; a job that was running was interrupted by the restart."""
        try:
            self.staged_dir.mkdir(parents=True, exist_ok=True)
            self.job_dir.mkdir(parents=True, exist_ok=True)
            for part in self.staged_dir.glob("*.part"):
                part.unlink(missing_ok=True)  # an upload the restart cut off
            self.state = {**self.state, **json.loads(self.file.read_text())}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as err:
            LOG.warning("import state unreadable (%s); starting empty", type(err).__name__)
        job = self.state.get("job")
        if job and job.get("state") == "running":
            job["state"], job["step"], job["finished"] = "interrupted", "The site restarted while this was running.", now_iso()
            for it in job["items"]:
                if it["state"] == "running":
                    it["state"], it["detail"] = "pending", "Interrupted"
            self.save(force=True)
        # staged files that vanished from disk are not offered
        self.state["staged"] = [f for f in self.state.get("staged", []) if (self.staged_dir / f["name"]).is_file()]

    def save(self, force: bool = False) -> None:
        """Atomic, and at most once a second unless forced: progress ticks are cheap, state changes are not lost."""
        with self.lock:
            if not force and time.monotonic() - self.saved_at < 1.0:
                return
            self.saved_at = time.monotonic()
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                tmp = self.file.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.state))
                os.replace(tmp, self.file)
            except OSError as err:
                LOG.warning("import state not saved: %s", type(err).__name__)

    def view(self) -> dict:
        """A copy for the page and the status endpoint."""
        with self.lock:
            return json.loads(json.dumps({**self.state, "max_mb": MAX_FILE >> 20, "busy": self.busy()}))

    def busy(self) -> bool:
        job = self.state.get("job")
        return bool(job and job["state"] == "running")

    def take_typed(self) -> str | None:
        """The address typed into a form that was refused, once: the form opens holding it."""
        with self.lock:
            typed, self.typed = self.typed, None
            return typed

    def say(self, kind: str, text: str) -> None:
        with self.lock:
            self.state["notice"] = {"kind": kind, "text": text}
            self.save(force=True)

    # ---- staging
    def begin(self, raw_name: str | None, declared: int | None) -> tuple[Upload | None, str | None]:
        name = clean_name(raw_name)
        with self.lock:
            if self.busy():
                return None, "An import is running; wait for it to finish."
            if name is None:
                return None, "A TeslaFi export is a .csv or .csv.gz file."
            if declared is not None and declared > MAX_FILE:
                return None, f"Larger than the {MAX_FILE >> 20} MB limit per file."
            others = [f for f in self.state["staged"] if f["name"] != name]
            if len(others) >= MAX_FILES:
                return None, f"At most {MAX_FILES} files at a time."
            if sum(f["size"] for f in others) + (declared or 0) > MAX_STAGED:
                return None, "Too much waiting to import; start these first."
            try:
                self.staged_dir.mkdir(parents=True, exist_ok=True)
                free = shutil.disk_usage(self.staged_dir).free
            except OSError as err:
                return None, f"No room to keep uploads here ({type(err).__name__})."
            if declared and declared * 2 > free:
                return None, "Not enough free disk space for this file."
            return Upload(self, name, declared), None

    def keep(self, name: str, part: Path, size: int) -> str | None:
        with self.lock:
            if self.busy():
                part.unlink(missing_ok=True)
                return "An import is running; wait for it to finish."
            os.replace(part, self.staged_dir / name)
            self.state["staged"] = [f for f in self.state["staged"] if f["name"] != name] + [{"name": name, "size": size}]
            self.state["notice"] = None
            self.save(force=True)
        return None

    def unstage(self, name: str | None = None) -> None:
        with self.lock:
            if self.busy():
                return
            gone = [f for f in self.state["staged"] if name is None or f["name"] == name]
            for f in gone:
                (self.staged_dir / f["name"]).unlink(missing_ok=True)
            self.state["staged"] = [f for f in self.state["staged"] if f not in gone]
            self.save(force=True)

    # ---- jobs
    def _new_job(self, kind: str, items: list[dict]) -> dict:
        return {
            "id": uuid.uuid4().hex[:12],
            "kind": kind,
            "state": "running",
            "started": now_iso(),
            "finished": None,
            "step": "Starting",
            "pct": 0,
            "items": items,
            "result": {"drives": 0, "new_drives": 0, "charges": 0, "days": 0, "positions": 0},
            "places": None,
            "error": None,
        }

    def _launch(self, target, *args) -> None:
        if self.threaded:
            self.thread = threading.Thread(target=target, args=args, daemon=True, name="history-import")
            self.thread.start()
        else:
            target(*args)

    def _clear_job_files(self) -> None:
        shutil.rmtree(self.job_dir, ignore_errors=True)
        self.job_dir.mkdir(parents=True, exist_ok=True)

    def start_teslafi(self) -> str | None:
        """Turn the staged files into the job and run it. The reason when it cannot."""
        with self.lock:
            if self.busy():
                return "An import is already running."
            staged = list(self.state["staged"])
            if not staged:
                return "Upload a TeslaFi export first."
            self._clear_job_files()
            items = []
            for f in staged:
                os.replace(self.staged_dir / f["name"], self.job_dir / f["name"])
                items.append({"name": f["name"], "size": f["size"], "state": "pending", "detail": "", "pct": 0})
            self.state.update(staged=[], notice=None, job=self._new_job("teslafi", items))
            self.save(force=True)
            jid = self.state["job"]["id"]
        self._launch(self._run_teslafi, jid)
        return None

    def resume(self, restart: bool = False) -> str | None:
        """An interrupted (or failed) TeslaFi job again, from the files still on disk: the unfinished ones, or all of them."""
        with self.lock:
            job = self.state.get("job")
            if self.busy():
                return "An import is already running."
            if not job or job["kind"] != "teslafi" or job["state"] == "done":
                return "Nothing to resume."
            for it in job["items"]:
                if not (self.job_dir / it["name"]).is_file():
                    it["state"], it["detail"] = "skipped", "The uploaded file is gone; upload it again."
                elif restart or it["state"] != "done":
                    it["state"], it["detail"], it["pct"] = "pending", "", 0
            if restart:
                job["result"] = {"drives": 0, "new_drives": 0, "charges": 0, "days": 0, "positions": 0}
            job.update(state="running", finished=None, error=None, step="Starting", places=None)
            self.save(force=True)
            jid = job["id"]
        self._launch(self._run_teslafi, jid)
        return None

    def start_teslamate(self, url: str, cars: list[dict]) -> str | None:
        with self.lock:
            if self.busy():
                return "An import is already running."
            if not cars:
                return "That database has no cars."
            self._clear_job_files()
            items = [{"name": c.get("name") or c["vin"][-6:], "vin": c["vin"], "size": 0, "state": "pending", "detail": "", "pct": 0, "car": c} for c in cars]
            self.secret = url
            self.state.update(notice=None, job=self._new_job("teslamate", items))
            self.save(force=True)
            jid = self.state["job"]["id"]
        self._launch(self._run_teslamate, jid)
        return None

    def dismiss(self) -> None:
        with self.lock:
            if self.busy():
                return
            self.state["job"], self.state["notice"], self.secret = None, None, None
            self._clear_job_files()
            self.save(force=True)

    # ---- running
    def _job(self, jid: str) -> dict | None:
        job = self.state.get("job")
        return job if job and job["id"] == jid else None

    def _progress(self, jid: str, item: dict, step: str, pct: float, n_done: int, n_total: int) -> None:
        with self.lock:
            if not self._job(jid):
                return
            item["pct"] = int(pct)
            self.state["job"]["step"] = step
            self.state["job"]["pct"] = int((n_done + pct / 100) * 100 / max(n_total, 1))
            self.save()

    def _add(self, job: dict, got: dict) -> None:
        for k in ("drives", "new_drives", "charges", "days", "positions"):
            # a loader that cannot tell new from replaced (TeslaMate's) counts every drive as new
            job["result"][k] = job["result"].get(k, 0) + int((got.get(k, got.get("drives")) if k == "new_drives" else got.get(k)) or 0)

    def _finish(self, jid: str, error: str | None = None) -> None:
        with self.lock:
            job = self._job(jid)
            if not job:
                return
            loaded = any(i["state"] == "done" for i in job["items"])
            job["error"] = error
            job["state"] = "done" if loaded and not error else "failed"
            if job["state"] == "failed" and not error:
                why = [i["detail"] for i in job["items"] if i["state"] == "skipped" and i.get("detail")]
                job["error"] = f"Nothing was imported: {why[0]}" if len(why) == 1 else "Nothing was imported; each file says why."
            job["pct"] = 100 if job["state"] == "done" else job["pct"]
            job["step"] = "Done" if job["state"] == "done" else "Stopped"
            job["finished"] = now_iso()
            self.secret = None
            self.save(force=True)

    def _name_places(self, jid: str) -> None:
        if not self.after:
            return
        with self.lock:
            if self._job(jid):
                self.state["job"]["step"] = "Naming places"
                self.save(force=True)
        try:
            n = self.after()
        except Exception as err:  # noqa: BLE001 - the history is in; places can be named from the Places page
            LOG.warning("import: naming places failed (%s)", type(err).__name__)
            return
        with self.lock:
            if self._job(jid):
                self.state["job"]["places"] = n
                self.save(force=True)

    def _run_teslafi(self, jid: str) -> None:
        try:
            job = self._job(jid)
            todo = [i for i in job["items"] if i["state"] == "pending"]
            n_total = len(job["items"])
            for it in todo:
                n_done = sum(1 for i in job["items"] if i["state"] in ("done", "skipped"))
                with self.lock:
                    it["state"], it["detail"] = "running", ""
                    job["step"] = f"Reading {it['name']}"
                    self.save(force=True)
                path = self.job_dir / it["name"]
                try:
                    got = self.loader(path, lambda pct, step=None, it=it, nd=n_done: self._progress(jid, it, step or f"Reading {it['name']}", pct, nd, n_total))
                except Skip as why:
                    with self.lock:
                        it["state"], it["detail"] = "skipped", str(why)
                except Exception as err:  # noqa: BLE001 - one bad file does not stop the others
                    LOG.warning("import of %s failed: %s", it["name"], type(err).__name__)
                    with self.lock:
                        it["state"], it["detail"] = "skipped", explain(err)
                else:
                    with self.lock:
                        it.update(state="done", pct=100, detail=got.get("detail", ""))
                        self._add(job, got)
                        path.unlink(missing_ok=True)  # loaded: the upload is not kept
                with self.lock:
                    self.save(force=True)
            if any(i["state"] == "done" for i in job["items"]):
                self._name_places(jid)
            self._finish(jid)
        except Exception as err:  # noqa: BLE001
            LOG.exception("history import crashed")
            self._finish(jid, explain(err))

    def _run_teslamate(self, jid: str) -> None:
        try:
            job = self._job(jid)
            n_total = len(job["items"])
            for k, it in enumerate(job["items"]):
                with self.lock:
                    it["state"], it["detail"] = "running", ""
                    job["step"] = f"Reading {it['name']}"
                    self.save(force=True)
                try:
                    got = self.mate(self.secret, it["car"], lambda pct, step=None, it=it, nd=k: self._progress(jid, it, step or f"Reading {it['name']}", pct, nd, n_total))
                except Skip as why:
                    with self.lock:
                        it["state"], it["detail"] = "skipped", str(why)
                except Exception as err:  # noqa: BLE001
                    LOG.warning("teslamate import of one car failed: %s", type(err).__name__)
                    with self.lock:
                        it["state"], it["detail"] = "skipped", explain(err)
                else:
                    with self.lock:
                        it.update(state="done", pct=100, detail=got.get("detail", ""))
                        self._add(job, got)
                with self.lock:
                    self.save(force=True)
            if any(i["state"] == "done" for i in job["items"]):
                self._name_places(jid)
            self._finish(jid)
        except Exception as err:  # noqa: BLE001
            LOG.exception("history import crashed")
            self._finish(jid, explain(err))


def explain(err: BaseException) -> str:
    """What stopped an import, in words a person can act on. The type name stays in the log."""
    state = getattr(err, "sqlstate", None)
    if state == "23505":
        return "Some of it overlaps history already held and the two could not be merged. Try again."
    if state or type(err).__name__ in ("OperationalError", "InterfaceError", "PoolTimeout"):
        return "The database did not answer. Try again in a minute."
    if isinstance(err, (EOFError, gzip.BadGzipFile, zlib.error)):
        return "The file ends early or is damaged. Download the export again and upload that."
    if isinstance(err, (UnicodeError, ValueError, KeyError)):
        return "This file is not a TeslaFi export that can be read. Download it again from TeslaFi."
    if isinstance(err, MemoryError):
        return "The file is too large for this machine to read in one go."
    if isinstance(err, OSError):
        return "The file could not be read or written; check the disk has room. Try again."
    return "It stopped on an unexpected error; the site's log has the details. Try again."


class Skip(Exception):
    """A file or car that cannot be imported, with the reason to show."""


# ---- the real loaders (database writes); the Store takes any callable of the same shape
def load_teslafi_file(connect):
    """A loader for Store: parse the export with progress, then write it. `connect()` is a database connection."""

    def load(path: Path, progress) -> dict:
        import teslafi_load

        def on_read(read: int, total: int, newest) -> None:
            progress(read * 90 / total, f"Reading {path.name}: through {newest:%b %Y}")

        out = teslafi_load.parse(path, on_read)
        if not out["rows"]:
            raise Skip("No rows parsed; is this a TeslaFi export?")
        veh = out["vehicle"]
        vin = veh.get("vin")
        if not vin:
            raise Skip("No VIN in the file.")
        name = veh.get("name") or vin
        progress(92, f"Writing {name}")
        with connect() as conn:
            live = teslafi_load.live_since(conn, vin)
            if live and live <= out["first"]:
                raise Skip(f"{name} is logged live from {live:%b %-d, %Y}; this export holds nothing before that.")
            had = trips_held(conn, vin)
            counts = teslafi_load.store(path, out, vin, name, conn)
            counts["new_trips"] = max(0, trips_held(conn, vin) - had)
        if live and live < out["last"]:
            out = {**out, "last": live}  # the result counts the days the export added, not the ones the live source keeps
        return result_of(out, counts, name)

    return load


def trips_held(conn, vin: str) -> int:
    """The drives this car holds now: read either side of a write, the difference is what it added."""
    row = conn.execute("SELECT count(*) FROM trips WHERE vin = %s", (vin,)).fetchone()
    return int((row[0] if not isinstance(row, dict) else next(iter(row.values()))) or 0) if row else 0


def result_of(out: dict, counts: dict, name: str) -> dict:
    days = (out["last"].date() - out["first"].date()).days + 1
    return {
        "drives": counts.get("trips", 0),
        "new_drives": counts.get("new_trips", counts.get("trips", 0)),
        "charges": counts.get("charging_sessions", 0),
        "days": days,
        "positions": counts.get("cc_positions", 0),
        "detail": f"{name}, {out['first']:%b %Y} to {out['last']:%b %Y}",
    }


def load_teslamate_car(connect, tm_connect):
    """A `mate` for Store: read one car's whole history from TeslaMate's database, replay it, write it."""

    def run(url: str | None, car: dict, progress) -> dict:
        import teslamate_load
        from datetime import datetime as dt
        from teslafi_import import USABLE_KWH
        from teslamate_import import replay_tables
        from teslamate_live import TABLES, model_name

        if not url:
            raise Skip("The database address was not kept; start again.")
        since = dt(1970, 1, 1)
        name, vin = car.get("name") or car["vin"], car["vin"]
        tables = {}
        with tm_connect(url) as tm:
            for i, (table, sql) in enumerate(TABLES.items()):
                progress(i * 60 / len(TABLES), f"Reading {name}: {table.replace('_', ' ')}")
                tables[table] = [dict(r) for r in tm.execute(sql, (car["id"], since)).fetchall()]
        progress(65, f"Replaying {name}")
        out = replay_tables(tables, vin, name)
        del tables
        if not out["rows"]:
            raise Skip("No history for this car.")
        model = model_name(car)
        progress(85, f"Writing {name}")
        with connect() as conn:
            counts = teslamate_load.write_car(conn, out, vin, name, "Tesla", model, USABLE_KWH.get(model or ""), "teslamate import", say=lambda *_: None)
        return result_of(out, counts, name)

    return run
