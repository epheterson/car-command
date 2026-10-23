"""The connector runs on its sources: the app's Lucid token when there is one, the file as before when not."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from supervise import Supervisor, default_connectors, desired  # noqa: E402

ENTRY_DEFAULT = '[{"type":"lucid","config":{"refresh_token_file":"/secrets/lucid-token.json","interval":60,"driving_interval":15,"log_level":"info","api_log_level":"error","commands":true}}]'


def test_without_a_lucid_source_the_config_is_todays():
    assert default_connectors("/secrets/lucid-token.json", {"CC_POLL_IDLE_S": "60", "CC_POLL_DRIVING_S": "15"}) == ENTRY_DEFAULT
    # the shell's old default, plus the commands the site now switches: nothing else moved
    shell = subprocess.run(
        [
            "sh",
            "-c",
            'CC_POLL_IDLE_S=60 CC_POLL_DRIVING_S=15; echo "[{\\"type\\":\\"lucid\\",\\"config\\":{\\"refresh_token_file\\":\\"/secrets/lucid-token.json\\",\\"interval\\":${CC_POLL_IDLE_S:-60},\\"driving_interval\\":${CC_POLL_DRIVING_S:-15},\\"log_level\\":\\"info\\",\\"api_log_level\\":\\"error\\"}}]"',
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert shell.replace('"error"}}]', '"error","commands":true}}]') == ENTRY_DEFAULT
    assert desired([], {}, "/tmp/lucid-token.json") == {"connectors": ENTRY_DEFAULT, "token": None, "from": "file", "env": {"CC_GEOCODER_URL": ""}}
    assert desired([], {"CC_CONNECTORS_JSON": '[{"type":"x"}]'}, "/tmp/t")["connectors"] == '[{"type":"x"}]'


def test_a_lucid_source_in_the_app_is_used_from_a_private_file():
    rows = [{"id": 1, "config": {"pending": False}, "plain": json.dumps({"refresh_token": "rt-123"}), "enabled": True}]
    got = desired(rows, {}, "/tmp/lucid-token.json")
    assert got["from"] == "app" and got["token"] == "rt-123" and '"/tmp/lucid-token.json"' in got["connectors"]
    assert desired([dict(rows[0], plain=None)], {}, "/tmp/t")["from"] == "file", "a row the key cannot open falls back to the file"
    assert desired([dict(rows[0], config={"pending": True})], {}, "/tmp/t")["from"] == "file", "a sign-in not done yet is not a token"


class Child:
    def __init__(self):
        self.code, self.stopped = None, False

    def poll(self):
        return self.code

    def terminate(self):
        self.stopped = True
        self.code = 0

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


class DB:
    """cc_state and cc_sources in memory, behind the few statements the supervisor runs."""

    def __init__(self, rows=None, version="1"):
        self.rows, self.version, self.updates = rows or [], version, []

    def __call__(self):
        return self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, args=()):
        self.last = " ".join(sql.split()).lower()
        self.args = args
        if self.last.startswith("update cc_sources"):
            self.updates.append((self.last, args))
            if "case when" in self.last:  # another brand's state, as the supervisor writes it
                config, drop, error, ok, rid = args
                for r in self.rows:
                    if r["id"] == rid:
                        r.update(config=json.loads(config), last_error=error, ok=ok or r.get("ok", False))
                        if drop:
                            r["secret"] = None
        if self.last.startswith("delete from cc_sources"):
            self.rows = [r for r in self.rows if r["id"] != args[0]]

    def fetchone(self):
        # Only the sources_version row exists unless a test sets self.geocoder: a database
        # with no geocoder_url row is "unset", today's behaviour, same as every test below.
        if "cc_state" not in self.last:
            return None
        if self.args == ("sealing",):
            return ("bound-v1",)  # bound already, unless a test says otherwise
        if self.args == ("geocoder_url",):
            return (self.geocoder,) if getattr(self, "geocoder", None) is not None else None
        return (self.version,)

    def fetchall(self):
        if "cc_sources" not in self.last:
            return []
        return [(r["id"], json.dumps(r["config"]), r["secret"], r["enabled"]) for r in self.rows if r.get("kind", "lucid") == self.args[0]]

    def commit(self):
        pass

    def close(self):
        pass


class Vault:
    ready = True

    def seal(self, text, kind=None, sid=None):
        return ("sealed:" + text).encode()

    def open(self, blob, kind=None, sid=None):
        return bytes(blob).decode()[len("sealed:") :] if blob else None

    def open_row(self, blob, kind=None, sid=None):
        return self.open(blob, kind, sid), None


def make(tmp_path, db, sign_in=lambda e, p: "rt-new", env=None, **kw):
    tpl = tmp_path / "t.json"
    tpl.write_text('{"connectors": ${CC_CONNECTORS_JSON}}')
    children = []

    def spawn(path):
        children.append(Child())
        return children[-1]

    kw.setdefault("file_ok", lambda: True)  # a token file, as every install had before; the tests of none say so
    kw.setdefault("db_role", lambda: ("cc_plugin", "pw-role"))  # the plugin role is there; tests of none say so
    s = Supervisor(
        db=db, vault=Vault(), spawn=spawn, sign_in=sign_in, env=dict(env or {}), template=str(tpl), config_path=str(tmp_path / "c.json"), app_token_path=str(tmp_path / "lucid-token.json"), **kw
    )
    return s, children


def test_a_bump_with_nothing_changed_restarts_nothing(tmp_path):
    db = DB()
    s, children = make(tmp_path, db)
    s.start()
    db.version = "2"
    assert s.tick() is False and len(children) == 1 and not children[0].stopped


def test_a_sign_in_keeps_the_token_and_drops_the_password(tmp_path):
    db = DB(rows=[{"id": 7, "config": {"pending": True}, "secret": b"sealed:" + json.dumps({"email": "o@x", "password": "pw-lucid-9"}).encode(), "enabled": True}])
    s, children = make(tmp_path, db)
    s.start()
    sql, args = db.updates[0]
    assert "last_ok_at = now()" in sql and b"pw-lucid-9" not in args[0] and b"rt-new" in args[0]
    assert json.loads(args[1]) == {"pending": False}
    token_file = tmp_path / "lucid-token.json"
    assert json.loads(token_file.read_text()) == {"refresh_token": "rt-new"} and oct(token_file.stat().st_mode & 0o777) == "0o600"
    assert len(children) == 1 and s.running["from"] == "app", "signed in before the first start, so it starts on the new token"


def test_a_token_that_arrives_later_restarts_carconnectivity_once(tmp_path):
    db = DB()
    s, children = make(tmp_path, db)
    s.start()
    assert s.running["from"] == "file"
    db.rows = [{"id": 7, "config": {"pending": False}, "secret": b"sealed:" + json.dumps({"refresh_token": "rt-9"}).encode(), "enabled": True}]
    db.version = "2"
    assert s.tick() is True and children[0].stopped and len(children) == 2 and s.running["from"] == "app"
    assert s.tick() is False, "the same version again restarts nothing"


def test_only_the_geocoder_choice_changing_restarts_the_connector_once(tmp_path, monkeypatch):
    """Sources are unchanged; only cc_state.geocoder_url is new (the site bumps
    sources_version in the same transaction it writes that row — see app.write_geocoder_state).
    One restart, and the child sees the new CC_GEOCODER_URL."""
    monkeypatch.delenv("CC_GEOCODER_URL", raising=False)
    db = DB()
    s, children = make(tmp_path, db)
    s.start()
    assert os.environ["CC_GEOCODER_URL"] == ""

    db.version, db.geocoder = "2", "http://maps:8090/"
    assert s.tick() is True and children[0].stopped and len(children) == 2
    assert os.environ["CC_GEOCODER_URL"] == "http://maps:8090/"
    assert s.tick() is False, "the same version and geocoder again restarts nothing"


def test_a_failed_sign_in_keeps_nothing_secret(tmp_path, caplog):
    def refused(email, password):
        raise RuntimeError(f"UNAUTHENTICATED for {email} / {password}")

    db = DB(rows=[{"id": 7, "config": {"pending": True}, "secret": b"sealed:" + json.dumps({"email": "o@x", "password": "pw-lucid-9"}).encode(), "enabled": True}])
    s, children = make(tmp_path, db, sign_in=refused)
    s.start()
    sql, args = db.updates[0]
    assert "secret = null" in sql and "pw-lucid-9" not in str(args) and "pw-lucid-9" not in caplog.text
    assert len(children) == 1, "the connector still runs, on the file"


def test_no_database_or_no_key_falls_back_to_the_file(tmp_path):
    def down():
        raise OSError("no database")

    s, children = make(tmp_path, down)
    s.start()
    assert len(children) == 1 and json.loads((tmp_path / "c.json").read_text())["connectors"][0]["config"]["refresh_token_file"] == "/secrets/lucid-token.json"


def test_when_carconnectivity_exits_the_supervisor_exits_with_it(tmp_path):
    s, children = make(tmp_path, DB())
    s.start()
    children[0].code = 3
    with pytest.raises(SystemExit) as out:
        s.tick()
    assert out.value.code == 3


def test_the_entrypoint_hands_over_to_the_supervisor_and_the_image_carries_it():
    entry = (HERE / "entrypoint.sh").read_text()
    assert "/app/supervise.py" in entry and "exec carconnectivity" not in entry
    assert "supervise.py" in (HERE / "Dockerfile").read_text()
    compose = (HERE.parent / "docker-compose.yml").read_text()
    cc = compose[compose.index("  cc:") : compose.index("  schema:")]
    assert "CC_SECRET_KEY=${CC_SECRET_KEY:-}" in cc


def test_a_database_blip_mid_run_changes_nothing(tmp_path):
    """Once running on the app's token, a moment without the database is not "no Lucid source":
    restarting onto the file and back would be two rate-limited Lucid logins for nothing."""
    db = DB(rows=[{"id": 7, "config": {"pending": False}, "secret": b"sealed:" + json.dumps({"refresh_token": "rt-9"}).encode(), "enabled": True}])
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 2:  # the start: binding old secrets, then the sources
            raise OSError("too many connections")
        return db

    s, children = make(tmp_path, flaky)
    s.start()
    assert s.running["from"] == "app"
    assert s.tick() is False and s.tick() is False and len(children) == 1 and not children[0].stopped


def test_the_geocoder_choice_reaches_the_connector():
    d = desired([], {"CC_GEOCODER_STATE": "http://maps:8090/"}, "/tmp/t")
    assert d["env"]["CC_GEOCODER_URL"] == "http://maps:8090/"
    assert "CC_GEOCODER_URL" not in desired([], {"CC_GEOCODER_STATE": "public"}, "/tmp/t")["env"], "public: unset, the service's own rounded lookups"
    assert desired([], {"CC_GEOCODER_STATE": ""}, "/tmp/t")["env"] == {"CC_GEOCODER_URL": ""}
    assert desired([], {}, "/tmp/t")["env"] == {"CC_GEOCODER_URL": ""}, "no decision yet is off, not public"


def test_with_no_geocoder_decision_the_connector_names_nothing(tmp_path, monkeypatch):
    """A first start before the site has written cc_state.geocoder_url, or with a database
    that cannot be read, must not send a stop to the public geocoder."""

    def down():
        raise OSError("no database")

    for db in (DB(), down):
        monkeypatch.delenv("CC_GEOCODER_URL", raising=False)
        s, children = make(tmp_path, db)
        s.start()
        assert os.environ["CC_GEOCODER_URL"] == "", db
    db = DB()
    db.geocoder = "public"
    s, children = make(tmp_path, db)
    s.start()
    assert "CC_GEOCODER_URL" not in os.environ, "the site's explicit public decision unsets it"


def test_a_sign_in_the_connector_cannot_open_is_kept_for_a_restart_and_never_tried_empty(tmp_path):
    """No key in the connector (or another key): not the user's password. No empty login, and
    the sealed sign-in stays: the key, not the sign-in, may be what is wrong."""
    tried = []
    pending = {"id": 7, "config": {"pending": True}, "secret": b"sealed-with-another-key", "enabled": True}

    class NoOpen(Vault):
        def open(self, blob, *_where):
            return None

    for vault in (NoOpen(), type("NotReady", (Vault,), {"ready": False})()):
        db = DB(rows=[dict(pending)])
        s, children = make(tmp_path, db, sign_in=lambda e, p: tried.append(e) or "rt")
        s.vault = vault
        s.start()
        sql, args = db.updates[0]
        assert "secret" not in sql.split(" where ")[0] and "CC_SECRET_KEY" in args[1] and "kept" in args[1]
        assert json.loads(args[0]) == {"pending": True, "locked": True}
        db.rows[0]["config"] = {"pending": True, "locked": True}
        db.version = "2"
        s.tick()
        assert len(db.updates) == 1, "not tried again every tick"
    assert tried == [], "no sign-in with empty credentials"
    db.rows[0]["secret"] = b"sealed:" + json.dumps({"email": "o@x", "password": "pw"}).encode()
    s, _ = make(tmp_path, db, sign_in=lambda e, p: tried.append(e) or "rt-after-restart")
    s.start()
    assert tried == ["o@x"], "a restart (with the right key) tries it again"


# ---- other brands: installed on request, on trial for their first start, removed on request

CUPRA = {"connector": "cupra", "package": "carconnectivity-connector-seatcupra", "version": "0.6.4", "type": "seatcupra", "connector_id": "cupra", "brand": "Cupra"}
CREDS = {"username": "owner@example.com", "password": "pw-cupra-77", "brand": "cupra"}


def cc_row(state="install", rid=9, **config):
    return {"id": rid, "kind": "carconnectivity", "config": {**CUPRA, "state": state, **config}, "secret": b"sealed:" + json.dumps(CREDS).encode(), "enabled": True}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def brand(tmp_path, db, install_error=None, **kw):
    """A supervisor whose pip is a recorder, whose clocks the test turns, and whose plugins are
    "on disk" once installed (or from the start, for rows already running)."""
    asked = {"install": [], "uninstall": [], "env": []}
    on_disk = set(kw.pop("on_disk", ()))

    def install(config, env):
        asked["install"].append(config["package"])
        asked["env"].append(dict(env))
        err = install_error() if callable(install_error) else install_error
        if not err:
            on_disk.add(config["package"])
        return err

    def uninstall(config, env):
        asked["uninstall"].append(config["package"])
        asked["env"].append(dict(env))
        on_disk.discard(config["package"])

    clock, wall = Clock(), Clock(1_800_000_000.0)
    kw.setdefault("state_path", str(tmp_path / "starts.json"))
    s, children = make(tmp_path, db, install=install, uninstall=uninstall, clock=clock, wall=wall, installed=lambda c: c["package"] in on_disk, **kw)
    return s, children, asked, clock, wall


def blocks(tmp_path):
    return [b.get("connector_id") or b["type"] for b in json.loads((tmp_path / "c.json").read_text())["connectors"]]


def test_an_added_brand_is_installed_after_the_start_then_written_only_into_the_private_config(tmp_path, caplog, monkeypatch):
    import logging

    caplog.set_level(logging.INFO)
    monkeypatch.delenv("CC_CONNECTORS_JSON", raising=False)
    db = DB(rows=[cc_row()])
    s, children, asked, _, _ = brand(tmp_path, db, env={"CC_SECRET_KEY": "k-not-for-plugins", "DB_PASSWORD": "db-pw", "PATH": "/usr/bin", "PYTHONUSERBASE": "/plugins"})
    s.start()
    assert asked["install"] == [] and blocks(tmp_path) == ["lucid"], "nothing slow on the way up: Lucid first"
    assert s.tick() is True and asked["install"] == ["carconnectivity-connector-seatcupra"] and db.rows[0]["config"]["state"] == "ready"
    for env in asked["env"]:
        assert "CC_SECRET_KEY" not in env and "DB_PASSWORD" not in env and env["PYTHONUSERBASE"] == "/plugins", "pip and the import check never see the key"
    cfg = tmp_path / "c.json"
    data = json.loads(cfg.read_text())["connectors"]
    assert data[0]["type"] == "lucid" and data[1] == {"type": "seatcupra", "connector_id": "cupra", "config": {**CREDS, "log_level": "warning"}}
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    assert "pw-cupra" not in os.environ.get("CC_CONNECTORS_JSON", "") and "owner@example.com" not in os.environ.get("CC_CONNECTORS_JSON", ""), "never through the environment"
    assert "pw-cupra" not in caplog.text and "owner@example.com" not in caplog.text and "Cupra" in caplog.text
    assert len(children) == 2 and s.trial == [9]


def test_a_brand_that_runs_through_its_trial_is_marked_running(tmp_path):
    db = DB(rows=[cc_row()])
    s, children, _, clock, _ = brand(tmp_path, db)
    s.start()
    s.tick()
    clock.t += 30
    assert s.tick() is False and db.rows[0]["config"]["state"] == "ready", "still on trial"
    clock.t += 200
    s.tick()
    assert db.rows[0]["config"]["state"] == "running" and db.rows[0]["ok"] and db.rows[0]["last_error"] is None and s.trial == []


def test_a_brand_that_kills_the_start_is_taken_out_whenever_it_dies_on_trial(tmp_path):
    for late in (20, 125):  # a death at 125 s, seen by the first tick after it, is still the trial's
        db = DB(rows=[cc_row()])
        s, children, _, clock, _ = brand(tmp_path, db)
        s.start()
        s.tick()
        clock.t += late
        children[-1].code = 1
        assert s.tick() is True, late
        row = db.rows[0]
        assert row["config"]["state"] == "failed" and row["secret"] is None and "exit 1" in row["last_error"] and "pw-cupra" not in row["last_error"]
        assert blocks(tmp_path) == ["lucid"] and s.trial == []


def test_an_install_that_fails_keeps_the_sign_in_and_tries_again_later(tmp_path):
    errors = ["pip could not install carconnectivity-connector-seatcupra 0.6.4: ERROR: no network"] * 2 + [None]
    db = DB(rows=[cc_row()])
    s, children, asked, _, wall = brand(tmp_path, db, install_error=lambda: errors.pop(0))
    s.start()
    s.tick()
    row = db.rows[0]
    assert row["config"]["state"] == "retry" and row["secret"] is not None and "no network" in row["last_error"] and "1 minute" not in row["last_error"]
    first_wait = row["config"]["retry_at"] - wall.t
    assert first_wait == 60 and blocks(tmp_path) == ["lucid"]
    db.version = "2"
    s.tick()
    assert len(asked["install"]) == 1, "not before its time"
    wall.t += 61
    s.tick()
    assert db.rows[0]["config"]["retries"] == 2 and db.rows[0]["config"]["retry_at"] - wall.t == 120, "the wait doubles"
    wall.t += 121
    assert s.tick() is True and db.rows[0]["config"]["state"] == "ready" and blocks(tmp_path) == ["lucid", "cupra"]
    assert "retries" not in db.rows[0]["config"] and "retry_at" not in db.rows[0]["config"]


def test_a_failed_reinstall_of_a_working_brand_keeps_its_sign_in_and_the_rest_runs(tmp_path):
    """A new volume, or a newer image: the plugin that worked is gone and will not come back yet."""
    db = DB(rows=[cc_row("running")])
    s, children, asked, _, _ = brand(tmp_path, db, install_error="pip could not install it: ERROR: no network")
    s.start()
    assert blocks(tmp_path) == ["lucid"], "not on disk: left out of the start rather than crashing it"
    s.tick()
    row = db.rows[0]
    assert row["config"]["state"] == "retry" and row["secret"] is not None and len(children) == 1


def test_a_running_brand_is_checked_once_after_the_start_not_before(tmp_path):
    db = DB(rows=[cc_row("running")])
    s, children, asked, _, _ = brand(tmp_path, db, on_disk={"carconnectivity-connector-seatcupra"})
    s.start()
    assert asked["install"] == [] and blocks(tmp_path) == ["lucid", "cupra"] and s.trial == [], "already proven: in at once, no trial"
    s.tick()
    db.version = "2"
    s.tick()
    assert asked["install"] == ["carconnectivity-connector-seatcupra"], "checked once, not on every read"


def test_a_row_naming_anything_but_a_catalogue_entry_is_refused_without_install_or_import(tmp_path):
    for bad in ({"package": "carconnectivity-connector-evil", "version": "0.6.3"}, {"package": "carconnectivity-connector-evil"}, {"connector": "volvo"}, {"type": "lucid"}):
        db = DB(rows=[cc_row("install", **bad), dict(cc_row("running", rid=10, **bad))])
        s, children, asked, _, _ = brand(tmp_path, db, on_disk={"carconnectivity-connector-seatcupra", "carconnectivity-connector-evil"})
        s.start()
        s.tick()
        assert asked["install"] == [] and asked["uninstall"] == [], bad
        assert all(r["config"]["state"] == "failed" and "Refused" in r["last_error"] for r in db.rows), bad
        assert blocks(tmp_path) == ["lucid"], bad


def test_quick_deaths_with_brands_in_are_counted_across_restarts_and_the_second_starts_without_them(tmp_path):
    rows = [cc_row("running"), cc_row("running", rid=10, connector="seat", connector_id="seat", brand="SEAT")]
    on_disk = {"carconnectivity-connector-seatcupra"}
    db = DB(rows=rows)
    s, children, _, clock, _ = brand(tmp_path, db, on_disk=on_disk)
    s.start()
    clock.t += 40
    children[-1].code = 1
    with pytest.raises(SystemExit):
        s.tick()  # the first: the container restarts, as always
    assert json.loads((tmp_path / "starts.json").read_text()) == {"quick_deaths": 1}
    s, children, _, clock, _ = brand(tmp_path, db, on_disk=on_disk)  # the restarted container
    s.start()
    assert blocks(tmp_path) == ["lucid", "cupra", "seat"]
    clock.t += 40
    children[-1].code = 1
    assert s.tick() is True and blocks(tmp_path) == ["lucid"], "the second: without the other brands"
    for r in db.rows:
        assert r["config"]["state"] == "retry" and r["secret"] is not None and "2 times in a row" in r["last_error"]
    assert json.loads((tmp_path / "starts.json").read_text()) == {"quick_deaths": 0}


def test_a_start_that_lasts_resets_the_count_and_a_late_death_is_the_containers(tmp_path):
    db = DB(rows=[cc_row("running")])
    (tmp_path / "starts.json").write_text('{"quick_deaths": 1}')
    s, children, _, clock, _ = brand(tmp_path, db, on_disk={"carconnectivity-connector-seatcupra"})
    s.start()
    clock.t += 200
    s.tick()
    assert json.loads((tmp_path / "starts.json").read_text()) == {"quick_deaths": 0}
    children[-1].code = 1
    with pytest.raises(SystemExit):
        s.tick()
    assert json.loads((tmp_path / "starts.json").read_text()) == {"quick_deaths": 0}


def test_a_request_the_connector_cannot_open_is_kept_and_installed_after_a_restart(tmp_path):
    db = DB(rows=[cc_row()])

    class NoOpen(Vault):
        def open(self, blob, *_where):
            return None

    s, children, asked, _, _ = brand(tmp_path, db)
    s.vault = NoOpen()
    s.start()
    s.tick()
    row = db.rows[0]
    assert asked["install"] == [] and row["config"]["state"] == "install" and row["config"]["locked"] and row["secret"] is not None
    assert "CC_SECRET_KEY" in row["last_error"]
    updates = len(db.updates)
    db.version = "2"
    s.tick()
    assert len(db.updates) == updates and asked["install"] == [], "left alone until a restart"
    s, _, asked, _, _ = brand(tmp_path, db)  # restarted, with a key that opens it
    s.start()
    s.tick()
    assert asked["install"] == ["carconnectivity-connector-seatcupra"] and db.rows[0]["config"]["state"] == "ready" and "locked" not in db.rows[0]["config"]


def test_a_removal_takes_the_brand_and_its_plugin_out(tmp_path):
    db = DB(rows=[cc_row("running")])
    s, children, asked, _, _ = brand(tmp_path, db, on_disk={"carconnectivity-connector-seatcupra"})
    s.start()
    s.tick()
    db.rows[0].update(config={**db.rows[0]["config"], "state": "remove", "uninstall": True}, secret=None, enabled=False)
    db.version = "3"
    assert s.tick() is True
    assert db.rows == [] and asked["uninstall"] == ["carconnectivity-connector-seatcupra"] and blocks(tmp_path) == ["lucid"]
    assert "CC_SECRET_KEY" not in asked["env"][-1]


def test_a_removal_keeps_a_plugin_another_account_uses_or_when_not_asked(tmp_path):
    seat = cc_row("running", rid=10, connector="seat", connector_id="seat", brand="SEAT")
    db = DB(rows=[cc_row("remove", uninstall=True), seat])
    s, _, asked, _, _ = brand(tmp_path, db)
    s.start()
    s.tick()
    assert asked["uninstall"] == [] and [r["id"] for r in db.rows] == [10]
    db = DB(rows=[cc_row("remove", uninstall=False)])
    s, _, asked, _, _ = brand(tmp_path, db)
    s.start()
    s.tick()
    assert asked["uninstall"] == [] and db.rows == []


def test_without_a_lucid_token_anywhere_another_brand_runs_alone(tmp_path):
    db = DB(rows=[cc_row("running")])
    s, _, _, _, _ = brand(tmp_path, db, file_ok=lambda: False, on_disk={"carconnectivity-connector-seatcupra"})
    s.start()
    assert blocks(tmp_path) == ["cupra"]
    assert desired([], {}, "/tmp/t", [], file_ok=True)["connectors"] == ENTRY_DEFAULT, "with a token file, the Lucid block as always"


def test_the_environment_block_for_a_brand_wins_over_the_app():
    rows = [dict(cc_row("running"), plain=json.dumps(CREDS))]
    got = desired([], {"CC_CONNECTORS_JSON": '[{"type":"seatcupra","connector_id":"cupra","config":{}}]'}, "/tmp/t", rows)
    assert "cc" not in got
    got = desired([], {"CC_CONNECTORS_JSON": '[{"type":"volkswagen","config":{}}]'}, "/tmp/t", rows)
    assert got["cc"][0]["connector_id"] == "cupra" and got["cc_ids"] == [9]
    assert desired([], {}, "/tmp/t", [dict(rows[0], config={**rows[0]["config"], "package": "requests"})]).get("cc") is None, "a row naming anything else is never run"


# ---- who can see what


def test_children_get_a_short_list_and_never_the_key_or_passwords():
    import privsep

    env = {"CC_SECRET_KEY": "k", "DB_PASSWORD": "p", "WEBUI_PASSWORD": "w", "REST_PASSWORD": "r", "CC_CONNECTORS_JSON": "[]", "DB_USER": "u", "PATH": "/bin", "LANG": "en_US.UTF-8"}
    env.update(PYTHONPATH="/app", PYTHONUSERBASE="/plugins", CC_GEOCODER_URL="", TZ="UTC", PYTHONNOUSERSITE="1")
    got = privsep.child_env(env)
    assert got == {"PATH": "/bin", "LANG": "en_US.UTF-8", "PYTHONPATH": "/app", "PYTHONUSERBASE": "/plugins", "CC_GEOCODER_URL": "", "TZ": "UTC", "HOME": "/tmp"}


def test_carconnectivity_is_spawned_as_the_user_with_the_short_list(monkeypatch):
    import privsep

    seen = {}
    monkeypatch.setattr(privsep.subprocess, "Popen", lambda cmd, **kw: seen.update(cmd=cmd, **kw))
    monkeypatch.setattr(privsep.os, "geteuid", lambda: 0)
    monkeypatch.setenv("CC_SECRET_KEY", "k-secret")
    monkeypatch.setenv("DB_PASSWORD", "db-secret")
    privsep.spawn("/run/carcommand/carconnectivity.json")
    assert seen["cmd"] == ["carconnectivity", "/run/carcommand/carconnectivity.json"]
    assert "CC_SECRET_KEY" not in seen["env"] and "DB_PASSWORD" not in seen["env"]
    assert seen["user"] == privsep.ids()[0] and seen["group"] == privsep.ids()[1] and seen["extra_groups"] == [] and seen["user"] != 0
    ran = {}
    monkeypatch.setattr(privsep.subprocess, "run", lambda cmd, **kw: ran.update(kw))
    privsep.run(["pip"], env={})
    assert ran["user"] != 0 and ran["env"] == {}


def test_the_secrets_come_back_from_the_files(tmp_path):
    import privsep

    (tmp_path / "CC_SECRET_KEY").write_text("k-from-file")
    env = privsep.load_secrets({}, str(tmp_path))
    assert env == {"CC_SECRET_KEY": "k-from-file"}


def test_the_entrypoint_moves_the_secrets_into_root_only_files_and_out_of_the_environment(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text("import json, os\nprint(json.dumps(dict(os.environ)))\n")
    run_dir = tmp_path / "run"
    env = {
        "PATH": os.path.dirname(sys.executable) + ":/usr/bin:/bin",
        "CC_RUN_DIR": str(run_dir),
        "CC_SUPERVISE": str(probe),
        "CC_SECRET_KEY": "key-123",
        "DB_PASSWORD": "db-456",
        "REST_PASSWORD": "r",
        "DB_USER": "u",
    }
    done = subprocess.run(["/bin/sh", str(HERE / "entrypoint.sh")], capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    for name in ("CC_SECRET_KEY", "DB_PASSWORD", "WEBUI_PASSWORD", "REST_PASSWORD", "CC_CONNECTORS_JSON"):
        assert name not in seen, name
        f = run_dir / "secrets" / name
        st = f.stat()
        assert oct(st.st_mode & 0o777) == "0o400" and st.st_uid == os.geteuid(), name  # root's, in the container
    assert (run_dir / "secrets" / "CC_SECRET_KEY").read_text() == "key-123" and (run_dir / "secrets" / "WEBUI_PASSWORD").read_text() == ""
    assert oct((run_dir / "secrets").stat().st_mode & 0o777) == "0o700" and oct(run_dir.stat().st_mode & 0o777) == "0o711"
    assert seen["PYTHONNOUSERSITE"] == "1" and seen["DB_USER"] == "u"
    entry = (HERE / "entrypoint.sh").read_text()
    assert 'python3 -s "${CC_SUPERVISE:-/app/supervise.py}"' in entry and "setpriv --reuid=10001" in entry


@pytest.mark.parametrize("name", ["DB_PASSWORD", "WEBUI_PASSWORD", "REST_PASSWORD"])
def test_the_entrypoint_refuses_a_placeholder_password(tmp_path, name):
    probe = tmp_path / "probe.py"
    probe.write_text("print('started')\n")
    env = {"PATH": os.path.dirname(sys.executable) + ":/usr/bin:/bin", "CC_RUN_DIR": str(tmp_path / "run"), "CC_SUPERVISE": str(probe), "DB_PASSWORD": "db", name: "CHANGE_ME"}
    done = subprocess.run(["/bin/sh", str(HERE / "entrypoint.sh")], capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode != 0 and "started" not in done.stdout
    assert name in done.stderr and "CHANGE_ME" in done.stderr and "make setup" in done.stderr


def test_with_no_source_at_all_carconnectivity_idles_instead_of_restarting_forever(tmp_path, caplog):
    """No Lucid token anywhere and no other brand: no Lucid block to exit on, so no crash loop.
    It waits with a plain log line, and starts on a token file the moment one appears.
    """
    import logging

    caplog.set_level(logging.INFO)
    present = {"file": False}
    db = DB()
    s, children = make(tmp_path, db, file_ok=lambda: present["file"])
    s.start()
    assert json.loads((tmp_path / "c.json").read_text())["connectors"] == [] and len(children) == 1
    assert "No car source yet" in caplog.text and "Settings › Cars and sources" in caplog.text
    assert s.tick() is False and len(children) == 1, "idle is a steady state"
    present["file"] = True  # make lucid-login wrote it; no bump comes from that
    assert s.tick() is True and len(children) == 2
    assert json.loads((tmp_path / "c.json").read_text())["connectors"][0]["type"] == "lucid"
    assert desired([], {}, "/tmp/t", [], file_ok=False)["connectors"] == "[]"


# ---- sealed secrets are bound to their rows


def real_vault():
    from cryptography.fernet import Fernet

    from supervise import _Vault

    return _Vault(Fernet.generate_key().decode())


def test_a_secret_copied_from_another_row_is_never_opened_by_root(tmp_path):
    """Anything that can write cc_sources could move a Tesla token into a Lucid or
    other-brand row; root would then decrypt it into a file the plugin user reads."""
    v = real_vault()
    tesla_blob = v.seal(json.dumps({"refresh_token": "rt-tesla-stolen"}), "tesla", 3)
    db = DB(rows=[{"id": 7, "config": {"pending": False}, "secret": tesla_blob, "enabled": True}, dict(cc_row("running", rid=9), secret=tesla_blob)])
    s, children, _, _, _ = brand(tmp_path, db, on_disk={"carconnectivity-connector-seatcupra"})
    s.vault = v
    s.start()
    assert s.running["from"] == "file" and s.running["token"] is None, "the Lucid row's blob was sealed for another row"
    assert blocks(tmp_path) == ["lucid"], "and so was the other brand's"
    assert "rt-tesla-stolen" not in (tmp_path / "c.json").read_text() and not (tmp_path / "lucid-token.json").exists()
    db.rows[0]["secret"] = v.seal(json.dumps({"refresh_token": "rt-own"}), "lucid", 7)
    s2, _ = make(tmp_path, db)
    s2.vault = v
    s2.start()
    assert s2.running["token"] == "rt-own", "sealed for this row: it opens"


class BindDB:
    """cc_state and cc_sources for binding: the advisory lock recorded, the rebind only where
    the row still holds what was read."""

    def __init__(self, rows, done=False):
        self.rows, self.state, self.locks = rows, ({"sealing": "bound-v1"} if done else {}), []

    def __call__(self):
        return self

    def cursor(self):
        return self

    def execute(self, sql, args=()):
        self.sql, self.args = " ".join(sql.split()).lower(), args
        if self.sql.startswith("select pg_advisory_xact_lock"):
            self.locks.append(args[0])
        if self.sql == "update cc_sources set secret = %s where id = %s and secret = %s":
            for r in self.rows:
                if r["id"] == args[1] and r["secret"] == args[2]:
                    r["secret"] = args[0]
        if self.sql.startswith("insert into cc_state"):
            self.state[args[0]] = args[1]

    def fetchone(self):
        return (self.state[self.args[0]],) if self.args and self.args[0] in self.state else None

    def fetchall(self):
        return [(r["id"], r["kind"], r["secret"]) for r in self.rows]

    def commit(self):
        pass

    def close(self):
        pass


def test_bare_secrets_are_bound_at_the_start_under_the_lock(tmp_path):
    v = real_vault()
    db = BindDB([{"id": 7, "kind": "lucid", "secret": v._f.encrypt(b'{"refresh_token": "rt-old"}')}])
    s, _ = make(tmp_path, db)
    s.vault = v
    assert s.bind_sealed() == 1 and db.state["sealing"] == "bound-v1" and db.locks == [__import__("sealing").LOCK_KEY]
    assert v.open(db.rows[0]["secret"], "lucid", 7) == '{"refresh_token": "rt-old"}' and v.open(db.rows[0]["secret"], "tesla", 7) is None
    assert s.bind_sealed() == 0 and len(db.locks) == 1, "done: not even the lock again"


def test_a_value_this_key_cannot_open_leaves_binding_unfinished_and_is_kept(tmp_path):
    from cryptography.fernet import Fernet

    v = real_vault()
    other = Fernet(Fernet.generate_key()).encrypt(b"theirs")
    db = BindDB([{"id": 7, "kind": "lucid", "secret": v._f.encrypt(b"rt")}, {"id": 8, "kind": "tesla", "secret": other}])
    s, _ = make(tmp_path, db)
    s.vault = v
    assert s.bind_sealed() == 1 and "sealing" not in db.state and db.rows[1]["secret"] == other


def test_a_bare_value_written_after_binding_is_bound_where_it_is_read(tmp_path):
    """A split deploy: the old site writes a Lucid token bare after this side set the flag."""
    v = real_vault()
    bare = v._f.encrypt(json.dumps({"refresh_token": "rt-from-old-site"}).encode())
    db = DB(rows=[{"id": 7, "config": {"pending": False}, "secret": bare, "enabled": True}])
    s, children = make(tmp_path, db)
    s.vault = v
    s.start()
    assert s.running["token"] == "rt-from-old-site", "it opens for the row it is in"
    rebinds = [u for u in db.updates if u[0] == "update cc_sources set secret = %s where id = %s and secret = %s"]
    assert len(rebinds) == 1 and rebinds[0][1][1:] == (7, bare), "rebound in place, and only if still the value read"
    assert v.open(s.rows[0]["secret"], "lucid", 7) == json.dumps({"refresh_token": "rt-from-old-site"}), "and is bound to it"


def test_the_copy_here_is_the_shared_one():
    assert (HERE / "sealing.py").read_bytes() == (HERE.parent / "shared" / "sealing.py").read_bytes()
    assert "sealing.py" in next(line for line in (HERE / "Dockerfile").read_text().splitlines() if line.startswith("COPY render_config.py")).split()


# ---- the database plugin's own role


def test_the_child_logs_in_as_the_plugin_role_and_other_brands_wait_for_it(tmp_path):
    tpl_text = '{"connectors": ${CC_CONNECTORS_JSON}, "db": "${PLUGIN_DB_USER}:${PLUGIN_DB_PASSWORD}"}'
    env = {"DB_USER": "carcommand", "DB_PASSWORD": "main-pw"}
    logins = [None, ("cc_plugin", "pw-new")]
    db = DB(rows=[cc_row("running")])
    s, children, _, clock, _ = brand(tmp_path, db, env=env, db_role=lambda: logins.pop(0), on_disk={"carconnectivity-connector-seatcupra"})
    (tmp_path / "t.json").write_text(tpl_text)
    s.start()
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert cfg["db"] == "carcommand:main-pw" and [b["type"] for b in cfg["connectors"]] == ["lucid"], "no role yet: the main login, and no other brand"
    s.tick()
    assert len(children) == 1, "not retried before five minutes"
    clock.t += 301
    assert s.tick() is True
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert cfg["db"] == "cc_plugin:pw-new" and [b.get("connector_id") or b["type"] for b in cfg["connectors"]] == ["lucid", "cupra"]
    assert "pw-new" not in json.dumps(__import__("privsep").child_env(os.environ)), "the password reaches the child only through its config"


# ---- the site's catalogue and this image's at different releases


def test_a_working_brand_at_another_release_is_upgraded_with_its_sign_in_kept(tmp_path):
    db = DB(rows=[cc_row("running", version="0.6.3")])
    s, children, asked, _, _ = brand(tmp_path, db, on_disk={"carconnectivity-connector-seatcupra"})
    s.start()
    assert blocks(tmp_path) == ["lucid"], "not this image's release: not started as it stands"
    assert s.tick() is True
    row = db.rows[0]
    assert row["config"]["version"] == "0.6.4" and row["config"]["state"] == "ready" and row["secret"] is not None and row["last_error"] is None
    assert asked["install"] == ["carconnectivity-connector-seatcupra"] and blocks(tmp_path) == ["lucid", "cupra"]


def test_uninstall_goes_by_package_name_and_never_touches_the_image():
    import subprocess as sp

    import plugins

    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return sp.CompletedProcess(cmd, 0, "", "")

    old = {**CUPRA, "version": "0.6.1"}
    assert plugins.uninstall(old, {}, run=run, have={"carconnectivity-connector-seatcupra": ("0.6.1", True)}) is None
    assert calls and calls[-1][-1] == "carconnectivity-connector-seatcupra", "an older release of a catalogued package still comes out"
    calls.clear()
    plugins.uninstall(old, {}, run=run, have={"carconnectivity-connector-seatcupra": ("0.6.1", False)})
    plugins.uninstall({**CUPRA, "package": "requests"}, {}, run=run, have={"requests": ("2.34.2", True)})
    assert calls == [], "the image's copy, and anything the catalogue does not name, stay"


# ---- the start count lives on the plugin user's volume


def test_the_start_count_file_cannot_hang_or_fool_the_supervisor(tmp_path):
    db = DB(rows=[cc_row("running")])
    fifo = tmp_path / "starts.json"
    os.mkfifo(fifo)
    s, _, _, _, _ = brand(tmp_path, db, on_disk={"carconnectivity-connector-seatcupra"})
    assert s._deaths() == 0, "a FIFO with no writer: read as nothing, without blocking"
    s._set_deaths(1)  # and never written through
    fifo.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text('{"quick_deaths": 5}')
    fifo.symlink_to(target)
    assert s._deaths() == 0, "a link is not followed"
    s._set_deaths(1)
    assert target.read_text() == '{"quick_deaths": 5}', "nor written through"
    fifo.unlink()
    fifo.write_text('{"quick_deaths": 1}' + " " * 1000)
    assert s._deaths() == 0, "too big to be ours"
    fifo.write_text('{"quick_deaths": 1}')
    assert s._deaths() == 1


@pytest.mark.skipif(not os.environ.get("CC_TEST_PG_URL"), reason="needs a scratch Postgres: CC_TEST_PG_URL")
def test_against_postgres_binding_waits_for_the_other_side_and_never_writes_back_a_stale_value(tmp_path):
    """Two connections, as the site and the connector at the same start."""
    import threading
    import time

    import psycopg2

    import sealing

    url = os.environ["CC_TEST_PG_URL"]
    v = real_vault()
    setup = psycopg2.connect(url)
    setup.autocommit = True
    cur = setup.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS cc_sources (id BIGSERIAL PRIMARY KEY, kind TEXT, config JSONB DEFAULT '{}', secret BYTEA, enabled BOOLEAN DEFAULT TRUE)")
    cur.execute("CREATE TABLE IF NOT EXISTS cc_state (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("DELETE FROM cc_state WHERE key = %s", (sealing.DONE_KEY,))
    cur.execute("INSERT INTO cc_sources (kind, secret) VALUES ('tesla', %s) RETURNING id", (v._f.encrypt(b"rt-old"),))
    sid = cur.fetchone()[0]

    # The site holds the lock (it is binding); the connector's binding must wait, then see it done.
    site = psycopg2.connect(url)
    sc = site.cursor()
    sc.execute("SELECT pg_advisory_xact_lock(%s)", (sealing.LOCK_KEY,))
    s, _ = make(tmp_path, lambda: psycopg2.connect(url))
    s.vault = v
    got = {}
    t = threading.Thread(target=lambda: got.update(n=s.bind_sealed()))
    t.start()
    time.sleep(1.0)
    assert t.is_alive(), "the connector waits for the site's lock"
    # Meanwhile the site rotates the token (single-use) and finishes binding.
    fresh = v.seal("rt-rotated", "tesla", sid)
    sc.execute("UPDATE cc_sources SET secret = %s WHERE id = %s", (fresh, sid))
    sc.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (sealing.DONE_KEY, sealing.DONE_VALUE))
    site.commit()
    t.join(10)
    assert got == {"n": 0}, "it looked again inside the lock and found the work done"
    cur.execute("SELECT secret FROM cc_sources WHERE id = %s", (sid,))
    assert v.open(bytes(cur.fetchone()[0]), "tesla", sid) == "rt-rotated", "the rotated token stands"

    # A rebind computed from a value that has since changed writes nothing.
    stale_old = v._f.encrypt(b"rt-older")
    cur.execute(sealing.REBIND_SQL, (v.seal("rt-older", "tesla", sid), sid, stale_old))
    assert cur.rowcount == 0
    cur.execute("SELECT secret FROM cc_sources WHERE id = %s", (sid,))
    assert v.open(bytes(cur.fetchone()[0]), "tesla", sid) == "rt-rotated"
    site.close()
    setup.close()


def test_the_start_count_is_the_supervisors_alone(tmp_path):
    db = DB(rows=[cc_row("running")])
    s, _, _, _, _ = brand(tmp_path, db, on_disk={"carconnectivity-connector-seatcupra"})
    path = tmp_path / "starts.json"
    path.write_text('{"quick_deaths": 0}')
    os.chmod(path, 0o644)  # as an older supervisor left it
    s._set_deaths(2)
    assert s._deaths() == 2 and oct(path.stat().st_mode & 0o777) == "0o600"


def test_a_refused_lucid_sign_in_is_said_in_words_not_by_class_name():
    from supervise import lucid_failure

    class APIError(Exception):
        pass

    assert "APIError" not in lucid_failure(APIError("bad password for owner@example.com")) and "owner@" not in lucid_failure(APIError("owner@example.com"))
    assert lucid_failure(APIError()).startswith("Lucid did not accept the sign-in")
    assert lucid_failure(ConnectionError()).startswith("Lucid could not be reached")
