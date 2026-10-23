"""OpenEI's rate database: what is sent, and what a rate becomes. No network: every request
is answered from fixtures shaped like OpenEI's v7 answers (detail=full)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

import cost
import urdb

TZ = ZoneInfo("America/Chicago")
KEY = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789wxyz"
UTILITY = "Springfield Electric Co"


def _epoch(y, m, d):  # local midnight in Springfield, as OpenEI dates a rate
    return int(datetime(y, m, d, tzinfo=TZ).timestamp())


def _week(night, day, peak, evening):
    """A day of periods: night 0-6, day 6-16, peak 16-21, evening 21-24."""
    return [night] * 6 + [day] * 10 + [peak] * 5 + [evening] * 3


def rate(label, name, start, end=None, scale=1.0, supersedes=None, **extra):
    """A residential time-of-use rate as OpenEI lists it: four periods, the day period
    tiered, the peak with an adjustment, a fixed charge per month."""
    item = {
        "label": label,
        "uri": f"https://apps.openei.org/IURDB/rate/view/{label}",
        "approved": True,
        "is_default": False,
        "utility": UTILITY,
        "eiaid": 99999,
        "name": name,
        "startdate": start,
        "sector": "Residential",
        "servicetype": "Bundled",
        "description": "A long description nobody prices with.",
        "revisions": [1, 2, 3],
        "energyratestructure": [
            [{"unit": "kWh", "rate": round(0.12 * scale, 5)}],
            [{"max": 300, "unit": "kWh", "rate": round(0.25 * scale, 5)}, {"unit": "kWh", "rate": round(0.31 * scale, 5)}],
            [{"unit": "kWh", "rate": round(0.35 * scale, 5)}],
            [{"unit": "kWh", "rate": round(0.48 * scale, 5), "adj": 0.02}],
        ],
        "energyweekdayschedule": [_week(0, 1, 3, 2) for _ in range(12)],
        "energyweekendschedule": [_week(0, 1, 2, 1) for _ in range(12)],
        "fixedchargefirstmeter": 12.5,
        "fixedchargeunits": "$/month",
        "demandunits": "kW",
        "country": "USA",
        **extra,
    }
    if end:
        item["enddate"] = end
    if supersedes:
        item["supersedes"] = supersedes
    return item


L1, L2, L3, FLAT = "a" * 24, "b" * 24, "c" * 24, "d" * 24
ITEMS = [
    rate(L1, "Residential EV-TOU", _epoch(2023, 1, 1), _epoch(2023, 12, 31)),
    rate(L2, "Residential  EV-TOU", _epoch(2024, 1, 1), _epoch(2024, 12, 31), scale=1.1, supersedes=L1),  # stray space: same name
    rate(L3, "Residential EV-TOU (Sch)", _epoch(2025, 1, 1), scale=1.2, supersedes=L2),  # renamed, linked by supersedes
    {
        "label": FLAT,
        "utility": UTILITY,
        "name": "Residential Flat",
        "startdate": _epoch(2024, 6, 1),
        "energyratestructure": [[{"unit": "kWh", "rate": 0.2}], [{"unit": "kWh", "rate": 0.15}]],
        "energyweekdayschedule": [[0] * 24] * 5 + [[1] * 24] * 7,
        "energyweekendschedule": [[0] * 24] * 5 + [[1] * 24] * 7,
        "fixedchargeunits": "$/month",
        "demandratestructure": [[{"rate": 5.0}]],
    },
]


class Wire:
    """Answers each call from a queue and keeps what was asked."""

    def __init__(self, *answers):
        self.answers, self.asked, self.timeouts = list(answers), [], []

    def __call__(self, url, params, timeout=None):
        self.asked.append((url, dict(params)))
        self.timeouts.append(timeout)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


# --- parse -----------------------------------------------------------------------------


def test_a_four_period_time_of_use_rate_with_tiers_and_a_fixed_charge():
    v = urdb.version_of(urdb.trim(ITEMS[0]))
    assert v["from"] == "2023-01-01"
    assert [p["price"] for p in v["periods"]] == [0.12, 0.25, 0.35, 0.5], "tier 1's rate plus its adjustment, $/kWh"
    assert [p["key"] for p in v["periods"]] == ["p0", "p1", "p2", "p3"]
    assert [p["label"] for p in v["periods"]] == ["Super off-peak", "Off-peak", "Part-peak", "Peak"]
    assert v["weekday"][6][17] == 3 and v["weekday"][0][2] == 0 and v["weekend"][6][17] == 2, "the schedules are already period indexes"
    assert "first tier" in v["fixed_note"] and "Off-peak $0.31 over 300 kWh" in v["fixed_note"]
    assert "Fixed charge $12.50/month, not priced." in v["fixed_note"] and "Demand" not in v["fixed_note"]
    plan = {"name": "x", "source": "urdb:x", "versions": [v]}
    assert cost.valid_plan(plan)
    # An hour of 10 kW at 5pm on a July weekday is peak: $5.00.
    start = datetime(2023, 7, 5, 17, tzinfo=TZ)
    priced = cost.session_cost([(start, start + timedelta(hours=1), 10.0)], start, start + timedelta(hours=1), TZ, plan)
    assert priced["cost"] == pytest.approx(5.0) and priced["kwh"]["p3"] == pytest.approx(10.0)


def test_seasonal_labels_carry_their_months_and_flat_kinds():
    v = urdb.version_of(urdb.trim(ITEMS[3]))
    assert [p["label"] for p in v["periods"]] == ["All hours, Jan–May", "All hours, Jun–Dec"]
    assert urdb.kind(v) == "Seasonal" and "Demand charges, not priced." in v["fixed_note"]
    assert urdb.kind(urdb.version_of(urdb.trim(ITEMS[0]))) == "Time-of-use"
    one = dict(ITEMS[3], energyweekdayschedule=[[0] * 24] * 12, energyweekendschedule=[[0] * 24] * 12)
    assert urdb.kind(urdb.version_of(one)) == "Flat"
    assert urdb._months_text({11, 12, 1, 2}) == "Nov–Feb" and urdb._months_text({1, 3}) == "Jan, Mar"


@pytest.mark.parametrize(
    "change,says",
    [
        ({"energyratestructure": [[{"unit": "kW", "rate": 3.0}]] * 4}, "not per kWh"),
        ({"energyratestructure": []}, "no energy charges"),
        ({"energyweekdayschedule": [[0] * 24] * 11}, "12-month, 24-hour"),
        ({"energyweekendschedule": [[9] * 24] * 12}, "12-month, 24-hour"),
        ({"energyratestructure": [[{"unit": "kWh", "rate": -0.1}]] * 4}, "negative"),
        ({"startdate": None}, "no start date"),
    ],
)
def test_a_rate_that_cannot_price_a_kwh_says_why(change, says):
    with pytest.raises(urdb.URDBError, match=says):
        urdb.version_of(dict(ITEMS[0], **change))


# --- versions --------------------------------------------------------------------------


def test_versions_are_the_same_name_across_dates_and_a_rename_linked_by_supersedes():
    items = [urdb.trim(x) for x in ITEMS]
    assert [x["label"] for x in urdb.versions_of(L1, items)] == [L1, L2, L3]
    assert [x["label"] for x in urdb.versions_of(L3, items)] == [L1, L2, L3], "any version finds them all"
    assert [x["label"] for x in urdb.versions_of(FLAT, items)] == [FLAT]
    assert urdb.versions_of("e" * 24, items) == []
    other = dict(ITEMS[0], label="f" * 24, utility="Shelbyville Power")
    assert [x["label"] for x in urdb.versions_of(L1, items + [urdb.trim(other)])] == [L1, L2, L3], "another utility's plan of the same name is another plan"


def test_a_plan_from_versions_prices_each_charge_at_the_version_in_force():
    plan = urdb.to_plan(urdb.versions_of(L1, [urdb.trim(x) for x in ITEMS]))
    assert plan["source"] == f"urdb:{L3}" and plan["name"] == "Residential EV-TOU (Sch)" and plan["utility"] == UTILITY
    assert [v["from"] for v in plan["versions"]] == ["2023-01-01", "2024-01-01", "2025-01-01"]
    assert plan["link"].startswith("https://apps.openei.org/") and cost.valid_plan(plan)
    assert cost.version_at(date(2024, 6, 1), plan)["periods"][0]["price"] == pytest.approx(0.132)
    assert cost.version_at(date(2022, 6, 1), plan) is None, "before the first version, the plan does not price"
    assert urdb.to_plan([dict(urdb.trim(ITEMS[2]), uri="javascript:alert(1)")]).get("link") is None
    broken = dict(urdb.trim(ITEMS[1]), energyratestructure=[])
    assert len(urdb.to_plan([urdb.trim(ITEMS[0]), broken, urdb.trim(ITEMS[2])])["versions"]) == 2, "a version that cannot price is left out"


def test_the_list_is_one_row_per_plan_current_first():
    found = urdb.listing([urdb.trim(x) for x in ITEMS], date(2026, 10, 4), "")
    assert found["total"] == 2 and found["utilities"] == [UTILITY]
    tou, flat = found["rows"]
    assert tou["label"] == L3 and tou["versions"] == 3 and tou["first"] == date(2023, 1, 1) and tou["current"] and tou["end"] is None
    assert tou["kind"] == "Time-of-use" and tou["tiered"] and tou["fixed"] == "$12.50/month"
    assert flat["kind"] == "Seasonal" and flat["fixed"] is None
    assert [r["name"] for r in urdb.listing([urdb.trim(x) for x in ITEMS], date(2026, 10, 4), "flat")["rows"]] == ["Residential Flat"]


# A large utility's EV plans as OpenEI lists them, shaped on a real listing: one open today
# with thirteen versions (renamed once, linked by `supersedes`), a sibling open today, a rate
# closed to new customers whose end date is the day it closed (before its newest version
# starts), and one that ended. Plus a flat plan open to all.
def _lab(prefix: str, n: int) -> str:
    return f"{prefix}{n:023x}"


EV2 = [
    rate(
        _lab("e", i),
        "Electric Vehicle EV-2" if i < 6 else "Electric Vehicle EV2 (Sch)",
        _epoch(2014 + i, 3, 1),
        None if i == 12 else _epoch(2015 + i, 2, 28),
        scale=1 + i / 20,
        supersedes=_lab("e", i - 1) if i else None,
    )
    for i in range(13)
]
UNTIERED = [[{"unit": "kWh", "rate": 0.16}], [{"unit": "kWh", "rate": 0.3}], [{"unit": "kWh", "rate": 0.42}], [{"unit": "kWh", "rate": 0.55}]]
EV_OPEN = rate(_lab("f", 1), "Electric Vehicle EV (Sch)", _epoch(2026, 3, 27), energyratestructure=UNTIERED)
EV_CLOSED = rate(_lab("f", 2), "Electric Vehicle EV (Sch) Rate A; Closed rate", _epoch(2026, 3, 27), _epoch(2025, 12, 1), scale=1.25)
EV_ENDED = rate(_lab("f", 3), "Electric Vehicle EV-B", _epoch(2019, 4, 12), _epoch(2021, 12, 31))
FLAT_OPEN = dict(ITEMS[3], label=_lab("f", 4), name="E-1 Residential Service", startdate=_epoch(2026, 3, 27))
LARGE = [*EV2, EV_OPEN, EV_CLOSED, EV_ENDED, FLAT_OPEN]
TODAY = date(2026, 10, 13)


def test_a_rate_closed_to_new_customers_is_not_read_as_ended_before_it_began():
    """OpenEI gives a closed rate's newest version the day it closed as its end: start
    2026-03-27, end 2025-12-01. That is no end, and never a start after an end."""
    row = urdb.summary([urdb.trim(EV_CLOSED)], TODAY)
    assert row["start"] == date(2026, 3, 27) and row["end"] is None and row["closed_since"] == date(2025, 12, 1)
    assert row["closed"] and not row["open"] and not row["ended"] and row["current"]
    assert row["status"] == "Closed to new customers since Dec 1, 2025"
    ended = urdb.summary([urdb.trim(EV_ENDED)], TODAY)
    assert ended["ended"] and not ended["open"] and ended["status"] == "Ended Dec 31, 2021"
    named = urdb.summary([urdb.trim(dict(FLAT_OPEN, name="E-7 Time of Use; Closed Rate"))], TODAY)
    assert named["closed"] and named["status"] == "Closed to new customers" and named["closed_since"] is None, "closed by its name alone"
    assert urdb.summary([urdb.trim(EV_OPEN)], TODAY)["open"]


def test_the_list_puts_the_plan_like_yours_first_then_open_ev_plans_then_the_rest():
    found = urdb.listing([urdb.trim(x) for x in LARGE], TODAY, "", {"name": "Springfield EV2A", "source": "manual"})
    rows = found["rows"]
    assert [r["name"] for r in rows] == [
        "Electric Vehicle EV2 (Sch)",
        "Electric Vehicle EV (Sch)",
        "E-1 Residential Service",
        "Electric Vehicle EV (Sch) Rate A; Closed rate",
        "Electric Vehicle EV-B",
    ]
    assert rows[0]["like"] and rows[0]["versions"] == 13 and rows[0]["first"] == date(2014, 3, 1) and rows[0]["ev"]
    assert [r["like"] for r in rows[1:]] == [False] * 4 and [r["ev"] for r in rows] == [True, True, False, True, True]
    assert [r["open"] for r in rows] == [True, True, True, False, False] and found["open"] == 3
    assert all(r["end"] is None or r["start"] <= r["end"] for r in rows), "no row reads as ending before it starts"
    # the plan in use came from OpenEI: that very tariff, by any of its versions' ids
    mine = urdb.listing([urdb.trim(x) for x in LARGE], TODAY, "", {"name": "Whatever", "source": f"urdb:{_lab('e', 4)}"})["rows"]
    assert mine[0]["mine"] and mine[0]["name"] == "Electric Vehicle EV2 (Sch)" and not any(r["like"] for r in mine)
    # a typed-in plan named for no tariff flags none
    assert not any(r["like"] for r in urdb.listing([urdb.trim(x) for x in LARGE], TODAY, "", {"name": "Home", "source": "manual"})["rows"])


@pytest.mark.parametrize(
    "mine, theirs, alike",
    [
        ("PG&E EV2A", "Electric Vehicle EV2 (Sch)", 1),  # the rate's option letter
        ("EV-A", "Electric Vehicle EV (Sch) Rate A; Closed rate", 2),  # "EV Rate A" is EV-A
        ("E-TOU-C", "E-TOU-C Residential Time of Use Baseline Region P", 2),
        ("PG&E EV2A", "Electric Vehicle EV (Sch)", 0),
        ("Home", "Electric Vehicle EV2 (Sch)", 0),
        ("Time of use", "E-TOU-D Residential Time of Use", 0),  # plain words are no code
    ],
)
def test_a_plan_is_like_another_by_the_code_in_its_name(mine, theirs, alike):
    assert urdb.likeness(urdb.codes(mine), urdb.codes(theirs)) == alike


def test_regional_variants_are_one_plan_and_many_like_plans_flag_none():
    """Twenty baseline regions of one tariff are one family: flagged once, listed once in the
    short list with how many variants it has. Four different tariffs alike flag none."""
    regions = [dict(EV_OPEN, label=_lab("a", i), name=f"E-TOU-C Residential Time of Use Baseline Region {c}{' (All Electric Service)' if i % 2 else ''}") for i, c in enumerate("PPQQRS")]
    found = urdb.listing([urdb.trim(x) for x in regions], TODAY, "", {"name": "E-TOU-C", "source": "manual"})
    assert sum(r["like"] for r in found["rows"]) == 1 and len(found["short"]) == 1
    assert found["short"][0]["family"] == "E-TOU-C Residential Time of Use" and found["short"][0]["variants"] == 6
    assert [r["name"][-24:] for r in urdb.kin_of(found["rows"], _lab("a", 3))][:2] == ["Time of Use Baseline Region P"[-24:], "line Region P (All Electric Service)"[-24:]]
    many = [dict(EV_OPEN, label=_lab("b", i), name=f"E-TOU-{c} Residential") for i, c in enumerate("ABCD")]
    rows = urdb.listing([urdb.trim(x) for x in many], TODAY, "", {"name": "E-TOU", "source": "manual"})["rows"]
    assert not any(r["like"] for r in rows)


def test_the_short_list_is_what_an_ev_household_chooses_between():
    found = urdb.listing([urdb.trim(x) for x in LARGE], TODAY, "", {"name": "Springfield EV2A", "source": "manual"})
    assert [r["name"] for r in found["short"]] == ["Electric Vehicle EV2 (Sch)", "Electric Vehicle EV (Sch)"], "the flat plan, the closed and the ended wait behind Show all"
    assert found["total"] == 5


# --- requests --------------------------------------------------------------------------


def test_search_requests_carry_only_what_was_typed_and_the_key():
    wire = Wire((200, {"items": [ITEMS[3]]}))
    client = urdb.Client(key=KEY, call=wire)
    got = client.all(urdb.search_params("62701"))
    url, sent = wire.asked[0]
    assert url == "https://api.openei.org/utility_rates"
    assert sent == {
        "version": 7,
        "format": "json",
        "sector": "Residential",
        "approved": "true",
        "detail": "full",
        "limit": 500,
        "orderby": "startdate",
        "direction": "desc",
        "offset": 0,
        "address": "62701",
        "api_key": KEY,
    }
    assert got == [urdb.trim(ITEMS[3])] and "description" not in got[0]
    assert urdb.search_params(" Springfield  Electric Co ")["ratesforutility"] == "Springfield Electric Co"
    assert "address" not in urdb.search_params("Springfield Electric Co")
    assert urdb.page_params(L1) == {"version": 7, "format": "json", "detail": "full", "getpage": L1}
    for bad in ("", "x" * 121):
        with pytest.raises(urdb.URDBError):
            urdb.query(bad)
    with pytest.raises(urdb.URDBError):
        urdb.page_params("../etc")
    assert "api_key" not in urdb.cache_key({**urdb.search_params("62701"), "api_key": KEY})


def test_a_relay_gets_the_same_parameters_without_a_key():
    wire = Wire((200, {"items": []}))
    client = urdb.Client(key=KEY, relay="https://relay.example/urdb", call=wire)
    client.get(urdb.page_params(L1))
    assert wire.asked == [("https://relay.example/urdb", urdb.page_params(L1))]
    assert urdb.Client(relay="https://relay.example/urdb").ready and not urdb.Client().ready


def test_a_long_list_is_read_page_by_page():
    page = [dict(ITEMS[3], label=f"{i:024x}") for i in range(urdb.PAGE)]
    wire = Wire((200, {"items": page}), (200, {"items": ITEMS[:2]}))
    assert len(urdb.Client(key=KEY, call=wire).all(urdb.search_params("62701"))) == urdb.PAGE + 2
    assert [p["offset"] for _, p in wire.asked] == [0, urdb.PAGE]


@pytest.mark.parametrize(
    "answer,says",
    [
        ((403, {"error": {"code": "API_KEY_INVALID", "message": f"An invalid api_key was supplied: {KEY}"}}), "refused the API key"),
        ((429, {"error": {"code": "OVER_RATE_LIMIT", "message": "You have exceeded your rate limit."}}), "hourly limit"),
        ((500, {"error": {"code": "ODD", "message": f"key {KEY} broke"}}), "OpenEI answered: key \\*\\*\\* broke"),
        ((502, None), "502"),
        (httpx.ReadTimeout(f"timed out reading https://api.openei.org/utility_rates?api_key={KEY}"), "20 seconds"),
        (httpx.ConnectError(f"refused https://api.openei.org/utility_rates?api_key={KEY}"), "ConnectError"),
    ],
)
def test_errors_are_friendly_and_never_carry_the_key(answer, says, caplog):
    client = urdb.Client(key=KEY, call=Wire(answer))
    with pytest.raises(urdb.URDBError, match=says) as err:
        client.get(urdb.search_params("62701"))
    assert KEY not in str(err.value) and err.value.__cause__ is None
    assert KEY not in caplog.text
    with pytest.raises(urdb.URDBError, match="needs an OpenEI API key"):
        urdb.Client().get(urdb.search_params("62701"))


# --- the cache and the key -------------------------------------------------------------


class Rows:
    """cc_urdb_cache and cc_state in memory, behind the statements urdb runs."""

    def __init__(self):
        self.cache, self.state, self.commits = {}, {}, 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def commit(self):
        self.commits += 1

    def execute(self, sql, args=()):
        s = " ".join(sql.split()).lower()
        out = []
        if s.startswith("select items from cc_urdb_cache"):
            out = [{"items": self.cache[args[0]]}] if args[0] in self.cache else []
        elif s.startswith("insert into cc_urdb_cache"):
            self.cache[args[0]] = args[1]
        elif s.startswith("insert into cc_state"):
            self.state[args[0]] = args[1]
        elif s.startswith("select value from cc_state"):
            out = [{"value": self.state[args[0]]}] if args[0] in self.state else []
        elif s.startswith("delete from cc_state"):
            self.state.pop(args[0], None)

        class R:
            def fetchone(self):
                return out[0] if out else None

            def fetchall(self):
                return out

        return R()


def test_a_search_is_asked_once_a_day():
    rows = Rows()
    wire = Wire((200, {"items": ITEMS}))
    client = urdb.Client(key=KEY, call=wire)
    params = urdb.search_params(UTILITY)
    assert urdb.fetch(lambda: rows, client, params, cache_only=True) is None
    first = urdb.fetch(lambda: rows, client, params)
    again = urdb.fetch(lambda: rows, client, params)
    assert first == again and len(wire.asked) == 1
    assert all(KEY not in k and KEY not in v for k, v in rows.cache.items()), "the key is in no cache row"


def test_the_key_is_sealed_shown_by_its_last_four_and_removable():
    import sources

    vault, rows = sources.Vault(sources.new_key()), Rows()
    assert urdb.key_hint(rows) is None and urdb.load_key(rows, vault) is None
    urdb.save_key(rows, vault, urdb.check_key(f"  {KEY} "))
    assert KEY not in rows.state[urdb.KEY_STATE] and urdb.key_hint(rows) == "…wxyz" and urdb.load_key(rows, vault) == KEY
    assert urdb.load_key(rows, sources.Vault(sources.new_key())) is None, "another install's key opens nothing"
    urdb.forget_key(rows)
    assert urdb.key_hint(rows) is None
    for bad in ("", "short", "has spaces in it and is long enough", "x" * 81):
        with pytest.raises(urdb.URDBError):
            urdb.check_key(bad)


def test_money_formats():
    assert urdb._money(12.5) == "$12.50" and urdb._money(0.39167) == "$0.39167" and urdb._money(0.31) == "$0.31" and urdb._money(1234.5) == "$1,234.50"
    assert urdb._day(_epoch(2024, 4, 1)) == date(2024, 4, 1) and urdb._day("x") is None
    assert urdb._day(0) == date(1970, 1, 1)


def test_the_request_log_line_carries_no_key(monkeypatch, caplog):
    """The real transport: httpx's own INFO line names the URL, key and all, unless masked."""
    import logging

    mock = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"items": []})))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: mock.get(url, **kw))
    caplog.set_level(logging.DEBUG, logger="httpx")
    assert urdb.Client(key=KEY).get(urdb.search_params("62701")) == []
    assert "api_key=***" in caplog.text and KEY not in caplog.text


# --- review fixes ----------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_every_request_of_one_search_shares_one_deadline():
    """Three pages at 12 s each would take 36 s; the search stops at the deadline, and says it."""
    clock = Clock()
    page = [dict(ITEMS[3], label=f"{i:024x}") for i in range(urdb.PAGE)]

    def slow(url, params, timeout=None):
        wire.timeouts.append(timeout)
        clock.t += 12
        return 200, {"items": page}

    wire = Wire()
    client = urdb.Client(key=KEY, call=slow, clock=clock)
    with pytest.raises(urdb.URDBError, match=f"did not answer within {urdb.DEADLINE_S} seconds"):
        client.all(urdb.search_params("62701"))
    assert urdb.DEADLINE_S == 20 and wire.timeouts == [20, 8], "each request gets only what is left"


def test_a_search_cut_at_the_page_cap_says_so_and_asks_newest_first():
    page = [dict(ITEMS[3], label=f"{i:024x}", name=f"Plan {i}") for i in range(urdb.PAGE)]
    wire = Wire(*[(200, {"items": page})] * urdb.MAX_PAGES)
    got = urdb.Client(key=KEY, call=wire).all(urdb.search_params("62701"))
    assert got.more and len(wire.asked) == urdb.MAX_PAGES
    assert {(p["orderby"], p["direction"]) for _, p in wire.asked} == {("startdate", "desc")}
    found = urdb.listing(got, date(2026, 10, 4))
    assert found["more"] and found["total"] > len(found["rows"]) == urdb.SHOWN
    assert not urdb.listing([urdb.trim(x) for x in ITEMS], date(2026, 10, 4))["more"]


def test_every_version_is_fetched_by_following_supersedes():
    pages = {x["label"]: urdb.trim(x) for x in ITEMS}
    asked = []
    got = urdb.chain(lambda label: asked.append(label) or pages.get(label), L3)
    assert [x["label"] for x in got] == [L3, L2, L1] and asked == [L3, L2, L1]
    loop = {L1: dict(pages[L1], supersedes=L2), L2: pages[L2]}
    assert [x["label"] for x in urdb.chain(loop.get, L2)] == [L2, L1], "a loop ends"
    assert urdb.chain(lambda label: None, L3) == []


def test_a_same_day_duplicate_takes_the_version_clicked():
    twin = "e" * 24
    items = [urdb.trim(x) for x in ITEMS + [rate(twin, "Residential EV-TOU (Sch)", ITEMS[2]["startdate"], scale=2.0)]]
    assert urdb.versions_of(L3, items)[-1]["label"] == L3
    assert urdb.versions_of(twin, items)[-1]["label"] == twin
    assert [x["label"] for x in urdb.versions_of(L1, items)][:2] == [L1, L2]


def test_utilities_come_from_the_bundled_list_by_name_initials_or_alias():
    pge = 14328
    for typed in ("pg&e", "PG&E", "pge", "pacific gas", "Pacific Gas & Electric Co", "pacific gas & electric co."):
        assert urdb.suggest(typed)[0]["eia"] == pge, typed
    assert urdb.utility_for("pg&e")["name"] == "Pacific Gas & Electric Co"
    assert urdb.utility_for("Pacific Gas & Electric Co.")["eia"] == pge
    assert urdb.utility_for("pacific") is None, "a partial name is a suggestion, not a pick"
    assert urdb.suggest("") == [] and len(urdb.suggest("electric")) == 8
    assert urdb.search_params(eia=pge) == {**urdb.search_params("62701", eia=pge)}, "a pick wins over the text"
    assert urdb.search_params(eia=pge)["eia"] == pge and "ratesforutility" not in urdb.search_params(eia=pge)
    with pytest.raises(urdb.URDBError):
        urdb.search_params(eia=-3)
    meta = urdb.utilities_meta()
    assert meta["year"] >= 2024 and 1000 < meta["count"] < 5000
