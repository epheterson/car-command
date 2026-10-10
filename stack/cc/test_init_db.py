"""init_db creates the plugin's schema, or explains itself and exits non-zero."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent / "init_db.py"


def run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, check=False)


def test_without_a_database_url_it_says_so():
    r = run({})
    assert r.returncode == 2
    assert "DATABASE_URL is not set" in r.stderr


def test_a_missing_plugin_is_reported_rather_than_traced():
    # Run outside the connector image, where the plugin is not installed. The operator
    # needs a sentence, not a stack trace from a container that then exits anyway.
    r = run({"DATABASE_URL": "postgresql://u:p@nowhere:5432/x", "PATH": "/usr/bin:/bin"})
    assert r.returncode == 3
    assert "init_db:" in r.stderr


def test_the_password_never_reaches_the_output():
    secret = "hunter2-should-not-appear"
    r = run({"DATABASE_URL": f"postgresql://u:{secret}@nowhere:5432/x", "PATH": "/usr/bin:/bin"})
    assert secret not in r.stderr and secret not in r.stdout


def test_it_rewrites_the_scheme_for_sqlalchemy():
    """The stack speaks psycopg; SQLAlchemy needs the driver named in the URL."""
    source = SCRIPT.read_text()
    assert 'url.replace("postgresql://", "postgresql+psycopg2://", 1)' in source


def test_the_dockerfile_copies_it():
    dockerfile = (SCRIPT.parent / "Dockerfile").read_text()
    assert "init_db.py" in dockerfile, "the schema step would not exist in the image"
