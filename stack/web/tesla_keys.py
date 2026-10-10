"""The app's key pair. Tesla knows the app by its public half, served at
    https://<domain>/.well-known/appspecific/com.tesla.3p.public-key.pem
and a car accepts the app once its owner adds that key. The private half signs the
streaming setup (T2) and never leaves the host: it is made once, sealed with
CC_SECRET_KEY and kept in cc_state. Losing it means every car adds a new key."""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

STATE_KEY = "tesla_private_key"
PUBLIC_PATH = "/.well-known/appspecific/com.tesla.3p.public-key.pem"


def new_private_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def _public(private_pem: str):
    return serialization.load_pem_private_key(private_pem.encode(), password=None).public_key()


def public_pem(private_pem: str) -> str:
    return _public(private_pem).public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def public_hex(private_pem: str) -> str:
    """The uncompressed point, the form Tesla's partner_accounts/public_key answers with."""
    return _public(private_pem).public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint).hex()


def stored(get, vault) -> str | None:
    blob = get(STATE_KEY)
    return vault.open(blob.encode(), "state", STATE_KEY, legacy=True) if blob else None  # a key from before binding still opens


def ensure(get, set, vault) -> str:
    """The key, made and sealed on first use. Raises sources.NoKey when nothing can be sealed."""
    have = stored(get, vault)
    if have:
        return have
    pem = new_private_pem()
    set(STATE_KEY, vault.seal(pem, "state", STATE_KEY).decode())
    return pem
