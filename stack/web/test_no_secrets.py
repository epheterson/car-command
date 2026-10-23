"""Nothing personal and nothing secret in the tracked tree.

Taking a VIN or a coordinate out of history is cheap only while a repository is private
and has one reader. Afterwards it costs a force-push everyone has to notice, or a new
repository. So never add one.

This runs over `git ls-files`, not the working directory, because an untracked file is
not the problem — `stack/.env` is supposed to exist and is supposed to hold secrets.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Shapes that are credentials wherever they appear.
CREDENTIALS = {
    "a GitHub token": r"gh[pousr]_[A-Za-z0-9]{30,}",
    "an OpenAI key": r"sk-[A-Za-z0-9]{32,}",
    "an Anthropic key": r"sk-ant-[A-Za-z0-9-]{24,}",
    "a Slack token": r"xox[baprs]-[A-Za-z0-9-]{12,}",
    "an AWS key id": r"AKIA[0-9A-Z]{16}",
    "a Google API key": r"AIza[0-9A-Za-z_-]{35}",
    "a private key": r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----",
    "a Discord webhook": r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}",
    "a password in a URL": r"://[A-Za-z0-9._%-]+:[^@/\s\"'{}$]{8,}@[A-Za-z0-9.-]+",
}

# Shapes that are personal rather than secret. A VIN is the registration of a specific
# car; the coordinates are where it sleeps. Neither belongs in a repository that is
# meant to go public, and both were in this one.
PERSONAL = {
    # A VIN is 17 characters and always carries letters, which keeps this off the
    # all-numeric junk values TeslaFi puts in its export.
    "a real-looking VIN": r"\b(?=[A-HJ-NPR-Z0-9]{17}\b)(?=[0-9]{0,16}[A-HJ-NPR-Z])[A-HJ-NPR-Z0-9]{11}[0-9]{6}\b",
    "a Bay Area coordinate": r"\b3[678]\.\d{3,}\s*,\s*-12[12]\.\d{3,}",
    # Where the owner keeps notes, or whose machine this is. Written so the pattern does
    # not match its own source.
    "a path into the owner's notes": r"~/v[a]ult\b",
    "a home directory": r"/Users/[a-z][a-z0-9_-]+",
}

# Files the public snapshot leaves out (scripts/publish-snapshot.sh), so they may say where
# things live. They are still checked for credentials and real cars; only where-things-live is let be.
PRIVATE_ONLY = (
    "CLAUDE.md",
    "PLAN.md",
    "docs/status.md",
    "docs/teslafi-",
    "docs/ideas-from-elsewhere.md",
    "docs/plans/",
    "docs/release-readiness.md",
    "docs/tesla-plan.md",
    "docs/history/",
    "docs/design/",
)
WHERE_THINGS_LIVE = {"a path into the owner's notes", "a home directory"}


def private_only(rel: str) -> bool:
    return rel.startswith(PRIVATE_ONLY)


# The fictional cars this repo tests against all end in a sequential id — 000001, 000002.
# A real VIN's last six are a serial number and effectively never look like that.
ALLOWED = re.compile(r"0{3}\d{3}$|TESTVIN|ALPHA|BRAVO|CHARLIE", re.I)
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".gz", ".zip", ".woff", ".woff2")


def tracked_files() -> list[Path]:
    """Tracked files, minus this one: it spells out every pattern it looks for, and a
    scanner that reports itself is a scanner someone switches off."""
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, text=True, check=True).stdout
    here = Path(__file__).resolve()
    return [p for rel in out.split("\0") if rel and not rel.endswith(SKIP_SUFFIXES) for p in [REPO / rel] if p.resolve() != here]


def test_git_is_actually_listing_files():
    """A scan over an empty list passes silently, which is worse than no scan."""
    files = tracked_files()
    assert len(files) > 40, f"only {len(files)} tracked files found; the scan would prove nothing"


@pytest.mark.parametrize("label,pattern", sorted(CREDENTIALS.items()))
def test_no_credential_is_committed(label, pattern):
    offenders = []
    for path in tracked_files():
        try:
            text = path.read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for m in re.finditer(pattern, text):
            hit = m.group(0)
            if re.search(r"CHANGE_ME|EXAMPLE|example|xxxx|XXXX|placeholder|your[-_]|REDACTED|\$\{|\{secret\}|nowhere", hit):
                continue
            offenders.append(f"{path.relative_to(REPO)}: {hit[:40]}…")
    assert not offenders, f"{label} is committed:\n  " + "\n  ".join(offenders)


@pytest.mark.parametrize("label,pattern", sorted(PERSONAL.items()))
def test_nothing_identifies_a_real_car_or_place(label, pattern):
    offenders = []
    for path in tracked_files():
        if label in WHERE_THINGS_LIVE and private_only(path.relative_to(REPO).as_posix()):
            continue
        try:
            text = path.read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for m in re.finditer(pattern, text):
            if not ALLOWED.search(m.group(0)):
                offenders.append(f"{path.relative_to(REPO)}: {m.group(0)}")
    assert not offenders, f"{label} is committed:\n  " + "\n  ".join(sorted(set(offenders))[:12])


def test_the_env_file_is_never_tracked():
    """.env holds the database password. The example beside it holds CHANGE_ME."""
    tracked = {p.relative_to(REPO).as_posix() for p in tracked_files()}
    assert "stack/.env" not in tracked
    assert "stack/.env.example" in tracked
    assert not any(t.startswith("stack/secrets/") for t in tracked), "the token directory is tracked"
    assert "Makefile.local" not in tracked, "the deploy host is meant to stay untracked"


def test_the_scan_would_catch_something():
    """Every pattern is checked against a string it must match, so a broken regex that
    quietly matches nothing cannot pass for a clean repository."""
    samples = {
        "a GitHub token": "ghp_" + "a" * 36,
        "an OpenAI key": "sk-" + "b" * 40,
        "an Anthropic key": "sk-ant-" + "c" * 30,
        "a Slack token": "xoxb-" + "1" * 20,
        "an AWS key id": "AKIA" + "A" * 16,
        "a Google API key": "AIza" + "d" * 35,
        "a private key": "-----BEGIN PRIVATE KEY-----",
        "a Discord webhook": "discord.com/api/webhooks/12345/" + "e" * 30,
        "a password in a URL": "postgresql://carcommand:s3cretpassword@carcommand-db",
        "a real-looking VIN": "1HGCM82633A004352",  # a widely published example VIN
        "a Bay Area coordinate": "37.8199, -122.4783",  # the Golden Gate Bridge
        "a path into the owner's notes": "see ~/" + "vault/me/notes.md",
        "a home directory": "/Users/" + "someone/Repos",
    }
    for label, pattern in {**CREDENTIALS, **PERSONAL}.items():
        assert re.search(pattern, samples[label]), f"the {label} pattern matches nothing"


def test_the_private_files_are_the_ones_the_snapshot_leaves_out():
    """The exemption above holds only while the public snapshot really drops those files."""
    script = (REPO / "scripts" / "publish-snapshot.sh").read_text()
    dropped = script[script.index("rm -rf PLAN.md") :].split("\n")[0].split()[2:] + ["CLAUDE.md"]
    for prefix in PRIVATE_ONLY:
        assert any(prefix.rstrip("/").startswith(d.split("*")[0].rstrip("/")) for d in dropped), prefix
