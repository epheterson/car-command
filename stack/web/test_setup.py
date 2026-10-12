"""scripts/setup.sh is the first thing a stranger runs, so it gets tested like code.

The three things that matter: it never damages an existing install, every generated
secret is its own secret, and the file it writes is not readable by the rest of the box.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "setup.sh"
EXAMPLE = REPO / "stack" / ".env.example"


@pytest.fixture
def install(tmp_path):
    """A checkout with only the two files the script needs."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "stack").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "setup.sh")
    shutil.copy(EXAMPLE, tmp_path / "stack" / ".env.example")
    os.chmod(tmp_path / "scripts" / "setup.sh", 0o755)
    return tmp_path


def run(install, expect=0):
    r = subprocess.run([str(install / "scripts" / "setup.sh")], capture_output=True, text=True)
    assert r.returncode == expect, r.stderr
    return r


def env_values(install) -> dict[str, str]:
    out = {}
    for line in (install / "stack" / ".env").read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_it_writes_an_env_and_a_secrets_directory(install):
    run(install)
    assert (install / "stack" / ".env").is_file()
    assert (install / "stack" / "secrets").is_dir()


def test_no_placeholder_survives(install):
    run(install)
    assert "CHANGE_ME" not in (install / "stack" / ".env").read_text()


def test_every_secret_is_its_own(install):
    # One value shared by the database, the web UI and the REST API would make three
    # doors into one: whoever gets the REST password gets the database.
    run(install)
    vals = env_values(install)
    secrets = [vals[k] for k in ("DB_PASSWORD", "WEBUI_PASSWORD", "REST_PASSWORD")]
    assert len(set(secrets)) == len(secrets), "a generated password was reused"
    assert all(len(s) >= 32 for s in secrets), secrets


def test_two_installs_do_not_share_a_password(install, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    (other / "scripts").mkdir()
    (other / "stack").mkdir()
    shutil.copy(SCRIPT, other / "scripts" / "setup.sh")
    shutil.copy(EXAMPLE, other / "stack" / ".env.example")
    os.chmod(other / "scripts" / "setup.sh", 0o755)
    run(install)
    run(other)
    assert env_values(install)["DB_PASSWORD"] != env_values(other)["DB_PASSWORD"]


def test_the_env_file_is_not_readable_by_anyone_else(install):
    run(install)
    mode = stat.S_IMODE((install / "stack" / ".env").stat().st_mode)
    assert mode == 0o600, oct(mode)
    assert stat.S_IMODE((install / "stack" / "secrets").stat().st_mode) == 0o700


def test_running_it_twice_leaves_the_first_env_alone(install):
    # The one that would actually hurt: .env holds the password to the database volume,
    # so a regenerated one means an unreadable database and a confused operator.
    run(install)
    before = (install / "stack" / ".env").read_text()
    r = run(install)
    assert (install / "stack" / ".env").read_text() == before
    assert "already exists" in r.stdout


def test_it_says_what_to_do_next(install):
    r = run(install)
    assert "docker compose" in r.stdout and "3044" in r.stdout


def test_the_example_carries_no_real_secret():
    text = EXAMPLE.read_text()
    for line in text.splitlines():
        if line.startswith(("DB_PASSWORD", "WEBUI_PASSWORD", "REST_PASSWORD")):
            assert line.endswith("=CHANGE_ME"), f"a real value may have been committed: {line.split('=')[0]}"


def test_the_make_target_exists():
    makefile = (REPO / "Makefile").read_text()
    assert "\nsetup:" in makefile and "scripts/setup.sh" in makefile


def test_the_deploy_targets_protect_host_only_files_from_delete():
    """--delete plus a narrow exclude is how a host-only file disappears. It can cost a .env
    backup: --exclude .env spares a file named exactly that and nothing
    else, so the backup beside it was removed on the next deploy."""
    makefile = (REPO / "Makefile").read_text()
    assert "--delete" in makefile
    # `.env*` does not match `.staging.env`, which is exactly how the staging env file
    # was deleted by the deploy that was supposed to leave host-only files alone.
    for pattern in ("P .env*", "P *.env", "P secrets/***", "P docker-compose.override.yml", "P backups/***"):
        assert f"--filter='{pattern}'" in makefile, f"nothing protects {pattern} from --delete"
    # Both targets have to ship the same things by the same rules, or one of them is
    # still a trap: `web` once synced only stack/, and --delete removed scripts/ from the
    # host on every site deploy.
    body = makefile[makefile.index("\ndeploy:") :]
    assert body.count("$(call DEPLOY") >= 2, "deploy and web must share one definition"
    assert "rsync -a scripts/" in makefile, "the host never gets backup.sh, restore.sh or staging.sh"


def test_the_pinned_subnet_can_be_overridden():
    compose = (REPO / "stack" / "docker-compose.yml").read_text()
    assert "${CC_SUBNET:-" in compose, "a hard-pinned subnet collides on somebody else's host"
