"""render_config turns a ${VAR} template into a config file, or refuses."""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE / "render_config.py"


def run(template: str, out: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), template, str(out)], env=env, capture_output=True, text=True, check=False)


def test_renders_every_placeholder(tmp_path: Path) -> None:
    tpl = tmp_path / "t.json"
    tpl.write_text('{"db": "postgresql://${DB_USER}:${DB_PASSWORD}@carcommand-db/${DB_NAME}"}')
    out = tmp_path / "out.json"
    env = {"DB_USER": "u", "DB_PASSWORD": "p@ss", "DB_NAME": "carcommand"}
    result = run(str(tpl), out, env)
    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text()) == {"db": "postgresql://u:p@ss@carcommand-db/carcommand"}


def test_missing_variable_fails_and_writes_nothing(tmp_path: Path) -> None:
    tpl = tmp_path / "t.json"
    tpl.write_text('{"user": "${WEBUI_USER}"}')
    out = tmp_path / "out.json"
    result = run(str(tpl), out, env={})
    assert result.returncode == 2
    assert "WEBUI_USER" in result.stderr
    assert not out.exists()


def test_output_is_valid_json_or_fails(tmp_path: Path) -> None:
    tpl = tmp_path / "t.json"
    tpl.write_text('{"broken": ${X}')  # renders to invalid JSON
    out = tmp_path / "out.json"
    result = run(str(tpl), out, env={"X": "1"})
    assert result.returncode == 3
    assert not out.exists()


def test_never_echoes_values(tmp_path: Path) -> None:
    tpl = tmp_path / "t.json"
    tpl.write_text('{"a": "${SECRET}"}')
    out = tmp_path / "out.json"
    result = run(str(tpl), out, env={"SECRET": "hunter2"})
    assert "hunter2" not in result.stdout + result.stderr


def test_a_default_style_placeholder_is_refused_with_an_explanation(tmp_path: Path) -> None:
    """`${VAR:-default}` is docker compose syntax, not string.Template's.

    The two sit two lines apart in this stack's compose file and template, so writing one
    here is an easy mistake. It used to surface as an uncaught ValueError traceback inside
    a container that then restarted forever, which says nothing about the cause.
    """
    tpl = tmp_path / "t.json"
    tpl.write_text('{"db": "postgresql://u:p@${DB_HOST:-carcommand-db}/x"}')
    out = tmp_path / "out.json"
    result = run(str(tpl), out, {"DB_HOST": "somewhere"})
    assert result.returncode == 4, result.stderr
    assert "there is no default syntax here" in result.stderr
    assert not out.exists(), "a refused render must not leave a half-written config"
    assert "somewhere" not in result.stderr, "values are never printed"


def test_the_entrypoint_does_not_hide_repeated_logs() -> None:
    """--hide-repeated-log breaks more than logging.

    carconnectivity's DuplicateFilter does self._last_log[module][levelno] before checking
    that levelno is there, so the first message a module logs at a new level raises
    KeyError inside logging.Handler.handle. The exception unwinds into whatever was
    logging. In practice that was the database plugin's trip agent, which logs
    "Starting new trip" as its first INFO after a WARNING: every drive died on its own
    log line and no trip was ever written.
    """
    # CarConnectivity is started by the supervisor now (privsep.spawn); the rule moved with it.
    supervise = (SCRIPT.parent / "supervise.py").read_text() + (SCRIPT.parent / "privsep.py").read_text()
    code = "\n".join(line for line in supervise.splitlines() if not line.lstrip().startswith("#"))
    assert "--hide-repeated-log" not in code
    assert 'subprocess.Popen(["carconnectivity", config_path]' in code


def test_compose_supplies_every_placeholder_the_template_needs():
    """A placeholder with nothing behind it is a container that never starts.

    render_config refuses `${VAR:-default}` on purpose, so the defaults have to live in
    the compose file instead. That puts the two halves in different files, which is
    exactly the arrangement where one gets edited and the other does not: the template
    grows a placeholder, nothing supplies it, and the connector restarts forever with
    the reason on line one of a log nobody is reading yet.
    """
    import re

    root = HERE.parent
    template = (root / "config" / "carconnectivity.json.template").read_text()
    compose = (root / "docker-compose.yml").read_text()

    # The connector's own environment block, up to the next key at that indentation.
    cc = compose[compose.index("  cc:") :]
    env = cc[cc.index("    environment:") :]
    env = env[: env.index("\n    volumes:")]
    supplied = set(re.findall(r"^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)=", env, re.M))

    needed = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", template))
    # The database plugin's own login is made at each start by the supervisor (plugin_role.py), not compose.
    by_supervisor = {"PLUGIN_DB_USER", "PLUGIN_DB_PASSWORD"}
    assert all(n in (HERE / "supervise.py").read_text() for n in by_supervisor)
    supplied |= by_supervisor
    assert needed <= supplied, f"the template needs {sorted(needed - supplied)}, which the cc service does not set"


def test_the_driving_interval_is_one_the_connector_will_accept():
    """The Lucid connector refuses anything under 15 seconds and raises on start, so a
    smaller default here is not a slower poll — it is a container that restart-loops
    with the whole reason on one line of a log nobody is tailing yet. Learned by doing
    it: 10 took the connector down for two minutes."""
    import re

    compose = (HERE.parent / "docker-compose.yml").read_text()
    defaults = re.findall(r"CC_POLL_DRIVING_S=\$\{CC_POLL_DRIVING_S:-(\d+)\}", compose)
    assert defaults, "no default for the driving poll interval"
    for value in defaults:
        assert int(value) >= 15, f"the connector refuses a driving interval of {value}s; its floor is 15"


def test_the_entrypoint_default_connectors_block_is_lucid_at_the_environment_rates(tmp_path: Path) -> None:
    """CC_CONNECTORS_JSON empty means the Lucid block, with the polling rates the environment
    sets, so an install that names no connector behaves exactly as before the block moved
    out of the template. Any other brand goes in through the variable."""
    # The default moved from the entrypoint's shell into the supervisor, which renders it.
    import sys

    sys.path.insert(0, str(SCRIPT.parent))
    from supervise import desired

    block = json.loads(desired([], {"CC_POLL_IDLE_S": "60", "CC_POLL_DRIVING_S": "15"}, "/tmp/t")["connectors"])
    assert block[0]["type"] == "lucid" and block[0]["config"]["interval"] == 60 and block[0]["config"]["driving_interval"] == 15
    got = desired([], {"CC_CONNECTORS_JSON": '[{"type":"volkswagen","config":{"username":"u"}}]'}, "/tmp/t")["connectors"]
    assert json.loads(got)[0]["type"] == "volkswagen", "a block from the environment is used as given"
    template = (SCRIPT.parent.parent / "config" / "carconnectivity.json.template").read_text()
    assert '"connectors": ${CC_CONNECTORS_JSON}' in template and '"type": "lucid"' not in template
