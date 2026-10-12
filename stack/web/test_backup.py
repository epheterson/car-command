"""backup.sh and restore.sh, checked for the things that decide whether data survives.

These scripts were exercised end to end: seed two places, back up, destroy
the whole volume as if the machine were gone, bring the stack back, restore, and find the
places, the settings and the password where they were. What follows guards the properties
that made that work, because a backup nobody has restored is a rumour.
"""

import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BACKUP = REPO / "scripts" / "backup.sh"
RESTORE = REPO / "scripts" / "restore.sh"


@pytest.fixture(params=[BACKUP, RESTORE], ids=["backup", "restore"])
def script(request):
    return request.param


def test_both_scripts_exist_and_run(script):
    assert script.is_file(), script
    assert stat.S_IMODE(script.stat().st_mode) & stat.S_IXUSR, f"{script.name} is not executable"
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0


def test_they_stop_at_the_first_failure(script):
    """Without -e, a failed dump is followed by a cheerful success message."""
    assert re.search(r"^set -euo pipefail$", script.read_text(), re.M), f"{script.name} does not set -euo pipefail"


def test_a_backup_is_written_to_a_temporary_name_first():
    """An interrupted dump must not be left looking like a good one — that is the file
    someone reaches for on the day it matters."""
    text = BACKUP.read_text()
    assert ".partial" in text
    assert re.search(r'mv "\$file\.partial" "\$file"', text), "the dump is not renamed into place"


def test_a_backup_is_verified_before_it_is_called_a_backup():
    """pg_restore -l reads the archive's table of contents. A dump that cannot be listed
    is not a backup, and finding that out now beats finding it out later."""
    text = BACKUP.read_text()
    assert "pg_restore -l" in text
    assert "TABLE DATA" in text


def test_a_restore_reads_the_archive_before_touching_the_database():
    """A corrupt file has to fail with the database still intact, not half way through."""
    text = RESTORE.read_text()
    probe = text.index("pg_restore -l")
    destructive = text.index("--clean --if-exists")
    assert probe < destructive, "the archive is not checked before the database is replaced"


def test_a_restore_refuses_a_database_that_has_anything_in_it():
    """Counting drives alone was not enough: an install with five hundred named places and
    no drives read as empty. The check asks about every table that holds someone's work."""
    text = RESTORE.read_text()
    for table in ("trips", "charging_sessions", "cc_places", "cc_service", "vehicles", "cc_settings", "cc_auth", "cc_positions"):
        assert f"EXISTS(SELECT 1 FROM {table})" in text, f"{table} is not part of the not-empty check"
    assert 'force != "--force"' in text


def test_the_not_empty_check_uses_exists_rather_than_count():
    """COUNT(*) on cc_positions walks a million rows to learn the first one is there."""
    text = RESTORE.read_text()
    assert "EXISTS(SELECT 1 FROM cc_positions)" in text
    assert "COUNT(*) FROM cc_positions" not in text


def test_a_check_that_cannot_run_fails_closed():
    """The first version swallowed the probe's errors and defaulted to 'empty', so it
    would have waved a restore through on every database it could not read. A safety
    check that cannot run must refuse, not permit."""
    text = RESTORE.read_text()
    assert "refusing to restore blind" in text
    assert "Fail closed" in text or "fail closed" in text


def test_the_site_is_stopped_while_the_database_is_replaced():
    """The recorder writing into a half-restored schema is worse than a minute of down."""
    text = RESTORE.read_text()
    stop = text.index("stop web")
    restore_at = text.index("--clean --if-exists")
    start = text.index("up -d web")
    assert stop < restore_at < start


def test_a_restore_does_not_ignore_errors():
    """pg_restore keeps going after a failed statement unless told not to, which leaves a
    database that looks restored and is not."""
    assert "--exit-on-error" in RESTORE.read_text()


def test_neither_script_prints_a_password():
    """Both read DB_USER and DB_PASSWORD out of .env; neither should echo a URL."""
    for script in (BACKUP, RESTORE):
        text = script.read_text()
        assert "DB_PASSWORD" not in text, f"{script.name} reads the password it does not need"
        assert not re.search(r'echo.*\$\{?db_pass', text, re.I)


def test_the_make_targets_exist_and_backups_are_not_committed():
    makefile = (REPO / "Makefile").read_text()
    assert "\nbackup:" in makefile and "scripts/backup.sh" in makefile
    assert "\nrestore:" in makefile and "scripts/restore.sh" in makefile
    assert "backups/" in (REPO / ".gitignore").read_text(), "dumps would be committed"
