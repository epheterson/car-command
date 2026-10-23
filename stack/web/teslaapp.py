"""The Tesla developer app's three values (Client ID, Client secret, domain), as the owner enters them on
Connect Tesla. Pure: the sealing key and the store come from the caller. What the environment sets
(TESLA_CLIENT_ID, TESLA_CLIENT_SECRET, TESLA_DOMAIN) wins field by field and the page says so."""

from __future__ import annotations

import json
import re

import tesla_net

KEY = "tesla_app"  # the cc_state row holding the sealed values
FIELDS = ("client_id", "client_secret", "domain")
LABELS = {"client_id": "Client ID", "client_secret": "Client secret", "domain": "Domain"}


def clean_domain(raw: str | None) -> str:
    """The hostname alone: no scheme, path or port. '' when it is not a public hostname Tesla could reach."""
    host = re.sub(r"^[a-z]+://", "", (raw or "").strip().lower()).split("/")[0].split("?")[0].split("#")[0]
    return host if tesla_net.valid_host(host) else ""


def merge(env: dict, stored: dict) -> dict:
    """Each field from the environment when it is set there, else from what was entered. `from` says which."""
    out = {"from": {}}
    for f in FIELDS:
        e, s = (env.get(f) or "").strip(), (stored.get(f) or "").strip()
        out[f] = e or s
        out["from"][f] = "env" if e else "app" if s else ""
    return out


def read_form(form, stored: dict, env: dict) -> tuple[dict | None, str | None]:
    """What the form changes, over what is kept. A blank field keeps its kept value, so the secret need not be typed again.
    (values to store, why not). A field the environment sets is not stored: it would never be read."""
    new = dict(stored)
    for f in FIELDS:
        if (env.get(f) or "").strip():
            new.pop(f, None)
            continue
        raw = (form.get(f) or "").strip()
        if f == "domain" and raw:
            raw = clean_domain(raw)
            if not raw:
                return None, "The domain is the hostname alone, such as cars.example.com: https, reachable from the internet."
        if raw:
            new[f] = raw
    got = merge(env, new)
    missing = [LABELS[f] for f in FIELDS if not got[f]]
    if missing:
        return None, f"Still needed: {', '.join(missing)}."
    return {f: new[f] for f in FIELDS if f in new}, None


def pack(vault, values: dict) -> str:
    """The sealed text to keep. Raises sources.NoKey when nothing can be sealed."""
    return vault.seal(json.dumps(values), "state", KEY).decode()


def unpack(vault, blob: str | None) -> dict:
    plain = vault.open(blob.encode(), "state", KEY) if blob else None
    try:
        got = json.loads(plain) if plain else {}
    except ValueError:
        return {}
    return {f: got[f] for f in FIELDS if isinstance(got.get(f), str)} if isinstance(got, dict) else {}
