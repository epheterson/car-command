"""Checking Cloudflare Access JWTs: a good one, every way a forged or stale one fails, who may present
one, and how the team's keys are kept."""

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import proxyauth

TEAM, AUD = "example-team", "aud-tag-1"
NOW = 1_800_000_000.0


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY, OTHER = make_key(), make_key()


def jwk(key, kid):
    numbers = key.public_key().public_numbers()
    return {"kty": "RSA", "kid": kid, "alg": "RS256", "use": "sig", "n": b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")), "e": b64(numbers.e.to_bytes(3, "big"))}


def jwks(**keys):
    return {"keys": [jwk(k, kid) for kid, k in keys.items()], "public_cert": {}, "public_certs": []}


def jwt(claims=None, key=KEY, kid="k1", alg="RS256", sign=True, **over):
    payload = {"iss": proxyauth.issuer(TEAM), "aud": [AUD], "sub": "u-1", "email": "owner@example.test", "iat": NOW - 10, "nbf": NOW - 10, "exp": NOW + 3600, **(claims or {}), **over}
    head = b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    body = b64(json.dumps(payload).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()) if sign else b""
    return f"{head}.{body}.{b64(sig)}"


class Certs:
    """A fake of the team's certs endpoint that counts how often it is asked."""

    def __init__(self, doc=None, fail=False):
        self.doc, self.fail, self.calls = doc if doc is not None else jwks(k1=KEY), fail, []

    def __call__(self, team):
        self.calls.append(team)
        if self.fail:
            raise OSError("down")
        return self.doc


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def verifier(certs=None, clock=None, **kw):
    return proxyauth.Verifier(fetch=certs or Certs(), clock=clock or Clock(), **kw)


def test_a_good_token_is_verified_and_its_claims_returned():
    claims = verifier().verify(jwt(), TEAM, AUD)
    assert claims["email"] == "owner@example.test" and claims["sub"] == "u-1"


def test_aud_may_be_a_string_or_one_of_several():
    assert verifier().verify(jwt(aud=AUD), TEAM, AUD)
    assert verifier().verify(jwt(aud=["other", AUD]), TEAM, AUD)


@pytest.mark.parametrize(
    "bad,reason",
    [
        ({"exp": NOW - 61}, "exp"),
        ({"aud": ["someone-else"]}, "aud"),
        ({"aud": []}, "aud"),
        ({"aud": None}, "aud"),
        ({"iss": "https://other-team.cloudflareaccess.com"}, "iss"),
        ({"iss": "https://example-team.cloudflareaccess.com.evil.test"}, "iss"),
        ({"nbf": NOW + 3600}, "nbf"),
        ({"iat": NOW + 3600}, "iat"),
        ({"exp": "never"}, "exp"),
        ({"exp": True}, "exp"),
    ],
)
def test_claims_that_do_not_fit_are_refused_and_say_why(bad, reason):
    assert verifier().check(jwt(**bad), TEAM, AUD) == (None, reason)


def _without(name):
    token = jwt()
    head, body, _ = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=="))
    del payload[name]
    body = b64(json.dumps(payload).encode())
    return f"{head}.{body}.{b64(KEY.sign(f'{head}.{body}'.encode(), padding.PKCS1v15(), hashes.SHA256()))}"


@pytest.mark.parametrize("name", ["exp", "iat"])
def test_a_token_without_exp_or_iat_is_refused(name):
    assert verifier().verify(_without(name), TEAM, AUD) is None


def test_sixty_seconds_of_leeway_on_exp_and_not_more():
    assert verifier().verify(jwt(exp=NOW - 59), TEAM, AUD)
    assert verifier().verify(jwt(exp=NOW - 61), TEAM, AUD) is None


def test_alg_none_is_refused_before_a_key_is_looked_up():
    certs = Certs()
    v = verifier(certs)
    token = jwt(alg="none", sign=False)
    assert v.check(token, TEAM, AUD) == (None, "alg:none")
    assert v.verify(token.rsplit(".", 1)[0] + ".", TEAM, AUD) is None
    assert certs.calls == [], "no fetch for a token that was never going to pass"


@pytest.mark.parametrize("alg", ["HS256", "HS384", "RS384", "ES256", "rs256", ""])
def test_only_rs256_is_accepted(alg):
    assert verifier().verify(jwt(alg=alg), TEAM, AUD) is None


def test_hs256_signed_with_the_public_key_is_refused():
    """The classic confusion: the attacker signs with HMAC, using the public key as the secret."""
    import hashlib
    import hmac

    from cryptography.hazmat.primitives import serialization

    pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    head = b64(json.dumps({"alg": "HS256", "kid": "k1"}).encode())
    body = b64(json.dumps({"iss": proxyauth.issuer(TEAM), "aud": [AUD], "exp": NOW + 3600, "iat": NOW}).encode())
    sig = b64(hmac.new(pem, f"{head}.{body}".encode(), hashlib.sha256).digest())
    assert verifier().verify(f"{head}.{body}.{sig}", TEAM, AUD) is None


def test_a_wrong_type_is_refused():
    token = jwt()
    head, body, sig = token.split(".")
    assert verifier().verify(f"{b64(json.dumps({'alg': 'RS256', 'kid': 'k1', 'typ': 'at+jwt'}).encode())}.{body}.{sig}", TEAM, AUD) is None


def test_a_signature_from_another_key_is_refused():
    assert verifier().check(jwt(key=OTHER), TEAM, AUD) == (None, "signature")


def test_a_tampered_payload_is_refused():
    head, body, sig = jwt().split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    claims["email"] = "attacker@example.test"
    assert verifier().verify(f"{head}.{b64(json.dumps(claims).encode())}.{sig}", TEAM, AUD) is None


@pytest.mark.parametrize("junk", ["", "a.b", "a.b.c.d", "....", "x" * 9000, "not a jwt", "e30.e30.e30", "!!!.!!!.!!!"])
def test_junk_is_not_a_token(junk):
    assert verifier().verify(junk, TEAM, AUD) is None


# ---- the team's keys


def test_an_unknown_kid_refetches_once_and_then_finds_it():
    certs = Certs(jwks(old=OTHER))
    v = verifier(certs)
    assert v.verify(jwt(), TEAM, AUD) is None, "k1 is not among the keys yet"
    certs.doc = jwks(k1=KEY)
    v.clock.t += proxyauth.REFETCH_COOLDOWN_S + 1
    assert v.verify(jwt(), TEAM, AUD)
    assert len(certs.calls) == 2


def test_unknown_kids_cannot_make_the_server_fetch_in_a_loop():
    certs = Certs()
    v = verifier(certs)
    for i in range(50):
        assert v.verify(jwt(kid=f"unknown-{i}"), TEAM, AUD) is None
    assert len(certs.calls) == 1, "within the cooldown, one fetch serves them all"


def test_a_failed_fetch_fails_closed():
    v = verifier(Certs(fail=True))
    assert v.check(jwt(), TEAM, AUD) == (None, "no-key")


def test_keys_that_cannot_be_refreshed_keep_verifying_for_a_bounded_time():
    certs, clock = Certs(), Clock()
    v = verifier(certs, clock)
    assert v.verify(jwt(), TEAM, AUD)
    certs.fail = True
    clock.t += proxyauth.JWKS_TTL_S + 100
    fresh = dict(iat=clock.t - 5, nbf=clock.t - 5, exp=clock.t + 3600)
    assert v.verify(jwt(**fresh), TEAM, AUD), "an outage does not lock the owner out of a key we hold"
    clock.t += proxyauth.JWKS_STALE_MAX_S
    fresh = dict(iat=clock.t - 5, nbf=clock.t - 5, exp=clock.t + 3600)
    assert v.verify(jwt(**fresh), TEAM, AUD) is None, "and not for ever"


def test_the_keys_are_asked_for_again_after_an_hour():
    certs, clock = Certs(), Clock()
    v = verifier(certs, clock)
    v.verify(jwt(), TEAM, AUD)
    clock.t += proxyauth.JWKS_TTL_S + 1
    v.verify(jwt(iat=clock.t - 5, nbf=clock.t - 5, exp=clock.t + 3600), TEAM, AUD)
    assert len(certs.calls) == 2


def test_the_keys_survive_a_restart_through_the_saved_copy():
    saved = {}
    certs = Certs()
    first = verifier(certs, save=lambda team, at, doc: saved.update({team: (at, doc)}))
    assert first.verify(jwt(), TEAM, AUD)
    down = Certs(fail=True)
    second = verifier(down, load=lambda team: saved.get(team))
    assert second.verify(jwt(), TEAM, AUD), "a fresh process, the certs endpoint unreachable"


def test_weak_or_odd_keys_are_not_used():
    small = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    doc = jwks(small1=small)
    assert proxyauth.parse_jwks(doc) == {}
    odd = jwk(KEY, "odd")
    odd["e"] = b64((1 << 70).to_bytes(9, "big"))
    assert proxyauth.parse_jwks({"keys": [odd, {"kty": "EC", "kid": "ec"}, "junk", {"kty": "RSA"}]}) == {}
    assert set(proxyauth.parse_jwks(jwks(k1=KEY))) == {"k1"}


# ---- verdicts


def test_a_verdict_is_remembered_for_a_minute_and_no_longer():
    certs, clock = Certs(), Clock()
    v = verifier(certs, clock)
    token = jwt()
    assert v.verify(token, TEAM, AUD) and len(certs.calls) == 1
    certs.fail = True
    assert v.cached(token, TEAM, AUD)[0] and v.verify(token, TEAM, AUD) and len(certs.calls) == 1, "no network, no signature work"
    clock.t += proxyauth.VERDICT_TTL_S + 1
    assert v.cached(token, TEAM, AUD) is None


def test_a_refusal_is_remembered_too():
    certs = Certs()
    v = verifier(certs)
    bad = jwt(key=OTHER)
    v.verify(bad, TEAM, AUD)
    v.verify(bad, TEAM, AUD)
    assert v.cached(bad, TEAM, AUD) == (None, "signature") and len(certs.calls) == 1


def test_the_verdict_is_per_team_and_audience():
    v = verifier()
    token = jwt()
    assert v.verify(token, TEAM, AUD)
    assert v.cached(token, TEAM, "another-aud") is None and v.cached(token, "another-team", AUD) is None
    assert v.verify(token, TEAM, "another-aud") is None


def test_the_verdict_cache_is_bounded():
    v = verifier()
    for i in range(proxyauth.VERDICT_MAX + 50):
        v.check(f"junk{i}", TEAM, AUD)
    assert len(v.verdicts) <= proxyauth.VERDICT_MAX


# ---- the team: it becomes part of a URL this server fetches


@pytest.mark.parametrize(
    "raw", ["example-team", "Example-Team", "example-team.cloudflareaccess.com", "https://example-team.cloudflareaccess.com", "https://example-team.cloudflareaccess.com/", " example-team ", "xn--abc"]
)
def test_a_team_is_read_from_the_spellings_an_owner_pastes(raw):
    assert proxyauth.normalize_team(raw) in (raw.strip().lower().replace(".cloudflareaccess.com", "").replace("https://", "").rstrip("/"),)
    assert proxyauth.valid_team(proxyauth.normalize_team(raw))


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "http://example-team.cloudflareaccess.com",
        "evil.test",
        "a.b",
        "a/b",
        "a@evil.test",
        "a:80",
        "a b",
        "-a",
        "a-",
        "a#b",
        "a?b",
        "a\\b",
        "example-team.cloudflareaccess.com.evil.test",
        "https://evil.test/example-team.cloudflareaccess.com",
        "https://a.b.cloudflareaccess.com",
        "a" * 64,
        "ａ",
        "a%2eb",
        None,
        5,
        "ftp://example-team",
        "https://user@example-team.cloudflareaccess.com",
    ],
)
def test_anything_else_is_not_a_team(raw):
    assert proxyauth.normalize_team(raw) is None


def test_no_url_is_built_for_a_bad_team():
    for name in ("evil.test/x", "", "a.b", None):
        assert not proxyauth.valid_team(name)
        with pytest.raises(ValueError):
            proxyauth.certs_url(name)
    certs = Certs()
    assert verifier(certs).verify(jwt(), "evil.test/x", AUD) is None and certs.calls == []
    assert proxyauth.certs_url("example-team") == "https://example-team.cloudflareaccess.com/cdn-cgi/access/certs"


def test_pinning_learns_only_the_audience_from_a_token_of_the_configured_team():
    v = verifier()
    aud, claims = v.pin(jwt(aud=[AUD, "second"]), TEAM)
    assert aud == AUD and claims["email"] == "owner@example.test"
    assert v.pin(jwt(key=OTHER), TEAM) is None, "signed by someone else"
    assert v.pin(jwt(iss="https://evil-team.cloudflareaccess.com"), TEAM) is None, "another team's token, even with valid certs"
    assert v.pin(jwt(aud=[]), TEAM) is None and v.pin("nonsense", TEAM) is None and v.pin(jwt(exp=NOW - 3600), TEAM) is None


def test_a_token_from_another_team_with_its_own_valid_certs_pins_nothing():
    """Anyone can run a team and sign a good token. It is refused because the team is not the one configured."""
    mine, theirs = "example-team", "attacker-team"
    attacker = proxyauth.Verifier(fetch=lambda team: jwks(k1=OTHER) if team == theirs else jwks(k1=KEY), clock=Clock())
    forged = jwt(key=OTHER, iss=proxyauth.issuer(theirs))
    assert attacker.pin(forged, theirs) is not None, "it is a good token of its own team"
    assert attacker.pin(forged, mine) is None, "and worth nothing against ours"


# ---- who may present it


def test_by_default_a_private_loopback_or_tailnet_peer_may():
    for peer in ("127.0.0.1", "::1", "10.1.2.3", "172.18.0.5", "192.168.1.40", "100.101.102.103", "::ffff:192.168.1.40", "fd00::1"):
        assert proxyauth.peer_trusted(peer, []), peer
    for peer in ("93.184.216.34", "8.8.8.8", "2606:4700::1", "::ffff:93.184.216.34", "", None, "garbage"):
        assert not proxyauth.peer_trusted(peer, []), peer


def test_a_list_of_networks_narrows_it():
    nets = proxyauth.parse_proxies("172.18.0.0/16, 192.168.1.5, nonsense,")
    assert len(nets) == 2
    assert proxyauth.peer_trusted("172.18.0.9", nets) and proxyauth.peer_trusted("192.168.1.5", nets) and proxyauth.peer_trusted("::ffff:172.18.0.9", nets)
    assert not proxyauth.peer_trusted("192.168.1.40", nets) and not proxyauth.peer_trusted("127.0.0.1", nets)


def test_an_allowlist_of_people_when_set():
    emails = proxyauth.parse_emails(" Owner@Example.test, ,guest@example.test")
    assert emails == {"owner@example.test", "guest@example.test"}
    assert proxyauth.allowed_identity({"email": "OWNER@example.test"}, emails)
    assert not proxyauth.allowed_identity({"email": "stranger@example.test"}, emails)
    assert not proxyauth.allowed_identity({"common_name": "service"}, emails)
    assert proxyauth.allowed_identity({"email": "anyone@example.test"}, set()) and proxyauth.allowed_identity({"common_name": "svc"}, set())


def test_identity_is_the_email_or_a_service_tokens_name():
    assert proxyauth.identity({"email": "a@example.test", "sub": "u"}) == "a@example.test"
    assert proxyauth.identity({"common_name": "svc.access"}) == "svc.access"
    assert proxyauth.identity({}) == "someone"
