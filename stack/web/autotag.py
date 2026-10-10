"""Auto-tag rules: a drive from one named place to another gets a tag on its own.

For example, Home → the office is a Commute both ways, and Home → the station a Station
Commute one way. A rule names a start place, an end place, or both, and a tag; "both
ways" makes it symmetric. A drive that
already carries a tag is left alone — a tag typed by hand outranks a rule.
"""

from __future__ import annotations

DDL = """
CREATE TABLE IF NOT EXISTS cc_auto_tags (
    id         SERIAL PRIMARY KEY,
    from_name  TEXT,
    to_name    TEXT,
    tag        TEXT NOT NULL,
    both_ways  BOOLEAN NOT NULL DEFAULT TRUE,
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _same(rule_name: str | None, place: str | None) -> bool:
    """A blank rule side means anywhere; otherwise the names match case-blind."""
    if not rule_name:
        return True
    return bool(place) and rule_name.strip().lower() == place.strip().lower()


def match(rules: list[dict], from_name: str | None, to_name: str | None) -> str | None:
    """The tag the first active rule gives this drive, or None."""
    for r in rules:
        if not r.get("active", True) or not r.get("tag"):
            continue
        if not r.get("from_name") and not r.get("to_name"):
            continue  # a rule with neither side would tag every drive; that is a mistake, not a rule
        if _same(r.get("from_name"), from_name) and _same(r.get("to_name"), to_name):
            return r["tag"]
        if r.get("both_ways") and _same(r.get("from_name"), to_name) and _same(r.get("to_name"), from_name):
            return r["tag"]
    return None


def plan(rules: list[dict], drives: list[dict]) -> list[tuple[int, str]]:
    """(trip id, tag) for every untagged drive a rule matches."""
    out = []
    for d in drives:
        if d.get("tag"):
            continue
        tag = match(rules, d.get("from"), d.get("to"))
        if tag:
            out.append((d["id"], tag))
    return out
