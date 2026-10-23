"""Sources kept in the database, sealed with the key from .env."""

import pytest

from sources import KINDS, NoKey, Vault, effective, env_sources, env_tokens, last4, new_key

ALPHA, BRAVO = "ALPHA0000000000001", "BRAVO0000000000002"


def row(kind, vin, plain, enabled=True, config=None, i=1):
    return {"id": i, "vin": vin, "kind": kind, "label": "", "config": config or {}, "enabled": enabled, "plain": plain, "last_ok_at": None, "last_error": None}


def test_a_secret_goes_in_sealed_and_comes_out_whole():
    v = Vault(new_key())
    blob = v.seal("fake-token-1234")
    assert b"fake-token" not in blob and v.open(blob) == "fake-token-1234" and v.ready and v.problem is None


def test_without_a_key_nothing_is_sealed_and_the_page_can_say_why():
    for key, problem in ((None, "not set"), ("", "not set"), ("not-a-key", "not a valid key")):
        v = Vault(key)
        assert not v.ready and problem in v.problem
        with pytest.raises(NoKey):
            v.seal("x")
        assert v.open(b"anything") is None


def test_a_row_sealed_with_another_key_does_not_open():
    blob = Vault(new_key()).seal("fake-token-1234")
    assert Vault(new_key()).open(blob) is None


def test_last_four_only():
    assert last4("fake-token-1234") == "…1234" and last4("abc") == "…"


def test_env_tokens_and_what_the_page_lists_from_env():
    env = {"TESLAFI_TOKENS": f" {ALPHA}:tok-a , tok-b ,", "TESLAMATE_DB_URL": "postgresql://tm:pw@host:5432/teslamate"}
    assert env_tokens(env) == [(ALPHA, "tok-a"), (None, "tok-b")]
    listed = env_sources(env)
    assert [(s["kind"], s["vin"]) for s in listed] == [("teslafi", ALPHA), ("teslafi", None), ("teslamate", None)]
    assert all("tok" not in str(s) and "pw" not in str(s) for s in listed), "nothing secret in what the page lists"
    assert env_sources({}) == [] and set(KINDS) == {"teslafi", "teslamate", "lucid", "tesla", "carconnectivity"}


def test_effective_uses_env_until_the_table_has_that_kind():
    env = {"TESLAFI_TOKENS": f"{ALPHA}:tok-a", "TESLAMATE_DB_URL": "postgresql://env"}
    got = effective([], env)
    assert got["teslafi"] == [f"{ALPHA}:tok-a"] and got["teslamate"] == {"url": "postgresql://env", "vins": []} and got["moved"] == set()
    rows = [row("teslafi", BRAVO, "tok-b")]
    got = effective(rows, env)
    assert got["teslafi"] == [f"{BRAVO}:tok-b"] and got["teslamate"]["url"] == "postgresql://env" and got["moved"] == {"teslafi"}
    rows.append(row("teslamate", None, "postgresql://table", config={"vins": [ALPHA]}, i=2))
    assert effective(rows, env)["teslamate"] == {"url": "postgresql://table", "vins": [ALPHA]}


def test_effective_ignores_rows_it_cannot_open():
    env = {"TESLAFI_TOKENS": f"{ALPHA}:tok-a"}
    got = effective([row("teslafi", BRAVO, None)], env)
    assert got["teslafi"] == [f"{ALPHA}:tok-a"] and got["moved"] == set(), "a row the key cannot open does not count as moved"
    assert effective([row("teslafi", BRAVO, "tok-b", enabled=False)], env)["teslafi"] == [f"{ALPHA}:tok-a"]


from sources import check_teslafi, check_teslamate, scrub


class Fetched(Exception):
    pass


def test_teslafi_check_names_the_car():
    row = {"vin": ALPHA, "display_name": "Alpha", "battery_level": 57}
    got = check_teslafi("fake-token-1234", lambda tok, cmd: row if cmd == "lastGood" else {})
    assert got == {"ok": True, "vin": ALPHA, "name": "Alpha", "model": got["model"], "battery": 57}
    assert check_teslafi("t", lambda tok, cmd: {})["ok"] is False, "an answer without a car is not a working token"


def test_a_check_never_repeats_the_secret():
    import httpx

    def refused(tok, cmd):
        req = httpx.Request("GET", "https://www.teslafi.com/feed.php")
        raise httpx.HTTPStatusError(f"401 for token {tok}", request=req, response=httpx.Response(401, request=req))

    got = check_teslafi("fake-token-1234", refused)
    assert got["ok"] is False and "fake-token" not in got["error"] and "refused" in got["error"]

    def down(url):
        raise OSError(f'connection to server at "host" failed for {url}')

    got = check_teslamate("postgresql://tm:sEcr3t7@host:5432/teslamate", down)
    assert got["ok"] is False and "sEcr3t7" not in got["error"]
    assert scrub("a sEcr3t7 b", "sEcr3t7", "") == "a *** b"


def test_teslamate_check_lists_the_cars_and_wants_a_postgres_url():
    class TM:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, sql, *a):
            class R:
                def fetchall(self_inner):
                    return [{"vin": ALPHA, "name": "Alpha", "model": "3"}, {"vin": BRAVO, "name": None, "model": "Y"}]

            return R()

    got = check_teslamate("postgresql://tm:pw@host:5432/teslamate", lambda url: TM())
    assert got == {"ok": True, "cars": [{"vin": ALPHA, "name": "Alpha", "model": "3"}, {"vin": BRAVO, "name": "", "model": "Y"}]}
    assert check_teslamate("mysql://x", lambda url: TM())["ok"] is False


from sources import add, load, remove, set_config


class Store:
    """The store's statements kept in a list; anything else answers as an empty database."""

    def __init__(self):
        self.rows, self.next, self.state = [], 1, {}

    def commit(self):
        pass

    def execute(self, sql, args=()):
        s = " ".join(sql.split()).lower()
        rows = self.rows

        class R:
            def __init__(self, out):
                self.out = out

            def fetchall(self):
                return self.out

            def fetchone(self):
                return self.out[0] if self.out else None

        if s.startswith("select id from cc_sources where kind = %s and config->>'connector'"):
            import json as _json

            return R([{"id": r["id"]} for r in rows if r["kind"] == args[0] and _json.loads(r["config"]).get("connector") == args[1]])
        if s.startswith("select pg_advisory_xact_lock"):
            self.locks = getattr(self, "locks", []) + [args[0]]
            return R([{"pg_advisory_xact_lock": None}])
        if s == "update cc_sources set secret = %s where id = %s and secret = %s":  # a rebind: only if unchanged
            for r in rows:
                if r["id"] == args[1] and r["secret"] == args[2]:
                    r["secret"] = args[0]
            return R([])
        if s == "update cc_sources set secret = %s where id = %s":  # a new row's secret, sealed for its id
            for r in rows:
                if r["id"] == args[1]:
                    r["secret"] = args[0]
            return R([])
        if s.startswith("select value from cc_state where key"):
            return R([{"value": self.state[args[0]]}] if args[0] in self.state else [])
        if s.startswith("select id, kind, secret from cc_sources"):
            return R([{"id": r["id"], "kind": r["kind"], "secret": r["secret"]} for r in rows if r["secret"] is not None])
        if s.startswith("update cc_sources set enabled = false"):
            for r in rows:
                if r["id"] == args[-1]:
                    r.update(enabled=False, secret=None, config=args[0], last_error=None)
            return R([])
        if s.startswith("select id from cc_sources where kind"):
            return R([{"id": r["id"]} for r in rows if r["kind"] == args[0] and r["vin"] == args[1]])
        if s.startswith("insert into cc_state"):
            self.state[args[0]] = args[1]
            return R([])
        if s.startswith("update cc_sources set secret = null"):
            # the expiry: pending Lucid sign-ins older than the limit (a test marks a row "old")
            import json as _json

            hit = [r for r in rows if r["kind"] == "lucid" and _json.loads(r["config"]).get("pending") and r.get("updated_at") == "old"]
            for r in hit:
                r.update(secret=None, config=_json.dumps({"pending": False}), last_error=args[0])
            return R([{"id": r["id"]} for r in hit])
        if s.startswith("update cc_sources set secret"):
            for r in rows:
                if r["id"] == args[-1]:
                    r.update(secret=args[0], label=args[1], config=args[2], enabled=True)
            return R([])
        if s.startswith("insert into cc_sources"):
            rows.append({"id": self.next, "vin": args[0], "kind": args[1], "label": args[2], "config": args[3], "secret": args[4], "enabled": True, "last_ok_at": None, "last_error": None})
            self.next += 1
            return R([{"id": self.next - 1}])
        if s.startswith("select id, vin, kind"):
            return R([dict(r) for r in rows])
        if s.startswith("delete from cc_sources"):
            self.rows[:] = [r for r in rows if r["id"] != args[0]]
            return R([])
        if s.startswith("update cc_sources set config"):
            for r in rows:
                if r["id"] == args[1]:
                    r["config"] = args[0]
            return R([])
        if s.startswith("update cc_sources set last_ok_at"):
            for r in rows:
                if r["id"] == args[0]:
                    from datetime import datetime, timezone

                    r.update(last_ok_at=datetime.now(timezone.utc), last_error=None)
            return R([])
        if s.startswith("update cc_sources set last_error"):
            for r in rows:
                if r["id"] == args[1]:
                    r["last_error"] = args[0]
            return R([])
        from test_empty import EmptyConnection  # every other query the pages make: an empty database

        return EmptyConnection().execute(sql, args)


def test_the_store_seals_replaces_by_car_and_opens():
    import json

    conn, v = Store(), Vault(new_key())
    a = add(conn, v, "teslafi", ALPHA, "Alpha", {}, "tok-a")
    b = add(conn, v, "teslafi", ALPHA, "Alpha", {}, "tok-a2")
    assert a == b and len(conn.rows) == 1, "a new token for the same car replaces the old one"
    assert b"tok-a2" not in bytes(conn.rows[0]["secret"])
    got = load(conn, v)
    assert got[0]["plain"] == "tok-a2" and got[0]["kind"] == "teslafi" and "secret" not in got[0]
    set_config(conn, a, {"vins": [ALPHA]})
    assert json.loads(conn.rows[0]["config"]) == {"vins": [ALPHA]}
    remove(conn, a)
    assert load(conn, v) == []


def test_a_row_the_key_cannot_open_is_shown_not_used():
    conn = Store()
    add(conn, Vault(new_key()), "teslafi", ALPHA, "Alpha", {}, "tok-a")
    got = load(conn, Vault(new_key()))
    assert got[0]["plain"] is None and got[0]["vin"] == ALPHA
    assert effective(got, {})["teslafi"] == []


def test_a_long_error_is_masked_before_it_is_cut():
    """psycopg echoes the whole address on a malformed one; cut first and the mask misses."""
    url = "postgresql://user:hunter2xx@[bad/db"

    def malformed(u):
        raise ValueError("z" * 135 + f' in URI: "{u}"')  # the 200-character cut lands inside the password

    got = check_teslamate(url, malformed)
    assert got["ok"] is False and "hunter" not in got["error"] and "postgresql://user:h" not in got["error"]

    def odd(tok, cmd):
        raise ValueError("q" * 110 + tok)  # the 160-character cut lands inside the token

    got = check_teslafi("fake-token-abcdefgh", odd)
    assert "fake-tok" not in got["error"]


from sources import bump, expire_pending  # noqa: E402


def test_a_bump_writes_a_new_version():
    conn = Store()
    bump(conn)
    first = conn.state["sources_version"]
    bump(conn)
    assert conn.state["sources_version"] != first


def test_an_unclaimed_sign_in_is_cleared():
    import json

    conn, v = Store(), Vault(new_key())
    add(conn, v, "lucid", None, "Lucid", {"pending": True}, json.dumps({"email": "a@b", "password": "pw-secret-1"}))
    assert expire_pending(conn) == 0, "a sign-in just made is left for the connector"
    conn.rows[0]["updated_at"] = "old"
    assert expire_pending(conn) == 1
    row = conn.rows[0]
    assert row["secret"] is None and "did not sign in" in row["last_error"]


def test_tokens_in_env_are_a_bare_token_or_vin_colon_token_and_nothing_else():
    from sources import env_token_problems, env_tokens, parse_tokens, split_token

    assert split_token("tok-a") == (None, "tok-a") and split_token(f" {ALPHA}:tok-a ") == (ALPHA, "tok-a")
    for bad in ("Comet:faketoken0000", "My Car:tok", f"{ALPHA}:", ":tok", "a b", "", "   "):
        assert split_token(bad) is None, bad
    good, bad = parse_tokens(f"Comet:faketoken0000, tok-b ,{ALPHA}:tok-c,,{ALPHA}:")
    assert good == [(None, "tok-b"), (ALPHA, "tok-c")] and len(bad) == 2
    assert bad[0].startswith("entry 1 of 4") and bad[1].startswith("entry 4 of 4"), bad
    assert all("faketoken" not in line and "Comet" not in line for line in bad), "the line names the place, never the value"
    env = {"TESLAFI_TOKENS": "Comet:faketoken0000"}
    assert env_tokens(env) == [] and len(env_token_problems(env)) == 1 and env_tokens({}) == [] and env_token_problems({}) == []


def test_the_poller_refuses_a_malformed_entry_with_a_log_line_and_makes_no_car_of_it(caplog):
    import logging

    from teslafi_live import TeslaFiPoller

    with caplog.at_level(logging.WARNING, logger="carcommand.teslafi"):
        p = TeslaFiPoller(["Comet:faketoken0000", f"{ALPHA}:tok-a", "tok-b"], connect=None)
    assert p.tokens == ["tok-a", "tok-b"] and p.vin_for_token == {"tok-a": ALPHA}
    assert [r.getMessage() for r in caplog.records if "TESLAFI_TOKENS" in r.getMessage()] == [
        "TESLAFI_TOKENS: entry 1 is not a token or VIN:token (the part before the colon must be the car's VIN); ignored"
    ]
    assert "faketoken" not in caplog.text


def test_env_example_says_the_token_format_plainly():
    from pathlib import Path

    text = (Path(__file__).parent.parent / ".env.example").read_text()
    assert "the token alone" in text and "VIN, a colon, then the token" in text and "Name:abc123 is refused" in text
