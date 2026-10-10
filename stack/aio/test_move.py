"""scripts/move-to-one-container.sh against a stand-in docker: what it refuses, what it says when
a step fails, and that it leaves compose down (not stopped) and its database volume alone."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

FAKE_DOCKER = r"""#!/bin/sh
echo "$*" >> "$CALLS"
case "$*" in
  "image inspect"*) exit 0 ;;
  "volume inspect"*) [ -n "$FAKE_VOLUME_EXISTS" ] && exit 0; exit 1 ;;
  "compose"*" ps "*) echo db; exit 0 ;;
  "compose"*) exit 0 ;;
  "run"*" restore") cat >/dev/null; exit "${FAKE_RESTORE_CODE:-0}" ;;
  "run"*) cat >/dev/null; exit 0 ;;
esac
exit 0
"""


@pytest.fixture
def checkout(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "move-to-one-container.sh", tmp_path / "scripts")
    (tmp_path / "scripts" / "backup.sh").write_text('#!/bin/sh\nmkdir -p "$1" && echo dump > "$1/carcommand-1.dump"\n')
    (tmp_path / "stack").mkdir()
    (tmp_path / "stack" / "docker-compose.yml").write_text("services: {}\n")
    key = "A" * 43 + "="
    (tmp_path / "stack" / ".env").write_text(
        f"DB_USER=carcommand\nDB_PASSWORD=dbpw\nDB_NAME=carcommand\nCC_SECRET_KEY={key}\nDISCORD_WEBHOOK_URL=https://example.invalid/h\nMAPS_URL=http://maps:8090\n"
    )
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text(FAKE_DOCKER)
    for f in (bin_ / "docker", tmp_path / "scripts" / "backup.sh", tmp_path / "scripts" / "move-to-one-container.sh"):
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def move(checkout, **env):
    calls = checkout / "calls"
    calls.write_text("")
    e = {**os.environ, "PATH": f"{checkout / 'bin'}:{os.environ['PATH']}", "CALLS": str(calls), **env}
    done = subprocess.run(["bash", str(checkout / "scripts" / "move-to-one-container.sh"), "img:test", "vol-test"], env=e, capture_output=True, text=True)
    return done, calls.read_text().splitlines()


def test_an_existing_volume_is_refused_before_anything_changes(checkout):
    done, calls = move(checkout, FAKE_VOLUME_EXISTS="1")
    assert done.returncode != 0 and "exists already" in done.stderr
    assert not any(" stop " in c or c.endswith(" down") or c.startswith("run") for c in calls)
    assert "up -d" not in done.stderr, "nothing was changed, so nothing to put back"


def test_a_failed_restore_says_how_to_put_compose_back_and_remove_the_half_made_volume(checkout):
    done, calls = move(checkout, FAKE_RESTORE_CODE="1")
    assert done.returncode != 0
    assert "docker compose -f stack/docker-compose.yml up -d" in done.stderr
    assert "docker volume rm vol-test" in done.stderr
    assert not any(c.endswith(" down") for c in calls), "compose is only taken down after the data is safely across"


def test_a_move_takes_compose_down_but_never_its_volume(checkout):
    done, calls = move(checkout)
    assert done.returncode == 0, done.stderr
    downs = [c for c in calls if c.startswith("compose") and " down" in c]
    assert downs and not any("-v" in c.split() for c in downs)
    assert any(c.startswith("compose") and c.endswith("stop web cc") for c in calls), "nothing written after the dump"
    assert "--stop-timeout 30" in done.stdout and "rm -f" not in done.stdout
    settings = (checkout / "stack" / "secrets" / "one-container.env").read_text()
    assert "DISCORD_WEBHOOK_URL=" in settings
    for kept_back in ("DB_PASSWORD", "CC_SECRET_KEY", "MAPS_URL"):
        assert kept_back not in settings
