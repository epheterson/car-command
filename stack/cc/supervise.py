#!/usr/bin/env python3
"""Run CarConnectivity, and run it again when its sources change.

The connectors block is CC_CONNECTORS_JSON when that is set, as before. Otherwise it is the
Lucid connector, reading its refresh token from a Lucid source kept in the app when there is
one (sealed in cc_sources with CC_SECRET_KEY, written to a 0600 file under /tmp), and from
secrets/lucid-token.json when there is not, which is how every install ran before this.
A Lucid source added in the app arrives as a pending sign-in (e-mail and password, sealed):
this signs in once, keeps only the refresh token, and drops the password, success or not.
Every ten seconds it reads cc_state's sources_version; when the rendered config or the token
changed it stops CarConnectivity and starts it again, and not otherwise: each start is a
Lucid token login, and those are rate-limited. When CarConnectivity exits on its own, this
exits with its code and Docker restarts the container, as it always did.

Other brands (cc_sources of kind carconnectivity, added in Settings) are handled by
plugins.py: a request is installed when the image lacks it, then goes into the config as
one more connector block, its sign-in written only into the 0600 config file, never into
the environment or a log. A start that adds one is on trial for TRIAL_S: if CarConnectivity
dies while it is, the new account is marked failed and the rest starts again without it,
so a bad plugin or a wrong password for one brand never takes the others down with it. If
CarConnectivity dies soon after starting with other brands in QUICK_LIMIT times in a row
(counted in a file on the plugins volume, across container restarts), it starts without
them, keeps their sign-ins and tries each again later.

The start is fast and puts nothing new first: Lucid and every account already running come
up with the plugins already on disk; installs, retries and the check of each plugin happen
on the ticks after. A plugin that will not install is retried with a growing wait and keeps
its sealed sign-in; a sign-in is dropped only when it is what failed.

This process runs as root only to read the key and passwords (privsep.py); CarConnectivity,
pip and the import checks run as the unprivileged user, without them.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import signal
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plugin_role  # noqa: E402
import plugins  # noqa: E402
import privsep  # noqa: E402
import sealing  # noqa: E402
import render_config  # noqa: E402

LOG = logging.getLogger("supervise")
FILE_TOKEN = "/secrets/lucid-token.json"
UNKNOWN = object()  # the database could not be read: not the same as "no Lucid source"
TRIAL_S = 120
ROLE_RETRY_S = 300
QUICK_LIMIT = 2
RETRY_FIRST_S, RETRY_MAX_S = 60, 6 * 3600


def _span(seconds: float) -> str:
    return f"{round(seconds / 3600)} hours" if seconds >= 7200 else f"{round(seconds / 60)} minutes" if seconds >= 120 else f"{int(seconds)} seconds"


def default_connectors(token_path: str, env) -> str:
    """The Lucid block: the token file and the polling rates. Commands are on in the connector
    (0.2.0 and later; older ones ignore the key): Settings › Commands on the site is the switch,
    and nothing reaches the connector's command paths but the site."""
    idle, driving = env.get("CC_POLL_IDLE_S") or "60", env.get("CC_POLL_DRIVING_S") or "15"
    return f'[{{"type":"lucid","config":{{"refresh_token_file":"{token_path}","interval":{idle},"driving_interval":{driving},"log_level":"info","api_log_level":"error","commands":true}}}}]'


def cc_blocks(cc_rows: list[dict], installed=lambda config: True) -> list[tuple[int, dict]]:
    """(id, block) for each other brand's account that is a catalogue entry, on disk, and opens."""
    out = []
    for r in cc_rows or []:
        config = r.get("config") or {}
        if not r.get("enabled") or config.get("state") not in ("ready", "running") or not r.get("plain") or plugins.problem(config) or not installed(config):
            continue
        try:
            values = json.loads(r["plain"])
        except ValueError:
            continue
        if isinstance(values, dict):
            out.append((r["id"], plugins.block(config, values)))
    return out


def _ids(blocks) -> set:
    return {b.get("connector_id") or b.get("type") for b in blocks if isinstance(b, dict)}


def desired(rows: list[dict], env, app_token_path: str, cc_rows: list[dict] = (), file_ok: bool = True, installed=lambda config: True) -> dict:
    extra = cc_blocks(cc_rows, installed)
    if (env.get("CC_CONNECTORS_JSON") or "").strip():
        out = {"connectors": env["CC_CONNECTORS_JSON"], "token": None, "from": "env"}
        try:
            taken = _ids(json.loads(env["CC_CONNECTORS_JSON"]))
        except ValueError:
            taken = set()
        extra = [(i, b) for i, b in extra if (b.get("connector_id") or b["type"]) not in taken]  # the environment's own block for a brand wins
    else:
        out = None
        for r in rows:
            if not r["enabled"] or (r["config"] or {}).get("pending") or not r["plain"]:
                continue
            try:
                token = json.loads(r["plain"])["refresh_token"]
            except (ValueError, KeyError, TypeError):
                continue
            out = {"connectors": default_connectors(app_token_path, env), "token": token, "from": "app"}
            break
        if out is None:
            # No Lucid token anywhere: no Lucid block to fail on a missing file. With no other brand
            # either, CarConnectivity runs with no connector at all (its REST API answers, with no
            # cars) instead of exiting on the missing file and being restarted forever.
            lucid = default_connectors(FILE_TOKEN, env) if file_ok else "[]"
            out = {"connectors": lucid, "token": None, "from": "file"}
    if extra:
        out["cc"] = [b for _i, b in extra]
        out["cc_ids"] = [i for i, _b in extra]
    # CC_GEOCODER_STATE is cc_state's geocoder_url, handed in by the caller: a local URL,
    # "public" (CC_GEOCODER_URL unset: the service's own public lookups), or "" for off.
    # Absent means the site has not decided yet, or the database could not be read; that
    # is off too, so no stop goes to a third party before Settings > Maps says it may.
    state = env.get("CC_GEOCODER_STATE") or ""
    out["env"] = {} if state == "public" else {"CC_GEOCODER_URL": state}
    return out


def lucid_failure(err: BaseException) -> str:
    """Why a sign-in failed, in words: never the exception's class name, never its text (which can carry the account)."""
    if isinstance(err, (OSError, TimeoutError)):
        return "Lucid could not be reached. Check the connection, then sign in again."
    return "Lucid did not accept the sign-in. Check the e-mail and password, then sign in again."


class Supervisor:
    def __init__(
        self,
        db,
        vault,
        spawn,
        sign_in,
        env,
        template,
        config_path,
        app_token_path,
        install=plugins.install,
        uninstall=plugins.uninstall,
        clock=time.monotonic,
        file_ok=None,
        installed=None,
        wall=time.time,
        state_path=None,
        db_role=None,
    ):
        self.db, self.vault, self.spawn, self.sign_in, self.env = (db, vault, spawn, sign_in, env)
        self.template, self.config_path, self.app_token_path = (template, config_path, app_token_path)
        self.install, self.uninstall, self.clock, self.wall = (install, uninstall, clock, wall)
        self.file_ok = file_ok or (lambda: os.path.isfile(FILE_TOKEN) and os.path.getsize(FILE_TOKEN) > 0)
        self._have = None  # what is on disk, read once, and again only after pip ran
        self.installed = installed or (lambda config: plugins.installed(config, self.env, have=self._present()))
        self.state_path = state_path
        # (role, password) for CarConnectivity's database plugin, or None while there is none:
        # then it runs on the main role and no other brand is started (plugin_role.py).
        self.db_role, self.db_login, self.role_tried = db_role or (lambda: None), None, None
        self.child, self.version, self.running = None, None, None
        self.cc_rows, self.trial, self.started, self.verified, self.proven = ([], [], 0.0, False, False)
        self.reads = 0  # reads of the sources since this process started

    def _present(self) -> dict:
        if self._have is None:
            self._have = plugins.present(self.env)
        return self._have

    # ---- the database, every failure read as "nothing there"
    def _read(self, act: bool = True):
        """The sources. With act, also the work other brands ask for (installs, retries,
        removals): never at the start, so Lucid and every working source come up first.
        """
        try:
            conn = self.db()
        except Exception as err:  # noqa: BLE001 - no database: run on the file, as before
            LOG.warning("sources: no database (%s)", type(err).__name__)
            return UNKNOWN, None, None
        try:
            cur = conn.cursor()
            try:
                cur.execute("SELECT value FROM cc_state WHERE key = %s", ("sources_version",))
                got = cur.fetchone()
                version = got[0] if got else None
                cur.execute("SELECT value FROM cc_state WHERE key = %s", ("geocoder_url",))
                got = cur.fetchone()
                geocoder = got[0] if got else None
                cur.execute("SELECT id, config, secret, enabled FROM cc_sources WHERE kind = %s ORDER BY id", ("lucid",))
                rows = [{"id": i, "config": c if isinstance(c, dict) else json.loads(c or "{}"), "secret": s, "enabled": e} for i, c, s, e in cur.fetchall()]
                cur.execute("SELECT id, config, secret, enabled FROM cc_sources WHERE kind = %s ORDER BY id", ("carconnectivity",))
                cc_rows = [{"id": i, "config": c if isinstance(c, dict) else json.loads(c or "{}"), "secret": s, "enabled": e} for i, c, s, e in cur.fetchall()]
            except Exception as err:  # noqa: BLE001 - no tables yet
                LOG.info("sources: %s", type(err).__name__)
                conn.rollback() if hasattr(conn, "rollback") else None
                return UNKNOWN, None, None
            for r in rows:
                # a sign-in this key could not open waits for a restart (a new key), not every tick
                if r["config"].get("pending") and r["secret"] is not None and (not r["config"].get("locked") or not self.reads):
                    self._sign_in(conn, r)
            for r in rows:
                r["plain"] = self._open(conn, r, "lucid")
            if act:
                cc_rows = self._other_brands(conn, cc_rows)
            for r in cc_rows:
                r["plain"] = self._open(conn, r, "carconnectivity")
            self.cc_rows = cc_rows
            self.reads += 1
            return version, rows, geocoder
        finally:
            conn.close()

    def _open(self, conn, row, kind: str) -> str | None:
        """The row's secret. A bare one (written for this row by older code, before binding)
        is bound to the row in place, under the advisory lock, only if it is still unchanged."""
        if not self.vault.ready or row["secret"] is None:
            return None
        plain, new = self.vault.open_row(row["secret"], kind, row["id"])
        if new is not None:
            cur = conn.cursor()
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (sealing.LOCK_KEY,))
            cur.execute(sealing.REBIND_SQL, (new, row["id"], row["secret"]))
            conn.commit()
            row["secret"] = new
            LOG.info("sources: a sealed secret written before binding was bound to its row (%s %s)", kind, row["id"])
        return plain

    # ---- other brands: install what was asked for, retry what failed, take out what was removed
    def _cc_update(self, conn, row, state, error=None, drop_secret=False, ok=False, **extra) -> None:
        config = {k: v for k, v in row["config"].items() if k not in ("retries", "retry_at", "locked")}
        config.update(state=state, **extra)
        conn.cursor().execute(
            "UPDATE cc_sources SET config = %s, secret = CASE WHEN %s THEN NULL ELSE secret END, last_error = %s, "
            "last_ok_at = CASE WHEN %s THEN now() ELSE last_ok_at END, updated_at = now() WHERE id = %s",
            (json.dumps(config), drop_secret, error, ok, row["id"]),
        )
        row["config"] = config
        if drop_secret:
            row["secret"] = None
        conn.commit()

    def _retry_later(self, conn, row, error: str) -> None:
        """A plugin that would not install: the sign-in is kept (nothing says it is wrong) and
        the install is tried again, later each time."""
        n = int(row["config"].get("retries") or 0) + 1
        wait = min(RETRY_MAX_S, RETRY_FIRST_S * 2 ** (n - 1))
        self._cc_update(conn, row, "retry", f"{error[:240]} Trying again in {_span(wait)}.", retries=n, retry_at=self.wall() + wait)
        LOG.warning("%s: %s; again in %ss", row["config"].get("brand"), error, wait)

    def _try_install(self, conn, row, name) -> None:
        self._have = None
        err = self.install(row["config"], privsep.child_env(self.env))
        if err:
            self._retry_later(conn, row, err)
        else:
            self._cc_update(conn, row, "ready")
            LOG.info("%s: plugin ready", name)

    def _other_brands(self, conn, cc_rows: list[dict]) -> list[dict]:
        keep = []
        for r in cc_rows:
            config = r["config"]
            name = config.get("brand") or config.get("type") or "a brand"
            state = config.get("state")
            if state == "remove":
                others = {o["config"].get("package") for o in cc_rows if o is not r and o["config"].get("state") != "remove"}
                if config.get("uninstall") and config.get("package") not in others:
                    self._have = None
                    err = self.uninstall(config, privsep.child_env(self.env))
                    LOG.info("%s: plugin %s", name, "removed" if not err else f"not removed ({err})")
                conn.cursor().execute("DELETE FROM cc_sources WHERE id = %s", (r["id"],))
                conn.commit()
                LOG.info("%s: removed", name)
                continue
            keep.append(r)
            if not r["enabled"]:
                continue
            refused = plugins.problem(config)
            newer = plugins.upgrade_to(config) if refused else None
            if newer and state in ("install", "retry", "ready", "running"):
                # The same connector at another release than this image pins: install that one, sign-in kept.
                LOG.info("%s: %s %s becomes %s, the release this image pins", name, config.get("package"), config.get("version"), newer)
                self._cc_update(conn, r, "install", version=newer)
                config, state, refused = r["config"], "install", None
            if refused and state in ("install", "retry", "ready", "running"):
                # Not this image's catalogue, exactly: nothing is installed, imported or run.
                self._cc_update(conn, r, "failed", f"Refused: {refused}. Remove it and add the brand again from the list.")
                LOG.warning("%s: refused (%s)", name, refused)
                continue
            if state == "install":
                if r["secret"] is None:
                    self._cc_update(conn, r, "failed", "This request has no sign-in. Remove it and add the brand again.")
                elif config.get("locked") and self.verified:
                    continue  # waits for a restart, with a key that may open it
                elif self._open(conn, r, "carconnectivity") is None:
                    # Never dropped for this: the key may be the wrong one, not the sign-in.
                    self._cc_update(
                        conn,
                        r,
                        "install",
                        "CarConnectivity cannot open this sign-in: its container has no CC_SECRET_KEY, or a different one from the site's. Add the key to .env (make secret-key) and restart the cc container; the sign-in is kept and tried again then.",
                        locked=True,
                    )
                    LOG.warning("%s: a request is waiting but CC_SECRET_KEY cannot open it; kept for a restart with the right key", name)
                else:
                    LOG.info("%s: installing %s %s if the image lacks it", name, config.get("package"), config.get("version"))
                    self._try_install(conn, r, name)
            elif state == "retry" and self.wall() >= float(config.get("retry_at") or 0):
                self._try_install(conn, r, name)
            elif state in ("ready", "running") and not self.verified:
                # Once per container start, after it is up: the volume may be new, or the image's
                # Python newer. A plugin that is missing or no longer loads is put back, or retried.
                self._have = None
                err = self.install(config, privsep.child_env(self.env))
                if err:
                    self._retry_later(conn, r, err)
        self.verified = True
        return keep

    def _judge_trial(self, died: int | None) -> None:
        """After a start that added accounts: they ran TRIAL_S, or CarConnectivity died first.
        A death while any is still on trial is theirs, however long it took."""
        ids, self.trial = self.trial, []
        self._db_each(ids, lambda conn, r: self._trial_verdict(conn, r, died))

    def _trial_verdict(self, conn, r, died) -> None:
        if died is None:
            self._cc_update(conn, r, "running", ok=True)
            return
        msg = (
            f"CarConnectivity stopped while starting with it (exit {died}), so it was taken out and the rest started without it. "
            "That is most often a wrong sign-in; its log says why (docker logs on the cc container). Check it and add it again."
        )
        self._cc_update(conn, r, "failed", msg, drop_secret=True)
        LOG.warning("%s: CarConnectivity exited %s while starting with it; taken out", r["config"].get("brand"), died)

    def _db_each(self, ids, fn) -> None:
        try:
            conn = self.db()
        except Exception as err:  # noqa: BLE001 - nothing to record it in; the next start tries again
            LOG.warning("sources: no database to record the start in (%s)", type(err).__name__)
            return
        try:
            for r in [r for r in self.cc_rows if r["id"] in ids]:
                fn(conn, r)
        finally:
            conn.close()

    # ---- quick deaths with other brands in: counted across container restarts
    def _deaths(self) -> int:
        """The count, read without trusting the file: it lives on the plugin user's volume, so
        it may be a link, a FIFO or a very large file by now. Any of those reads as 0."""
        if not self.state_path:
            return 0
        try:
            fd = os.open(self.state_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            return 0
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > 256:
                return 0
            return int(json.loads(os.read(fd, 256).decode()).get("quick_deaths", 0))
        except (OSError, ValueError, TypeError, AttributeError, UnicodeDecodeError):
            return 0
        finally:
            os.close(fd)

    def _set_deaths(self, n: int) -> None:
        if not self.state_path:
            return
        try:
            fd = os.open(self.state_path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("not a regular file")
                os.fchmod(fd, 0o600)  # the supervisor's alone; one written before was 0644
                os.ftruncate(fd, 0)
                os.write(fd, json.dumps({"quick_deaths": n}).encode())
            finally:
                os.close(fd)
        except OSError as err:
            LOG.warning("could not keep the start count (%s)", type(err).__name__)

    def _isolate(self, died: int) -> None:
        """CarConnectivity keeps dying soon after starting with other brands in: start without
        them. Their sign-ins are kept (nothing proves them wrong); each is tried again later.
        """
        ids = list(self.running.get("cc_ids", []))

        def mark(conn, r):
            msg = f"CarConnectivity stopped within {TRIAL_S // 60} minutes of starting {QUICK_LIMIT} times in a row (exit {died}) with the other brands in, so it now runs without them."
            self._retry_later(conn, r, msg)

        self._db_each(ids, mark)
        self._set_deaths(0)

    # ---- Lucid
    def _env(self, geocoder) -> dict:
        """self.env plus the geocoder choice, as desired() expects it: absent when the
        database has no row for it (or could not be read), which desired() reads as off.
        """
        env = dict(self.env)
        env.pop("CC_GEOCODER_STATE", None)
        if geocoder is not None:
            env["CC_GEOCODER_STATE"] = geocoder
        return env

    def _sign_in(self, conn, row) -> None:
        """One attempt. Afterwards the row holds the refresh token, or nothing secret at all once
        Lucid has refused it. A sign-in this key cannot open is kept, marked, for a restart."""
        plain = self._open(conn, row, "lucid")
        cur = conn.cursor()
        if not plain:
            # Not the user's password: this container cannot read what the site sealed. Say so,
            # keep it (the key may be what is wrong), and do not try a login with nothing.
            cur.execute(
                "UPDATE cc_sources SET config = %s, last_error = %s, updated_at = now() WHERE id = %s",
                (
                    json.dumps({"pending": True, "locked": True}),
                    "The connector cannot open this sign-in: it has no CC_SECRET_KEY, or a different one from the site's. Add the key to .env (make secret-key) and restart the connector; the sign-in is kept and tried again then.",
                    row["id"],
                ),
            )
            row.update(config={"pending": True, "locked": True})
            conn.commit()
            LOG.warning("Lucid: a sign-in is waiting but CC_SECRET_KEY cannot open it; kept for a restart with the right key")
            return
        creds = json.loads(plain)
        try:
            token = self.sign_in(creds.get("email", ""), creds.get("password", ""))
            sealed = self.vault.seal(json.dumps({"refresh_token": token}), "lucid", row["id"])
            cur.execute("UPDATE cc_sources SET secret = %s, config = %s, last_ok_at = now(), last_error = NULL, updated_at = now() WHERE id = %s", (sealed, json.dumps({"pending": False}), row["id"]))
            row.update(secret=sealed, config={"pending": False})
            LOG.info("Lucid: signed in; the refresh token is kept, the password is not")
        except Exception as err:  # noqa: BLE001 - say what kind of failure, never the credentials
            cur.execute(
                "UPDATE cc_sources SET secret = NULL, config = %s, last_error = %s, updated_at = now() WHERE id = %s",
                (json.dumps({"pending": False}), lucid_failure(err), row["id"]),
            )
            row.update(secret=None, config={"pending": False})
            LOG.warning("Lucid: sign-in failed (%s); nothing kept", type(err).__name__)
        conn.commit()

    # ---- the child
    def _render(self, want: dict) -> None:
        if want["token"] is not None:
            fd = os.open(self.app_token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump({"refresh_token": want["token"]}, fh)
            privsep.hand_over(self.app_token_path)
        os.environ.update({k: v for k, v in self.env.items() if isinstance(v, str)})
        os.environ["CC_CONNECTORS_JSON"] = want["connectors"]
        user, password = want.get("db_login") or (self.env.get("DB_USER", ""), self.env.get("DB_PASSWORD", ""))
        os.environ["PLUGIN_DB_USER"], os.environ["PLUGIN_DB_PASSWORD"] = user, password
        if "CC_GEOCODER_URL" in want.get("env", {}):
            os.environ["CC_GEOCODER_URL"] = want["env"]["CC_GEOCODER_URL"]
        else:
            os.environ.pop("CC_GEOCODER_URL", None)
        code = render_config.main(["render_config.py", self.template, self.config_path])
        if code:
            raise SystemExit(code)
        if want.get("cc"):
            # Other brands' sign-ins go straight into the 0600 file, never through the environment.
            with open(self.config_path, encoding="utf-8") as fh:
                data = json.load(fh)
            (data.get("carConnectivity") or data)["connectors"].extend(want["cc"])
            fd = os.open(self.config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
        privsep.hand_over(self.config_path)

    def _run(self, want: dict) -> None:
        self._render(want)
        self.child = self.spawn(self.config_path)
        self.running, self.started, self.proven = want, self.clock(), False
        # on trial: accounts that have not yet run through a start
        self.trial = [i for i in want.get("cc_ids", []) if next((r for r in self.cc_rows if r["id"] == i), {}).get("config", {}).get("state") == "ready"]
        brands = [r["config"].get("brand") or r["config"].get("type") for r in self.cc_rows if r["id"] in want.get("cc_ids", [])]
        if self.idle(want):
            LOG.info("No car source yet: CarConnectivity runs with no connector and waits. Add one on Settings › Cars and sources, or put a Lucid token in secrets/lucid-token.json.")
            return
        LOG.info(
            "CarConnectivity started, %s%s",
            "no Lucid source" if want["connectors"] == "[]" else "Lucid token from " + {"app": "the app", "file": "secrets/lucid-token.json", "env": "CC_CONNECTORS_JSON"}[want["from"]],
            f"; also {', '.join(map(str, brands))}" if brands else "",
        )

    @staticmethod
    def idle(want: dict) -> bool:
        """Running with no connector: nothing to read from until a source is added."""
        return want["connectors"] == "[]" and not want.get("cc")

    def bind_sealed(self) -> int:
        """At the start, before anything is opened: every bare source secret resealed for the row
        it is in. The site does the same at its start; both hold the same advisory lock and look
        again inside it, and a blob is replaced only if it is still the one read. The flag goes
        on only when no bare or unreadable value is left. Never raises."""
        if not self.vault.ready:
            return 0
        try:
            conn = self.db()
        except Exception:  # noqa: BLE001
            return 0
        try:
            cur = conn.cursor()

            def done() -> bool:
                cur.execute("SELECT value FROM cc_state WHERE key = %s", (sealing.DONE_KEY,))
                got = cur.fetchone()
                return bool(got and got[0] == sealing.DONE_VALUE)

            if done():
                return 0
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (sealing.LOCK_KEY,))
            if done():  # the site finished while this waited for the lock
                conn.commit()
                return 0
            cur.execute("SELECT id, kind, secret FROM cc_sources WHERE secret IS NOT NULL")
            rows = [{"id": i, "kind": k, "secret": s} for i, k, s in cur.fetchall()]
            redone, stuck = self.vault.migrate(rows)
            for sid, old, new in redone:
                cur.execute(sealing.REBIND_SQL, (new, sid, old))
            if stuck:
                LOG.warning("sources: %s sealed secrets do not open with this key; kept as they are, binding unfinished", stuck)
            else:
                cur.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (sealing.DONE_KEY, sealing.DONE_VALUE))
            conn.commit()
            if redone:
                LOG.info("sources: bound %s sealed secrets to their rows", len(redone))
            return len(redone)
        except Exception as err:  # noqa: BLE001 - no tables yet: the next start, or the site, does it
            LOG.info("sources: sealed secrets not bound yet (%s)", type(err).__name__)
            conn.rollback() if hasattr(conn, "rollback") else None
            return 0
        finally:
            conn.close()

    def _role(self) -> None:
        """The database plugin's own role, tried at the start and every five minutes until it works."""
        if self.db_login is not None or (self.role_tried is not None and self.clock() - self.role_tried < ROLE_RETRY_S):
            return
        self.role_tried = self.clock()
        self.db_login = self.db_role()
        if self.db_login is None:
            LOG.warning("CarConnectivity's database plugin has no role of its own yet, so it uses the main one and no other brand is started")

    def start(self) -> None:
        self.bind_sealed()
        self._role()
        # Nothing slow first: the sources as they stand, and only plugins already on disk.
        # Installs, retries and the check of each plugin come on the next ticks.
        version, rows, geocoder = self._read(act=False)
        # Unreadable at start: run on the file, as every install did before sources were in the app.
        self.version = None if version is UNKNOWN else version
        self.rows, self.geocoder = rows or [], geocoder
        self._run(self._want(self.rows, geocoder))

    def _want(self, rows, geocoder) -> dict:
        # Other brands' code runs only beside a database login that cannot reach cc_sources.
        want = desired(rows, self._env(geocoder), self.app_token_path, self.cc_rows if self.db_login else [], self.file_ok(), self.installed)
        want["db_login"] = self.db_login
        return want

    def _died(self, code: int) -> bool:
        """CarConnectivity exited. True when this handled it (and started it again); False when
        the container should restart, as it always has."""
        if self.trial:
            self._judge_trial(code)  # the new account goes; the rest starts again without it
            self._run(self._want(self.rows, self.geocoder))
            return True
        if self.running.get("cc") and self.clock() - self.started < TRIAL_S:
            n = self._deaths() + 1
            self._set_deaths(n)
            if n >= QUICK_LIMIT:
                self._isolate(code)
                self._run(self._want(self.rows, self.geocoder))
                return True
        return False

    def tick(self) -> bool:
        code = self.child.poll()
        if code is not None:
            if self._died(code):
                return True
            raise SystemExit(code)
        if self.clock() - self.started >= TRIAL_S and not self.proven:
            self.proven = True
            if self.trial:
                self._judge_trial(None)
            if self.running.get("cc"):
                self._set_deaths(0)
        self._role()
        version, rows, geocoder = self._read()
        if version is UNKNOWN:
            return False  # a database that cannot be read right now changes nothing that is running
        self.rows, self.geocoder = rows, geocoder
        want = self._want(rows, geocoder)
        idle_with_file = self.idle(self.running) and self.file_ok()  # make lucid-login wrote one; no bump comes from that
        same = want.get("cc_ids") == self.running.get("cc_ids") and want.get("db_login") == self.running.get("db_login")
        if version == self.version and same and not idle_with_file:
            return False
        self.version = version
        if want == self.running:
            return False
        LOG.info("sources changed; restarting CarConnectivity")
        self.stop()
        self._run(want)
        return True

    def stop(self) -> None:
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=30)
            except Exception:  # noqa: BLE001
                self.child.kill()


def _connect():
    import psycopg2

    e = os.environ
    return psycopg2.connect(host=e["DB_HOST"], dbname=e["DB_NAME"], user=e["DB_USER"], password=e["DB_PASSWORD"], connect_timeout=5)


class _Vault:
    def __init__(self, key: str | None):
        self._f = None
        if key:
            try:
                from cryptography.fernet import Fernet

                self._f = Fernet(key.encode())
            except Exception:  # noqa: BLE001 - no key, no app sources: the file as before
                LOG.warning("CC_SECRET_KEY is not a valid key; using the token file")

    @property
    def ready(self) -> bool:
        return self._f is not None

    def seal(self, text: str, kind: str, sid) -> bytes:
        return sealing.seal(self._f, kind, sid, text)

    def open(self, blob, kind: str, sid):
        """Only a secret sealed for this very row (sealing.py): one copied from another row,
        or from before binding, opens as nothing."""
        return sealing.open_bound(self._f, blob, kind, sid)

    def migrate(self, rows) -> tuple[list[tuple], int]:
        return sealing.migrate(self._f, rows)

    def open_row(self, blob, kind: str, sid):
        return sealing.open_row(self._f, blob, kind, sid)


def _sign_in(email: str, password: str) -> str:
    import asyncio

    from lucidmotors import LucidAPI

    async def go():
        async with LucidAPI() as api:
            await api.login(email, password)
            if not api.refresh_token:
                raise RuntimeError("no refresh token came back")
            return api.refresh_token

    return asyncio.run(go())


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s supervise: %(message)s")
    # No --hide-repeated-log. Its DuplicateFilter indexes self._last_log[module][levelno]
    # before checking that levelno is present, so the first message a module logs at a new
    # level raises KeyError inside logging. The exception unwinds through the caller, and in
    # practice that caller was the database plugin's trip agent: every attempt to start a
    # trip died on its own log line. Reported upstream.
    privsep.load_secrets(os.environ)  # the entrypoint moved them out of the environment
    run_dir = privsep.RUN_DIR
    work = os.path.join(run_dir, "pip")  # root's directory; pip, as the user, only reads it
    os.makedirs(work, exist_ok=True)
    # The entrypoint's umask is 077; pip, as the plugin user, reads its two files through the group.
    os.chown(work, 0, privsep.ids()[1])
    os.chmod(work, 0o750)
    s = Supervisor(
        db=_connect,
        vault=_Vault(os.environ.get("CC_SECRET_KEY")),
        spawn=privsep.spawn,
        sign_in=_sign_in,
        env=dict(os.environ),
        template="/config/carconnectivity.json.template",
        config_path=os.path.join(run_dir, "carconnectivity.json"),
        app_token_path=os.path.join(run_dir, "lucid-token.json"),
        install=functools.partial(plugins.install, run=privsep.run, tmpdir=work),
        uninstall=functools.partial(plugins.uninstall, run=privsep.run),
        state_path=os.path.join(os.environ.get("PYTHONUSERBASE") or "/plugins", ".carcommand-starts.json"),
        db_role=functools.partial(plugin_role.setup, dict(os.environ), secrets_dir=os.path.join(run_dir, "secrets")),
    )

    def on_term(*_):
        s.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    s.start()
    while True:
        time.sleep(10)
        s.tick()


if __name__ == "__main__":
    sys.exit(main())
