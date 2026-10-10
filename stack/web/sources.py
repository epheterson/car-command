"""Where each car's data comes from, kept in the database and set up from Settings.

A source is one row of cc_sources: its kind, the car it answers for, its plain settings
and its secret, sealed with CC_SECRET_KEY from stack/.env. The key never reaches the
database, so a dump of it holds no usable token. A secret is never shown back: the page
shows the last four characters of what was just typed, and nothing after. Sources still in
.env keep working and are listed as such until moved in; per kind, once the table has a
source of that kind that opens, that kind's .env value is no longer read."""

from __future__ import annotations

import sealing

KINDS = {"teslafi": "TeslaFi", "teslamate": "TeslaMate", "lucid": "Lucid", "tesla": "Tesla", "carconnectivity": "CarConnectivity"}

DDL = """
CREATE TABLE IF NOT EXISTS cc_sources (
    id          BIGSERIAL PRIMARY KEY,
    vin         VARCHAR,
    kind        VARCHAR NOT NULL,
    label       VARCHAR NOT NULL DEFAULT '',
    config      JSONB NOT NULL DEFAULT '{}'::jsonb,
    secret      BYTEA,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_ok_at  TIMESTAMPTZ,
    last_error  TEXT
);
"""


class NoKey(Exception):
    """Sealing needs CC_SECRET_KEY."""


class Vault:
    """Fernet with the key from .env. Without a key it can say why, and seals nothing."""

    def __init__(self, key: str | None):
        self._fernet = None
        self.problem: str | None = "CC_SECRET_KEY is not set in stack/.env"
        key = (key or "").strip()
        if key:
            from cryptography.fernet import Fernet

            try:
                self._fernet = Fernet(key.encode())
                self.problem = None
            except (ValueError, TypeError):
                self.problem = "CC_SECRET_KEY in stack/.env is not a valid key"

    @property
    def ready(self) -> bool:
        return self._fernet is not None

    def seal(self, text: str, kind: str = "value", sid="") -> bytes:
        """Sealed for one place: a source row's (kind, id), or a named value's (sealing.py)."""
        if not self.ready:
            raise NoKey(self.problem)
        return sealing.seal(self._fernet, kind, sid, text)

    def open(self, blob, kind: str = "value", sid="", legacy: bool = False) -> str | None:
        """The secret, or None when there is no key, it was sealed with another, or it was
        sealed for another place. legacy: a value from before binding opens too (never for
        cc_sources, whose old values are bound once at startup by bind_sealed)."""
        if not self.ready or blob is None:
            return None
        return sealing.open_bound(self._fernet, blob, kind, sid, legacy)

    def migrate(self, rows) -> tuple[list[tuple], int]:
        return sealing.migrate(self._fernet, rows) if self.ready else ([], len(rows))

    def open_row(self, blob, kind: str, sid) -> tuple[str | None, bytes | None]:
        """A source row's secret, and a rebound blob when it was bare (sealing.open_row)."""
        if not self.ready or blob is None:
            return None, None
        return sealing.open_row(self._fernet, blob, kind, sid)


def new_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def last4(text: str) -> str:
    return "…" + text[-4:] if len(text) >= 8 else "…"


def env_tokens(env) -> list[tuple[str | None, str]]:
    """TESLAFI_TOKENS as (vin or None, token): comma separated, optionally VIN:token."""
    out = []
    for part in (env.get("TESLAFI_TOKENS") or "").split(","):
        part = part.strip()
        if not part:
            continue
        vin, _sep, tok = part.rpartition(":")
        out.append((vin.strip() or None, tok.strip()))
    return out


def env_sources(env) -> list[dict]:
    """What the page lists as "from .env": the kind and the car, nothing secret."""
    out = [{"kind": "teslafi", "vin": vin, "from_env": True} for vin, _tok in env_tokens(env)]
    if (env.get("TESLAMATE_DB_URL") or "").strip():
        out.append({"kind": "teslamate", "vin": None, "from_env": True})
    return out


def effective(rows: list[dict], env) -> dict:
    """What the pollers use. Per kind: the table's enabled sources that open, once there is
    one; .env until then. `moved` names the kinds the table has taken over."""
    usable = [r for r in rows if r["enabled"] and r["plain"]]
    moved = {r["kind"] for r in usable}
    if "teslafi" in moved:
        teslafi = [f"{r['vin']}:{r['plain']}" if r["vin"] else r["plain"] for r in usable if r["kind"] == "teslafi"]
    else:
        teslafi = [f"{vin}:{tok}" if vin else tok for vin, tok in env_tokens(env)]
    tm = next((r for r in usable if r["kind"] == "teslamate"), None)
    if tm:
        teslamate = {"url": tm["plain"], "vins": list((tm["config"] or {}).get("vins") or [])}
    else:
        teslamate = {"url": (env.get("TESLAMATE_DB_URL") or "").strip() or None, "vins": []}
    return {"teslafi": teslafi, "teslamate": teslamate, "moved": moved}


def scrub(text: str, *secrets: str) -> str:
    """The text with every secret (and the password inside a database URL) masked."""
    for s in secrets:
        if not s:
            continue
        text = text.replace(s, "***")
        if "://" in s and "@" in s:
            creds = s.split("://", 1)[1].rsplit("@", 1)[0]
            password = creds.split(":", 1)[1] if ":" in creds else ""
            if password:
                text = text.replace(password, "***")
    return text


def check_teslafi(token: str, fetch) -> dict:
    """Ask TeslaFi for the car's last full reading with this token, as the poller would."""
    import httpx

    try:
        row = fetch(token, "lastGood")
    except httpx.HTTPStatusError as err:
        code = err.response.status_code
        return {"ok": False, "error": "TeslaFi refused the token." if code in (401, 403) else f"TeslaFi answered {code}."}
    except httpx.HTTPError:
        return {"ok": False, "error": "TeslaFi did not answer. Try again in a minute."}
    except Exception as err:  # noqa: BLE001 - whatever it was, say it without the token
        return {"ok": False, "error": scrub(f"TeslaFi's answer was not a reading: {err}", token)[:160]}  # masked, then cut: a cut first can split the token
    vin = (row or {}).get("vin")
    if not vin:
        return {"ok": False, "error": "TeslaFi answered, but not with a car. A token belongs to one vehicle: TeslaFi › Settings › Advanced › API."}
    from teslafi_import import model_from_vin

    return {"ok": True, "vin": vin, "name": (row.get("display_name") or row.get("vehicle_name") or "").strip(), "model": model_from_vin(vin), "battery": row.get("battery_level")}


def check_teslamate(url: str, connect) -> dict:
    """List the cars in a TeslaMate database, reading nothing else."""
    url = (url or "").strip()
    if not url.startswith(("postgresql://", "postgres://")):
        return {"ok": False, "error": "A TeslaMate database address starts with postgresql://"}
    try:
        from teslamate_live import list_cars  # TeslaMate's own tables are read only in that module

        with connect(url) as tm:
            rows = list_cars(tm)
    except Exception as err:  # noqa: BLE001 - say what failed, never the password
        first = str(err).strip().splitlines()[0] if str(err).strip() else type(err).__name__
        return {"ok": False, "error": scrub(f"Could not read that database: {first}", url)[:200]}  # masked, then cut
    return {"ok": True, "cars": [{"vin": r["vin"], "name": r["name"] or "", "model": r["model"] or ""} for r in rows]}


def add(conn, vault: Vault, kind: str, vin: str | None, label: str, config: dict, secret: str) -> int:
    """Seal and keep a source; a second one of the same kind for the same car replaces it."""
    import json

    if not vault.ready:
        raise NoKey(vault.problem)
    have = conn.execute("SELECT id FROM cc_sources WHERE kind = %s AND vin IS NOT DISTINCT FROM %s", (kind, vin)).fetchone()
    if have:
        sealed = vault.seal(secret, kind, have["id"])
        conn.execute("UPDATE cc_sources SET secret = %s, label = %s, config = %s, enabled = TRUE, updated_at = now(), last_error = NULL WHERE id = %s", (sealed, label, json.dumps(config), have["id"]))
        return have["id"]
    return _insert_sealed(conn, vault, kind, vin, label, config, secret)


def _insert_sealed(conn, vault: Vault, kind: str, vin, label: str, config: dict, secret: str) -> int:
    """A new row, then its secret sealed for it: the envelope names the row's id."""
    import json

    sid = conn.execute("INSERT INTO cc_sources (vin, kind, label, config, secret) VALUES (%s, %s, %s, %s, %s) RETURNING id", (vin, kind, label, json.dumps(config), None)).fetchone()["id"]
    conn.execute("UPDATE cc_sources SET secret = %s WHERE id = %s", (vault.seal(secret, kind, sid), sid))
    return sid


def add_connector(conn, vault: Vault, key: str, label: str, config: dict, values: dict) -> int:
    """Seal another brand's sign-in for the connector container, which installs its plugin and
    starts it. One account per catalogue entry: adding the same brand again replaces it."""
    import json

    if not vault.ready:
        raise NoKey(vault.problem)
    have = conn.execute("SELECT id FROM cc_sources WHERE kind = %s AND config->>'connector' = %s", ("carconnectivity", key)).fetchone()
    if have:
        sealed = vault.seal(json.dumps(values), "carconnectivity", have["id"])
        conn.execute("UPDATE cc_sources SET secret = %s, label = %s, config = %s, enabled = TRUE, updated_at = now(), last_error = NULL WHERE id = %s", (sealed, label, json.dumps(config), have["id"]))
        return have["id"]
    return _insert_sealed(conn, vault, "carconnectivity", None, label, config, json.dumps(values))


def retire(conn, source_id: int, config: dict, uninstall: bool) -> None:
    """Another brand's account on its way out: its sign-in is dropped now, and the connector
    container takes its block out of the config, removes the plugin when asked, and deletes
    the row."""
    import json

    conn.execute(
        "UPDATE cc_sources SET enabled = FALSE, secret = NULL, config = %s, last_error = NULL, updated_at = now() WHERE id = %s",
        (json.dumps({**config, "state": "remove", "uninstall": bool(uninstall)}), source_id),
    )


def load(conn, vault: Vault) -> list[dict]:
    """Every source with its secret opened (None when the key cannot), the sealed bytes left behind."""
    import json

    out, rebound = [], []
    for r in conn.execute("SELECT id, vin, kind, label, config, secret, enabled, last_ok_at, last_error FROM cc_sources ORDER BY kind, id").fetchall():
        config = r["config"] if isinstance(r["config"], dict) else json.loads(r["config"] or "{}")
        plain, new = vault.open_row(r["secret"], r["kind"], r["id"])
        if new is not None:
            rebound.append((r["id"], r["secret"], new))
        out.append(
            {
                "id": r["id"],
                "vin": r["vin"],
                "kind": r["kind"],
                "label": r["label"],
                "config": config,
                "enabled": r["enabled"],
                "plain": plain,
                "last_ok_at": r["last_ok_at"],
                "last_error": r["last_error"],
            }
        )
    rebind(conn, rebound)  # a bare value written for this row by older code: bound to it in place
    return out


def reseal(conn, vault: Vault, source_id: int, secret: str, kind: str = "tesla") -> None:
    """A new secret for a kept source, settings untouched: Tesla hands back a new refresh token each time."""
    conn.execute("UPDATE cc_sources SET secret = %s, updated_at = now() WHERE id = %s", (vault.seal(secret, kind, source_id), source_id))


def _bound_already(conn) -> bool:
    got = conn.execute("SELECT value FROM cc_state WHERE key = %s", (sealing.DONE_KEY,)).fetchone()
    return bool(got and got["value"] == sealing.DONE_VALUE)


def bind_sealed(conn, vault: Vault) -> int:
    """At startup: reseal every bare source secret for the row it is in (sealing.py). The
    connector does the same at its start; both hold the same advisory lock and look again
    inside it, and a blob is replaced only if it is still the one read. The flag goes on only
    when no bare or unreadable value is left; the caller commits."""
    if not vault.ready or _bound_already(conn):
        return 0
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (sealing.LOCK_KEY,))
    if _bound_already(conn):
        return 0
    rows = conn.execute("SELECT id, kind, secret FROM cc_sources WHERE secret IS NOT NULL").fetchall()
    redone, stuck = vault.migrate(rows)
    for sid, old, new in redone:
        conn.execute(sealing.REBIND_SQL, (new, sid, old))
    if stuck:
        import logging

        logging.getLogger("sources").warning("sources: %s sealed secrets do not open with this key; they stay as they are, and binding stays unfinished", stuck)
    else:
        conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (sealing.DONE_KEY, sealing.DONE_VALUE))
    return len(redone)


def rebind(conn, rows: list[tuple]) -> None:
    """Bare values found while reading, each bound to its own row (sealing.open_row): under the
    advisory lock, and only where the row still holds what was read."""
    import logging

    if not rows:
        return
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (sealing.LOCK_KEY,))
    for sid, old, new in rows:
        conn.execute(sealing.REBIND_SQL, (new, sid, old))
    logging.getLogger("sources").info("sources: bound %s sealed secrets written before binding to their rows", len(rows))


def remove(conn, source_id: int) -> None:
    conn.execute("DELETE FROM cc_sources WHERE id = %s", (source_id,))


def set_config(conn, source_id: int, config: dict) -> None:
    import json

    conn.execute("UPDATE cc_sources SET config = %s, updated_at = now() WHERE id = %s", (json.dumps(config), source_id))


def noted(conn, source_id: int, ok: bool, error: str | None) -> None:
    if ok:
        conn.execute("UPDATE cc_sources SET last_ok_at = now(), last_error = NULL WHERE id = %s", (source_id,))
    else:
        conn.execute("UPDATE cc_sources SET last_error = %s WHERE id = %s", ((error or "")[:300], source_id))


def bump(conn) -> None:
    """Tell the connector container its sources changed; it looks every ten seconds."""
    import time

    conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", ("sources_version", str(time.time_ns())))


def expire_pending(conn, minutes: int = 2) -> int:
    """A Lucid sign-in the connector has not picked up keeps its password no longer than this."""
    cur = conn.execute(
        "UPDATE cc_sources SET secret = NULL, config = jsonb_set(config, '{pending}', 'false'), last_error = %s, updated_at = now() "
        "WHERE kind = 'lucid' AND (config->>'pending')::boolean AND updated_at < now() - make_interval(mins => %s) RETURNING id",
        ("CarConnectivity did not sign in within two minutes, so the password was not kept. Is it running? Sign in again.", minutes),
    )
    return len(cur.fetchall())
