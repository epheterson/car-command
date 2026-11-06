"""Passkeys: where they can work, one-time challenges, and real register/sign-in ceremonies against a software
authenticator (an EC P-256 key signing exactly what a phone would)."""

import hashlib
import json
import struct

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url

import passkeys
from passkeys import RelyingParty, relying_party

RP = RelyingParty("garage.example.org", "https://garage.example.org")


class SoftKey:
    """A passkey on a pretend phone: makes and signs WebAuthn answers byte for byte."""

    def __init__(self, uv=True):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = b"cred-" + bytes(range(11))
        self.count = 0
        self.uv = uv

    def _cose(self):
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def _flags(self, attested):
        return 0x01 | (0x04 if self.uv else 0) | (0x40 if attested else 0)

    def _client(self, kind, challenge, origin):
        return json.dumps({"type": kind, "challenge": bytes_to_base64url(challenge), "origin": origin, "crossOrigin": False}).encode()

    def create(self, challenge, rp_id=RP.id, origin=RP.origin):
        auth = hashlib.sha256(rp_id.encode()).digest() + bytes([self._flags(True)]) + struct.pack(">I", self.count)
        auth += bytes(16) + struct.pack(">H", len(self.cred_id)) + self.cred_id + self._cose()
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        cid = bytes_to_base64url(self.cred_id)
        return {
            "id": cid,
            "rawId": cid,
            "type": "public-key",
            "response": {"clientDataJSON": bytes_to_base64url(self._client("webauthn.create", challenge, origin)), "attestationObject": bytes_to_base64url(att), "transports": ["internal", "hybrid"]},
        }

    def get(self, challenge, rp_id=RP.id, origin=RP.origin, count=None):
        self.count = self.count + 1 if count is None else count
        auth = hashlib.sha256(rp_id.encode()).digest() + bytes([self._flags(False)]) + struct.pack(">I", self.count)
        client = self._client("webauthn.get", challenge, origin)
        sig = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        cid = bytes_to_base64url(self.cred_id)
        return {
            "id": cid,
            "rawId": cid,
            "type": "public-key",
            "response": {"clientDataJSON": bytes_to_base64url(client), "authenticatorData": bytes_to_base64url(auth), "signature": bytes_to_base64url(sig)},
        }


def enrolled(soft):
    got = passkeys.registered(RP, soft.create(b"c" * 32), b"c" * 32)
    return {"id": got["id"], "public_key": got["public_key"], "sign_count": got["sign_count"]}


@pytest.mark.parametrize(
    "public, proto, host, want",
    [
        (None, "https", "garage.example.org", RelyingParty("garage.example.org", "https://garage.example.org")),
        (None, "https", "Garage.Example.org:8443", RelyingParty("garage.example.org", "https://garage.example.org:8443")),
        ("https://garage.example.org/carcommand", "http", "10.0.0.5:3044", RelyingParty("garage.example.org", "https://garage.example.org")),
        (None, "http", "garage.example.org", None),  # plain http: the browser has no passkeys
        (None, "https", "10.0.0.5:3044", None),  # an IP address is never a passkey's site
        (None, "https", "[::1]:3044", None),
        (None, "https", "carcommand", None),  # a bare name
        (None, "http", "localhost:3044", RelyingParty("localhost", "http://localhost:3044")),
        (None, "https", None, None),
    ],
)
def test_passkeys_work_only_on_a_secure_named_origin(public, proto, host, want):
    assert relying_party(public, proto, host) == want


def test_a_challenge_is_used_once_by_whoever_asked_and_expires():
    now = [0.0]
    ch = passkeys.Challenges(clock=lambda: now[0])
    raw = ch.new("cf:alpha", "cmd")
    assert not ch.take("cf:bravo", "cmd", raw)  # someone else's browser: and it is spent
    raw = ch.new("cf:alpha", "cmd")
    assert not ch.take("cf:alpha", "step", raw)  # another purpose
    raw, other = ch.new("cf:alpha", "cmd"), ch.new("cf:alpha", "cmd")  # two tabs
    assert ch.take("cf:alpha", "cmd", raw) and ch.take("cf:alpha", "cmd", other)
    assert not ch.take("cf:alpha", "cmd", raw)  # once
    assert not ch.take("cf:alpha", "cmd", None) and not ch.take("cf:alpha", "cmd", b"made up")
    raw = ch.new("cf:alpha", "cmd")
    now[0] = passkeys.CHALLENGE_S + 1
    assert not ch.take("cf:alpha", "cmd", raw)


def test_one_person_holds_only_a_few_challenges():
    ch = passkeys.Challenges()
    first = ch.new("cf:alpha", "cmd")
    for _ in range(passkeys.Challenges.PER_WHO):
        ch.new("cf:alpha", "cmd")
    keep = ch.new("cf:bravo", "cmd")
    assert not ch.take("cf:alpha", "cmd", first) and ch.take("cf:bravo", "cmd", keep)
    assert sum(1 for w, _, _ in ch.held.values() if w == "cf:alpha") == passkeys.Challenges.PER_WHO


def test_the_challenge_an_answer_signed_is_read_from_it():
    assert passkeys.challenge_of(SoftKey().get(b"q" * 32)) == b"q" * 32
    assert passkeys.challenge_of({"response": {"clientDataJSON": "!!"}}) is None
    assert passkeys.challenge_of(None) is None


def test_a_passkey_registers_and_then_signs_in():
    soft = SoftKey()
    key = enrolled(soft)
    assert key["id"] == bytes_to_base64url(soft.cred_id)
    challenge = b"s" * 32
    assert passkeys.authenticated(RP, soft.get(challenge), challenge, [key]) == (key["id"], 1)


def test_registration_needs_face_id_and_this_site():
    with pytest.raises(Exception):
        passkeys.registered(RP, SoftKey(uv=False).create(b"c" * 32), b"c" * 32)
    with pytest.raises(Exception):
        passkeys.registered(RP, SoftKey().create(b"c" * 32, origin="https://evil.example"), b"c" * 32)
    with pytest.raises(Exception):
        passkeys.registered(RP, SoftKey().create(b"c" * 32), b"d" * 32)


@pytest.mark.parametrize(
    "spoil",
    [
        lambda s, k: (s.get(b"x" * 32), b"s" * 32, [k]),  # an old or someone else's challenge
        lambda s, k: (s.get(b"s" * 32, origin="https://evil.example"), b"s" * 32, [k]),  # a look-alike site
        lambda s, k: (s.get(b"s" * 32, rp_id="evil.example"), b"s" * 32, [k]),
        lambda s, k: (s.get(b"s" * 32), b"s" * 32, []),  # a passkey this site never added (or removed)
        lambda s, k: (SoftKey().get(b"s" * 32), b"s" * 32, [k]),  # another key using this one's id
    ],
)
def test_a_sign_in_that_is_not_this_passkey_on_this_site_now_is_refused(spoil):
    soft = SoftKey()
    key = enrolled(soft)
    answer, challenge, keys = spoil(soft, key)
    with pytest.raises(Exception):
        passkeys.authenticated(RP, answer, challenge, keys)


def test_a_cloned_passkey_whose_count_goes_back_is_refused():
    soft = SoftKey()
    key = {**enrolled(soft), "sign_count": 5}
    with pytest.raises(Exception):
        passkeys.authenticated(RP, soft.get(b"s" * 32, count=3), b"s" * 32, [key])


def test_names_are_tidied():
    assert passkeys.clean_name("  Alpha's   iPhone ") == "Alpha's iPhone"
    assert passkeys.clean_name("") == "Passkey"
    assert len(passkeys.clean_name("x" * 200)) == passkeys.NAME_MAX


def test_options_ask_for_face_id_and_skip_passkeys_already_added():
    soft = SoftKey()
    key = enrolled(soft)
    reg = json.loads(passkeys.registration_options(RP, b"u" * 16, "Car Command", [key], b"c" * 32))
    assert reg["rp"] == {"id": RP.id, "name": "Car Command"}
    assert reg["authenticatorSelection"]["userVerification"] == "required"
    assert [c["id"] for c in reg["excludeCredentials"]] == [key["id"]]
    auth = json.loads(passkeys.authentication_options(RP, [key], b"s" * 32))
    assert auth["userVerification"] == "required" and [c["id"] for c in auth["allowCredentials"]] == [key["id"]]
