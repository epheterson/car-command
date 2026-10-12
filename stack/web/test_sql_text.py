"""psycopg reads every % in a query as a placeholder, comments included.

A SQL comment in facts.py that said "40→90 %" took the snapshot hook down: facts, summaries and the feed rebuild all failed with
"incomplete placeholder: '%'". A render test cannot see it and the unit tests mock the
connection, so the source is checked instead: on a `--` comment line, the only percent
signs allowed are the ones psycopg accepts.
"""

import re
from pathlib import Path

MODULES = sorted(Path(__file__).parent.glob("*.py"))
STRAY = re.compile(r"%(?![s%(])")


def test_sql_comments_carry_no_bare_percent_sign():
    bad = []
    for path in MODULES:
        for n, line in enumerate(path.read_text().split("\n"), 1):
            text = line.strip()
            if text.startswith("--") and STRAY.search(text):
                bad.append(f"{path.name}:{n}: {text}")
    assert not bad, "\n".join(bad)
