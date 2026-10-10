"""A sealed secret is bound to the row it was sealed for.

Every secret is sealed (Fernet, CC_SECRET_KEY) as an envelope naming where it belongs:
{"kind": ..., "id": ..., "v": <the secret>}. Opening one names where it is being read from,
and an envelope sealed for anywhere else opens as nothing. So a sealed value copied from one
row into another (a Tesla refresh token moved into a Lucid row, say, by anything that can
write the table) is refused rather than decrypted for whoever reads that row's result.

Values sealed before envelopes have none. One found in a source row opens for that row and
is rebound to it in place (`open_row`): only Car Command's own code ever wrote a bare value,
and always for the row it is in — an old process during a split deploy, a restored dump.
The attack this defends against copies a bound blob, which names its own row and stays
refused. `migrate` rebinds every bare value at startup, so most never get that far; the
flag in cc_state says only that a scan found none left, and is set only then.

Every rebinding write holds one Postgres advisory lock (LOCK_KEY), re-checks inside it, and
replaces a blob only if it is still the one that was read, so the site and the connector
rebinding at once can never write back a value someone has replaced since (a Tesla
refresh token is single-use).

This file is the same in both images: stack/shared/sealing.py, copied into stack/web and
stack/cc (a test in each checks the copy). Pure: no database, no key of its own.
"""

from __future__ import annotations

import json

DONE_KEY, DONE_VALUE = "sealing", "bound-v1"  # cc_state: a scan found no bare value left
LOCK_KEY = 0x6363_7365_616C  # pg_advisory_xact_lock: "ccseal"
REBIND_SQL = "UPDATE cc_sources SET secret = %s WHERE id = %s AND secret = %s"  # only if unchanged since read


def envelope(kind: str, sid, secret: str) -> str:
    return json.dumps({"kind": kind, "id": str(sid), "v": secret})


def seal(fernet, kind: str, sid, secret: str) -> bytes:
    return fernet.encrypt(envelope(kind, sid, secret).encode())


def _decrypt(fernet, blob) -> str | None:
    from cryptography.fernet import InvalidToken

    if blob is None:
        return None
    try:
        return fernet.decrypt(bytes(blob)).decode()
    except (InvalidToken, TypeError, ValueError):
        return None


def _parse(plain: str):
    try:
        got = json.loads(plain)
    except ValueError:
        return None
    if isinstance(got, dict) and set(got) == {"kind", "id", "v"} and isinstance(got["v"], str):
        return got
    return None


def open_bound(fernet, blob, kind: str, sid, legacy: bool = False) -> str | None:
    """The secret, when the blob was sealed for exactly this kind and row; else None. With
    legacy, a bare value opens too (for the site's own values, which no one rebinds)."""
    plain = _decrypt(fernet, blob)
    if plain is None:
        return None
    env = _parse(plain)
    if env is None:
        return plain if legacy else None
    return env["v"] if env["kind"] == kind and env["id"] == str(sid) else None


def open_row(fernet, blob, kind: str, sid) -> tuple[str | None, bytes | None]:
    """A source row's secret: (secret, None) when bound to this row; (secret, new blob) when
    bare, the new blob bound to this row for the caller to write back (REBIND_SQL, under
    LOCK_KEY); (None, None) when bound elsewhere or not this key's."""
    plain = _decrypt(fernet, blob)
    if plain is None:
        return None, None
    env = _parse(plain)
    if env is None:
        return plain, seal(fernet, kind, sid, plain)
    return (env["v"], None) if env["kind"] == kind and env["id"] == str(sid) else (None, None)


def migrate(fernet, rows) -> tuple[list[tuple], int]:
    """([(id, old blob, new blob)] for every bare value, resealed for its row, and how many
    rows this key cannot open at all. The flag may be set only when that second is 0."""
    out, stuck = [], 0
    for r in rows:
        plain = _decrypt(fernet, r["secret"])
        if plain is None:
            stuck += 1
            continue
        if _parse(plain) is None:
            out.append((r["id"], r["secret"], seal(fernet, r["kind"], r["id"], plain)))
    return out, stuck
