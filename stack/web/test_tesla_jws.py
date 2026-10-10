"""The streaming setup's signature, pinned to Tesla's own Go code: the scheme is
deterministic, so matching it byte for byte proves the framing and the nonce.

Every vector comes from teslamotors/vehicle-command v0.4.1 (commit
49977a18fd68567501d59e16a6c9e4a8b9348544):
- GO_SCALAR, GO_HELLO: internal/schnorr/schnorr_test.go, testKey() and goodSig().
- the RFC 6979 nonce and the redraw digest and nonce: internal/schnorr/sign_test.go,
  TestDeterministicNonce and TestRejectionSampling.
- GO_EMPTY and JWS_TOKEN: made for these tests by that commit's own code, a small Go program
  inside the module calling authentication.UnmarshalECDHPrivateKey(scalar).SchnorrSignature("")
  and authentication.SignMessage(key, JWS_CONFIG, "com.tesla.fleet.TelemetryClient")
  (internal/authentication/native.go and jwt.go, the signer cmd/tesla-jws uses)."""

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import tesla_jws

GO_SCALAR = 3 << 248  # testKey(): 0x03 then 31 zero bytes
GO_HELLO = bytes.fromhex(
    "7cfdbeb5baa730540401550bdefa20976453e8539ae4b2f26ce33125801a08f9"
    "0ed20c3d846497ff82cc9772e3db4703982f47bd0b0b89dfb9a49cd2e5240546"
    "02b1e05fbf95f5686faea7a5809eb92f5ecc22eae74ceccc5e2a65dd67ff20fc"
)
GO_EMPTY = bytes.fromhex(  # SchnorrSignature of the empty message with testKey(), run at that commit
    "ea1334c75226b4f33beb5b20c4c59967e746dc6620705d20fc0f5de512222df4"
    "64368396ddfdd67f8a73ab47639c6dd2ab0deb86625192871c34c0c33855bb50"
    "6cea9dd219be49fe994159aa5f028c3f0e09a4a8094113f8f08df567e63da000"
)
JWS_SCALAR = 0x08EA9503BDDCE4E9B6E39CDF96A6C31C95085378C4CC0939B516380A3999E52E
JWS_CONFIG = {
    "hostname": "telemetry.example.com",
    "port": 443,
    "ca": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
    "exp": 1790000000,
    "fields": {"Locked": {"interval_seconds": 60}, "BatteryLevel": {"interval_seconds": 300}},
    "alert_types": ["service"],
}
JWS_TOKEN = (  # authentication.SignMessage(JWS_SCALAR's key, JWS_CONFIG, the telemetry audience), run at that commit
    "eyJhbGciOiJUZXNsYS5TUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJhbGVydF90eXBlcyI6WyJzZXJ2aWNlIl0sImF1ZCI6ImNvbS50ZXNsYS5mbGVldC5UZWxlbWV0cnlDbGllbnQiLCJjYSI6Ii0tLS0tQkVHSU4gQ0VSVElGSUNBVEUtLS0tLVxuTUlJQlxuLS0tLS1FTkQgQ0VSVElGSUNBVEUtLS0tLVxuIiwiZXhwIjoxNzkwMDAwMDAwLCJmaWVsZHMiOnsiQmF0dGVyeUxldmVsIjp7ImludGVydmFsX3NlY29uZHMiOjMwMH0sIkxvY2tlZCI6eyJpbnRlcnZhbF9zZWNvbmRzIjo2MH19LCJob3N0bmFtZSI6InRlbGVtZXRyeS5leGFtcGxlLmNvbSIsImlzcyI6IkJCZkdFcmEyS3FsNEVHZTQwMVIrbnR2ZzJsQlU4SVc5QlpEMjBWQVFMRWJwY25VVlJMTEVpZ1J5bEhTZTV3bG9pZXNLQTR6bG5GbFpYTkVQeWRzS1BNaz0iLCJwb3J0Ijo0NDN9."
    "byBWvxjuOX1hj8iOXJbNPL3TIABVSXYBK5RX5PVWMwyoZmJ12uyAIQ3thQH4R5bor_IPXwiysjqFew15eTWi2nGFlPIvP54ptIpFL4KnIJtRn4nLYRtZE8OqULREPocE"
)


def key(scalar: int):
    return ec.derive_private_key(scalar, ec.SECP256R1())


def pem(scalar: int) -> str:
    return key(scalar).private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def test_the_nonce_is_rfc_6979s():
    """RFC 6979 A.2.5, P-256 with SHA-256, message "sample"."""
    scalar = bytes.fromhex("c9afa9d845ba75166b5c215767b1d6934e50c3db36e89b127b8a622b120f6721")
    assert tesla_jws.nonce(scalar, hashlib.sha256(b"sample").digest()).hex() == "a6e3c57dd01abe90086538398355dd4c3b17aa873382b0f24d6129493d8aad60"


def hmacs(monkeypatch):
    """Counts the HMAC steps nonce() takes: five when its first candidate is good, more when it draws again."""
    calls, real = [], tesla_jws.hmac.digest
    monkeypatch.setattr(tesla_jws.hmac, "digest", lambda *a: calls.append(1) or real(*a))
    return calls


def test_a_nonce_candidate_past_the_order_is_drawn_again(monkeypatch):
    """Tesla's TestRejectionSampling digest, whose first candidate is past the group order."""
    calls = hmacs(monkeypatch)
    tesla_jws.nonce(bytes.fromhex("c9afa9d845ba75166b5c215767b1d6934e50c3db36e89b127b8a622b120f6721"), hashlib.sha256(b"sample").digest())
    first_try = len(calls)
    calls.clear()
    digest = bytes.fromhex("0080c36864c5f2f460e3767983c65677b65cef901bcedcb223f9b365c68f52f6")
    assert tesla_jws.nonce(GO_SCALAR.to_bytes(32, "big"), digest).hex() == "264fc6592fbea24fd0954e0b86b886e8743161758ddad2f7e9fed75a0019e005"
    assert first_try == 5 and len(calls) > first_try, "the redraw path ran"


@pytest.mark.parametrize("message,expected", [(b"hello world", GO_HELLO), (b"", GO_EMPTY)])
def test_signatures_match_teslas_go_code(message, expected):
    assert tesla_jws.sign(key(GO_SCALAR), message) == expected


def test_a_token_matches_the_one_teslas_signer_made():
    assert tesla_jws.token(pem(JWS_SCALAR), JWS_CONFIG) == JWS_TOKEN


def test_the_token_names_its_key_and_audience_and_keeps_the_config():
    tok = tesla_jws.token(pem(JWS_SCALAR), {**JWS_CONFIG, "iss": "someone else"})
    head, body, sig = tok.split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
    assert json.loads(pad(head)) == {"alg": "Tesla.SS256", "typ": "JWT"}
    claims = json.loads(pad(body))
    point = key(JWS_SCALAR).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    assert claims["iss"] == base64.b64encode(point).decode() and claims["aud"] == tesla_jws.TELEMETRY_AUDIENCE
    assert claims["hostname"] == "telemetry.example.com" and len(pad(sig)) == 96


def test_only_a_p256_key_signs():
    with pytest.raises(ValueError):
        tesla_jws.sign(ec.generate_private_key(ec.SECP384R1()), b"x")
