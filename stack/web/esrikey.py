"""The owner's ArcGIS Location Platform API key, which Esri's World Imagery tiles need (Settings ›
Maps). Sealed in cc_state like the OpenEI key (urdb.py): only its last four are ever shown back.
Pure apart from the connection the callers pass in."""

from __future__ import annotations

import json
import re

KEY_STATE = "esri_key"
KEY = re.compile(r"^[A-Za-z0-9_\-=.]{20,400}$")  # ArcGIS keys are long runs of letters, digits and a few marks
SIGN_UP = "https://location.arcgis.com/sign-up/"


class EsriKeyError(Exception):
    pass


def check_key(text: str) -> str:
    text = (text or "").strip()
    if not KEY.match(text):
        raise EsriKeyError("That does not look like an ArcGIS API key: one long run of letters and digits.")
    return text


def save_key(conn, vault, text: str) -> None:
    from sources import last4

    value = json.dumps({"sealed": vault.seal(text, "state", KEY_STATE).decode(), "last4": last4(text)})
    conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (KEY_STATE, value))


def forget_key(conn) -> None:
    conn.execute("DELETE FROM cc_state WHERE key = %s", (KEY_STATE,))


def _stored(conn) -> dict | None:
    row = conn.execute("SELECT value FROM cc_state WHERE key = %s", (KEY_STATE,)).fetchone()
    try:
        return json.loads(row["value"]) if row else None
    except (TypeError, ValueError):
        return None


def key_hint(conn) -> str | None:
    """ "…1234" for a saved key, read without opening it; None for none."""
    kept = _stored(conn)
    return (kept.get("last4") or "…") if kept else None


def load_key(conn, vault) -> str | None:
    kept = _stored(conn)
    return vault.open(kept["sealed"].encode(), "state", KEY_STATE, legacy=True) if kept and kept.get("sealed") else None
