"""Passkeys (Face ID, Touch ID, a security key) as the step-up for tier 3 commands: the phone keeps the private key,
the site keeps only the public half in `cc_passkeys`. WebAuthn needs a secure origin with a real name, so a site
reached at http://<ip>:3044 offers none (`relying_party` is None there) and keeps its password step-up.

The ceremony is two calls each way: options (a fresh one-time challenge, held here for two minutes, bound to who
asked) and the answer, verified by the `webauthn` library against that challenge, this origin and the stored key."""

from __future__ import annotations

import ipaddress
import json
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

DDL = """
CREATE TABLE IF NOT EXISTS cc_passkeys (
    id          VARCHAR PRIMARY KEY,
    rp_id       VARCHAR NOT NULL,
    public_key  BYTEA NOT NULL,
    sign_count  BIGINT NOT NULL DEFAULT 0,
    name        VARCHAR NOT NULL,
    transports  JSONB NOT NULL DEFAULT '[]',
    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used   TIMESTAMPTZ
);
"""

CHALLENGE_S = 120  # how long a challenge waits for the phone's answer
NAME_MAX = 40
USER_KEY = "passkey_user"  # in cc_auth: the one site user's WebAuthn handle, random, made on the first passkey


@dataclass(frozen=True)
class RelyingParty:
    id: str  # the host name the passkey is bound to
    origin: str  # scheme://host[:port] the browser must report


def relying_party(public_url: str | None, proto: str, host: str | None) -> RelyingParty | None:
    """Where passkeys can work: the configured public address when there is one, else this request's own. None for
    plain http (bar localhost), an IP address, or a bare name: the browser refuses passkeys there."""
    if public_url:
        parsed = urlparse(public_url)
        proto, host = parsed.scheme, parsed.netloc
    if not host:
        return None
    name = (urlparse(f"//{host}").hostname or "").lower()
    if not name:
        return None
    try:
        ipaddress.ip_address(name)
        return None
    except ValueError:
        pass
    if name != "localhost" and ("." not in name or proto != "https"):
        return None
    return RelyingParty(name, f"{proto}://{host.lower()}")


class Challenges:
    """Challenges handed out and not yet answered, each for one (who, purpose), used once, gone after CHALLENGE_S.
    Several may wait at once (two tabs, two cars on the page); at most PER_WHO for any one person."""

    PER_WHO = 8

    def __init__(self, clock=time.monotonic) -> None:
        self.clock, self.lock, self.held = clock, threading.Lock(), {}

    def new(self, who: str, purpose: str) -> bytes:
        raw = secrets.token_bytes(32)
        with self.lock:
            now = self.clock()
            for k in [k for k, (_, _, at) in self.held.items() if now - at > CHALLENGE_S]:
                self.held.pop(k, None)
            mine = sorted((at, k) for k, (w, _, at) in self.held.items() if w == who)
            for _, k in mine[: max(0, len(mine) - self.PER_WHO + 1)]:
                self.held.pop(k, None)
            self.held[raw] = (who, purpose, now)
        return raw

    def take(self, who: str, purpose: str, raw: bytes | None) -> bool:
        """Whether `raw` was handed to `who` for `purpose` and is still fresh; it is gone either way."""
        with self.lock:
            got = self.held.pop(raw, None) if raw else None
        return got is not None and got[:2] == (who, purpose) and self.clock() - got[2] <= CHALLENGE_S


def challenge_of(answer) -> bytes | None:
    """The challenge a browser's answer signed, read from its clientDataJSON (verified again by the library)."""
    try:
        client = json.loads(base64url_to_bytes(answer["response"]["clientDataJSON"]))
        return base64url_to_bytes(client["challenge"])
    except (KeyError, TypeError, ValueError):
        return None


CHALLENGES = Challenges()


def listed(conn, rp_id: str | None = None) -> list[dict]:
    """The passkeys, newest first; only those bound to `rp_id` when one is given."""
    conn.execute(DDL)
    rows = conn.execute(
        "SELECT id, rp_id, public_key, sign_count, name, transports, created, last_used FROM cc_passkeys" + (" WHERE rp_id = %s" if rp_id else "") + " ORDER BY created DESC",
        (rp_id,) if rp_id else (),
    ).fetchall()
    keys = ("id", "rp_id", "public_key", "sign_count", "name", "transports", "created", "last_used")
    return [dict(r) if isinstance(r, dict) else dict(zip(keys, r)) for r in rows]


def user_handle(conn, store_get, store_set) -> bytes:
    held = store_get(conn, USER_KEY)
    if held:
        return base64url_to_bytes(held)
    raw = secrets.token_bytes(16)
    store_set(conn, USER_KEY, bytes_to_base64url(raw))
    return raw


def _descriptors(keys: list[dict]) -> list[PublicKeyCredentialDescriptor]:
    return [PublicKeyCredentialDescriptor(id=base64url_to_bytes(k["id"])) for k in keys]


def registration_options(rp: RelyingParty, user: bytes, site_name: str, existing: list[dict], challenge: bytes) -> str:
    """The options for navigator.credentials.create(), as JSON: a passkey the phone keeps (resident), Face ID
    required, none of the ones already added made again."""
    opts = generate_registration_options(
        rp_id=rp.id,
        rp_name=site_name,
        user_id=user,
        user_name=site_name,
        user_display_name=site_name,
        challenge=challenge,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=_descriptors(existing),
    )
    return options_to_json(opts)


def registered(rp: RelyingParty, answer: dict, challenge: bytes) -> dict:
    """The new passkey from the phone's answer, verified: {id, public_key, sign_count, transports}. Raises on any fault."""
    got = verify_registration_response(
        credential=answer,
        expected_challenge=challenge,
        expected_rp_id=rp.id,
        expected_origin=rp.origin,
        require_user_verification=True,
    )
    transports = (answer.get("response") or {}).get("transports") or []
    return {
        "id": bytes_to_base64url(got.credential_id),
        "public_key": got.credential_public_key,
        "sign_count": got.sign_count,
        "transports": [t for t in transports if isinstance(t, str)][:8],
    }


def authentication_options(rp: RelyingParty, keys: list[dict], challenge: bytes) -> str:
    opts = generate_authentication_options(
        rp_id=rp.id,
        challenge=challenge,
        allow_credentials=_descriptors(keys),
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return options_to_json(opts)


def authenticated(rp: RelyingParty, answer: dict, challenge: bytes, keys: list[dict]) -> tuple[str, int]:
    """(passkey id, its new sign count) for a verified answer from one of `keys`. Raises on any fault, including a
    passkey not in `keys` or a sign count that went backwards (a cloned key)."""
    match = next((k for k in keys if k["id"] == answer.get("id")), None)
    if match is None:
        raise ValueError("unknown passkey")
    got = verify_authentication_response(
        credential=answer,
        expected_challenge=challenge,
        expected_rp_id=rp.id,
        expected_origin=rp.origin,
        credential_public_key=bytes(match["public_key"]),
        credential_current_sign_count=int(match["sign_count"]),
        require_user_verification=True,
    )
    return match["id"], got.new_sign_count


def add(conn, rp: RelyingParty, key: dict, name: str) -> None:
    conn.execute(DDL)
    conn.execute(
        "INSERT INTO cc_passkeys (id, rp_id, public_key, sign_count, name, transports) VALUES (%s, %s, %s, %s, %s, %s)",
        (key["id"], rp.id, key["public_key"], key["sign_count"], clean_name(name), json.dumps(key["transports"])),
    )


def used(conn, key_id: str, sign_count: int) -> None:
    conn.execute("UPDATE cc_passkeys SET sign_count = %s, last_used = now() WHERE id = %s", (sign_count, key_id))


def remove(conn, key_id: str) -> bool:
    conn.execute(DDL)
    return conn.execute("DELETE FROM cc_passkeys WHERE id = %s", (key_id,)).rowcount > 0


def clean_name(name: str | None) -> str:
    text = " ".join(str(name or "").split())[:NAME_MAX]
    return text or "Passkey"
