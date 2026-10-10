"""The version a release is cut from, and the changelog section its notes are drafted from."""

import re
from pathlib import Path

from version import VERSION

CHANGELOG = Path(__file__).resolve().parents[2] / "CHANGELOG.md"


def test_the_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+(-(rc|beta|alpha)\.\d+)?", VERSION)


def test_the_changelog_has_a_section_for_the_version():
    # A pre-release (0.2.0-rc.1) is drafted from its release's section (0.2.0).
    base = VERSION.split("-")[0]
    assert f"## [{base}]" in CHANGELOG.read_text(encoding="utf-8")
