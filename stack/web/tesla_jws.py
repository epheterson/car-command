"""Tesla.SS256: the signature Tesla asks for on a streaming setup.

A car takes a Fleet Telemetry config only when it is signed with the app's private key
(the one the owner added to the car). Tesla's scheme is a Schnorr signature over P-256
with SHA-256, carried in a compact JWS whose header names the algorithm "Tesla.SS256".
Signing here means no vehicle-command proxy has to run beside the site.

A signature is 96 bytes: the public nonce V = vG (x and y, 32 bytes each) and
r = v - a·c mod n, where a is the private scalar and c the challenge, SHA-256 over the
length-prefixed generator, nonce, public key and message. The nonce is RFC 6979's, so the
same key and message always sign the same way, which is what lets the tests pin it to
the vectors Tesla's own Go code produces."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ALGORITHM = "Tesla.SS256"
TELEMETRY_AUDIENCE = "com.tesla.fleet.TelemetryClient"
# P-256: the group order and the generator, uncompressed.
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
G = bytes.fromhex("04" "6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296" "4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5")


def _point(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _framed(*parts: bytes) -> bytes:
    return b"".join(len(p).to_bytes(4, "big") + p for p in parts)


def nonce(scalar: bytes, digest: bytes) -> bytes:
    """RFC 6979 section 3.2 for P-256 and SHA-256."""
    h1 = (int.from_bytes(digest, "big") % N).to_bytes(32, "big")
    k, v = b"\x00" * 32, b"\x01" * 32
    k = hmac.digest(k, v + b"\x00" + scalar + h1, "sha256")
    v = hmac.digest(k, v, "sha256")
    k = hmac.digest(k, v + b"\x01" + scalar + h1, "sha256")
    v = hmac.digest(k, v, "sha256")
    while True:
        v = hmac.digest(k, v, "sha256")
        if 0 < int.from_bytes(v, "big") < N:
            return v
        k = hmac.digest(k, v + b"\x00", "sha256")
        v = hmac.digest(k, v, "sha256")


def sign(key: ec.EllipticCurvePrivateKey, message: bytes) -> bytes:
    """The 96-byte signature of `message`."""
    if not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("Tesla.SS256 signs with a P-256 key.")
    a = key.private_numbers().private_value
    v = int.from_bytes(nonce(a.to_bytes(32, "big"), hashlib.sha256(message).digest()), "big")
    big_v = _point(ec.derive_private_key(v, ec.SECP256R1()))
    c = int.from_bytes(hashlib.sha256(_framed(G, big_v, _point(key), message)).digest(), "big")
    return big_v[1:] + ((v - a * c) % N).to_bytes(32, "big")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _json(value: dict) -> bytes:
    # sorted and compact, as Go's encoding/json writes a map: the vectors depend on it
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def token(private_pem: str, claims: dict, audience: str = TELEMETRY_AUDIENCE) -> str:
    """A compact JWS over `claims`, issued by the key itself (its public point, base64) for `audience`."""
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    payload = {**claims, "iss": base64.b64encode(_point(key)).decode(), "aud": audience}
    head = _b64url(_json({"alg": ALGORITHM, "typ": "JWT"})) + "." + _b64url(_json(payload))
    return head + "." + _b64url(sign(key, head.encode()))
