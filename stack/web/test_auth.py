import time

import pytest

import auth

# ---- passwords -------------------------------------------------------------


def test_password_round_trips_and_rejects_the_wrong_one():
    stored = auth.hash_password("correct horse battery", n=2**10)
    assert auth.verify_password(stored, "correct horse battery")
    assert not auth.verify_password(stored, "correct horse batterz")
    assert not auth.verify_password(stored, "")
    assert not auth.verify_password(None, "correct horse battery")


def test_same_password_hashes_differently_each_time():
    a = auth.hash_password("hunter2hunter2", n=2**10)
    b = auth.hash_password("hunter2hunter2", n=2**10)
    assert a != b, "a per-password salt is what stops one rainbow table doing every install"
    assert auth.verify_password(a, "hunter2hunter2") and auth.verify_password(b, "hunter2hunter2")


@pytest.mark.parametrize("stored", ["", "not-a-hash", "scrypt$x$8$1$aaaa$bbbb", "bcrypt$1$8$1$aaaa$bbbb", "a$b$c"])
def test_a_damaged_hash_is_a_failed_login_not_a_crash(stored):
    assert auth.verify_password(stored, "anything at all") is False


def test_password_problem_reports_the_two_ways_it_goes_wrong():
    assert auth.password_problem("shortie") == "Use at least 8 characters."
    assert auth.password_problem("longenough", "longenoug") == "Those two passwords are not the same."
    assert auth.password_problem("longenough", "longenough") is None


# ---- signed values ---------------------------------------------------------


def test_a_session_survives_a_restart_but_not_a_month():
    secret = auth.new_secret()
    token = auth.sign(secret, "u", now=1000)
    assert auth.unsign(secret, token, now=1000 + auth.SESSION_MAX_AGE - 1) == "u"
    assert auth.unsign(secret, token, now=1000 + auth.SESSION_MAX_AGE + 1) is None


def test_a_session_signed_with_another_secret_is_not_a_session():
    token = auth.new_session(auth.new_secret())
    assert not auth.valid_session(auth.new_secret(), token)


@pytest.mark.parametrize("token", [None, "", "u", "u.1000", "u.1000.zzzz", "u.notanumber.zzzz", "....."])
def test_a_malformed_cookie_is_no_session_not_a_crash(token):
    assert auth.unsign(auth.new_secret(), token) is None


def test_tampering_with_the_payload_breaks_the_signature():
    secret = auth.new_secret()
    token = auth.sign(secret, "u")
    forged = "admin" + token[1:]
    assert auth.unsign(secret, forged) is None


# ---- CSRF ------------------------------------------------------------------


def test_csrf_token_is_bound_to_the_session_cookie():
    secret = auth.new_secret()
    mine, theirs = auth.new_session(secret), auth.new_session(secret)
    assert auth.csrf_ok(secret, mine, auth.csrf_token(secret, mine))
    assert not auth.csrf_ok(secret, mine, auth.csrf_token(secret, theirs))
    assert not auth.csrf_ok(secret, mine, None)
    assert not auth.csrf_ok(secret, mine, "")


def test_csrf_token_is_stable_for_one_session_so_a_page_can_carry_it():
    secret = auth.new_secret()
    cookie = auth.new_session(secret)
    assert auth.csrf_token(secret, cookie) == auth.csrf_token(secret, cookie)


# ---- the rule --------------------------------------------------------------


def g(path, method="GET", *, enabled=True, password_set=True, signed_in=True, csrf_valid=True):
    return auth.gate(path, method, enabled=enabled, password_set=password_set, signed_in=signed_in, csrf_valid=csrf_valid)


def test_a_signed_in_reader_gets_every_page():
    for path in ("/", "/drives", "/stats", "/api/now", "/drives.csv"):
        assert g(path) == "pass"


def test_a_stranger_is_sent_to_sign_in():
    assert g("/", signed_in=False) == "login"
    assert g("/api/now", signed_in=False) == "login"
    assert g("/places", "POST", signed_in=False) == "login"


def test_the_healthcheck_and_the_login_page_stay_reachable():
    for path in ("/health", "/favicon.ico", "/login", "/static/style.css"):
        assert g(path, signed_in=False) == "pass"


def test_an_unconfigured_install_funnels_to_setup_and_never_serves_data():
    for path in ("/", "/api/drives", "/login"):
        assert g(path, password_set=False, signed_in=False) == "setup", path
    for path in ("/setup", "/static/style.css", "/favicon.ico"):
        assert g(path, password_set=False, signed_in=False) == "pass", f"{path}: the setup page is drawn with it"
    assert g("/health", password_set=False, signed_in=False) == "pass", "an uptime monitor sees 200 before setup"


def test_setting_the_first_password_still_needs_a_token():
    assert g("/setup", "POST", password_set=False, signed_in=False, csrf_valid=False) == "csrf"
    assert g("/setup", "POST", password_set=False, signed_in=False) == "pass"


def test_a_write_without_a_token_is_refused_even_signed_in():
    assert g("/places", "POST", csrf_valid=False) == "csrf"
    assert g("/settings", "POST", csrf_valid=False) == "csrf"
    assert g("/", csrf_valid=False) == "pass", "reads never need one"


def test_switching_sign_in_off_still_checks_tokens_on_writes():
    assert g("/", enabled=False, signed_in=False) == "pass"
    assert g("/places", "POST", enabled=False, signed_in=False) == "pass"
    assert g("/places", "POST", enabled=False, signed_in=False, csrf_valid=False) == "csrf"


def test_sign_in_off_does_not_strand_an_install_without_a_password():
    # The proxy is the lock; there is no password to set and no setup page to see.
    assert g("/", enabled=False, password_set=False, signed_in=False) == "pass"


# ---- throttling ------------------------------------------------------------


def test_a_person_who_fat_fingers_it_twice_notices_nothing():
    f = auth.Failures()
    for i in range(auth.LOCKOUT_AFTER - 1):
        f.record("10.1.2.5", now=i)
    assert f.wait("10.1.2.5", now=10) == 0.0


def test_the_wait_doubles_and_stops_at_a_quarter_hour():
    assert auth.lockout_seconds(4) == 0.0
    assert auth.lockout_seconds(5) == 5.0
    assert auth.lockout_seconds(6) == 10.0
    assert auth.lockout_seconds(60) == auth.LOCKOUT_CEILING


def test_a_guesser_is_held_and_the_wait_runs_down():
    f = auth.Failures()
    for _ in range(6):
        f.record("10.1.2.5", now=100)
    assert f.wait("10.1.2.5", now=100) == 10.0
    assert f.wait("10.1.2.5", now=105) == 5.0
    assert f.wait("10.1.2.5", now=111) == 0.0


def test_signing_in_forgives_the_earlier_misses():
    f = auth.Failures()
    for _ in range(6):
        f.record("10.1.2.5", now=100)
    f.clear("10.1.2.5")
    assert f.wait("10.1.2.5", now=100) == 0.0


def test_rotating_the_address_does_not_dodge_the_brake():
    # X-Forwarded-For is trusted from anything that reaches the port, so per-client
    # counting alone is a brake a guesser steers around. The global count is not.
    f = auth.Failures()
    for i in range(60):
        f.record(f"10.0.{i // 250}.{i % 250}", now=100)
    assert f.wait("10.9.9.9", now=100) > 0, "a fresh address inherits the global hold"


def test_the_table_cannot_grow_without_limit():
    f = auth.Failures(tracked=64)
    for i in range(5000):
        f.record(f"10.0.{i // 250}.{i % 250}", now=i)
    assert len(f.by_key) <= 64


def test_a_token_is_kept_only_as_its_hash_and_read_from_the_header_only():
    from auth import bearer_of, mint_token, token_hash

    t = mint_token()
    assert len(t) >= 40 and token_hash(t) != t and len(token_hash(t)) == 64
    assert bearer_of(f"Bearer {t}") == t and bearer_of(f"bearer {t}") == t
    assert bearer_of(None) is None and bearer_of("") is None and bearer_of("Basic abc") is None
    assert bearer_of("Bearer ") is None


def test_a_scoped_token_answers_for_its_car_only():
    from auth import token_allows

    assert token_allows(None, "ANY") and token_allows(None, None) and token_allows(None, "all")
    assert token_allows("V1", "V1")
    assert not token_allows("V1", "V2") and not token_allows("V1", None) and not token_allows("V1", "all")


def test_alerts_and_the_audit_log_name_an_address_a_client_cannot_choose():
    from auth import seen_from

    assert seen_from({"cf-connecting-ip": "198.51.100.1"}, "203.0.113.7") == "203.0.113.7", "a header sent straight from the internet is ignored"
    assert seen_from({"x-forwarded-for": "198.51.100.1, 203.0.113.9"}, "172.18.0.5") == "203.0.113.9 via 172.18.0.5"
    assert seen_from({"x-forwarded-for": "10.0.0.2"}, "192.168.1.40") == "10.0.0.2 via 192.168.1.40", "a forged home address still shows the socket"
    assert seen_from({}, "192.168.1.40") == "192.168.1.40" and seen_from({}, None) == "?"


def test_the_install_manifest_opens_before_sign_in():
    from auth import OPEN_PATHS, OPEN_PREFIXES

    assert "/manifest.webmanifest" in OPEN_PATHS and "/static/" in OPEN_PREFIXES


# ---- admin tokens ------------------------------------------------------------


@pytest.mark.parametrize("posted", ["read", "", None, "Admin ", "ADMIN", "root", "admin; drop"])
def test_a_token_is_admin_only_when_it_says_exactly_that(posted):
    assert auth.kind_of(posted) == "read"
    assert auth.kind_of("admin") == "admin"


def test_the_kind_column_arrives_at_startup_and_old_tokens_read():
    """A token minted before kinds existed reads; the ALTER is in the startup DDL only,
    since store_get runs auth.DDL on every call and an ALTER there takes its lock each time.
    """
    assert "ALTER TABLE" not in auth.DDL and "cc_api_tokens" not in auth.DDL
    assert "ADD COLUMN IF NOT EXISTS kind VARCHAR NOT NULL DEFAULT 'read'" in auth.API_DDL
    assert "CREATE TABLE IF NOT EXISTS cc_api_log" in auth.API_DDL


@pytest.mark.parametrize(
    "ip",
    [
        "10.1.2.3",
        "172.20.0.1",
        "192.168.1.40",
        "100.101.102.103",
        "127.0.0.1",
        "::1",
        "fd7a:115c:a1e0::1",
        "::ffff:192.168.1.9",
    ],
)
def test_home_addresses(ip):
    assert auth.home_address(ip)


@pytest.mark.parametrize(
    "ip",
    [
        "203.0.113.9",
        "8.8.8.8",
        "172.32.0.1",
        "100.128.0.1",
        "2001:db8::1",
        "testclient",
        "",
        None,
        "192.168.1.1.evil",
    ],
)
def test_addresses_away_from_home(ip):
    assert not auth.home_address(ip)


def test_an_admin_token_answers_from_home_only():
    assert auth.admin_refusal({}, "192.168.1.40", False) is None
    assert auth.admin_refusal({}, "100.101.102.103", False) is None, "the tailnet is home"
    assert "local network" in auth.admin_refusal({}, "203.0.113.9", False)


@pytest.mark.parametrize(
    "header",
    [
        "CF-Connecting-IP",
        "cf-connecting-ip",
        "Cf-Access-Jwt-Assertion",
        "cf-access-authenticated-user-email",
    ],
)
def test_cloudflare_is_never_home_even_from_a_lan_socket(header):
    """The tunnel's container sits on the LAN, so its socket looks local; its headers do not."""
    assert "Cloudflare" in auth.admin_refusal({header: "x"}, "172.18.0.5", False)


def test_a_forwarded_public_hop_is_not_home():
    assert auth.admin_refusal({"x-forwarded-for": "10.1.2.2, 203.0.113.9"}, "192.168.1.2", False), "a forged private address in front does not hide the real one behind it"
    assert auth.admin_refusal({"x-real-ip": "203.0.113.9"}, "192.168.1.2", False)
    assert auth.admin_refusal({"x-forwarded-for": "10.1.2.2"}, "192.168.1.2", False) is None


def test_the_owner_can_allow_admin_tokens_from_anywhere():
    assert auth.admin_refusal({"cf-connecting-ip": "203.0.113.9"}, "203.0.113.9", True) is None


class _Deleted:
    def __init__(self, n):
        self.rowcount = n


class _LogConn:
    def __init__(self, backlog=0):
        self.sent, self.commits, self.backlog = [], 0, backlog

    def execute(self, sql, params=()):
        self.sent.append((" ".join(sql.split()), params))
        n = min(self.backlog, params[-1]) if sql.lstrip().startswith("DELETE") else 0
        self.backlog -= n
        return _Deleted(n)

    def commit(self):
        self.commits += 1


def test_a_call_is_logged_with_the_token_named_not_held():
    conn = _LogConn()
    auth.log_call(
        conn,
        {"label": "Home Assistant", "last4": "a1b2", "kind": "admin"},
        "POST",
        "/api/admin/tesla/register",
        200,
        "192.168.1.40",
    )
    sql, params = conn.sent[0]
    assert sql.startswith("INSERT INTO cc_api_log")
    assert params == (
        "Home Assistant",
        "a1b2",
        "POST",
        "/api/admin/tesla/register",
        200,
        "192.168.1.40",
    )


def test_the_log_is_pruned_in_batches_with_a_commit_each():
    conn = _LogConn(backlog=700)
    assert auth.prune_log(conn, batch=300) == 700
    deletes = [s for s, _ in conn.sent if s.startswith("DELETE")]
    assert len(deletes) == 3 and conn.commits == 3, "300, 300, then 100 and done"
    assert all("LIMIT" in d for d in deletes)


def test_the_tesla_network_is_refused_even_from_anywhere():
    tesla = (auth.network_of("192.168.62.0/24"),)
    for anywhere in (False, True):
        assert "Tesla" in auth.admin_refusal({}, "192.168.62.9", anywhere, tesla)
        assert "Tesla" in auth.admin_refusal({"x-forwarded-for": "192.168.62.9"}, "192.168.1.2", anywhere, tesla)
    assert auth.admin_refusal({}, "192.168.1.2", False, tesla) is None


def test_a_request_through_funnel_is_refused_even_from_this_machine():
    """In the one container, Funnel's proxy reaches the site from 127.0.0.1; Tailscale marks
    what it forwards from the internet, and strips the mark from anything sent with one."""
    for anywhere in (False, True):
        assert "Tesla" in auth.admin_refusal({"Tailscale-Funnel-Request": "?1"}, "127.0.0.1", anywhere)
    assert auth.admin_refusal({}, "127.0.0.1", False) is None


def test_no_socket_address_is_no_admin_call():
    assert "address" in auth.admin_refusal({}, None, False)
    assert "address" in auth.admin_refusal({}, None, True)


def test_a_subnet_setting_falls_back_when_it_is_not_one():
    assert str(auth.network_of("10.9.0.0/16")) == "10.9.0.0/16"
    assert str(auth.network_of("nonsense")) == "192.168.62.0/24"
    assert str(auth.network_of(None)) == "192.168.62.0/24"


def test_the_refusal_cap_counts_per_token_and_reports_the_rest_once():
    now = [0.0]
    cap = auth.RefusalCap(limit=2, window=60, clock=lambda: now[0])
    assert [cap.admit("a", True) for _ in range(4)] == [(True, 0), (True, 0), (False, 0), (False, 0)]
    assert cap.admit("b", True) == (True, 0), "per token"
    assert cap.admit("a", False) == (True, 0), "a call that goes through is always written"
    now[0] = 61
    assert cap.admit("a", True) == (True, 2), "the next window carries the two held back"
    assert cap.admit("a", True) == (True, 0)


def test_the_throttle_counts_against_the_address_a_client_cannot_forge():
    key = auth.throttle_key
    assert key({}, "203.0.113.7") == "203.0.113.7"
    assert key({"x-forwarded-for": "198.51.100.1"}, "203.0.113.7") == "203.0.113.7", "straight from the internet, the header is the client's own words"
    assert key({"x-forwarded-for": "198.51.100.1, 203.0.113.9"}, "172.18.0.5") == "203.0.113.9", "behind a proxy at home: the hop it saw, not the forged one in front"
    assert key({"x-forwarded-for": "192.168.1.40"}, "172.18.0.5") == "192.168.1.40", "all at home: the farthest"
    assert key({}, None) == "?"


# ---- a stranger cannot lock the owner out ------------------------------------------


def _tripped(f, now=100):
    for i in range(60):
        f.record(f"203.0.113.{i}", now=now)


def test_a_known_device_is_not_held_by_the_global_brake():
    f = auth.Failures()
    _tripped(f)
    assert f.wait("198.51.100.7", now=100) > 0, "a stranger on a fresh address waits"
    assert f.wait("198.51.100.7", now=100, known=True) == 0.0, "the owner's own browser does not"


def test_a_known_device_still_has_its_own_limit():
    f = auth.Failures()
    for _ in range(6):
        f.record("198.51.100.7", now=100)
    assert f.wait("198.51.100.7", now=100, known=True) == 10.0


def test_the_owner_hears_once_when_the_global_brake_engages():
    heard = []
    f = auth.Failures(on_brake=lambda: heard.append(1))
    _tripped(f)
    assert heard == [1]
    f.clear("someone")
    _tripped(f, now=200)
    assert heard == [1, 1], "a fresh attack after a good sign-in is told again"


def test_a_device_cookie_is_its_own_kind_of_signed_value():
    secret = auth.new_secret()
    device = auth.new_device(secret)
    assert auth.known_device(secret, device)
    assert not auth.valid_session(secret, device), "it opens nothing"
    assert not auth.known_device(secret, auth.new_session(secret)), "and a session is not one"
    assert not auth.known_device(auth.new_secret(), device) and not auth.known_device(secret, None)


def test_a_device_cookie_lasts_a_year():
    secret = auth.new_secret()
    old = auth.sign(auth.device_key(secret), "d", now=time.time() - 300 * 86400)
    assert auth.known_device(secret, old)
    older = auth.sign(auth.device_key(secret), "d", now=time.time() - 400 * 86400)
    assert not auth.known_device(secret, older)


def test_an_ipv6_client_counts_as_its_64_against_the_sign_in_brake():
    assert auth.sign_in_key({}, "2001:db8:1:2::5") == auth.sign_in_key({}, "2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert auth.sign_in_key({}, "203.0.113.7") == "203.0.113.7"


# ---- revoking sessions --------------------------------------------------------------


def test_a_session_from_before_the_generation_changed_is_no_session():
    secret = auth.new_secret()
    old = auth.new_session(secret, 0)
    assert auth.valid_session(secret, old, 0)
    assert not auth.valid_session(secret, old, 1), "signing out everywhere ends it"
    assert auth.valid_session(secret, auth.new_session(secret, 1), 1)


def test_a_session_made_before_generations_existed_counts_as_the_first():
    secret = auth.new_secret()
    legacy = auth.sign(secret, "uAAAAAAAAAAAA")
    assert auth.valid_session(secret, legacy, 0) and not auth.valid_session(secret, legacy, 1)


def test_a_forged_generation_breaks_the_signature():
    secret = auth.new_secret()
    token = auth.new_session(secret, 1)
    assert not auth.valid_session(secret, "e2" + token[2:], 2)


def test_one_signed_out_session_ends_and_the_others_carry_on():
    secret = auth.new_secret()
    mine, other = auth.new_session(secret), auth.new_session(secret)
    revoked = auth.revoke({}, secret, mine, now=time.time())
    assert not auth.valid_session(secret, mine, 0, revoked) and auth.valid_session(secret, other, 0, revoked)


def test_the_revoked_list_forgets_what_has_expired_anyway():
    secret = auth.new_secret()
    stale = auth.sign(secret, "e0:uOLD", now=1000)
    revoked = auth.revoke({}, secret, stale, now=1000)
    later = auth.revoke(revoked, secret, auth.new_session(secret), now=1000 + auth.SESSION_MAX_AGE + 1)
    assert "e0:uOLD" not in later and len(later) == 1
    assert auth.revoke({}, secret, "not a session", now=1000) == {}


# ---- placeholders ---------------------------------------------------------------


def test_a_placeholder_from_the_example_env_is_named():
    env = {"REST_PASSWORD": "CHANGE_ME", "CC_PASSWORD": "", "CC_SECRET": "CHANGE_ME", "DATABASE_URL": "postgresql://carcommand:CHANGE_ME@db:5432/carcommand"}
    assert auth.placeholders(env) == ["DATABASE_URL (DB_PASSWORD)", "REST_PASSWORD", "CC_SECRET"]
    assert auth.placeholders({"DATABASE_URL": "postgresql://u:a1b2@db/x", "REST_PASSWORD": "r", "CC_PASSWORD": "a good long password"}) == []
