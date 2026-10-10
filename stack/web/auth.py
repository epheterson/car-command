"""Single-user sign-in: a password, a signed session cookie, and a CSRF token bound to it.

Everything here is stdlib and pure, so the decisions the middleware makes are testable
without a database, a browser or a running app. The app owns the cookies; this module
owns the cryptography and the rule for what an unauthenticated request may reach.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import time

SESSION_COOKIE = "cc_session"
CSRF_FIELD = "csrf_token"
SESSION_MAX_AGE = 30 * 24 * 3600  # a month; a browser kept open should not sign out weekly
MIN_PASSWORD = 8

# Reachable without a session: the healthcheck a container runs, the assets a login page
# needs, and the pages that exist to get you a session in the first place.
# The Tesla key path: Tesla fetches the app's public key with no session, and a login page there fails pairing.
OPEN_PATHS = ("/health", "/favicon.ico", "/login", "/setup", "/manifest.webmanifest", "/.well-known/appspecific/com.tesla.3p.public-key.pem")
OPEN_PREFIXES = ("/static/",)


# Credentials live in their own table rather than in cc_settings, so the password hash
# and the signing key can never ride along in the settings dict a page renders from.
DDL = """
CREATE TABLE IF NOT EXISTS cc_auth (
    key   VARCHAR PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# The API's tables, at startup only (app.apply_schema): store_get runs DDL above on every
# call, and an ALTER there would take its lock each time. A token made before kinds existed
# reads data and nothing more, which is what the column's default says.
API_DDL = """
CREATE TABLE IF NOT EXISTS cc_api_tokens (
    hash         VARCHAR PRIMARY KEY,
    label        VARCHAR NOT NULL,
    vin          VARCHAR,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ
);
ALTER TABLE cc_api_tokens ADD COLUMN IF NOT EXISTS kind VARCHAR NOT NULL DEFAULT 'read';
ALTER TABLE cc_api_tokens ADD COLUMN IF NOT EXISTS last4 VARCHAR;
ALTER TABLE cc_api_tokens ADD COLUMN IF NOT EXISTS share_place BOOLEAN NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS cc_api_log (
    id         BIGSERIAL PRIMARY KEY,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    label      VARCHAR NOT NULL,
    last4      VARCHAR,
    method     VARCHAR NOT NULL,
    path       VARCHAR NOT NULL,
    status     INTEGER NOT NULL,
    client_ip  VARCHAR
);
CREATE INDEX IF NOT EXISTS cc_api_log_at ON cc_api_log (at);
"""


def store_get(conn, key: str) -> str | None:
    conn.execute(DDL)
    row = conn.execute("SELECT value FROM cc_auth WHERE key = %s", (key,)).fetchone()
    if row is None:
        return None
    return row["value"] if isinstance(row, dict) else row[0]


def store_set(conn, key: str, value: str) -> None:
    conn.execute(DDL)
    conn.execute(
        "INSERT INTO cc_auth (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )
    conn.commit()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---- passwords -------------------------------------------------------------


def hash_password(password: str, *, salt: bytes | None = None, n: int = 2**14) -> str:
    """scrypt, cost parameters in the string so a future default can be raised without
    invalidating a stored hash. n=16384 is ~50 ms here: slow for a guesser, invisible on a login.
    """
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=8, p=1, dklen=32)
    return f"scrypt${n}$8$1${_b64(salt)}${_b64(digest)}"


def verify_password(stored: str | None, password: str) -> bool:
    if not stored or not password:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        want = hashlib.scrypt(
            password.encode(),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_unb64(digest)),
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(want, _unb64(digest))


def password_problem(password: str, confirm: str | None = None) -> str | None:
    """The message to show, or None when the password is acceptable."""
    if confirm is not None and password != confirm:
        return "Those two passwords are not the same."
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    return None


# ---- signed values ---------------------------------------------------------


def new_secret() -> str:
    return _b64(secrets.token_bytes(32))


def sign(secret: str, payload: str, *, now: float | None = None) -> str:
    """payload.issued_at.signature — stateless, so restarts and a second worker keep sessions."""
    body = f"{payload}.{int(now or time.time())}"
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def unsign(
    secret: str,
    token: str | None,
    *,
    max_age: int = SESSION_MAX_AGE,
    now: float | None = None,
) -> str | None:
    """The payload when the signature holds and the token is young enough, else None."""
    if not token:
        return None
    try:
        payload, issued, mac = token.rsplit(".", 2)
        want = hmac.new(secret.encode(), f"{payload}.{issued}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(want, _unb64(mac)):
            return None
        if (now or time.time()) - int(issued) > max_age:
            return None
    except (ValueError, TypeError):
        return None
    return payload


def new_session(secret: str) -> str:
    # Random payload, so two sign-ins in the same second are still two distinct cookies:
    # without it the CSRF token below would be bound to the clock rather than to a session.
    return sign(secret, "u" + _b64(secrets.token_bytes(9)))


def valid_session(secret: str, cookie: str | None) -> bool:
    return unsign(secret, cookie) is not None


# ---- CSRF ------------------------------------------------------------------


def csrf_token(secret: str, session_cookie: str | None) -> str:
    """Derived from the session cookie, so a token is only mintable by whoever holds that
    cookie. With sign-in switched off there is no cookie and the token falls back to the
    secret alone; SameSite=Lax on the session is what carries the cross-site case there.
    """
    return _b64(hmac.new(secret.encode(), (session_cookie or "anon").encode(), hashlib.sha256).digest())


def csrf_ok(secret: str, session_cookie: str | None, submitted: str | None) -> bool:
    return bool(submitted) and hmac.compare_digest(csrf_token(secret, session_cookie), submitted)


# ---- throttling failed sign-ins --------------------------------------------
# scrypt costs a guesser 50 ms, which over a night is a hundred thousand tries. This
# is the actual brake. It is in memory on purpose: a restart forgiving a lockout is a
# fair trade for not writing a row per guess, and there is one process.
LOCKOUT_AFTER = 5
LOCKOUT_CEILING = 900.0
TRACKED_KEYS = 4096


def lockout_seconds(failures: int) -> float:
    """Nothing for the first four wrong guesses in a row, then doubling from five
    seconds to a quarter-hour ceiling. A person who fat-fingers it twice notices nothing."""
    if failures < LOCKOUT_AFTER:
        return 0.0
    return min(LOCKOUT_CEILING, 5.0 * 2 ** (failures - LOCKOUT_AFTER))


class Failures:
    """Consecutive failures per client, and across all of them.

    The global count matters because the deployment trusts X-Forwarded-For from anything
    that can reach the port, so a guesser can present a fresh address on every try and
    never accumulate a per-client count. The per-client count is the one that gives a
    real person a useful message; the global one is the one that actually holds a door."""

    def __init__(self, tracked: int = TRACKED_KEYS, global_brake: bool = True):
        self.tracked = tracked
        self.global_brake = global_brake  # off where there is nothing a stranger could lock the owner out of
        self.by_key: dict[str, tuple[int, float]] = {}
        self.total = 0
        self.last = 0.0

    def record(self, key: str, now: float) -> None:
        count = self.by_key.get(key, (0, 0.0))[0] + 1
        self.by_key[key] = (count, now)
        if len(self.by_key) > self.tracked:
            # Bounded, so a guesser rotating addresses cannot grow this without limit.
            for stale in sorted(self.by_key, key=lambda k: self.by_key[k][1])[: self.tracked // 4]:
                del self.by_key[stale]
        self.total, self.last = self.total + 1, now

    def clear(self, key: str) -> None:
        self.by_key.pop(key, None)
        self.total = 0

    def wait(self, key: str, now: float) -> float:
        """Seconds this client must wait, 0 when it may try."""
        held = []
        count, at = self.by_key.get(key, (0, 0.0))
        if count:
            held.append(lockout_seconds(count) - (now - at))
        if self.total and self.global_brake:
            # Ten times the per-client allowance before everyone is slowed down.
            held.append(lockout_seconds(self.total // 10) - (now - self.last))
        return max(0.0, max(held, default=0.0))


# ---- the rule --------------------------------------------------------------


def gate(
    path: str,
    method: str,
    *,
    enabled: bool,
    password_set: bool,
    signed_in: bool,
    csrf_valid: bool,
) -> str:
    """What to do with one request, as a word the middleware acts on.

    'pass'  — serve it
    'setup' — no password has been chosen yet; send them to choose one
    'login' — send them to sign in (a GET) or refuse (anything else)
    'csrf'  — a write without a good token
    """
    open_path = path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)
    write = method not in ("GET", "HEAD", "OPTIONS")
    if enabled and not password_set:
        # An unconfigured install is not a public one: everything funnels to /setup,
        # and the form that sets the password is still a write like any other.
        if path != "/setup":
            return "setup"
    elif enabled and not signed_in and not open_path:
        return "login"
    if write and not csrf_valid:
        return "csrf"
    return "pass"


# ---- API tokens --------------------------------------------------------------------
# A token is minted once, shown once, and only its hash is kept: the table cannot leak
# what it does not hold. Scoped to one car or to all of them. It stands in for the
# session cookie on /api/ paths only, so a token in a script cannot open a page.


def mint_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def bearer_of(authorization: str | None) -> str | None:
    """The raw token a request carries in its Authorization: Bearer header, or None.

    Header only, on purpose. A ?token= in the query string would sit in every access
    log line, in browser history and in any referrer, which is the same as writing the
    secret down. Anything that can make an HTTP request can set a header."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


def token_allows(scope_vin: str | None, requested_vin: str | None) -> bool:
    """A token for one car answers only for that car. `all` and no vin both mean the
    whole garage, which a scoped token must not see."""
    if scope_vin is None:
        return True
    return requested_vin == scope_vin


def client_ip(headers, fallback: str | None) -> str:
    """Who signed in, as seen through Cloudflare or a proxy: the first forwarded address, else the socket's."""
    for name in ("cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
        v = headers.get(name) if headers is not None else None
        if v:
            return v.split(",")[0].strip()[:64]
    return fallback or "?"


# ---- admin tokens ------------------------------------------------------------------
# A read token reads. An admin token can also drive setup (/api/admin/), so it is held to
# the house: it answers only to a request that started on the local network or the
# tailnet and did not come in through Cloudflare, unless the owner says otherwise.

KINDS = ("read", "admin", "status")
# What a status token may call, and nothing else: the car now, and the stream of it. Not the
# house: its power over time shows when nobody is home, and its grid state shows outages.
STATUS_PATHS = ("/api/now", "/api/stream")
# Keys that say where the car is or how it is moving, or that give its speed from two readings
# (the odometer and the trip meters); a status token never sees them.
LOCATION_KEYS = frozenset({"lat", "lon", "address", "place", "place_named", "heading", "speed_mph", "at_home", "odometer_mi", "meters"})
ADMIN_PREFIX = "/api/admin/"
LOG_KEEP_DAYS = 90
LOG_BATCH = 300

HOME_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",  # the tailnet (and carrier-grade NAT, which a home router does not hand out)
        "127.0.0.0/8",
        "::1/128",
        "fc00::/7",  # unique local; the tailnet's IPv6 addresses are in here
    )
)


def kind_of(text) -> str:
    """The posted kind: admin or status when it says exactly that, read otherwise."""
    kind = str(text or "").strip()
    return kind if kind in ("admin", "status") else "read"


def status_payload(value, share_place: bool = False):
    """What a status token is shown of a car (or a list of them): without location, and, when the owner has
    allowed it, with the two coarse facts and nothing finer. `place` is a name the owner gave a place, never a
    street (place_named is None for a place the site named itself), and `at_home` is None when the position is
    unknown rather than false."""
    if isinstance(value, list):
        return [status_payload(v, share_place) for v in value]
    if not isinstance(value, dict):
        return value
    out = without_location(value)
    if share_place and "vin" in value:
        out["place"] = value.get("place_named")
        out["at_home"] = bool(value.get("at_home")) if value.get("lat") is not None and value.get("lon") is not None else None
    return out


def without_location(value):
    """A payload with every location key removed, all the way down."""
    if isinstance(value, dict):
        return {k: without_location(v) for k, v in value.items() if k not in LOCATION_KEYS}
    if isinstance(value, list):
        return [without_location(v) for v in value]
    return value


def home_address(text: str | None) -> bool:
    """Whether an address is on the local network, the tailnet or this machine."""
    try:
        ip = ipaddress.ip_address(str(text or "").strip().strip("[]"))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in HOME_NETS)


# ---- the first password ------------------------------------------------------------
# Until a password exists, whoever reaches /setup chooses it. So it answers only to a
# socket on the home network (link-local too: a laptop on a cable to the box), judged by
# the connection's own address and never by a forwarded header. Anyone else needs the
# setup code the site prints to its log at start, which only the box's owner can read.

# The first password without the code: the machine itself, or a device on the home network that reached the site
# directly, in the first hour after the site first started (the rule Zimi settled on, with a time bound for the
# proxies that add no header). A Docker bridge (172.16/12), the tailnet (100.64/10, Tailscale's fd7a:115c:a1e0::/48),
# link-local, anything a proxy passed on, and anything public still need the code.
FIRST_RUN_LAN_NETS = (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("192.168.0.0/16"), ipaddress.ip_network("fc00::/7"))
TAILSCALE6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
FIRST_RUN_WINDOW_S = 3600
# Headers a proxy adds, and any header that starts with x-forwarded-. Any of them means the socket's address is the
# proxy's, not the client's.
PROXY_HEADERS = frozenset(
    (
        "x-real-ip",
        "x-client-ip",
        "x-cluster-client-ip",
        "x-original-forwarded-for",
        "forwarded",
        "via",
        "cf-connecting-ip",
        "cf-connecting-ipv6",
        "cf-ray",
        "true-client-ip",
        "fastly-client-ip",
        "x-envoy-external-address",
        "tailscale-user-login",
        "tailscale-user-name",
        "tailscale-funnel-request",
    )
)


def proxied(headers) -> bool:
    """Whether anything on the request says a proxy passed it on."""
    return any(name in PROXY_HEADERS or name.startswith("x-forwarded-") for name in (str(k).lower() for k in headers))


# ---- the container's own networks (the split Zimi's netguard keeps)
# A neighbouring container is the one device that can sit inside a home-looking range without being the owner:
# Docker Desktop hands compose networks 192.168.x. These are the subnets such a neighbour comes from.
# The daemon's bridges and virtual networks, seen from the host or from a host-network container: Docker, Podman, libvirt,
# LXD, CNI, flannel, calico, vxlan.
_BRIDGE_IFACES = ("docker", "br-", "virbr", "lxdbr", "cni", "podman", "flannel", "cali", "vxlan")
_PROC_ROUTE = "/proc/net/route"
_PROC_INET6 = "/proc/net/if_inet6"
_CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")
_CONTAINER_WORDS = ("containerd", "kubepods", "docker", "libpod", "lxc")
_own_networks: tuple | None = None


def _ipv4_routes(path: str | None = None) -> list:
    """(interface, network) for each connected IPv4 route in /proc/net/route: the subnets this machine sits on.
    The hex fields are little-endian."""
    out = []
    try:
        with open(path or _PROC_ROUTE, encoding="utf-8") as fh:
            rows = fh.read().splitlines()[1:]
    except OSError:
        return out
    for row in rows:
        parts = row.split()
        if len(parts) < 8 or parts[2] != "00000000":  # a gateway: not connected
            continue
        try:
            dest = ipaddress.IPv4Address(bytes.fromhex(parts[1])[::-1])
            mask = ipaddress.IPv4Address(bytes.fromhex(parts[7])[::-1])
            net = ipaddress.ip_network(f"{dest}/{mask}", strict=False)
        except ValueError:
            continue
        if net.prefixlen:  # the default route is no subnet
            out.append((parts[0], net))
    return out


def _ipv6_addrs(path: str | None = None) -> list:
    """(interface, network) for each global IPv6 address in /proc/net/if_inet6."""
    out = []
    try:
        with open(path or _PROC_INET6, encoding="utf-8") as fh:
            rows = fh.read().splitlines()
    except OSError:
        return out
    for row in rows:
        parts = row.split()
        if len(parts) < 6:
            continue
        try:
            addr = ipaddress.IPv6Address(bytes.fromhex(parts[0]))
            net = ipaddress.ip_network(f"{addr}/{int(parts[2], 16)}", strict=False)
        except ValueError:
            continue
        if not (addr.is_loopback or addr.is_link_local):
            out.append((parts[5], net))
    return out


def looks_like_container(env, cgroup: str, mountinfo: str, markers: bool) -> bool:
    """Whether this process runs in a container: a marker file, Kubernetes' environment, or a cgroup or mount that names
    containerd, kubepods, docker, libpod or lxc (a runtime that leaves no marker file)."""
    return bool(markers or env.get("KUBERNETES_SERVICE_HOST") or any(word in text for text in (cgroup, mountinfo) for word in _CONTAINER_WORDS))


def _in_container() -> bool:
    def read(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    return looks_like_container(os.environ, read("/proc/1/cgroup"), read("/proc/self/mountinfo"), any(os.path.exists(p) for p in _CONTAINER_MARKERS))


def own_networks(routes=None, addrs=None, in_container=None) -> tuple:
    """The container networks this server is attached to, read once.

    The daemon's bridges (docker0, br-*) wherever it runs. Inside a bridge-network container, which sees no bridge of
    its own, every subnet it is attached to, gateway included: there a published port's connection arrives from the
    gateway (Docker Desktop), and a neighbour from the same subnet. A host-network container sees the host's interfaces,
    its LAN among them, and so only its bridges count, as on the host. Linux only; elsewhere there is nothing to read."""
    global _own_networks
    given = routes is not None or addrs is not None or in_container is not None
    if not given and _own_networks is not None:
        return _own_networks
    pairs = (_ipv4_routes() if routes is None else routes) + (_ipv6_addrs() if addrs is None else addrs)
    if in_container is None:
        in_container = _in_container()
    bridged = [net for iface, net in pairs if iface.startswith(_BRIDGE_IFACES)]
    nets = tuple(bridged if (bridged or not in_container) else [net for _, net in pairs])
    if routes is None and addrs is None:
        _own_networks = nets
    return nets


def on_own_network(ip) -> bool:
    return any(ip.version == net.version and ip in net for net in own_networks())


def setup_keyless_why(peer: str | None, headers, own: list | None = None, first_start: float | None = None, now: float | None = None) -> str:
    """'yes' when the first password may be chosen without the setup code, 'expired' when only the first hour has
    run out for a device that would otherwise qualify, else 'no'. The request must have reached the site directly (no
    proxy header). The machine itself (loopback) and a home-network range qualify in the first hour after the
    site first started, unless it is a network the container itself sits on: on Docker Desktop every published-port
    connection arrives from the compose network's gateway, and a neighbouring container is in the same subnet, so
    there the code is always needed. The hour bounds the proxies that add no header (nginx proxy_pass by default,
    TCP proxies, ssh -R), which would otherwise make the internet look like a home device."""
    if proxied(headers) or os.environ.get("KUBERNETES_SERVICE_HOST"):
        # In Kubernetes a NodePort's SNAT makes the internet look like the node's own address: no network is home.
        return "no"
    try:
        ip = ipaddress.ip_address(str(peer or "").strip().strip("[]"))
    except ValueError:
        return "no"
    ip = getattr(ip, "ipv4_mapped", None) or ip
    # Loopback is the window's too: a proxy that adds no header (nginx's default, socat, ssh -R, Tailscale Funnel)
    # on this machine connects from loopback, so loopback proves nothing after the first hour.
    lan = ip.is_loopback or (
        any(ip in net for net in FIRST_RUN_LAN_NETS) and ip not in TAILSCALE6 and not (on_own_network(ip) if own is None else any(ip.version == n.version and ip in n for n in own))
    )
    if not lan:
        return "no"
    now = time.time() if now is None else now
    return "yes" if first_start is not None and now - first_start < FIRST_RUN_WINDOW_S else "expired"


def setup_keyless(peer: str | None, headers, own: list | None = None, first_start: float | None = None, now: float | None = None) -> bool:
    return setup_keyless_why(peer, headers, own, first_start, now) == "yes"


def setup_code(secret: str) -> str:
    """The setup code: derived from the signing key, so it survives a restart, is the same in
    every worker, and stops mattering once a password is set. Twelve base32 characters."""
    raw = base64.b32encode(hmac.new(secret.encode(), b"first-run setup code", hashlib.sha256).digest()).decode()[:12]
    return "-".join(raw[i : i + 4] for i in range(0, 12, 4))


def setup_code_ok(secret: str, submitted: str | None) -> bool:
    """Case, spaces and dashes forgiven; an empty answer never matches."""
    given = "".join(ch for ch in str(submitted or "").upper() if ch.isascii() and ch.isalnum())
    return bool(given) and hmac.compare_digest(given, setup_code(secret).replace("-", ""))


def throttle_key(headers, peer: str | None) -> str:
    """The address failed sign-ins count against: the nearest one a client cannot write.

    X-Forwarded-For is read from the socket backwards. A hop at home (a proxy on this
    network, Cloudflare's tunnel, the tailnet) is trusted to report the one before it; the
    first address that is not at home is the client, whatever it forged in front. So a
    stranger cannot spread its guesses over made-up addresses to trip the global brake
    and lock the owner out. With every hop at home, the farthest one."""
    if not peer:
        return "?"
    hops = [h.strip() for h in ((headers.get("x-forwarded-for") if headers is not None else None) or "").split(",") if h.strip()] + [peer]
    for hop in reversed(hops):
        if not home_address(hop):
            return hop[:64]
    return hops[0][:64]


def client_prefix(address: str) -> str:
    """The throttle's key for an address: an IPv6 one counts as its /64, the smallest network a
    provider hands out, so a client cannot dodge the brake by cycling through its own addresses."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 4:
        return address
    if ip.ipv4_mapped:  # an IPv4 client seen through a dual-stack socket is still that IPv4 client
        return str(ip.ipv4_mapped)
    return str(ipaddress.ip_network(f"{address}/64", strict=False))


def through_cloudflare(headers) -> bool:
    """Cloudflare sets CF-Connecting-IP on everything it forwards, and Access adds its own
    Cf-Access-* headers; a request carrying either came in from the internet."""
    return any(name.lower() == "cf-connecting-ip" or name.lower().startswith("cf-access-") for name in headers.keys())


TESLA_SUBNET = "192.168.62.0/24"  # docker-compose's tesla network, unless CC_TESLA_SUBNET says otherwise


def network_of(text: str | None, default: str = TESLA_SUBNET):
    """A subnet setting as a network; the default when it is not one."""
    try:
        return ipaddress.ip_network(str(text or "").strip(), strict=False)
    except ValueError:
        return ipaddress.ip_network(default)


def _within(text: str, nets) -> bool:
    try:
        ip = ipaddress.ip_address(str(text or "").strip().strip("[]"))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip.version == net.version and ip in net for net in nets)


def admin_refusal(headers, peer: str | None, anywhere: bool, never=()) -> str | None:
    """Why an admin token may not be used for this request, or None when it may.

    `peer` is the socket's own address, taken before any proxy header rewrote it; with none
    there is nothing to judge by and the answer is no. `never` are networks that are never
    home, whatever Settings says: the containers that face the internet for Tesla.
    Every address the request names must be at home: the socket's, and each hop a proxy
    wrote into X-Forwarded-For or X-Real-IP. A proxy appends the address it saw, so a
    client that forges a private one in front still shows its own behind it."""
    if not peer:
        return "admin tokens need the connection's own address, and this request has none"
    hops = [peer]
    for name in ("x-forwarded-for", "x-real-ip"):
        hops += [h for h in (headers.get(name) or "").split(",") if h.strip()]
    if any(_within(h, never) for h in hops):
        return "admin tokens are refused from the Tesla stream's network, which faces the internet"
    if anywhere:
        return None
    if through_cloudflare(headers):
        return "admin tokens are refused through Cloudflare; use the local network or the tailnet, or allow them from anywhere in Settings"
    if not all(home_address(h) for h in hops):
        return "admin tokens work only from the local network or the tailnet unless Settings allows them from anywhere"
    return None


class RefusalCap:
    """At most `limit` refused calls a token's are written per `window` seconds, so a token
    holder knocking in a loop cannot fill cc_api_log. The rest are counted here, and the
    token's first call in a later window carries that count to be written as one row. In
    memory: a restart forgets a count, never a call that went through (those always log)."""

    def __init__(self, limit: int = 10, window: float = 60.0, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.by_key: dict[str, list] = {}  # key -> [window start, written, held back]

    def admit(self, key: str, refused: bool) -> tuple[bool, int]:
        """(write this call, refused calls held back to report now)."""
        now = self.clock()
        start, written, held = self.by_key.get(key, (now, 0, 0))
        report = 0
        if now - start >= self.window:
            start, written, report, held = now, 0, held, 0
        if not refused:
            self.by_key[key] = [start, written, held]
            return True, report
        if written < self.limit:
            self.by_key[key] = [start, written + 1, held]
            return True, report
        self.by_key[key] = [start, written, held + 1]
        return False, report


def log_call(conn, token: dict, method: str, path: str, status: int, ip: str) -> None:
    """One admin call in cc_api_log. The caller commits."""
    conn.execute(
        "INSERT INTO cc_api_log (label, last4, method, path, status, client_ip) VALUES (%s, %s, %s, %s, %s, %s)",
        (str(token.get("label") or "token")[:40], token.get("last4"), method[:10], path[:300], int(status), (ip or "?")[:64]),
    )


def prune_log(conn, days: int = LOG_KEEP_DAYS, batch: int = LOG_BATCH) -> int:
    """Forget calls older than `days`, a batch at a time with a commit after each, so a
    long-idle install clearing a backlog never holds the table. The rows removed."""
    gone = 0
    while True:
        cur = conn.execute(
            "DELETE FROM cc_api_log WHERE id IN (SELECT id FROM cc_api_log WHERE at < now() - make_interval(days => %s) ORDER BY id LIMIT %s)",
            (days, batch),
        )
        conn.commit()
        n = cur.rowcount if cur is not None and cur.rowcount is not None and cur.rowcount > 0 else 0
        gone += n
        if n < batch:
            return gone
