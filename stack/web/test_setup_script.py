"""make setup ends by saying where to open the site and how to get the setup code."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def checkout(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "stack").mkdir()
    shutil.copy(ROOT / "scripts" / "setup.sh", tmp_path / "scripts" / "setup.sh")
    shutil.copy(ROOT / "stack" / ".env.example", tmp_path / "stack" / ".env.example")
    (tmp_path / "bin").mkdir()
    return tmp_path


def run(checkout, docker_prints=None):
    env = {**os.environ, "PATH": f"{checkout / 'bin'}:/usr/bin:/bin"}
    if docker_prints is not None:
        shim = checkout / "bin" / "docker"
        shim.write_text(f"#!/bin/sh\n{docker_prints}\n")
        shim.chmod(0o755)
    return subprocess.run(["bash", str(checkout / "scripts" / "setup.sh")], capture_output=True, text=True, env=env, cwd=checkout, timeout=60)


def test_before_the_site_has_run_it_says_how_to_get_the_code(checkout):
    r = run(checkout)
    assert r.returncode == 0 and "Open http://localhost:3044 and, if asked for a setup code, get it with:" in r.stdout
    assert "docker compose -f stack/docker-compose.yml exec web python3 setup_code.py" in r.stdout


def test_once_the_site_runs_it_prints_the_code(checkout):
    r = run(checkout, docker_prints="echo ABCD-EFG2-JK34")
    assert r.returncode == 0 and "Open http://localhost:3044 and, if asked, use setup code ABCD-EFG2-JK34" in r.stdout


def test_a_published_port_from_the_env_is_the_one_named(checkout):
    (checkout / "stack" / ".env").write_text("CC_PORT=127.0.0.1:3055\n")
    r = run(checkout, docker_prints="echo ABCD-EFG2-JK34")
    assert "Open http://localhost:3055 and, if asked, use setup code ABCD-EFG2-JK34" in r.stdout, "also when .env already exists"
