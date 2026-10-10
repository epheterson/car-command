"""A password inside an address never reaches a log line in the connector container."""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from logscrub import ScrubFilter, scrub  # noqa: E402


def test_scrub_masks_only_the_password():
    assert scrub("postgresql://u:secret@h/db") == "postgresql://u:***@h/db"
    got = scrub("{'db_url': 'postgresql://cc:example-pw1@carcommand-db:5432/cc', 'log_level': 'info'}")
    assert "example-pw1" not in got and "postgresql://cc:***@carcommand-db:5432/cc'" in got
    assert scrub("https://example.com/a:b@c") == "https://example.com/a:b@c", "a colon in the path is not a password"
    assert scrub("http://maps:8090/reverse?lat=1") == "http://maps:8090/reverse?lat=1", "a port is not a password"
    assert scrub("redis://:only-pw@h:6379") == "redis://:***@h:6379"


def test_the_filter_cleans_message_args_and_traceback():
    f = ScrubFilter()
    r = logging.LogRecord("carconnectivity.plugins.database", logging.INFO, __file__, 1, "Loading database plugin with config %s", ({"db_url": "postgresql://u:secret@h/db"},), None)
    assert f.filter(r) and "secret" not in r.getMessage() and "postgresql://u:***@h/db" in r.getMessage()
    try:
        raise RuntimeError("could not connect to postgresql://u:secret@h/db")
    except RuntimeError:
        r = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    f.filter(r)
    assert "secret" not in logging.Formatter().format(r) and "***" in r.exc_text


def test_every_record_in_a_container_process_is_scrubbed_whatever_handler_writes_it(tmp_path):
    """A fresh interpreter, as the supervisor starts CarConnectivity: sitecustomize runs first,
    the program adds its own handlers afterwards (logging.basicConfig), as CarConnectivity does."""
    probe = (
        "import logging, sys\n"
        "logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(name)s:%(levelname)s:%(message)s')\n"
        "log = logging.getLogger('carconnectivity.plugins.database')\n"
        "log.info('Loading database plugin with config %s', {'db_url': 'postgresql://u:secret@h/db'})\n"
        "log.warning('connect to %s failed', 'postgresql://u:secret@h/db')\n"
        "try:\n"
        "    raise OSError('postgresql://u:secret@h/db refused')\n"
        "except OSError:\n"
        "    logging.getLogger('psycopg').exception('lost the database')\n"
        "logging.getLogger().addHandler(logging.FileHandler(sys.argv[1]))\n"
        "logging.getLogger('late').error('and %s', 'postgresql://u:secret@h/db')\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "CC_GEOCODER_URL"}
    env["PYTHONPATH"] = str(HERE)
    late = tmp_path / "late.log"
    done = subprocess.run([sys.executable, "-c", probe, str(late)], capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    out = done.stdout + done.stderr + late.read_text()
    assert "secret" not in out
    assert out.count("postgresql://u:***@h/db") >= 5 and "lost the database" in out and "Traceback" in out


def test_the_database_plugin_logs_warnings_only():
    tpl = (HERE.parent / "config" / "carconnectivity.json.template").read_text()
    plugins = json.loads(tpl.replace("${CC_CONNECTORS_JSON}", "[]"))["carConnectivity"]["plugins"]
    assert next(p for p in plugins if p["type"] == "database")["config"]["log_level"] == "warning"
    assert "logscrub.install()" in (HERE / "sitecustomize.py").read_text() and "logscrub.py" in (HERE / "Dockerfile").read_text()
