"""The one container's supervisor: secrets made once and kept, each process's environment,
restarts with backoff, a clean stop, bounded logs, one container per volume."""

import fcntl
import json
import logging
import os
import re
import signal
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import carcommand as cc  # noqa: E402

# ---------------------------------------------------------------- secrets


def test_the_first_start_generates_each_secret_into_its_own_private_file(tmp_path):
    d = tmp_path / "secrets"
    got = cc.ensure_secrets(d, {})
    assert set(got) == {"DB_PASSWORD", "WEBUI_PASSWORD", "REST_PASSWORD", "CC_SECRET_KEY", "CC_SECRET"}
    assert len(set(got.values())) == 5, "every secret is its own"
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    for name, value in got.items():
        assert stat.S_IMODE((d / name).stat().st_mode) == 0o600
        assert (d / name).read_text() == value
    assert cc.valid_key(got["CC_SECRET_KEY"])
    assert not list(d.glob(".*.tmp")), "no half-written file left behind"


def test_the_key_opens_what_the_site_seals():
    fernet = pytest.importorskip("cryptography.fernet")
    f = fernet.Fernet(cc._fernet().encode())
    assert f.decrypt(f.encrypt(b"teslafi-token")) == b"teslafi-token"


def test_a_second_start_keeps_them(tmp_path):
    first = cc.ensure_secrets(tmp_path, {})
    assert cc.ensure_secrets(tmp_path, {}) == first


def test_the_environment_seeds_a_missing_secret_and_never_overrides_a_kept_one(tmp_path, caplog):
    key = cc._fernet()
    got = cc.ensure_secrets(tmp_path, {"CC_SECRET_KEY": key})
    assert got["CC_SECRET_KEY"] == key, "a move from compose brings its key this way"
    other = cc._fernet()
    again = cc.ensure_secrets(tmp_path, {"CC_SECRET_KEY": other})
    assert again["CC_SECRET_KEY"] == key, "the file wins: the sealed sources stay readable"
    assert "using the file" in caplog.text
    assert other not in caplog.text and key not in caplog.text, "no value is ever logged"


def test_a_key_that_is_not_a_key_stops_the_first_start(tmp_path):
    with pytest.raises(SystemExit, match="not a key"):
        cc.ensure_secrets(tmp_path, {"CC_SECRET_KEY": "hunter2"})
    assert not (tmp_path / "CC_SECRET_KEY").exists()


def test_an_emptied_file_is_refused_rather_than_used(tmp_path):
    cc.ensure_secrets(tmp_path, {})
    (tmp_path / "DB_PASSWORD").write_text("")
    with pytest.raises(SystemExit, match="empty"):
        cc.ensure_secrets(tmp_path, {})


def test_a_volume_from_before_the_signing_key_file_keeps_the_key_in_its_database(tmp_path):
    """Its sessions are signed with the key the site keeps in the database; a new one here would sign everyone out."""
    first = cc.ensure_secrets(tmp_path, {})
    (tmp_path / "CC_SECRET").unlink()
    again = cc.ensure_secrets(tmp_path, {})
    assert "CC_SECRET" not in again and not (tmp_path / "CC_SECRET").exists()
    assert {k: v for k, v in first.items() if k != "CC_SECRET"} == again
    assert cc.ensure_secrets(tmp_path, {"CC_SECRET": "moved-from-compose"})["CC_SECRET"] == "moved-from-compose", "the environment seeds it"
    (tmp_path / "CC_SECRET").write_text("written-by-hand")
    assert cc.ensure_secrets(tmp_path, {})["CC_SECRET"] == "written-by-hand", "a file put there by hand is used"


@pytest.mark.parametrize("name", ["DB_PASSWORD", "WEBUI_PASSWORD", "REST_PASSWORD", "CC_SECRET"])
def test_a_placeholder_from_the_example_env_is_refused(tmp_path, name):
    with pytest.raises(SystemExit, match="CHANGE_ME"):
        cc.ensure_secrets(tmp_path, {name: "CHANGE_ME"})


def test_the_site_gets_the_signing_key_when_there_is_one():
    assert cc.web_env({}, dict(S, CC_SECRET="sig"))["CC_SECRET"] == "sig"
    assert "CC_SECRET" not in cc.web_env({}, S)
    assert cc.web_env({}, S)["CC_INSTALL"] == "one-container", "the welcome page shows docker exec, not make"
    assert "CC_SECRET" not in cc.cc_env({}, dict(S, CC_SECRET="sig")), "the connector never needs it"


def test_commands_run_in_the_site_read_the_signing_key_too(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "DATA", tmp_path)
    made = cc.ensure_secrets(tmp_path / "secrets", {})
    assert cc.read_secrets()["CC_SECRET"] == made["CC_SECRET"], "setup_code.py must see the key the site signs with"
    (tmp_path / "secrets" / "CC_SECRET").unlink()
    assert "CC_SECRET" not in cc.read_secrets()


# ---------------------------------------------------------------- environments

S = {"DB_PASSWORD": "dbpw", "WEBUI_PASSWORD": "uipw", "REST_PASSWORD": "restpw", "CC_SECRET_KEY": "key="}


def test_the_site_gets_the_containers_settings_and_its_wiring():
    env = cc.web_env({"DISCORD_WEBHOOK_URL": "https://example.invalid/hook", "DB_PASSWORD": "from-run", "CC_POLL_IDLE_S": ""}, S)
    assert env["DISCORD_WEBHOOK_URL"] == "https://example.invalid/hook"
    assert env["DATABASE_URL"] == "postgresql://cc_site:dbpw@127.0.0.1:5432/carcommand"
    assert env["CC_REST_URL"] == "http://127.0.0.1:40000" and env["REST_PASSWORD"] == "restpw"
    assert env["CC_SECRET_KEY"] == "key="
    assert "DB_PASSWORD" not in env and "WEBUI_PASSWORD" not in env
    assert env["CC_POLL_IDLE_S"] == "60" and env["CC_POLL_DRIVING_S"] == "15", "compose's defaults, not the site's own"
    assert env["PATH"].startswith("/opt/web/bin:")


def test_the_connector_gets_what_compose_gives_it_and_none_of_the_sites_settings():
    env = cc.cc_env({"DISCORD_WEBHOOK_URL": "x", "TESLA_CLIENT_SECRET": "y", "CC_CONNECTORS_JSON": '[{"type":"x"}]'}, S)
    assert "DISCORD_WEBHOOK_URL" not in env and "TESLA_CLIENT_SECRET" not in env
    assert (env["DB_HOST"], env["DB_USER"], env["DB_NAME"], env["DB_PASSWORD"]) == ("127.0.0.1", "cc_site", "carcommand", "dbpw")
    assert env["REST_PASSWORD"] == "restpw" and env["WEBUI_PASSWORD"] == "uipw" and env["CC_SECRET_KEY"] == "key="
    assert env["CC_CONNECTORS_JSON"] == '[{"type":"x"}]'
    assert env["PYTHONPATH"] == "/app", "sitecustomize (the rounded positions) loads only from the path"
    assert env["PYTHONUSERBASE"].endswith("/plugins") and env["PYTHONNOUSERSITE"] == "1"
    assert "TZ" not in env and cc.cc_env({"TZ": "Europe/Berlin"}, S)["TZ"] == "Europe/Berlin"
    assert "/opt/web" not in env["PATH"], "the connector runs on its own Python"


def test_the_site_and_the_connector_agree_on_the_rest_login():
    assert cc.web_env({"REST_USER": "ops"}, S)["REST_USER"] == cc.cc_env({"REST_USER": "ops"}, S)["REST_USER"] == "ops"
    assert cc.web_env({}, S)["REST_USER"] == cc.cc_env({}, S)["REST_USER"] == "admin"


def test_the_connector_is_started_through_its_own_entrypoint_which_drops_privileges_itself():
    text = (HERE / "carcommand.py").read_text()
    assert '[f"{CC_DIR}/entrypoint.sh"]' in text
    assert (HERE.parent / "cc" / "privsep.py").exists(), "the entrypoint this relies on keeps the key from plugin processes"


# ---------------------------------------------------------------- processes


def test_backoff_doubles_to_five_minutes():
    assert [cc.backoff(n) for n in range(1, 12)] == [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300]


class Child:
    _pid = 1000

    def __init__(self, stubborn=False):
        Child._pid += 1
        self.pid, self.returncode, self.stubborn, self.signals, self.stdout = Child._pid, None, stubborn, [], None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        if sig == signal.SIGKILL or not self.stubborn:
            self.returncode = -sig


def make(names=("postgres", "web", "cc")):
    children = []

    def spawn(proc):
        children.append(Child())
        return children[-1]

    procs = [cc.Proc(n, ["x"], {}, whole_group=False) for n in names]
    sup = cc.Supervisor(procs, cc.Logs(None, out=open(os.devnull, "w")), spawn=spawn)
    for p in procs:
        p.start(0, spawn, sup.logs)
    return sup, children


def test_a_process_that_stops_is_started_again_later_and_later():
    sup, _ = make(("cc",))
    p = sup.get("cc")
    now = 0
    gaps = []
    for _ in range(4):
        p.child.returncode = 1
        sup.tick(now + 5)  # it ran five seconds: a crash, not a run
        assert p.state == "waiting"
        start = p.next_start
        gaps.append(start - (now + 5))
        sup.tick(start - 0.5)
        assert p.state == "waiting", "not before its time"
        sup.tick(start)
        assert p.state == "running"
        now = start
    assert gaps == [1, 2, 4, 8]
    assert p.restarts == 4 and p.status(now)["state"] == "running"


def test_a_long_run_resets_the_count():
    sup, _ = make(("cc",))
    p = sup.get("cc")
    p.failures = 6
    p.child.returncode = 1
    sup.tick(cc.HEALTHY_RUN_S + 1)
    assert p.next_start == cc.HEALTHY_RUN_S + 1 + 1


def test_a_site_that_ignores_the_stop_is_killed_with_its_group():
    answers = iter([False] * cc.WEB_CHECK_FAILS)
    sup, children = make(("web",))
    sup.check_web = lambda: next(answers)
    web = sup.get("web")
    children[0].stubborn = True
    t = cc.HEALTHY_RUN_S
    for _ in range(cc.WEB_CHECK_FAILS):
        sup.tick(t)
        t += cc.WEB_CHECK_S
    assert children[0].signals == [signal.SIGTERM]
    asked = t - cc.WEB_CHECK_S
    sup.tick(asked + cc.WEB_KILL_AFTER_S - 1)
    assert children[0].signals == [signal.SIGTERM], "a moment to stop on its own"
    sup.tick(asked + cc.WEB_KILL_AFTER_S)
    assert children[0].signals == [signal.SIGTERM, signal.SIGKILL]
    sup.tick(asked + cc.WEB_KILL_AFTER_S + 1)
    assert web.state == "waiting"


def test_the_site_stops_quickly():
    argv = cc.web_argv()
    assert argv[argv.index("--timeout-graceful-shutdown") + 1] == "3"
    assert "--no-proxy-headers" in argv and "app:server" in argv


def test_the_site_is_restarted_when_it_stops_answering():
    answers = iter([True] + [False] * cc.WEB_CHECK_FAILS)
    sup, _ = make(("web",))
    sup.check_web = lambda: next(answers)
    web = sup.get("web")
    t = cc.HEALTHY_RUN_S
    for _ in range(cc.WEB_CHECK_FAILS):
        sup.tick(t)
        t += cc.WEB_CHECK_S
        assert web.state == "running"
    sup.tick(t)  # the last failure: told to stop
    assert web.child.signals == [signal.SIGTERM]
    sup.tick(t + 1)
    assert web.state == "waiting"


def test_stop_signals_everything_then_kills_what_ignores_it():
    sup, children = make(("web", "cc"))
    children[1].stubborn = True
    clock = iter(range(100))
    cc.stop_all(sup.procs, 3, clock=lambda: next(clock), sleep=lambda s: None)
    assert children[0].signals == [signal.SIGTERM]
    assert children[1].signals == [signal.SIGTERM, signal.SIGKILL]
    assert all(p.state == "stopped" for p in sup.procs)


def test_postgres_gets_a_fast_shutdown_and_only_the_postmaster_is_signalled():
    p = cc.postgres_proc()
    assert p.stop_signal == signal.SIGINT and not p.whole_group and p.user == "postgres"
    assert "listen_addresses=127.0.0.1" in p.argv, "never on the container's network"


def test_a_real_child_is_logged_and_its_exit_noticed(tmp_path):
    logs = cc.Logs(tmp_path, out=open(os.devnull, "w"))
    p = cc.Proc("echo", [sys.executable, "-c", "print('hello from a child')"], dict(os.environ))
    p.start(0, cc.spawn, logs)
    p.child.wait(timeout=10)
    assert p.exited(1) and p.last_exit == 0 and p.state == "waiting"
    for _ in range(50):
        if "hello from a child" in (tmp_path / "echo.log").read_text():
            break
        time.sleep(0.05)
    assert "hello from a child" in (tmp_path / "echo.log").read_text()


def test_logs_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "LOG_BYTES", 2000)
    logs = cc.Logs(tmp_path, out=open(os.devnull, "w"))
    for i in range(2000):
        logs.line("web", f"line {i} " + "x" * 40)
    files = sorted(tmp_path.glob("web.log*"))
    assert [f.name for f in files] == ["web.log", "web.log.1", "web.log.2"]
    assert sum(f.stat().st_size for f in files) <= 3 * 2100
    assert "line 1999" in (tmp_path / "web.log").read_text()


# ---------------------------------------------------------------- Postgres and the volume


def test_what_is_in_the_data_directory(tmp_path):
    d = tmp_path / "postgres"
    assert cc.pg_state(d) == "empty"
    d.mkdir()
    assert cc.pg_state(d) == "empty"
    (d / "PG_VERSION").write_text("18\n")
    assert cc.pg_state(d) == "ready"
    (d / "PG_VERSION").write_text("17\n")
    assert cc.pg_state(d) == "Postgres 17", "an older cluster is named, never upgraded in place"
    (d / "PG_VERSION").unlink()
    (d / "notes.txt").write_text("mine")
    assert cc.pg_state(d) == "not a database", "a directory with someone's files is never initialised over"


def test_the_role_sql_quotes_the_password():
    sql = cc.role_sql("it's")
    assert "PASSWORD 'it''s'" in sql
    assert sql.count("\\gexec") == 4, "rename, role, database and the plugin's role: each only when missing"


def test_the_sites_role_is_no_superuser_and_may_manage_only_the_plugins_role():
    sql = cc.role_sql("pw")
    assert "ALTER ROLE cc_site WITH LOGIN NOSUPERUSER CREATEROLE PASSWORD" in sql
    assert "GRANT cc_plugin TO cc_site WITH ADMIN OPTION;" in sql
    assert "ALTER DATABASE carcommand OWNER TO cc_site;" in sql
    assert " SUPERUSER " not in sql.replace("NOSUPERUSER", "")


def test_the_sites_role_is_named_like_no_user_in_the_image():
    text = (HERE / "Dockerfile").read_text()
    users = set(re.findall(r"useradd -r -u \d+ -g \d+ (?:-d \S+ )?(?:-s \S+ )?(\w+)", text)) | {"root"}
    assert users >= {"postgres", "carcommand", "web"}
    assert cc.DB_USER not in users and cc.PLUGIN_ROLE not in users, "peer authentication must never map an OS user onto them"


def test_only_postgres_may_use_the_socket_and_everyone_else_needs_a_password(tmp_path):
    lines = [x.split() for x in cc.HBA.splitlines() if x.strip() and not x.startswith("#")]
    assert lines == [
        ["local", "all", "postgres", "peer"],
        ["host", "all", "all", "127.0.0.1/32", "scram-sha-256"],
        ["host", "all", "all", "::1/128", "scram-sha-256"],
    ]
    (tmp_path / "pg_hba.conf").write_text("local all all trust\n")  # an old or edited file is replaced
    cc.write_hba(tmp_path)
    assert (tmp_path / "pg_hba.conf").read_text() == cc.HBA
    assert stat.S_IMODE((tmp_path / "pg_hba.conf").stat().st_mode) == 0o600
    argv = cc.postgres_proc().argv
    assert "unix_socket_permissions=0770" in argv


def test_wait_ready_waits_as_long_as_postgres_is_alive_and_says_so(caplog):
    caplog.set_level(logging.INFO)
    t = iter(range(0, 10000, 5))
    answers = iter([False] * 30 + [True])  # 150 s of crash recovery
    proc = cc.Proc("postgres", ["x"], {})
    proc.child = Child()
    cc.wait_ready(proc, ready=lambda: next(answers), clock=lambda: next(t), sleep=lambda s: None)
    assert "still starting" in caplog.text


def test_wait_ready_gives_up_only_when_postgres_has_stopped():
    proc = cc.Proc("postgres", ["x"], {})
    proc.child = Child()
    proc.child.returncode = 1
    with pytest.raises(SystemExit, match="stopped while starting"):
        cc.wait_ready(proc, ready=lambda: False, clock=lambda: 0, sleep=lambda s: None)


def test_any_end_of_the_loop_stops_the_others_then_postgres(monkeypatch):
    sup, children = make(("postgres", "web", "cc"))
    order = []
    for c in children:
        c.send_signal = (lambda c: lambda sig: (order.append((c.pid, sig)), setattr(c, "returncode", -sig)))(c)

    def boom(now):
        raise RuntimeError("a bug in a tick")

    sup.tick = boom
    sup.get("postgres").stop_signal = signal.SIGINT  # as postgres_proc() makes it
    with pytest.raises(RuntimeError):
        cc.serve(sup, threading.Event(), clock=lambda: 0)
    pg, web, cc_ = (c.pid for c in children)
    assert order[-1] == (pg, signal.SIGINT), "Postgres last, with a fast shutdown"
    assert {(web, signal.SIGTERM), (cc_, signal.SIGTERM)} <= set(order[:-1])


def test_one_container_per_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "DATA", tmp_path)
    monkeypatch.setattr(cc, "_LOCK", {})
    other = os.open(tmp_path / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another container holds it
    with pytest.raises(SystemExit, match="another container"):
        cc.lock_volume()
    os.close(other)
    fd = cc.lock_volume()
    assert cc.lock_volume() == fd, "taken once per process: restore and the bring-up share it"
    probe = os.open(tmp_path / ".lock", os.O_RDWR)
    with pytest.raises(BlockingIOError):
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(probe)
    os.close(fd)


# ---------------------------------------------------------------- restore and the key


def test_a_restore_keeps_the_volumes_key_unless_told_to_replace_it(tmp_path):
    kept = cc.ensure_secrets(tmp_path, {})["CC_SECRET_KEY"]
    other = cc._fernet()
    assert cc.key_change(tmp_path, {}, force=False) is None
    assert cc.key_change(tmp_path, {"CC_SECRET_KEY": kept}, force=False) is None
    with pytest.raises(SystemExit, match="not the key this volume keeps"):
        cc.key_change(tmp_path, {"CC_SECRET_KEY": other}, force=False)
    assert cc.key_change(tmp_path, {"CC_SECRET_KEY": other}, force=True) == other
    assert (tmp_path / "CC_SECRET_KEY").read_text() == kept, "nothing written before the restore succeeds"
    with pytest.raises(SystemExit, match="not a key"):
        cc.key_change(tmp_path, {"CC_SECRET_KEY": "nope"}, force=True)


def test_a_new_volume_takes_the_key_from_the_environment(tmp_path):
    key = cc._fernet()
    assert cc.key_change(tmp_path, {"CC_SECRET_KEY": key}, force=False) is None
    assert cc.ensure_secrets(tmp_path, {"CC_SECRET_KEY": key})["CC_SECRET_KEY"] == key


def test_a_restore_is_one_transaction():
    text = (HERE / "carcommand.py").read_text()
    assert '"--single-transaction"' in text and '"--exit-on-error"' in text


# ---------------------------------------------------------------- the image


def test_the_build_context_keeps_host_secrets_out():
    lines = [x.strip() for x in (HERE / "Dockerfile.dockerignore").read_text().splitlines() if x.strip() and not x.startswith("#")]
    assert lines[0] == "*", "everything out, then only what the image copies back in"
    assert not any(x.startswith("!") and (".env" in x or "secrets" in x) for x in lines)


def test_both_images_install_connectors_from_the_same_local_wheels_and_pypi():
    """stack/cc/wheels/ holds a connector build CC_CONNECTORS may pin before it is on PyPI; the
    folder is tracked (its .gitkeep) so the COPY never fails, and the wheels never are."""
    cc_image, one = (HERE.parent / "cc" / "Dockerfile").read_text(), (HERE / "Dockerfile").read_text()
    assert "COPY wheels/ /tmp/wheels/" in cc_image and "COPY cc/wheels/ /tmp/wheels/" in one
    for text in (cc_image, one):
        assert "pip install --no-cache-dir --find-links /tmp/wheels -r /tmp/connectors.txt" in text
    assert (HERE.parent / "cc" / "wheels" / ".gitkeep").exists()
    assert "cc/wheels" not in (HERE / "Dockerfile.dockerignore").read_text(), "the one-container build needs the folder in its context"
    assert "stack/cc/wheels/*.whl" in (HERE.parents[1] / ".gitignore").read_text().splitlines()


def test_the_image_ships_whole_source_directories_so_new_modules_come_along():
    text = (HERE / "Dockerfile").read_text()
    for line in ("COPY cc/ /app/", "COPY config/ /config/", "COPY web/ /srv/web/"):
        assert line in text
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "carcommand"]' in text
    assert "COPY shared/connectors.json shared/sealing.py /app/" in text and "COPY shared/connectors.json shared/sealing.py /srv/web/" in text, "one catalogue, one sealing module"


def test_the_volume_lets_each_part_through_to_its_own_directory_and_no_further(tmp_path, monkeypatch):
    """/data is passed through, never listed; the plugins, the connector's files and the
    database are their owners' alone."""
    me = (os.getuid(), os.getgid())
    monkeypatch.setattr(cc, "DATA", tmp_path)
    monkeypatch.setattr(cc, "RUN", tmp_path / "run")
    monkeypatch.setattr(cc, "PG_SOCKET", str(tmp_path / "pgsock"))
    monkeypatch.setattr(cc, "ids", lambda name: me)
    os.chmod(tmp_path, 0o755)
    cc.prepare_dirs()
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o711
    for name in ("postgres", "plugins", "connector", "terrain", "imports"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o700, name


def test_the_site_is_told_where_to_keep_terrain_tiles():
    assert cc.web_env({}, S)["TERRAIN_DIR"] == str(cc.DATA / "terrain")


# ---------------------------------------------------------------- Tesla's live stream


def test_the_auth_key_comes_from_the_environment_a_file_it_names_or_the_volume(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    assert cc.tailscale_key({}, secrets) is None
    assert cc.tailscale_key({"TS_AUTHKEY": " tskey-auth-env\n"}, secrets) == "tskey-auth-env"
    assert cc.tailscale_key({"TS_AUTH_KEY": "tskey-auth-alias"}, secrets) == "tskey-auth-alias"
    given = tmp_path / "given"
    given.write_text("tskey-auth-file\n")
    assert cc.tailscale_key({"TS_AUTHKEY": f"file:{given}"}, secrets) == "tskey-auth-file", "as compose passes it"
    (secrets / "tailscale-authkey").write_text("tskey-auth-volume\n")
    assert cc.tailscale_key({}, secrets) == "tskey-auth-volume"
    assert cc.tailscale_key({"TS_AUTHKEY": "tskey-auth-env"}, secrets) == "tskey-auth-env", "the run line wins"
    with pytest.raises(SystemExit, match="cannot be read"):
        cc.tailscale_key({"TS_AUTHKEY": f"file:{tmp_path / 'missing'}"}, secrets)


def test_the_stream_runs_with_a_key_or_a_node_that_signed_in_before_and_not_otherwise(tmp_path):
    assert not cc.stream_wanted(None, tmp_path / "net"), "no key, no node: nothing extra runs"
    assert cc.stream_wanted("tskey-auth-x", tmp_path / "net")
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tailscaled.state").write_text("{}")
    assert cc.stream_wanted(None, tmp_path / "net"), "the key is spent once; the node's state carries it after"


def test_the_auth_key_never_reaches_the_site():
    env = cc.web_env({"TS_AUTHKEY": "tskey-auth-x", "TS_AUTH_KEY": "tskey-auth-y"}, S)
    assert "TS_AUTHKEY" not in env and "TS_AUTH_KEY" not in env
    assert "tskey" not in repr(cc.cc_env({"TS_AUTHKEY": "tskey-auth-x"}, S))


def test_the_site_is_pointed_at_the_receiver_and_its_name(tmp_path):
    certs = tmp_path / "certs"
    certs.mkdir()
    got = cc.stream_web_settings({}, certs)
    assert got == {"TESLA_STREAM_ZMQ": cc.STREAM_ZMQ}, "no name until the node has its certificate"
    (certs / "host").write_text("carcommand-stream.tail0000.ts.net\n")
    got = cc.stream_web_settings({"TESLA_STREAM_ZMQ": "tcp://elsewhere:5284"}, certs)
    assert got == {"TESLA_STREAM_ZMQ": cc.STREAM_ZMQ, "TESLA_STREAM_HOST": "carcommand-stream.tail0000.ts.net"}
    got = cc.stream_web_settings({"TESLA_STREAM_HOST": "mine.tail0000.ts.net"}, certs)
    assert got["TESLA_STREAM_HOST"] == "mine.tail0000.ts.net", "a name on the run line is kept"
    env = cc.web_env({"TESLA_STREAM_ZMQ": "tcp://elsewhere:5284"}, S, got)
    assert env["TESLA_STREAM_ZMQ"] == cc.STREAM_ZMQ and env["TESLA_STREAM_HOST"] == "mine.tail0000.ts.net"
    assert cc.web_env({"TESLA_STREAM_ZMQ": "tcp://elsewhere:5284"}, S)["TESLA_STREAM_ZMQ"] == "tcp://elsewhere:5284", "with the stream off, nothing changes"


def test_the_node_is_set_up_as_compose_sets_it_with_its_state_in_the_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "DATA", tmp_path)
    env = cc.net_env(Path("/run/tesla-net/authkey"))
    compose = (HERE.parent / "docker-compose.yml").read_text()
    for name in ("TS_HOSTNAME", "TS_EXTRA_ARGS", "TS_AUTH_ONCE", "TS_USERSPACE"):
        assert f"- {name}={env[name]}" in compose, name
    assert env["TS_STATE_DIR"] == str(tmp_path / "tesla" / "net"), "the node's identity survives an upgrade"
    assert env["TS_AUTHKEY"] == "file:/run/tesla-net/authkey", "a path on tailscale up's command line, never the key"
    assert env["TS_SERVE_CONFIG"] == "/srv/tesla/serve.json" and env["TS_SOCKET"].startswith(str(cc.NET_RUN))
    assert env["TS_CERT_DIR"] == str(tmp_path / "tesla" / "certs") and env["RECEIVER_UID"] == ""
    assert not any(k.startswith(("DB_", "CC_", "REST_", "WEBUI_")) for k in env)
    assert "TS_AUTHKEY" not in cc.net_env(None), "a node that signed in before needs no key"


def test_the_receiver_runs_as_its_own_user_once_it_has_a_certificate(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "DATA", tmp_path)
    net, receiver = cc.stream_procs(None)
    assert (net.name, net.user, net.argv) == ("tesla-net", "tsnet", ["/bin/sh", "/srv/tesla/net.sh"])
    assert (receiver.name, receiver.user) == ("receiver", "telemetry")
    assert receiver.argv == ["/usr/local/bin/fleet-telemetry", "-config", "/srv/tesla/telemetry.json"]
    assert receiver.umask == 0o007, "the site reaches its socket through the group"
    assert set(receiver.env) <= {"PATH", "HOME"}
    assert receiver.state == "waiting" and not receiver.due(10**9), "held until the certificate is there"
    certs = tmp_path / "tesla" / "certs"
    certs.mkdir(parents=True)
    (certs / "tls.crt").write_text("crt")
    assert not receiver.due(10**9), "the key too"
    (certs / "tls.key").write_text("key")
    assert receiver.due(10**9)
    assert {"tsnet", "telemetry"}.isdisjoint({"postgres", "carcommand", "web"})


def test_the_receivers_config_and_the_funnel_match_the_supervisors_paths():
    tel = json.loads((HERE / "tesla-telemetry.json").read_text())
    assert tel["host"] == "127.0.0.1", "reached through Funnel, never from the network"
    assert tel["zmq"]["addr"] == cc.STREAM_ZMQ and cc.STREAM_ZMQ.startswith("ipc://")
    assert tel["tls"] == {"server_cert": "/data/tesla/certs/tls.crt", "server_key": "/data/tesla/certs/tls.key"}
    compose_tel = json.loads((HERE.parent / "tesla" / "telemetry.json").read_text())
    assert {k: v for k, v in tel.items() if k not in ("host", "zmq", "tls")} == {k: v for k, v in compose_tel.items() if k not in ("host", "zmq", "tls")}
    serve = json.loads((HERE / "tesla-serve.json").read_text())
    compose_serve = json.loads((HERE.parent / "tesla" / "serve.json").read_text())
    assert serve["TCP"]["8443"] == {"TCPForward": f"127.0.0.1:{tel['port']}"}
    (site,) = serve["Web"].values()
    assert [h["Proxy"] for h in site["Handlers"].values()] == [f"http://127.0.0.1:{cc.PORT}/.well-known/appspecific/com.tesla.3p.public-key.pem"]
    assert serve["AllowFunnel"] == compose_serve["AllowFunnel"] and serve["TCP"]["443"] == compose_serve["TCP"]["443"]
    assert list(site["Handlers"]) == list(next(iter(compose_serve["Web"].values()))["Handlers"]), "the key's path alone"


def test_the_image_takes_the_binaries_from_the_images_compose_runs():
    compose = (HERE.parent / "docker-compose.yml").read_text()
    docker = (HERE / "Dockerfile").read_text()
    for image in ("tailscale/tailscale", "tesla/fleet-telemetry"):
        ref = re.search(rf"image: ({re.escape(image)}:\S+@sha256:[0-9a-f]{{64}})", compose).group(1)
        assert f"FROM {ref} AS" in docker, f"{image}: the same tag and digest in both, or one moved without the other"


def test_the_stream_directories_belong_to_their_parts(tmp_path, monkeypatch):
    me = (os.getuid(), os.getgid())
    monkeypatch.setattr(cc, "DATA", tmp_path)
    monkeypatch.setattr(cc, "NET_RUN", tmp_path / "run-net")
    monkeypatch.setattr(cc, "STREAM_RUN", tmp_path / "run-stream")
    monkeypatch.setattr(cc, "ids", lambda name: me)
    key = cc.prepare_stream("tskey-auth-x")
    modes = {p: stat.S_IMODE(p.stat().st_mode) for p in (tmp_path / "tesla", tmp_path / "tesla" / "net", tmp_path / "tesla" / "certs", tmp_path / "run-net", tmp_path / "run-stream")}
    assert list(modes.values()) == [0o711, 0o700, 0o2750, 0o700, 0o2750]
    assert key == tmp_path / "run-net" / "authkey" and key.read_text() == "tskey-auth-x"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert cc.prepare_stream(None) is None and not key.exists(), "no key given: none left lying about"


def stream_sup(tmp_path, monkeypatch, web_env=None):
    monkeypatch.setattr(cc, "DATA", tmp_path)
    certs = tmp_path / "tesla" / "certs"
    certs.mkdir(parents=True)
    sup, children = make(("web",))
    web = sup.get("web")
    web.env = dict(web_env or {})
    _, receiver = cc.stream_procs(None)
    sup.procs.append(receiver)
    sup.watchers.append(cc.StreamWatch(receiver, web, certs))
    return sup, web, receiver, certs


def write_cert(certs, body):
    for name in ("tls.key", "tls.crt"):  # in net.sh's order: the certificate last
        tmp = certs / f"new.{name}"
        tmp.write_text(body)
        os.replace(tmp, certs / name)


def test_a_renewed_certificate_restarts_the_receiver(tmp_path, monkeypatch):
    sup, web, receiver, certs = stream_sup(tmp_path, monkeypatch, {"TESLA_STREAM_HOST": "carcommand-stream.tail0000.ts.net"})
    sup.tick(1)
    assert receiver.state == "waiting", "no certificate yet"
    write_cert(certs, "one")
    sup.tick(2)
    sup.tick(3)
    assert receiver.state == "running"
    first = receiver.child
    sup.tick(100)
    assert first.signals == [], "nothing changed, nothing restarted"
    write_cert(certs, "two, a little longer")
    sup.tick(200)
    assert first.signals == [signal.SIGTERM]
    sup.tick(201)
    sup.tick(203)
    assert receiver.state == "running" and receiver.child is not first
    assert web.child.signals == [], "the site already knew the name"


def test_the_site_learns_the_streams_name_once_the_node_has_it(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    sup, web, receiver, certs = stream_sup(tmp_path, monkeypatch)
    sup.tick(1)
    assert web.child.signals == []
    (certs / "host").write_text("carcommand-stream.tail0000.ts.net\n")
    write_cert(certs, "one")
    sup.tick(2)
    assert web.env["TESLA_STREAM_HOST"] == "carcommand-stream.tail0000.ts.net"
    assert web.child.signals == [signal.SIGTERM], "restarted once, to read it"
    assert "carcommand-stream.tail0000.ts.net" in caplog.text


def test_a_name_on_the_run_line_that_is_not_the_nodes_is_said(tmp_path, monkeypatch, caplog):
    sup, web, receiver, certs = stream_sup(tmp_path, monkeypatch, {"TESLA_STREAM_HOST": "carcommand-stream.tail0000.ts.net"})
    (certs / "host").write_text("carcommand-stream-1.tail0000.ts.net\n")
    write_cert(certs, "one")
    sup.tick(1)
    assert web.env["TESLA_STREAM_HOST"] == "carcommand-stream.tail0000.ts.net" and web.child.signals == []
    assert "carcommand-stream-1.tail0000.ts.net" in caplog.text


def test_status_says_what_the_receiver_waits_for(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cc, "RUN", tmp_path)
    sup, web, receiver, certs = stream_sup(tmp_path, monkeypatch)
    sup.status_path = tmp_path / "status.json"
    sup.tick(1)
    assert cc.status() == 0
    out = capsys.readouterr().out
    assert re.search(r"receiver\s+waiting\s+for its certificate", out), out


# ---------------------------------------------------------------- Tesla's signing proxy


def test_the_proxy_runs_unless_the_owner_has_one_and_the_site_is_told_where_it_is():
    # a Tesla app may be entered on the Connect Tesla page, not only in the environment: the proxy runs and waits for its key
    assert cc.proxy_env({}) == {"TESLA_PROXY_URL": "https://localhost:4443", "TESLA_PROXY_CERT": str(cc.PROXY_DIR / "tls.crt")}
    env = cc.proxy_env({"TESLA_CLIENT_ID": "client-alpha"})
    assert env == {"TESLA_PROXY_URL": "https://localhost:4443", "TESLA_PROXY_CERT": str(cc.PROXY_DIR / "tls.crt")}
    assert cc.proxy_env({"TESLA_CLIENT_ID": "x", "TESLA_PROXY_URL": "https://elsewhere:4443"}) == {}, "a proxy of the owner's own is left alone"
    s = {"DB_PASSWORD": "d", "WEBUI_PASSWORD": "w", "REST_PASSWORD": "r", "CC_SECRET_KEY": "k"}
    assert cc.web_env({"TESLA_CLIENT_ID": "client-alpha"}, s)["TESLA_PROXY_URL"] == "https://localhost:4443"
    assert cc.tesla_proxy_proc({"TESLA_PROXY_URL": "https://elsewhere:4443"}, s) is None


def test_the_proxy_listens_on_localhost_with_its_own_tls_key():
    argv = cc.proxy_argv(Path("/run/aio/tesla-proxy"))
    assert argv[0] == cc.PROXY_BIN
    opts = dict(zip(argv[1::2], argv[2::2]))
    assert opts == {"-key-file": "/run/aio/tesla-proxy/key.pem", "-cert": "/run/aio/tesla-proxy/tls.crt", "-tls-key": "/run/aio/tesla-proxy/tls.key", "-host": "localhost", "-port": "4443"}


def test_the_proxy_waits_for_the_key_then_writes_it_private_and_becomes_the_proxy(tmp_path, monkeypatch, capsys):
    import json

    monkeypatch.setattr(cc, "PROXY_DIR", tmp_path / "tesla-proxy")
    answers = [(3, ""), (3, ""), (0, json.dumps({"key.pem": "APP KEY", "tls.key": "TLS KEY", "tls.crt": "CERT"}))]
    slept, ran = [], []
    cc.tesla_proxy({}, export=lambda env: answers.pop(0), sleep=slept.append, execve=lambda *a: ran.append(a))
    assert slept == [cc.PROXY_WAIT_S] * 2
    assert capsys.readouterr().out.count("waiting for Connect Tesla") == 1, "said once, not every minute"
    d = tmp_path / "tesla-proxy"
    modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in d.iterdir()}
    assert modes == {"key.pem": 0o600, "tls.key": 0o600, "tls.crt": 0o644}
    assert stat.S_IMODE(d.stat().st_mode) == 0o711
    assert (d / "key.pem").read_text() == "APP KEY"
    assert ran and ran[0][0] == cc.PROXY_BIN and ran[0][2] == {"PATH": cc.BASE_PATH, "HOME": "/tmp"}, "the proxy gets no secret from the environment"


def test_the_image_carries_the_pinned_proxy_and_its_own_user():
    text = (HERE / "Dockerfile").read_text()
    assert re.search(r"COPY --from=tesla/vehicle-command:\d+\.\d+\.\d+@sha256:[0-9a-f]{64} /usr/local/bin/tesla-http-proxy", text)
    assert "useradd -r -u 10005 -g 10005 -d /tmp teslaproxy" in text


def test_status_keeps_a_space_after_the_longest_name(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cc, "RUN", tmp_path)
    procs = {n: {"state": "running", "up_s": 5, "restarts": 0, "last_exit": None} for n in ("web", "tesla-net", "tesla-proxy")}
    (tmp_path / "status.json").write_text(json.dumps({"at": int(time.time()), "processes": procs}))
    assert cc.status() == 0
    lines = capsys.readouterr().out.splitlines()
    for name in procs:
        assert any(re.match(rf"{re.escape(name)} +running", line) for line in lines), lines
    assert len({line.index("running") for line in lines if "running" in line}) == 1, "one column"
