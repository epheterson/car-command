#!/usr/local/bin/python3
"""Car Command in one container: Postgres, the schema step, the site and the connector.

The same programs the compose stack runs, from the same sources (stack/web, stack/cc,
stack/config), started in order and kept running by this file:

    postgres   the database, in /data/postgres, on 127.0.0.1 and a local socket only
    schema     stack/cc/init_db.py, once, before anything reads the tables
    web        the site on port 3044, as the user `web`
    cc         the connector, through stack/cc/entrypoint.sh, as compose runs it

Everything kept is under /data: postgres/, secrets/ (made on the first start, one file per
value, root's alone), logs/ (bounded), plugins/ and connector/ (the connector's files).
Settings come from the environment under the names stack/.env uses; the passwords and the
key are generated here instead, and once written, the files win over the environment.

A process that stops is started again after 1, 2, 4 ... seconds, at most five minutes
apart; a run of a minute or more resets the count. SIGTERM stops the site and the
connector, then gives Postgres a fast shutdown.

    carcommand start            the container's own command
    carcommand status | health  what is running; health is the container's healthcheck
    carcommand web CMD...       a command in the site's directory, as the site, with its settings
    carcommand demo             six weeks of a fictional car, the same steps as `make demo`
    carcommand backup > FILE    a pg_dump of the database (custom format)
    carcommand restore [--force] < FILE   into a stopped container's volume (docs/install.md)

Only the Python standard library: this runs before anything else is up.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import json
import logging
import logging.handlers
import os
import secrets as rnd
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

LOG = logging.getLogger("carcommand")

DATA = Path(os.environ.get("CC_DATA") or "/data")
RUN = Path(os.environ.get("CC_AIO_RUN") or "/run/aio")
PG_MAJOR = "18"
PG_BIN = Path(f"/usr/lib/postgresql/{PG_MAJOR}/bin")
PG_SOCKET = "/run/postgresql"
WEB_DIR, WEB_BIN, CC_DIR = "/srv/web", "/opt/web/bin", "/app"
BASE_PATH = "/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
PORT = 3044
# Internal to the container; .env's DB_USER and DB_NAME do not apply here. The site's role is
# never named like an OS user in the image: peer authentication over the socket would then let
# that user in as it (pg_hba.conf allows peer for postgres alone, but the names stay apart anyway).
DB_USER, DB_NAME, OLD_DB_USER = "cc_site", "carcommand", "carcommand"
PLUGIN_ROLE = "cc_plugin"  # the database plugin's own role, stack/cc/plugin_role.py
REST_URL = "http://127.0.0.1:40000"

BACKOFF_MAX_S = 300
HEALTHY_RUN_S = 60  # a run this long was not a crash loop: the next failure starts the count again
WEB_CHECK_S, WEB_CHECK_FAILS = 30, 4
WEB_KILL_AFTER_S = 10  # a site asked to stop that has not, after this long, is killed
STOP_OTHERS_S, STOP_DB_S = 6, 20  # within docker stop --stop-timeout 30
LOG_BYTES, LOG_KEEP = 2_000_000, 2  # per process: the file and two older ones, about 6 MB


def _hex() -> str:
    return rnd.token_hex(24)


def _fernet() -> str:
    """32 random bytes, URL-safe base64: a Fernet key, as `make setup` writes one."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


# What the first start generates, under the names stack/.env gives them.
GENERATED = {"DB_PASSWORD": _hex, "WEBUI_PASSWORD": _hex, "REST_PASSWORD": _hex, "CC_SECRET_KEY": _fernet}
# What the site never needs to see, whatever the container was started with.
NOT_FOR_WEB = {"DB_PASSWORD", "WEBUI_PASSWORD", "DB_HOST", "DB_USER", "DB_NAME", "PYTHONPATH", "PYTHONUSERBASE", "PYTHONNOUSERSITE", "CC_DATA", "CC_AIO_RUN"}


# ---------------------------------------------------------------- secrets


def valid_key(value: str) -> bool:
    try:
        return len(base64.urlsafe_b64decode(value.encode())) == 32
    except (binascii.Error, ValueError):
        return False


def write_secret(path: Path, value: str) -> None:
    """Written whole or not at all, 0600 from the first byte."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def ensure_secrets(directory: Path, env, make=GENERATED) -> dict:
    """One file per value. Absent: taken from the environment if it is set there (a move
    from compose brings CC_SECRET_KEY this way), generated if not. Present: the file wins,
    because the database's password and the key that opens sealed sources must not change
    because a container was started with different flags."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    out = {}
    for name, gen in make.items():
        path, given = directory / name, (env.get(name) or "").strip()
        if path.exists():
            value = path.read_text().strip()
            if given and given != value:
                LOG.warning("%s is set in the environment, but %s already holds one; using the file. To change it, change the file.", name, path)
        else:
            if name == "CC_SECRET_KEY" and given and not valid_key(given):
                raise SystemExit("CC_SECRET_KEY in the environment is not a key (32 bytes, URL-safe base64). Leave it unset and one is generated.")
            value = given or gen()
            write_secret(path, value)
            LOG.info("%s: %s, kept in %s", name, "taken from the environment" if given else "generated", path)
        if not value:
            raise SystemExit(f"{path} is empty. Delete it to have one generated, or put the value back.")
        out[name] = value
    return out


# ---------------------------------------------------------------- environments


def db_url(s: dict) -> str:
    return f"postgresql://{DB_USER}:{s['DB_PASSWORD']}@127.0.0.1:5432/{DB_NAME}"


def _polls(env) -> dict:
    return {"CC_POLL_IDLE_S": env.get("CC_POLL_IDLE_S") or "60", "CC_POLL_DRIVING_S": env.get("CC_POLL_DRIVING_S") or "15"}


def web_env(env, s: dict) -> dict:
    """The container's settings (the names stack/.env uses), plus what compose wired in."""
    out = {k: v for k, v in env.items() if k not in NOT_FOR_WEB}
    out.update(_polls(env))
    out.update(
        PATH=f"{WEB_BIN}:{BASE_PATH}",
        HOME="/tmp",
        DATABASE_URL=db_url(s),
        CC_REST_URL=REST_URL,
        REST_USER=env.get("REST_USER") or "admin",
        REST_PASSWORD=s["REST_PASSWORD"],
        CC_SECRET_KEY=s["CC_SECRET_KEY"],
    )
    return out


def cc_env(env, s: dict) -> dict:
    """What compose hands the connector, and nothing of the site's (alert channels, Tesla app)."""
    out = {
        "PATH": BASE_PATH,
        "LANG": env.get("LANG") or "en_US.UTF-8",
        "LC_ALL": env.get("LC_ALL") or "en_US.UTF-8",
        "HOME": "/tmp",
        "PYTHONPATH": CC_DIR,
        # Other brands' connectors install here (pip --user); root's Python never reads it.
        "PYTHONUSERBASE": str(DATA / "plugins"),
        "PYTHONNOUSERSITE": "1",
        "DB_HOST": "127.0.0.1",
        "DB_USER": DB_USER,
        "DB_NAME": DB_NAME,
        "DB_PASSWORD": s["DB_PASSWORD"],
        "WEBUI_USER": env.get("WEBUI_USER") or "admin",
        "WEBUI_PASSWORD": s["WEBUI_PASSWORD"],
        "REST_USER": env.get("REST_USER") or "admin",
        "REST_PASSWORD": s["REST_PASSWORD"],
        "CC_SECRET_KEY": s["CC_SECRET_KEY"],
        "CC_CONNECTORS_JSON": env.get("CC_CONNECTORS_JSON") or "",
    }
    out.update(_polls(env))
    if env.get("TZ"):
        out["TZ"] = env["TZ"]
    return out


def schema_env(env, s: dict) -> dict:
    return {"PATH": BASE_PATH, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "HOME": "/tmp", "PYTHONPATH": CC_DIR, "DATABASE_URL": db_url(s)}


def ids(name: str) -> tuple[int, int]:
    import pwd

    p = pwd.getpwnam(name)
    return p.pw_uid, p.pw_gid


def as_user(name: str | None) -> dict:
    """Popen arguments that run a child as `name`, when this is root (it is, in the image)."""
    if not name or os.geteuid() != 0:
        return {}
    uid, gid = ids(name)
    return {"user": uid, "group": gid, "extra_groups": []}


# ---------------------------------------------------------------- logs


class Logs:
    """Each process's lines, prefixed, to the container's output and to /data/logs/NAME.log,
    which rotates at LOG_BYTES and keeps LOG_KEEP older files."""

    def __init__(self, directory: Path | None, out=None):
        self.directory, self.out, self.lock, self.files = directory, out or sys.stdout, threading.Lock(), {}
        if directory:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _file(self, name: str):
        if self.directory is None:
            return None
        if name not in self.files:
            h = logging.handlers.RotatingFileHandler(self.directory / f"{name}.log", maxBytes=LOG_BYTES, backupCount=LOG_KEEP, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(message)s"))
            lg = logging.getLogger(f"carcommand.file.{name}")
            lg.propagate, lg.handlers = False, [h]
            lg.setLevel(logging.INFO)
            self.files[name] = lg
        return self.files[name]

    def line(self, name: str, text: str) -> None:
        text = text.rstrip("\n")
        with self.lock:
            self.out.write(f"{name:<8}| {text}\n")
            self.out.flush()
        f = self._file(name)
        if f:
            f.info("%s %s", time.strftime("%Y-%m-%dT%H:%M:%S"), text)

    def pump(self, name: str, stream) -> threading.Thread:
        def run():
            for raw in iter(stream.readline, b""):
                self.line(name, raw.decode("utf-8", "replace"))
            stream.close()

        t = threading.Thread(target=run, name=f"log-{name}", daemon=True)
        t.start()
        return t


class _ToLogs(logging.Handler):
    def __init__(self, logs: Logs):
        super().__init__()
        self.logs = logs

    def emit(self, record):
        self.logs.line("supervise", f"{record.levelname} {record.getMessage()}")


# ---------------------------------------------------------------- processes


def backoff(failures: int) -> int:
    """Seconds before the next start after `failures` short runs in a row."""
    return min(BACKOFF_MAX_S, 2 ** max(0, failures - 1))


class Proc:
    """One long-running program: started, watched, started again after a pause, stopped."""

    def __init__(self, name, argv, env, cwd="/", user=None, stop_signal=signal.SIGTERM, whole_group=True, hint=""):
        self.name, self.argv, self.env, self.cwd, self.user = name, argv, env, cwd, user
        self.stop_signal, self.whole_group, self.hint = stop_signal, whole_group, hint
        self.child, self.started, self.failures, self.restarts = None, None, 0, 0
        self.next_start, self.last_exit, self.state = 0.0, None, "stopped"

    def start(self, now: float, spawn, logs: Logs) -> None:
        self.child = spawn(self)
        if getattr(self.child, "stdout", None) is not None:
            logs.pump(self.name, self.child.stdout)
        self.started, self.state = now, "running"
        LOG.info("%s started (pid %s)", self.name, self.child.pid)

    def exited(self, now: float) -> bool:
        """True once, when a running child has gone; schedules its next start."""
        if self.state != "running" or self.child.poll() is None:
            return False
        code, ran = self.child.returncode, now - self.started
        self.failures = 1 if ran >= HEALTHY_RUN_S else self.failures + 1
        delay = backoff(self.failures)
        self.last_exit, self.next_start, self.state, self.restarts = code, now + delay, "waiting", self.restarts + 1
        LOG.warning("%s exited with %s after %d s; starting it again in %d s%s", self.name, code, ran, delay, f". {self.hint}" if self.hint and self.failures >= 2 else "")
        return True

    def due(self, now: float) -> bool:
        return self.state == "waiting" and now >= self.next_start

    def signal(self, sig) -> None:
        if self.child is None or self.child.poll() is not None:
            return
        try:
            if self.whole_group:
                os.killpg(self.child.pid, sig)
            else:
                self.child.send_signal(sig)
        except ProcessLookupError:
            pass

    def alive(self) -> bool:
        return self.child is not None and self.child.poll() is None

    def status(self, now: float) -> dict:
        out = {"state": self.state, "restarts": self.restarts, "last_exit": self.last_exit}
        if self.state == "running":
            out.update(pid=self.child.pid, up_s=int(now - self.started))
        if self.state == "waiting":
            out["next_start_in_s"] = max(0, int(self.next_start - now))
        return out


def spawn(proc: Proc):
    return subprocess.Popen(proc.argv, env=proc.env, cwd=proc.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, **as_user(proc.user))


def stop_all(procs: list[Proc], timeout: float, clock=time.monotonic, sleep=time.sleep) -> None:
    """Signal them together, wait up to `timeout`, then kill whatever is left."""
    for p in procs:
        p.signal(p.stop_signal)
    deadline = clock() + timeout
    while any(p.alive() for p in procs) and clock() < deadline:
        sleep(0.1)
    for p in procs:
        if p.alive():
            LOG.warning("%s did not stop in %d s; killing it", p.name, timeout)
            p.signal(signal.SIGKILL)
    for p in procs:
        if p.child is not None:
            try:
                p.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        p.state = "stopped"


def web_answers(timeout: float = 5) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - any failure is no answer
        return False


class Supervisor:
    """Starts what is due, notices what stopped, restarts the site when it stops answering."""

    def __init__(self, procs: list[Proc], logs: Logs, spawn=spawn, check_web=web_answers, status_path: Path | None = None):
        self.procs, self.logs, self.spawn, self.check_web, self.status_path = procs, logs, spawn, check_web, status_path
        self.web_fails, self.web_checked, self.web_term_at = 0, 0.0, None

    def get(self, name: str) -> Proc:
        return next(p for p in self.procs if p.name == name)

    def tick(self, now: float) -> None:
        for p in self.procs:
            p.exited(now)
            if p.due(now):
                p.start(now, self.spawn, self.logs)
        self._watch_web(now)
        self._write_status(now)

    def _watch_web(self, now: float) -> None:
        web = next((p for p in self.procs if p.name == "web"), None)
        if web is None or web.state != "running":
            self.web_term_at = None
            return
        if self.web_term_at is not None:
            # Asked to stop and still there: a hung worker ignores SIGTERM. Its whole group goes.
            if now - self.web_term_at >= WEB_KILL_AFTER_S:
                LOG.warning("the site did not stop %d s after being asked; killing it", WEB_KILL_AFTER_S)
                web.signal(signal.SIGKILL)
                self.web_term_at = None
            return
        if now - web.started < HEALTHY_RUN_S or now - self.web_checked < WEB_CHECK_S:
            return
        self.web_checked = now
        if self.check_web():
            self.web_fails = 0
            return
        self.web_fails += 1
        if self.web_fails >= WEB_CHECK_FAILS:
            LOG.warning("the site has not answered /health %d times in a row; restarting it", self.web_fails)
            self.web_fails = 0
            web.signal(signal.SIGTERM)
            self.web_term_at = now

    def _write_status(self, now: float) -> None:
        if not self.status_path:
            return
        body = {"at": int(time.time()), "processes": {p.name: p.status(now) for p in self.procs}}
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body))
        os.replace(tmp, self.status_path)


# ---------------------------------------------------------------- Postgres


def pg(name: str) -> str:
    return str(PG_BIN / name)


def run_logged(argv, logs: Logs, name: str, **kw) -> int:
    """A one-shot program, its output in the logs; returns its exit code."""
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=kw.pop("stdin", subprocess.DEVNULL), **kw)
    logs.pump(name, p.stdout).join()
    return p.wait()


def pg_state(pgdata: Path) -> str:
    """ "ready" (a cluster of this major version), "empty" (nothing there yet), or why not."""
    version = pgdata / "PG_VERSION"
    if version.exists():
        v = version.read_text().strip()
        return "ready" if v == PG_MAJOR else f"Postgres {v}"
    if pgdata.exists() and any(pgdata.iterdir()):
        return "not a database"
    return "empty"


def prepare_dirs() -> None:
    # Each part may pass through /data to its own directory; none may list it or read another's.
    os.chmod(DATA, 0o711)
    for path, owner, mode in (
        (DATA / "postgres", "postgres", 0o700),
        (DATA / "plugins", "carcommand", 0o700),
        (DATA / "connector", "carcommand", 0o700),
        (Path(PG_SOCKET), "postgres", 0o750),  # the socket is Postgres's own: nobody else reaches it
    ):
        path.mkdir(parents=True, exist_ok=True)
        uid, gid = ids(owner)
        st = path.stat()
        if (st.st_uid, st.st_gid) != (uid, gid):
            # One level only, except Postgres's own directory, which must be all its own.
            if path == DATA / "postgres":
                subprocess.run(["chown", "-R", f"{uid}:{gid}", str(path)], check=True)
            else:
                os.chown(path, uid, gid)
        os.chmod(path, mode)
    RUN.mkdir(parents=True, exist_ok=True)


# Written on every start, over whatever is there: the superuser over the socket only as the OS
# user postgres (this supervisor, as postgres), every other role by password over loopback.
HBA = """# Written by carcommand on every start; changes here do not last.
local   all   postgres                peer
host    all   all       127.0.0.1/32  scram-sha-256
host    all   all       ::1/128       scram-sha-256
"""


def write_hba(pgdata: Path) -> None:
    path = pgdata / "pg_hba.conf"
    tmp = pgdata / ".pg_hba.conf.tmp"
    tmp.write_text(HBA)
    os.chmod(tmp, 0o600)
    if os.geteuid() == 0:
        os.chown(tmp, *ids("postgres"))
    os.replace(tmp, path)


def init_postgres(logs: Logs) -> None:
    pgdata = DATA / "postgres"
    state = pg_state(pgdata)
    if state == "empty":
        LOG.info("first start: creating the database in %s", pgdata)
        code = run_logged(
            [pg("initdb"), "-D", str(pgdata), "-U", "postgres", "--auth-local=peer", "--auth-host=scram-sha-256", "-E", "UTF8", "--locale=en_US.UTF-8"],
            logs,
            "initdb",
            env={"PATH": BASE_PATH, "LANG": "en_US.UTF-8", "TZ": "UTC"},
            **as_user("postgres"),
        )
        if code:
            raise SystemExit(f"initdb failed ({code}); {pgdata} is left as it is for a look")
    elif state != "ready":
        raise SystemExit(
            f"{pgdata} holds {state}, and this image runs Postgres {PG_MAJOR}. Nothing was changed. "
            "Take a dump with the image that wrote it (carcommand backup) and restore it into a new volume (docs/install.md)."
        )
    stale = pgdata / "postmaster.pid"
    if stale.exists():
        # This container holds the volume's lock, so no other Postgres is using it: the file
        # is left from a stop that did not finish. Its PID could belong to anything now.
        LOG.info("the database did not shut down cleanly last time; it recovers on start")
        stale.unlink()
    write_hba(pgdata)


def postgres_proc() -> Proc:
    argv = [
        pg("postgres"),
        "-D",
        str(DATA / "postgres"),
        "-c",
        "listen_addresses=127.0.0.1",
        "-c",
        f"unix_socket_directories={PG_SOCKET}",
        "-c",
        "unix_socket_permissions=0770",
        "-c",
        "timezone=UTC",
        "-c",
        "log_timezone=UTC",
    ]
    # SIGINT is Postgres's fast shutdown: sessions end, a checkpoint, done.
    return Proc("postgres", argv, {"PATH": BASE_PATH, "LANG": "en_US.UTF-8", "PGTZ": "UTC"}, cwd=str(DATA / "postgres"), user="postgres", stop_signal=signal.SIGINT, whole_group=False)


def pg_ready() -> bool:
    return subprocess.run([pg("pg_isready"), "-q", "-h", PG_SOCKET, "-U", "postgres"], **as_user("postgres")).returncode == 0


def wait_ready(proc: Proc, ready=pg_ready, clock=time.monotonic, sleep=time.sleep, every_s: float = 10) -> None:
    """Until Postgres accepts connections, however long that takes while it is alive: after an
    unclean stop it replays its log first, and stopping it halfway would only start that over."""
    t0 = said = clock()
    while True:
        if not proc.alive():
            raise SystemExit("Postgres stopped while starting; its lines above say why")
        if ready():
            return
        now = clock()
        if now - said >= every_s:
            LOG.info("the database is still starting (%d s); after an unclean stop it replays its log first", now - t0)
            said = now
        sleep(0.5)


def psql(sql: str, logs: Logs, db: str = "postgres") -> int:
    """SQL on stdin, as the superuser over the local socket: nothing secret on a command line."""
    p = subprocess.Popen(
        [pg("psql"), "-h", PG_SOCKET, "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=1", "-qAt"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={"PATH": BASE_PATH},
        **as_user("postgres"),
    )
    t = logs.pump("psql", p.stdout)
    p.stdin.write(sql.encode())
    p.stdin.close()
    t.join()
    return p.wait()


def role_sql(password: str) -> str:
    """Run as the superuser every start. The site's role: not a superuser, the database's owner,
    its password set from the file (so the file is what the password is), and CREATEROLE with
    ADMIN on the plugin's role only, which is what the connector's supervisor needs to give
    that role its password and grants (stack/cc/plugin_role.py). A volume from before the
    site's role was renamed keeps its tables: the old role is renamed."""
    quoted = password.replace("'", "''")
    return (
        f"SELECT format('ALTER ROLE %I RENAME TO %I', '{OLD_DB_USER}', '{DB_USER}') WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{OLD_DB_USER}') "
        f"AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DB_USER}') \\gexec\n"
        f"SELECT format('CREATE ROLE %I LOGIN', '{DB_USER}') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DB_USER}') \\gexec\n"
        f"ALTER ROLE {DB_USER} WITH LOGIN NOSUPERUSER CREATEROLE PASSWORD '{quoted}';\n"
        f"SELECT format('CREATE DATABASE %I OWNER %I', '{DB_NAME}', '{DB_USER}') WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '{DB_NAME}') \\gexec\n"
        f"ALTER DATABASE {DB_NAME} OWNER TO {DB_USER};\n"
        f"SELECT format('CREATE ROLE %I', '{PLUGIN_ROLE}') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{PLUGIN_ROLE}') \\gexec\n"
        f"GRANT {PLUGIN_ROLE} TO {DB_USER} WITH ADMIN OPTION;\n"
    )


# ---------------------------------------------------------------- start


_LOCK: dict = {}


def lock_volume() -> int:
    """One container per volume. Two Postgres servers on one data directory destroy it, and
    Postgres's own lock cannot see a server in another container; a kernel lock on a file in
    the volume can. Taken once per process."""
    if DATA in _LOCK:
        return _LOCK[DATA]
    DATA.mkdir(parents=True, exist_ok=True)
    fd = os.open(DATA / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise SystemExit(f"another container is using {DATA}; stop it first (docker ps shows which)")
    _LOCK[DATA] = fd
    return fd


def setup_logging(logs: Logs) -> None:
    root = logging.getLogger()
    root.handlers = [_ToLogs(logs)]
    root.setLevel(logging.INFO)


def bring_up_database(logs: Logs) -> tuple[dict, Proc]:
    """Everything up to a database the site can use: lock, directories, secrets, Postgres."""
    if os.geteuid() != 0:
        raise SystemExit("carcommand runs as root inside the container and starts each part as its own user; drop --user")
    lock_volume()
    prepare_dirs()
    s = ensure_secrets(DATA / "secrets", os.environ)
    init_postgres(logs)
    db = postgres_proc()
    db.start(time.monotonic(), spawn, logs)
    try:
        wait_ready(db)
        if psql(role_sql(s["DB_PASSWORD"]), logs):
            raise SystemExit("could not create the site's role and database")
    except BaseException:
        shutdown([db])
        raise
    return s, db


def shutdown(procs: list[Proc]) -> None:
    """Everything but Postgres first, then Postgres's fast shutdown: no client is left writing."""
    others = [p for p in procs if p.name != "postgres"]
    if others:
        stop_all(others, STOP_OTHERS_S)
    stop_all([p for p in procs if p.name == "postgres"], STOP_DB_S)


def serve(sup: Supervisor, stopping: threading.Event, clock=time.monotonic) -> None:
    """Tick until told to stop; whatever ends it, signal or bug, everything is stopped cleanly."""
    try:
        while not stopping.is_set():
            sup.tick(clock())
            stopping.wait(1)
        LOG.info("stopping: the site and the connector, then the database")
    finally:
        shutdown(sup.procs)


def web_argv() -> list[str]:
    # A short graceful shutdown: a stop or a restart is not held up by an open stream.
    return [f"{WEB_BIN}/uvicorn", "app:server", "--host", "0.0.0.0", "--port", str(PORT), "--no-proxy-headers", "--timeout-graceful-shutdown", "3"]


def start() -> int:
    t0 = time.monotonic()
    logs = Logs(DATA / "logs")
    setup_logging(logs)
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    s, db = bring_up_database(logs)
    env = dict(os.environ)
    try:
        if run_logged(["python3", f"{CC_DIR}/init_db.py"], logs, "schema", env=schema_env(env, s), cwd=CC_DIR, **as_user("carcommand")):
            raise SystemExit("the schema step failed; its lines above say why")
    except BaseException:
        shutdown([db])
        raise
    web = Proc("web", web_argv(), web_env(env, s), cwd=WEB_DIR, user="web")
    cc = Proc(
        "cc",
        [f"{CC_DIR}/entrypoint.sh"],
        cc_env(env, s),
        cwd=CC_DIR,
        # As root, as its own image starts it: the entrypoint moves the key and passwords into
        # root-only files and runs CarConnectivity, pip and plugins as carcommand without them
        # (stack/cc/privsep.py). Nothing of that is repeated here.
        user=None,
        hint="With no car source yet that is expected: add one on Settings › Cars and sources and it starts by itself.",
    )
    sup = Supervisor([db, web, cc], logs, status_path=RUN / "status.json")
    now = time.monotonic()
    try:
        for p in (web, cc):
            p.start(now, spawn, logs)
    except BaseException:
        shutdown(sup.procs)
        raise
    LOG.info("up in %.1f s: the site is on port %d", time.monotonic() - t0, PORT)
    serve(sup, stopping)
    LOG.info("stopped cleanly")
    return 0


# ---------------------------------------------------------------- the other commands


def read_secrets() -> dict:
    d = DATA / "secrets"
    try:
        return {n: (d / n).read_text().strip() for n in GENERATED}
    except PermissionError:
        raise SystemExit("run this as root (docker exec runs as root unless told otherwise)")
    except FileNotFoundError:
        raise SystemExit("this volume has not been started yet")


def web_command(argv: list[str]) -> int:
    """CMD as the site, in its directory, with its settings: how `docker compose exec web` ran things."""
    if not argv:
        raise SystemExit("usage: carcommand web COMMAND [ARGS...]")
    env = web_env(dict(os.environ), read_secrets())
    if os.geteuid() == 0:
        uid, gid = ids("web")
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    os.chdir(WEB_DIR)
    os.execvpe(argv[0], argv, env)


def demo() -> int:
    """The steps of `make demo`, in order."""
    out = "/tmp/TeslaFiData-demo.csv.gz"
    env = web_env(dict(os.environ), read_secrets())
    for argv in (["python3", "demo.py", out], ["python3", "-c", "import app, demo; demo.prepare(app)"], ["python3", "teslafi_load.py", out]):
        code = subprocess.run(argv, env=env, cwd=WEB_DIR, **as_user("web")).returncode
        if code:
            return code
    return 0


def status() -> int:
    try:
        body = json.loads((RUN / "status.json").read_text())
    except FileNotFoundError:
        print("not started yet")
        return 1
    age = int(time.time()) - body["at"]
    for name, p in body["processes"].items():
        extra = f"up {p['up_s']} s" if p["state"] == "running" else (f"next start in {p.get('next_start_in_s', 0)} s" if p["state"] == "waiting" else "")
        print(f"{name:<9}{p['state']:<9}{extra:<22}restarts {p['restarts']}" + (f", last exit {p['last_exit']}" if p["last_exit"] is not None else ""))
    if age > 30:
        print(f"(this was written {age} s ago: the supervisor is not keeping it current)")
        return 1
    return 0


def health() -> int:
    """The container's healthcheck: the supervisor is alive, Postgres runs, the site answers.
    The connector is not part of it: the site runs without one, and with no source it waits."""
    try:
        body = json.loads((RUN / "status.json").read_text())
    except (FileNotFoundError, ValueError):
        print("starting")
        return 1
    if int(time.time()) - body["at"] > 30:
        print("the supervisor has stopped writing its status")
        return 1
    if body["processes"].get("postgres", {}).get("state") != "running":
        print("the database is not running")
        return 1
    if not web_answers():
        print("the site does not answer")
        return 1
    print("ok")
    return 0


def backup() -> int:
    if sys.stdout.isatty():
        raise SystemExit("a dump is binary: send it to a file, as in  docker exec carcommand carcommand backup > carcommand.dump")
    return subprocess.run([pg("pg_dump"), "-h", PG_SOCKET, "-U", "postgres", "-d", DB_NAME, "-Fc"], env={"PATH": BASE_PATH}, **as_user("postgres")).returncode


# The tables scripts/restore.sh looks at: anything in any of them is someone's data.
DATA_TABLES = ("trips", "charging_sessions", "cc_places", "cc_service", "vehicles", "cc_settings", "cc_auth", "cc_positions")


def has_data(s: dict) -> bool:
    env = {"PATH": BASE_PATH, "PGPASSWORD": s["DB_PASSWORD"]}

    def q(sql: str) -> list[str]:
        return subprocess.run([pg("psql"), "-h", "127.0.0.1", "-U", DB_USER, "-d", DB_NAME, "-qAt", "-c", sql], env=env, capture_output=True, text=True, check=True).stdout.split()

    present = q("SELECT t FROM unnest(ARRAY['" + "','".join(DATA_TABLES) + "']) t WHERE to_regclass(t) IS NOT NULL")
    if not present:
        return False
    return q("SELECT " + " OR ".join(f"EXISTS(SELECT 1 FROM {t})" for t in present)) == ["t"]


def key_change(directory: Path, env, force: bool) -> str | None:
    """The key a restore should leave in the volume, when it is not the one there now.

    The move from compose hands in the old install's key. A volume with no key yet takes it on
    the first start like any other value. One that has a different key keeps it unless the
    restore is forced: then the database the key protects is being replaced, and the key
    that sealed that database's sources replaces the volume's, after the restore succeeds."""
    given, path = (env.get("CC_SECRET_KEY") or "").strip(), directory / "CC_SECRET_KEY"
    if not given or not path.exists() or path.read_text().strip() == given:
        return None
    if not valid_key(given):
        raise SystemExit("CC_SECRET_KEY in the environment is not a key (32 bytes, URL-safe base64)")
    if not force:
        raise SystemExit(
            "CC_SECRET_KEY in the environment is not the key this volume keeps. Restoring with the volume's key would "
            "leave the dump's sealed sources unreadable. Run restore --force to replace the database and the key together, "
            "or restore into a new volume."
        )
    return given


def restore(force: bool) -> int:
    """A dump on stdin into this volume's database, with nothing else running: the move from
    compose (scripts/move-to-one-container.sh) and putting a backup back."""
    if sys.stdin.isatty():
        raise SystemExit("send the dump on stdin: docker run --rm -i -v carcommand:/data IMAGE restore < FILE")
    logs = Logs(DATA / "logs")
    setup_logging(logs)
    lock_volume()
    new_key = key_change(DATA / "secrets", os.environ, force)
    s, db = bring_up_database(logs)
    try:
        if has_data(s) and not force:
            LOG.error("this volume's database already holds drives, places, settings or a password. Take a backup first; then run restore --force to replace it.")
            return 1
        # One transaction: a dump that fails halfway leaves the database as it was.
        args = [pg("pg_restore"), "-h", "127.0.0.1", "-U", DB_USER, "-d", DB_NAME, "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction"]
        if force:
            args += ["--clean", "--if-exists"]
        code = run_logged(args, logs, "restore", stdin=sys.stdin.buffer, env={"PATH": BASE_PATH, "PGPASSWORD": s["DB_PASSWORD"]})
        if code:
            LOG.error("pg_restore failed (%s); the lines above say where", code)
            return code
        if new_key:
            write_secret(DATA / "secrets" / "CC_SECRET_KEY", new_key)
            LOG.warning("CC_SECRET_KEY replaced with the one given, with the database it protects")
        LOG.info("restored; the site finishes any newer columns when it starts")
        return 0
    finally:
        shutdown([db])


def main(argv: list[str]) -> int:
    cmd, rest = (argv[1], argv[2:]) if len(argv) > 1 else ("start", [])
    if cmd == "start":
        return start()
    if cmd == "status":
        return status()
    if cmd == "health":
        return health()
    if cmd == "web":
        return web_command(rest)
    if cmd == "demo":
        return demo()
    if cmd == "backup":
        return backup()
    if cmd == "restore":
        return restore(force="--force" in rest)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
