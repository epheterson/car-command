"""Signing in through Cloudflare Access: checking the JWT it puts on every request.

Cloudflare Access authenticates the person at its edge and forwards the request with an RS256
JWT in `Cf-Access-Jwt-Assertion`. Anything else on the request, such as the e-mail header beside
it, can be written by whoever reaches the port, so only the signature counts.

The contract, which follows the one Zimi's SSO keeps:

1. Off unless the team and the application's audience tag are both pinned.
2. The header is read only from a trusted peer: the direct socket address, never a forwarded one.
   By default any private, loopback or tailnet address; narrowed by a list of networks.
3. A header from a trusted peer that does not verify is a refusal, never a fall-through to another
   way in. A header from anywhere else is ignored as if it had not been sent.
4. RS256 only, checked before any key is looked up: never none, never HS*.
5. The team's keys are kept in memory and in the database, refreshed hourly, and keep verifying
   for a bounded time when a refresh fails. A verdict is remembered for a minute at most, so a
   change at the edge takes effect promptly.

The team never comes from a request. It is configured (CF_ACCESS_TEAM, or typed into Settings by
someone signed in), because anyone can run their own team and sign a valid token with it."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import threading
import time
from typing import Callable

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

HEADER = "cf-access-jwt-assertion"
LEEWAY_S = 60
VERDICT_TTL_S = 60  # a verdict, good or bad, is reused this long: a page load's worth of requests, no more
VERDICT_MAX = 512
JWKS_TTL_S = 3600
JWKS_STALE_MAX_S = 7 * 24 * 3600  # keys that cannot be refreshed keep verifying this long, then tokens are refused
REFETCH_COOLDOWN_S = 60  # between fetches, whatever is asked, so unknown key ids cannot turn this into a request amplifier
JWKS_MAX_BYTES = 256 * 1024
FETCH_TIMEOUT_S = 5.0
JWT_MAX = 8192
MIN_RSA_BITS = 2048
MAX_RSA_EXPONENT = 1 << 64
# A team name becomes part of a URL fetched from this server, so it is held to a host label: lowercase
# letters, digits and hyphens, not starting or ending with a hyphen. Nothing that could name another host.
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
SERVICE_SUFFIX = ".cloudflareaccess.com"
TAILNET = ipaddress.ip_network("100.64.0.0/10")


def normalize_team(raw: object) -> str | None:
    """The team's name from any spelling an owner plausibly pastes: `name`, `name.cloudflareaccess.com`,
    or `https://name.cloudflareaccess.com/`. Anything else, including http and another host, is None."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower().rstrip("/")
    if text.startswith("https://"):
        text = text[len("https://") :]
    elif "://" in text:
        return None
    if text.endswith(SERVICE_SUFFIX):
        text = text[: -len(SERVICE_SUFFIX)]
    return text if LABEL.fullmatch(text) else None


def valid_team(name: object) -> bool:
    return isinstance(name, str) and bool(LABEL.fullmatch(name))


def issuer(team: str) -> str:
    return f"https://{team}{SERVICE_SUFFIX}"


def certs_url(team: str) -> str:
    if not valid_team(team):
        raise ValueError("not a team name")
    return f"{issuer(team)}/cdn-cgi/access/certs"


# ---- who may present the header


def parse_proxies(text: str | None) -> list:
    """The networks the header may arrive from, from a comma-separated list; invalid entries are dropped."""
    nets = []
    for part in (text or "").split(","):
        try:
            if part.strip():
                nets.append(ipaddress.ip_network(part.strip(), strict=False))
        except ValueError:
            continue
    return nets


def peer_trusted(peer: str | None, nets: list) -> bool:
    """Whether the socket's own address may assert the header: inside the listed networks, or with none
    listed, any private, loopback or tailnet address (cloudflared beside the site)."""
    try:
        ip = ipaddress.ip_address(peer or "")
    except ValueError:
        return False
    ip = getattr(ip, "ipv4_mapped", None) or ip  # a dual-stack socket shows an IPv4 peer as ::ffff:a.b.c.d
    if nets:
        return any(ip in net for net in nets)
    return ip.is_private or ip.is_loopback or (ip.version == 4 and ip in TAILNET)


# ---- the token


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _parse(token: str):
    """(header, claims, signing input, signature) of a JWT, or None. Nothing is trusted yet."""
    if not isinstance(token, str) or len(token) > JWT_MAX or token.count(".") != 2:
        return None
    head, body, sig = token.split(".")
    try:
        header, claims = json.loads(_unb64(head)), json.loads(_unb64(body))
        signature = _unb64(sig)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        return None
    return header, claims, f"{head}.{body}".encode(), signature


def unverified_claims(token: str) -> dict | None:
    """What a JWT says, unchecked: only for deciding whether to show a button, never to sign anyone in."""
    parsed = _parse(token)
    return parsed[1] if parsed else None


def first_audience(claims: dict) -> str | None:
    aud = claims.get("aud")
    aud = aud[0] if isinstance(aud, list) and aud else aud
    return aud if isinstance(aud, str) and aud else None


def identity(claims: dict) -> str:
    """Who the token is for: the person's e-mail, or a service token's common name."""
    for key in ("email", "common_name", "sub"):
        if isinstance(claims.get(key), str) and claims[key]:
            return claims[key]
    return "someone"


def subject(claims: dict) -> str | None:
    """What a form token is tied to: the person's e-mail, else a service token's name, else the token's own
    subject, with the kind in front so one kind's value cannot stand in for another's."""
    for kind, key in (("email", "email"), ("cn", "common_name"), ("sub", "sub")):
        if isinstance(claims.get(key), str) and claims[key]:
            return f"{kind}:{claims[key]}"
    return None


def parse_jwks(doc: object) -> dict[str, rsa.RSAPublicKey]:
    """The usable RSA keys of a certs document by kid: no short keys, no odd exponents."""
    keys = {}
    for jwk in doc.get("keys", []) if isinstance(doc, dict) else []:
        try:
            if jwk.get("kty") != "RSA" or not isinstance(jwk.get("kid"), str):
                continue
            n, e = int.from_bytes(_unb64(jwk["n"]), "big"), int.from_bytes(_unb64(jwk["e"]), "big")
            if n.bit_length() < MIN_RSA_BITS or not 3 <= e <= MAX_RSA_EXPONENT:
                continue
            keys[jwk["kid"]] = rsa.RSAPublicNumbers(e, n).public_key()
        except (AttributeError, KeyError, ValueError, TypeError):
            continue
    return keys


def fetch_jwks(team: str) -> dict:
    """The team's certs document. Raises on any failure."""
    r = httpx.get(certs_url(team), timeout=FETCH_TIMEOUT_S, follow_redirects=False)
    r.raise_for_status()
    if len(r.content) > JWKS_MAX_BYTES:
        raise ValueError("certs answer too large")
    return r.json()


class Verifier:
    """Checks Access JWTs for a team and audience. Keys are fetched lazily, kept in memory and (through
    `load` and `save`) in the database; a verdict is remembered for a minute, so a request is a dict lookup."""

    def __init__(self, fetch: Callable[[str], dict] = fetch_jwks, clock: Callable[[], float] = time.time, load: Callable | None = None, save: Callable | None = None):
        self.fetch, self.clock, self.load, self.save = fetch, clock, load, save
        self.keys: dict[str, tuple[float, dict]] = {}  # team -> (when fetched, kid -> key)
        self.tried: dict[str, float] = {}  # team -> when a fetch was last attempted
        self.verdicts: dict[str, tuple[float, dict | None, str | None]] = {}
        self.lock = threading.Lock()

    def clear(self) -> None:
        with self.lock:
            self.keys.clear(), self.tried.clear(), self.verdicts.clear()

    # keys

    def _usable(self, entry, now):
        return entry if entry and now - entry[0] < JWKS_STALE_MAX_S else None

    def _key(self, team: str, kid: str, now: float):
        with self.lock:
            entry = self.keys.get(team)
            if entry is None and self.load:
                try:
                    loaded = self.load(team)
                except Exception:  # noqa: BLE001 - a missing copy is the same as none
                    loaded = None
                if loaded:
                    entry = self.keys[team] = (loaded[0], parse_jwks(loaded[1]))
            fresh = entry is not None and now - entry[0] < JWKS_TTL_S
            if not (fresh and kid in entry[1]) and now - self.tried.get(team, -REFETCH_COOLDOWN_S) >= REFETCH_COOLDOWN_S:
                self.tried[team] = now
                try:
                    doc = self.fetch(team)
                    keys = parse_jwks(doc)
                    if keys:
                        entry = self.keys[team] = (now, keys)
                        if self.save:
                            self.save(team, now, doc)
                except Exception:  # noqa: BLE001 - whatever we held stays, for as long as it may
                    pass
            entry = self._usable(entry, now)
            return entry[1].get(kid) if entry else None

    # tokens

    def check(self, token: str, team: str, aud: str | None, pin: bool = False):
        """(claims, None) for a good token, else (None, reason). With `pin`, the audience is not
        checked against anything: the first one the token names is the answer (see pin)."""
        if not valid_team(team) or (not aud and not pin):
            return None, "unconfigured"
        now = self.clock()
        cache_key = hashlib.sha256("\0".join((team, aud or "", str(pin), token)).encode("utf-8", "replace")).hexdigest()
        hit = self._live(self.verdicts.get(cache_key), now)
        if hit:
            return hit[1], hit[2]
        claims, reason = self._check(token, team, aud, pin, now)
        if reason == "no-key":
            return claims, reason  # a missing key may be here a moment later: never remembered
        with self.lock:
            if len(self.verdicts) >= VERDICT_MAX:
                self.verdicts.clear()
            self.verdicts[cache_key] = (now + VERDICT_TTL_S, claims, reason)
        return claims, reason

    @staticmethod
    def _live(hit, now):
        """A remembered verdict, if it is still within its minute and a good token has not itself expired."""
        if not hit or hit[0] <= now:
            return None
        claims = hit[1]
        if claims is not None and float(claims.get("exp", 0)) + LEEWAY_S <= now:
            return None
        return hit

    def cached(self, token: str, team: str, aud: str) -> tuple | None:
        """The verdict already reached for this token, with no work and no network, or None."""
        cache_key = hashlib.sha256("\0".join((team, aud or "", "False", token)).encode("utf-8", "replace")).hexdigest()
        hit = self._live(self.verdicts.get(cache_key), self.clock())
        return (hit[1], hit[2]) if hit else None

    def verify(self, token: str, team: str, aud: str) -> dict | None:
        return self.check(token, team, aud)[0]

    def _check(self, token: str, team: str, aud: str | None, pin: bool, now: float):
        parsed = _parse(token)
        if parsed is None:
            return None, "malformed"
        header, claims, signing_input, signature = parsed
        if header.get("alg") != "RS256":  # before any key is looked up: none and HS* never get near one
            return None, f"alg:{header.get('alg')}"
        typ = header.get("typ")
        if typ is not None and (not isinstance(typ, str) or typ.upper() != "JWT"):
            return None, "typ"
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            return None, "kid-missing"
        key = self._key(team, kid, now)
        if key is None:
            return None, "no-key"
        try:
            key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError):
            return None, "signature"
        if claims.get("iss") != issuer(team):
            return None, "iss"
        audiences = claims.get("aud") if isinstance(claims.get("aud"), list) else [claims.get("aud")]
        if not pin and aud not in audiences:
            return None, "aud"

        def number(name):
            v = claims.get(name)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        exp, iat, nbf = number("exp"), number("iat"), number("nbf")
        if exp is None or exp + LEEWAY_S <= now:
            return None, "exp"
        if iat is None or iat > now + LEEWAY_S:
            return None, "iat"
        if nbf is not None and nbf > now + LEEWAY_S:
            return None, "nbf"
        return claims, None

    def pin(self, token: str, team: str) -> tuple[str, dict] | None:
        """For pinning the audience: what the token says its application is, once its signature checks
        against the already-configured team's own keys and its issuer is that team. (aud, claims) or None."""
        claims, reason = self.check(token, team, None, pin=True)
        aud = first_audience(claims) if claims else None
        return (aud, claims) if aud else None


def normalize_email(text: object) -> str:
    """An e-mail as compared: no stray spaces, no case."""
    return str(text or "").strip().lower()


def allowed_identity(claims: dict, emails: set[str]) -> bool:
    """With a list of e-mails set, only those people count; without one, the Access policy decides."""
    return not emails or normalize_email(claims.get("email")) in emails


def parse_emails(text: str | None) -> set[str]:
    return {normalize_email(e) for e in (text or "").split(",") if normalize_email(e)}
