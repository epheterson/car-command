"""Signing an app in without copying a token: the rules of the authorization-code flow with PKCE.

An app opens /connect in a browser, the owner allows it on a page of this site, and the app
trades a one-time code for a read token. Pure, so the routes in app.py only fetch, store and
answer: what a request may carry, how a redirect is built, what a code is kept as."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CODE_TTL_S = 300  # a code is for the minute after Allow, with room for a slow redirect
CLIENT_MAX = 40
STATE_MAX = 256
URI_MAX = 512
TOKEN_PATH = "/api/connect/token"  # the one API path that answers without a session or a token

# Kept at startup only (app.apply_schema), like the other tables. Only the hash of a code is
# stored, so the table cannot hand out a code it does not hold.
DDL = """
CREATE TABLE IF NOT EXISTS cc_connect_codes (
    hash         VARCHAR PRIMARY KEY,
    challenge    VARCHAR NOT NULL,
    client       VARCHAR NOT NULL,
    redirect_uri VARCHAR NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL
);
ALTER TABLE cc_connect_codes ADD COLUMN IF NOT EXISTS share_place BOOLEAN NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS cc_connect_apps (
    redirect_uri VARCHAR NOT NULL,
    client       VARCHAR NOT NULL,
    allowed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    token_hash   VARCHAR REFERENCES cc_api_tokens(hash) ON DELETE CASCADE,
    PRIMARY KEY (redirect_uri, client)
);
ALTER TABLE cc_connect_apps ADD COLUMN IF NOT EXISTS share_place BOOLEAN NOT NULL DEFAULT false;
"""

# Schemes that mean a web page or a script, never an app. An app's redirect is a scheme of its own.
REFUSED_SCHEMES = frozenset({"http", "https", "javascript", "data", "file", "blob", "vbscript", "about", "ftp", "ws", "wss", "mailto", "tel", "sms"})
# Browsers and launchers whose schemes open a web address handed to them (x-safari-https:,
# googlechromes:, microsoft-edge:, firefox://open-url?url=, intent:). A list of them can never be
# complete, so a scheme that merely names a browser or a web protocol is refused, and the
# redirect may not carry a web address or a host at all. An install that knows its apps lists
# them exactly in CC_APP_REDIRECTS and skips these rules.
HAND_OFF_WORDS = ("http", "chrome", "safari", "firefox", "edge", "opera", "brave", "browser", "search")
SCHEME = re.compile(r"[a-z][a-z0-9+.-]*")
CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")  # base64url of a SHA-256, unpadded
VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")  # RFC 7636's alphabet and length


def _plain(text: str) -> bool:
    return text.isprintable() and "\\" not in text


# The only shape an unlisted redirect may have: a scheme, then an optional // and a one-label
# authority that starts with a letter (so never a number, hex or decimal, that a browser reads as an
# IP), then a path of plain segments. No dots outside the scheme, no port, no userinfo, no
# percent-escapes, no query, no fragment: nothing a browser or launcher could read as a host or a
# web address. Anything else is for CC_APP_REDIRECTS, which lists exact addresses.
SHAPE = re.compile(r"[a-z][a-z0-9+.-]*:(?://[a-z][a-z0-9-]*)?(?:/[A-Za-z0-9_~-]+)*/?", re.ASCII)


def off_shape(uri: str, scheme: str) -> bool:
    """Whether this redirect falls outside the one shape an unlisted address may have, or names
    a scheme that is a browser's, a web protocol or `intent`."""
    return not SHAPE.fullmatch(uri) or scheme == "intent" or any(word in scheme for word in HAND_OFF_WORDS)


def redirect_problem(uri: str, exact: bool = False) -> str | None:
    """Why this cannot be an app's redirect, or None. A custom scheme only, with something after
    it, and no fragment (the answer is appended to the query). `exact`: the install listed this
    very address, which then skips the hand-off rules, but never admits http or https."""
    if not uri or len(uri) > URI_MAX or not _plain(uri) or " " in uri:
        return "The redirect address is not valid."
    parts = urlsplit(uri)
    scheme = parts.scheme
    if not scheme or not SCHEME.fullmatch(scheme):
        return "The redirect address has no app scheme."
    if scheme in REFUSED_SCHEMES:
        return "The redirect address must be an app's own scheme, not a web address."
    if not (parts.netloc or parts.path or parts.query) or parts.fragment or "#" in uri:
        return "The redirect address is not valid."
    if not exact and off_shape(uri, scheme):
        return "The redirect address must be the app's own plain callback, such as example-app://connected."
    return None


def allowlist(text: str | None) -> frozenset[str]:
    """CC_APP_REDIRECTS: exact redirect addresses, comma-separated. Empty means any app scheme."""
    return frozenset(x.strip() for x in (text or "").split(",") if x.strip())


def check_request(params, allowed: frozenset[str] = frozenset()) -> tuple[dict | None, str | None]:
    """The request's parameters cleaned, or the plain sentence that says what is wrong. Anything
    wrong is shown on a page and never redirected to: the redirect address is the thing in doubt."""
    client = (params.get("client") or "").strip()
    if not client or len(client) > CLIENT_MAX or not client.isprintable():
        return None, "The app's name is missing or too long."
    uri = params.get("redirect_uri") or ""
    problem = redirect_problem(uri, exact=uri in allowed)
    if problem:
        return None, problem
    if allowed and uri not in allowed:
        return None, "This install does not allow that redirect address."
    state = params.get("state") or ""
    if not state or len(state) > STATE_MAX or not state.isascii() or not state.isprintable():
        return None, "The request has no state."
    challenge = params.get("code_challenge") or ""
    if params.get("code_challenge_method") != "S256" or not CHALLENGE.fullmatch(challenge):
        return None, "The request is missing its S256 code challenge."
    return {"client": client, "redirect_uri": uri, "state": state, "code_challenge": challenge}, None


def redirect_to(uri: str, **extra: str) -> str:
    """The redirect address with these parameters added, any query it already has kept."""
    parts = urlsplit(uri)
    query = urlencode(parse_qsl(parts.query, keep_blank_values=True) + list(extra.items()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def form_action(uri: str) -> str:
    """The form-action source that lets the consent page's answer reach the app: browsers hold a
    redirect after a form post to the page's form-action, and the page allows only this site."""
    return f"{urlsplit(uri).scheme}:"


def new_code() -> str:
    return secrets.token_urlsafe(32)


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def challenge_of(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def pkce_ok(verifier: object, challenge: str) -> bool:
    return isinstance(verifier, str) and bool(VERIFIER.fullmatch(verifier)) and hmac.compare_digest(challenge_of(verifier), challenge)


def token_label(client: str, device: object) -> str:
    """What Settings lists the token as: the app, and the device it says it is on."""
    device = device.strip()[:CLIENT_MAX] if isinstance(device, str) and device.isprintable() else ""
    return f"{client} · {device}" if device else client
