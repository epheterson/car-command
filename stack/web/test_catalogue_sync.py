"""A deploy leaves the site and the connector on the same connectors.json."""

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FAKE_DOCKER = """#!/bin/sh
# exec NAME cat /app/connectors.json | exec NAME true | compose up -d --build --no-deps SVC
if [ "$1" = exec ]; then
    f="$FAKE/$2.json"
    [ -f "$f" ] || exit 1
    [ "$3" = cat ] && cat "$f"
    exit 0
fi
echo "$*" >> "$FAKE/compose.log"
"""


def host(tmp_path, cc: str | None, web: str | None) -> tuple[Path, dict]:
    dest = tmp_path / "dest"
    (dest / "shared").mkdir(parents=True)
    (dest / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "catalogue-sync.sh", dest / "scripts" / "catalogue-sync.sh")
    (dest / "shared" / "connectors.json").write_text('{"v": 2}\n')
    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    (fake / "bin" / "docker").write_text(FAKE_DOCKER)
    (fake / "bin" / "docker").chmod(0o755)
    for name, text in (("carcommand-cc", cc), ("carcommand-web", web)):
        if text is not None:
            (fake / f"{name}.json").write_text(text)
    return dest, {"PATH": f"{fake / 'bin'}:/usr/bin:/bin", "FAKE": str(fake)}


def run(dest, env, *args):
    return subprocess.run(["/bin/sh", str(dest / "scripts" / "catalogue-sync.sh"), *args], capture_output=True, text=True, env=env, timeout=30)


def test_only_the_container_on_another_catalogue_is_rebuilt(tmp_path):
    dest, env = host(tmp_path, cc='{"v": 1}\n', web='{"v": 2}\n')
    assert run(dest, env, "--check").returncode == 1
    done = run(dest, env)
    assert done.returncode == 0, done.stderr
    assert (Path(env["FAKE"]) / "compose.log").read_text() == "compose up -d --build --no-deps cc\n"


def test_agreeing_or_stopped_containers_are_left_alone(tmp_path):
    dest, env = host(tmp_path, cc='{"v": 2}\n', web=None)
    done = run(dest, env)
    assert done.returncode == 0 and "agree" in done.stdout and not (Path(env["FAKE"]) / "compose.log").exists()


def test_both_deploy_targets_keep_them_in_step():
    make = (ROOT / "Makefile").read_text()
    for target in ("deploy:", "web:"):
        body = make[make.index(f"\n{target}") :]
        body = body[: body.index("\n\n")]
        assert "./scripts/catalogue-sync.sh" in body, target
    web = make[make.index("\nweb:") :]
    assert "cmp -s shared/connectors.json cc/connectors.json" in web[: web.index("\n\n")], "make web ships the connector's files when the catalogue changed"
    assert os.access(ROOT / "scripts" / "catalogue-sync.sh", os.X_OK)
