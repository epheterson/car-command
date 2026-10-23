"""The first-run claim: while no password exists, /setup answers to the home network only.

Judged by the socket's own address (TruePeer), never by a forwarded header, which anyone
can write. From anywhere else it asks for the setup code the site prints to its log at
start, valid until a password is set.
"""

import importlib
import ipaddress
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import auth


def test_the_code_is_the_same_for_one_secret_and_differs_between_two():
    a, b = auth.new_secret(), auth.new_secret()
    assert auth.setup_code(a) == auth.setup_code(a)
    assert auth.setup_code(a) != auth.setup_code(b)
    assert re.fullmatch(r"[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}", auth.setup_code(a))


def test_the_code_is_read_forgivingly_but_never_empty():
    secret = auth.new_secret()
    code = auth.setup_code(secret)
    assert auth.setup_code_ok(secret, code)
    assert auth.setup_code_ok(secret, " " + code.lower().replace("-", " ") + " ")
    assert not auth.setup_code_ok(secret, "")
    assert not auth.setup_code_ok(secret, None)
    assert not auth.setup_code_ok(secret, auth.setup_code(auth.new_secret()))


@pytest.fixture(scope="module")
def app_module():
    import os

    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    os.environ.setdefault("REST_USER", "u")
    os.environ.setdefault("REST_PASSWORD", "p")
    sys.path.insert(0, str(Path(__file__).parent))
    return importlib.import_module("app")


@pytest.fixture
def fresh(app_module, monkeypatch):
    """An install with no password, and a database that only remembers what is stored."""
    stored = {}

    @contextmanager
    def fake_db():
        yield object()

    monkeypatch.setattr(app_module, "db", fake_db)
    monkeypatch.setattr(auth, "store_set", lambda conn, k, v: stored.__setitem__(k, v))

    def fake_load():
        app_module.AUTH["password"] = stored.get("password")

    monkeypatch.setattr(app_module, "load_auth", fake_load)
    app_module.AUTH.update(enabled=True, secret=auth.new_secret(), password=None)
    app_module.FAILURES.by_key.clear()
    app_module.FAILURES.total = 0
    monkeypatch.setattr(auth, "_own_networks", ())  # no network of the container's own, unless a test names one
    app_module.AUTH["first_start"] = time.time() - 60  # a minute into the first hour
    yield stored
    app_module.AUTH["password"] = None


def client_from(app_module, peer):
    from fastapi.testclient import TestClient

    # Through TruePeer, as uvicorn runs it, so the socket's own address is what is judged.
    return TestClient(app_module.server, follow_redirects=False, client=(peer, 50000))


def _token(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m
    return m.group(1)


def test_from_the_machine_itself_the_form_asks_for_no_code_and_sets_the_password(app_module, fresh):
    c = client_from(app_module, "127.0.0.1")
    page = c.get("/setup")
    assert page.status_code == 200 and 'name="code"' not in page.text
    r = c.post("/setup", data={"password": "a good long password", "confirm": "a good long password", "csrf_token": _token(page.text)})
    assert r.status_code == 303 and "password" in fresh


def test_from_outside_the_form_asks_for_the_code(app_module, fresh):
    c = client_from(app_module, "203.0.113.9")
    page = c.get("/setup")
    assert page.status_code == 200 and 'name="code"' in page.text and "setup_code.py" in page.text


def test_from_outside_without_the_code_nothing_is_set(app_module, fresh):
    c = client_from(app_module, "203.0.113.9")
    token = _token(c.get("/setup").text)
    for code in ("", "AAAA-BBBB-CCCC"):
        r = c.post("/setup", data={"password": "a good long password", "confirm": "a good long password", "code": code, "csrf_token": token})
        assert r.status_code == 403 and "setup code" in r.text.lower()
    assert "password" not in fresh


def test_from_outside_with_the_code_the_password_is_set(app_module, fresh):
    c = client_from(app_module, "203.0.113.9")
    token = _token(c.get("/setup").text)
    code = auth.setup_code(app_module.AUTH["secret"])
    r = c.post("/setup", data={"password": "a good long password", "confirm": "a good long password", "code": code, "csrf_token": token})
    assert r.status_code == 303 and "password" in fresh


def test_a_forged_forwarded_header_does_not_make_a_stranger_local(app_module, fresh):
    c = client_from(app_module, "203.0.113.9")
    forged = {"X-Forwarded-For": "192.168.1.20", "X-Real-IP": "127.0.0.1"}
    page = c.get("/setup", headers=forged)
    assert 'name="code"' in page.text
    r = c.post("/setup", headers=forged, data={"password": "a good long password", "confirm": "a good long password", "csrf_token": _token(page.text)})
    assert r.status_code == 403 and "password" not in fresh


def claim(app_module, peer, headers=None):
    c = client_from(app_module, peer)
    page = c.get("/setup", headers=headers or {})
    r = c.post("/setup", headers=headers or {}, data={"password": "a good long password", "confirm": "a good long password", "csrf_token": _token(page.text)})
    return page, r


@pytest.mark.parametrize("peer", ["10.0.0.7", "192.168.1.20", "fd12:3456::7", "127.0.0.1", "::1"])
def test_the_home_network_and_the_machine_choose_the_first_password_without_the_code(app_module, fresh, peer):
    page, r = claim(app_module, peer)
    assert 'name="code"' not in page.text
    assert r.status_code == 303 and "password" in fresh


@pytest.mark.parametrize("peer", ["172.17.0.5", "172.20.0.1", "100.101.102.103", "169.254.3.4", "fe80::1", "203.0.113.9", "8.8.8.8", "2606:4700::1"])
def test_a_docker_bridge_a_tailnet_link_local_or_a_stranger_needs_the_code(app_module, fresh, peer):
    page, r = claim(app_module, peer)
    assert 'name="code"' in page.text
    assert r.status_code == 403 and "password" not in fresh


@pytest.mark.parametrize("headers", [{"X-Forwarded-For": "192.168.1.9"}, {"X-Real-IP": "192.168.1.9"}, {"Forwarded": "for=192.168.1.9"}, {"CF-Connecting-IP": "192.168.1.9"}, {"Via": "1.1 proxy"}])
@pytest.mark.parametrize("peer", ["192.168.1.20", "10.0.0.7", "127.0.0.1"])
def test_anything_that_came_through_a_proxy_needs_the_code(app_module, fresh, peer, headers):
    """A reverse proxy beside the site connects from a private address, even from loopback."""
    page, r = claim(app_module, peer, headers)
    assert 'name="code"' in page.text and r.status_code == 403 and "password" not in fresh


def test_a_peer_in_the_containers_own_subnet_needs_the_code_the_gateway_included(app_module, fresh, monkeypatch):
    """On Docker Desktop every published-port connection arrives from the compose network's gateway, and a
    neighbouring container is in the same subnet: neither is a device on the home network."""
    monkeypatch.setattr(auth, "_own_networks", (ipaddress.ip_network("192.168.61.0/24"),))
    for peer in ("192.168.61.1", "192.168.61.77"):
        page, r = claim(app_module, peer)
        assert 'name="code"' in page.text and r.status_code == 403 and "password" not in fresh, peer
    page, r = claim(app_module, "192.168.1.20")
    assert 'name="code"' not in page.text and r.status_code == 303, "a real LAN address, kept by NAT on Linux, is keyless"


def test_the_setup_page_says_whether_it_needs_the_code(app_module, fresh):
    assert client_from(app_module, "192.168.1.20").get("/setup").text.count('name="code"') == 0
    far = client_from(app_module, "203.0.113.9").get("/setup").text
    assert far.count('name="code"') == 1 and "docker compose -f stack/docker-compose.yml exec web python3 setup_code.py" in far


def test_the_keyless_rule_for_a_peer_and_its_headers():
    own, t0 = [ipaddress.ip_network("192.168.61.0/24")], time.time() - 60

    def keyless(peer, headers=None):
        return auth.setup_keyless(peer, headers or {}, own, first_start=t0)

    assert keyless("10.1.2.3") and keyless("192.168.1.2") and keyless("127.0.0.1") and keyless("::1")
    assert keyless("::ffff:192.168.1.2") and keyless("fd00::5")
    assert not keyless("192.168.61.1") and not keyless("172.16.0.9")
    assert not keyless("192.168.1.2", {"x-forwarded-for": "1.2.3.4"}) and not keyless("127.0.0.1", {"Via": "x"})
    assert not keyless(None) and not keyless("") and not keyless("garbage")


@pytest.mark.parametrize("peer", ["fd7a:115c:a1e0::1", "fd7a:115c:a1e0:ab12:4843:cd96:6200:1"])
def test_tailscales_address_range_is_not_the_home_network(peer):
    assert not auth.setup_keyless(peer, {}, [], first_start=time.time())
    assert auth.setup_keyless("fd12:3456::7", {}, [], first_start=time.time())


@pytest.mark.parametrize(
    "header",
    [
        "X-Forwarded-Port",
        "X-Forwarded-Prefix",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "X-Forwarded-Anything-At-All",
        "X-Client-IP",
        "X-Cluster-Client-IP",
        "X-Original-Forwarded-For",
        "CF-Connecting-IPv6",
        "CF-Ray",
        "True-Client-IP",
        "Fastly-Client-IP",
        "X-Envoy-External-Address",
        "Tailscale-User-Login",
        "Tailscale-User-Name",
        "Tailscale-Funnel-Request",
        "Forwarded",
        "Via",
        "X-Real-IP",
    ],
)
def test_every_header_a_proxy_adds_means_proxied(header):
    assert auth.proxied({header: "x"}) and auth.proxied({header.lower(): "x"})
    assert not auth.setup_keyless("192.168.1.2", {header: "x"}, [], first_start=time.time())
    assert not auth.proxied({"Host": "x", "User-Agent": "y", "Accept": "*/*"})


def test_the_no_code_window_is_the_first_hour_after_the_first_start():
    now = 1_000_000.0
    for age, expected in ((0, True), (3599, True), (3601, False), (86400, False)):
        assert auth.setup_keyless("192.168.1.2", {}, [], first_start=now - age, now=now) is expected, age
    assert not auth.setup_keyless("192.168.1.2", {}, [], first_start=None, now=now), "no first start on record: the code"
    assert not auth.setup_keyless("127.0.0.1", {}, [], first_start=None, now=now), "a header-less proxy on this machine is loopback too"
    for age, expected in ((10, True), (7200, False)):
        assert auth.setup_keyless("127.0.0.1", {}, [], first_start=now - age, now=now) is expected, age
    assert auth.setup_keyless_why("127.0.0.1", {}, [], first_start=now - 7200, now=now) == "expired"
    assert auth.setup_keyless_why("192.168.1.2", {}, [], first_start=now - 7200, now=now) == "expired"
    assert auth.setup_keyless_why("203.0.113.9", {}, [], first_start=now - 7200, now=now) == "no", "the window is not why a stranger needs the code"
    assert auth.setup_keyless_why("192.168.1.2", {}, [], first_start=now - 5, now=now) == "yes"


def test_a_window_that_has_passed_is_said_on_the_page(app_module, fresh):
    app_module.AUTH["first_start"] = time.time() - 7200
    page = claim(app_module, "192.168.1.20")[0]
    assert 'name="code"' in page.text and "first hour" in page.text
    far = client_from(app_module, "203.0.113.9").get("/setup")
    assert "first hour" not in far.text, "a stranger is not told about a window that was never theirs"


def test_an_install_with_a_password_has_no_first_hour_to_open(app_module, monkeypatch):
    stored = {"password": "scrypt$x"}
    _first_start_run(app_module, monkeypatch, stored)
    assert "first_start" not in stored and app_module.AUTH["first_start"] is None


NET = ipaddress.ip_network
LAN = ("eth0", NET("192.168.1.0/24"))
BRIDGE = ("docker0", NET("172.17.0.0/16"))
COMPOSE = ("br-1a2b", NET("192.168.61.0/24"))


def test_a_bridge_network_container_distrusts_every_subnet_it_is_on():
    nets = auth.own_networks(routes=[("eth0", NET("192.168.61.0/24"))], addrs=[], in_container=True)
    assert nets == (NET("192.168.61.0/24"),), "the gateway included"


def test_a_host_network_container_distrusts_only_the_bridges():
    nets = auth.own_networks(routes=[LAN, BRIDGE, COMPOSE], addrs=[], in_container=True)
    assert LAN[1] not in nets and COMPOSE[1] in nets and BRIDGE[1] in nets, "the owner's phone on the LAN is still the home network"


def test_a_bare_host_keeps_its_lan_and_distrusts_its_bridges():
    assert auth.own_networks(routes=[LAN], addrs=[], in_container=False) == ()
    assert auth.own_networks(routes=[LAN, BRIDGE], addrs=[], in_container=False) == (BRIDGE[1],)


def test_with_no_proc_there_is_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_PROC_ROUTE", str(tmp_path / "missing-route"))
    monkeypatch.setattr(auth, "_PROC_INET6", str(tmp_path / "missing-inet6"))
    assert auth.own_networks(in_container=True) == () and auth.own_networks(in_container=False) == ()


def test_proc_net_route_is_read_little_endian(tmp_path):
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n" "eth0\t00000000\t0101A8C0\t0003\t0\t0\t0\t00000000\n" "eth0\t0001A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\n" "junk\n"
    )
    assert auth._ipv4_routes(str(route)) == [("eth0", NET("192.168.1.0/24"))]


def test_proc_if_inet6_gives_each_global_prefix_by_interface(tmp_path):
    inet6 = tmp_path / "if_inet6"
    inet6.write_text("fd000000000000000000000000000002 02 40 00 80 br-1a2b\nfe800000000000000000000000000002 02 40 20 80 eth0\n00000000000000000000000000000001 01 80 10 80 lo\nshort\n")
    assert auth._ipv6_addrs(str(inet6)) == [("br-1a2b", NET("fd00::/64"))]


def test_the_keyless_rule_uses_the_split(monkeypatch):
    """Inside a bridge-network container the LAN range is the container's own and needs the code; on a host or a
    host-network container only the bridges do, so the owner's phone does not."""
    t0 = time.time() - 60
    monkeypatch.setattr(auth, "_own_networks", auth.own_networks(routes=[("eth0", NET("192.168.61.0/24"))], addrs=[], in_container=True))
    assert not auth.setup_keyless("192.168.61.1", {}, first_start=t0) and auth.setup_keyless("192.168.1.20", {}, first_start=t0)
    monkeypatch.setattr(auth, "_own_networks", auth.own_networks(routes=[LAN, COMPOSE], addrs=[], in_container=True))
    assert auth.setup_keyless("192.168.1.20", {}, first_start=t0), "host networking: the LAN is trusted"
    assert not auth.setup_keyless("192.168.61.5", {}, first_start=t0), "its bridge is not"


def test_wrong_codes_are_throttled(app_module, fresh):
    c = client_from(app_module, "203.0.113.77")
    token = _token(c.get("/setup").text)
    statuses = [c.post("/setup", data={"password": "a good long password", "confirm": "a good long password", "code": "WRONG", "csrf_token": token}).status_code for _ in range(8)]
    assert 429 in statuses
    code = auth.setup_code(app_module.AUTH["secret"])
    r = c.post("/setup", data={"password": "a good long password", "confirm": "a good long password", "code": code, "csrf_token": token})
    assert r.status_code == 429 and "password" not in fresh, "even the right code waits its turn"
    app_module.FAILURES.clear("203.0.113.77")
    app_module.FAILURES.total = 0


def test_the_code_is_in_the_log_while_no_password_exists(app_module, monkeypatch, caplog):
    secret = auth.new_secret()

    @contextmanager
    def fake_db():
        class Conn:
            def execute(self, *a, **k):
                class R:
                    def fetchall(self):
                        return []

                    def fetchone(self):
                        return None

                return R()

        yield Conn()

    monkeypatch.setattr(app_module, "db", fake_db)
    monkeypatch.setattr(auth, "store_get", lambda conn, k: secret if k == "secret" else None)
    monkeypatch.setattr(auth, "store_set", lambda conn, k, v: None)
    monkeypatch.delenv("CC_PASSWORD", raising=False)
    monkeypatch.delenv("CC_SECRET", raising=False)
    app_module.AUTH["enabled"] = True
    with caplog.at_level("WARNING", logger=app_module.LOG.name):
        app_module.load_auth()
    assert auth.setup_code(secret) in caplog.text
    app_module.AUTH["password"] = None


def test_loopback_gets_the_first_hour_and_no_more(app_module, fresh):
    page, r = claim(app_module, "127.0.0.1")
    assert 'name="code"' not in page.text and r.status_code == 303
    fresh.clear()
    app_module.AUTH["password"] = None
    app_module.AUTH["first_start"] = time.time() - 7200
    page, r = claim(app_module, "127.0.0.1")
    assert 'name="code"' in page.text and "first hour" in page.text and r.status_code == 403 and "password" not in fresh


@pytest.mark.parametrize("iface", ["docker0", "br-1a2b3c", "virbr0", "lxdbr0", "cni0", "cni-podman0", "podman1", "flannel.1", "cali1234abcd", "vxlan.calico"])
def test_every_kind_of_bridge_is_distrusted_even_on_a_bare_host(iface):
    assert auth.own_networks(routes=[LAN, (iface, NET("10.88.0.0/16"))], addrs=[], in_container=False) == (NET("10.88.0.0/16"),)


def test_a_real_interface_is_not_a_bridge():
    for iface in ("eth0", "wlan0", "enp3s0", "ens18", "tailscale0", "wg0"):
        assert auth.own_networks(routes=[(iface, NET("10.88.0.0/16"))], addrs=[], in_container=False) == (), iface


@pytest.mark.parametrize(
    "env,cgroup,mountinfo,markers",
    [
        ({"KUBERNETES_SERVICE_HOST": "10.96.0.1"}, "", "", False),
        ({}, "0::/kubepods/besteffort/pod123/abc", "", False),
        ({}, "12:memory:/docker/abc123", "", False),
        ({}, "", "/var/lib/containerd/io.containerd.snapshotter/overlay 0:1 rw", False),
        ({}, "0::/machine.slice/libpod-abc.scope", "", False),
        ({}, "0::/lxc.payload.web/", "", False),
        ({}, "", "", True),
    ],
)
def test_a_container_is_recognised_however_it_was_started(env, cgroup, mountinfo, markers):
    assert auth.looks_like_container(env, cgroup, mountinfo, markers)


def test_a_bare_host_is_not_taken_for_a_container():
    assert not auth.looks_like_container({"HOME": "/home/me"}, "0::/user.slice/user-1000.slice/session-2.scope", "36 35 8:1 / / rw - ext4 /dev/sda1 rw", False)


def test_a_bridge_network_container_found_by_cgroup_distrusts_its_subnets():
    in_container = auth.looks_like_container({}, "0::/kubepods/x", "", False)
    assert auth.own_networks(routes=[("eth0", NET("192.168.61.0/24"))], addrs=[], in_container=in_container) == (NET("192.168.61.0/24"),)


def _first_start_run(app_module, monkeypatch, stored, cars=False):
    """load_auth against a database that holds `stored` in cc_auth, and a vehicles table with or without a car."""

    class R:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

        def fetchall(self):
            return []

    class Conn:
        def execute(self, sql, *a, **k):
            if "to_regclass" in sql:
                return R({"t": True})
            if "FROM vehicles" in sql:
                return R({"x": 1} if cars else None)
            if "FROM cc_auth" in sql:
                return R({"x": 1} if stored else None)
            return R(None)

    @contextmanager
    def fake_db():
        yield Conn()

    monkeypatch.setattr(app_module, "db", fake_db)
    monkeypatch.setattr(auth, "store_get", lambda conn, k: stored.get(k))
    monkeypatch.setattr(auth, "store_set", lambda conn, k, v: stored.__setitem__(k, v))
    monkeypatch.delenv("CC_PASSWORD", raising=False)
    monkeypatch.delenv("CC_SECRET", raising=False)
    app_module.AUTH["first_start"] = None
    app_module.load_auth()


def test_the_window_opens_only_on_a_database_with_no_history(app_module, monkeypatch):
    stored = {}
    _first_start_run(app_module, monkeypatch, stored)
    assert abs(float(stored["first_start"]) - time.time()) < 5 and "secret" in stored, "nothing in cc_auth, no cars: a fresh install"


def test_an_upgraded_site_with_a_signing_key_in_the_environment_and_no_password_has_no_window(app_module, monkeypatch):
    """CC_SECRET in .env means no secret row, but the site has history: other settings, or cars."""
    for stored, cars in (({"admin_anywhere": "0"}, False), ({"enabled": "0"}, False), ({}, True)):
        monkeypatch.setenv("CC_SECRET", "pinned-in-env")
        stored = dict(stored)
        _first_start_run(app_module, monkeypatch, stored, cars=cars)
        monkeypatch.setenv("CC_SECRET", "pinned-in-env")  # _first_start_run clears it; this install keeps it
        assert float(stored["first_start"]) == 0.0 and app_module.AUTH["first_start"] == 0.0, (stored, cars)
        assert not auth.setup_keyless("192.168.1.2", {}, [], first_start=app_module.AUTH["first_start"])


def test_a_restart_does_not_reopen_the_window(app_module, monkeypatch):
    stored = {}
    _first_start_run(app_module, monkeypatch, stored)
    first = float(stored["first_start"])
    _first_start_run(app_module, monkeypatch, stored)
    assert float(stored["first_start"]) == first and app_module.AUTH["first_start"] == first


def test_in_kubernetes_no_network_counts_as_home(app_module, fresh, monkeypatch):
    """A NodePort's SNAT makes the internet look like the node's own address, which no check here can tell from the
    LAN: so /setup always needs the code, loopback and the first hour included."""
    t0 = time.time() - 60
    for peer in ("127.0.0.1", "192.168.1.20", "10.0.0.7", "fd12:3456::7"):
        assert auth.setup_keyless(peer, {}, [], first_start=t0), peer
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    for peer in ("127.0.0.1", "192.168.1.20", "10.0.0.7", "fd12:3456::7"):
        assert not auth.setup_keyless(peer, {}, [], first_start=t0), peer
        assert auth.setup_keyless_why(peer, {}, [], first_start=t0) == "no"
    page, r = claim(app_module, "192.168.1.20")
    assert 'name="code"' in page.text and "first hour" not in page.text and r.status_code == 403 and "password" not in fresh


def test_a_wrong_setup_code_keeps_what_was_typed_in_the_password_fields(app_module, fresh):
    c = client_from(app_module, "203.0.113.9")
    page = c.get("/setup")
    r = c.post("/setup", data={"password": "a good long password", "confirm": "a good long password", "code": "AAAA-AAAA-AAAA", "csrf_token": _token(page.text)})
    assert r.status_code == 403 and "not right" in r.text
    assert r.text.count('value="a good long password"') == 2 and "password" not in fresh


def test_a_mismatch_keeps_the_fields_too(app_module, fresh):
    c = client_from(app_module, "192.168.1.20")
    page = c.get("/setup")
    r = c.post("/setup", data={"password": "a good long password", "confirm": "another one entirely", "csrf_token": _token(page.text)})
    assert 'value="a good long password"' in r.text and "password" not in fresh
