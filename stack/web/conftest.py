"""Every dependency in requirements.txt must be installed before a single test runs.

The tests used to reach for pytest.importorskip, which meant a missing psycopg quietly
skipped every test that imports app.py — on this machine, all of them, for days. A test
that skips itself when the thing it needs is absent is not a test; it is a green tick.
"""

import importlib
from pathlib import Path

import pytest

# distribution name in requirements.txt -> the module it actually provides
MODULES = {
    "fastapi": "fastapi",
    "python-multipart": "python_multipart",
    "uvicorn": "uvicorn",
    "jinja2": "jinja2",
    "psycopg": "psycopg",
    "psycopg-pool": "psycopg_pool",
    "httpx": "httpx",
    "cryptography": "cryptography",
    "pyzmq": "zmq",
}


def requirements() -> list[str]:
    lines = (Path(__file__).parent / "requirements.txt").read_text().splitlines()
    return [line.split("==")[0].split("[")[0].strip() for line in lines if line.strip() and not line.startswith("#")]


def pytest_collection_modifyitems(session, config, items):
    missing = [name for name in requirements() if not _importable(MODULES.get(name, name))]
    if missing:
        raise pytest.UsageError(f"requirements.txt is not installed here, so these tests would lie about passing: {', '.join(missing)}. Run: pip install -r requirements.txt")


def _importable(module: str) -> bool:
    try:
        importlib.import_module(module)
    except ImportError:
        return False
    return True


def pytest_report_header():
    """The versions under test, in the header, because they are not always the pinned ones."""
    import fastapi
    import starlette

    return f"fastapi {fastapi.__version__}, starlette {starlette.__version__} (requirements.txt pins {_pinned('fastapi')})"


def _pinned(name: str) -> str:
    for line in (Path(__file__).parent / "requirements.txt").read_text().splitlines():
        if line.split("==")[0].split("[")[0].strip() == name:
            return line.strip()
    return "?"


# The demo's three-period plan (demo.py), for tests that price charges.
from demo import RATES as SPRINGFIELD_RATES, SCHEDULE as SPRINGFIELD_SCHEDULE  # noqa: E402


@pytest.fixture
def springfield_plan():
    """The plan above in force for one test, and no plan again after it."""
    import cost

    plan = cost.compile_rows("Springfield EV", SPRINGFIELD_RATES, SPRINGFIELD_SCHEDULE)
    cost.use_plan(plan)
    yield plan
    cost.use_plan(None)


@pytest.fixture(autouse=True)
def _no_alert_log_database(monkeypatch):
    """Tests have no database: the sent log of every alert a request raises (a sign-in, a
    connect) would wait out the pool on each one. A test of the log gives its Recorder one."""
    import sys

    app = sys.modules.get("app")
    if app is not None and hasattr(app, "recorder"):
        monkeypatch.setattr(app.recorder, "log_connect", None)
