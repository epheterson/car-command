"""Regional average prices: the lookup, when a download is due, and the background worker.
No network: the worker is handed a fetch that returns a recorded file."""

import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import averages
from averages import Averages, due, newer, settings_view, valid
from test_rates_data import averages_data

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


def test_a_month_not_published_yet_takes_the_newest_earlier_figure_and_says_which():
    a = Averages(averages_data())
    got = a.price("US-IL", "electricity", "2026-09")
    assert got["value"] == 0.1922 and got["month"] == "2026-07" and got["wanted"] == "2026-09" and got["name"] == "Illinois"
    assert a.price("US-IL", "electricity", "2026-06")["value"] == 0.1989, "a published month is its own figure"
    assert a.price("DE", "electricity", "2026-03")["month"] == "2025-12", "Eurostat's half-years run months behind"


def test_unknown_regions_early_months_and_stale_series_are_unpriced_with_a_reason():
    a = Averages(averages_data())
    assert a.price("CA", "electricity", "2026-07") is None and a.why_not("CA", "electricity", "2026-07") == "no electricity average for CA"
    assert a.price("US-IL", "electricity", "2019-01") is None and "start in May 2026" in a.why_not("US-IL", "electricity", "2019-01")
    assert a.price("FR", "electricity", "2028-01") is None, "a figure more than two years old is not used"
    assert "newest France figure is from Dec 2025" in a.why_not("FR", "electricity", "2028-01")
    assert a.price(None, "electricity", "2026-07") is None and a.why_not(None, "electricity", "2026-07").startswith("no position")


def test_the_file_shape_is_checked_before_it_is_used():
    assert valid(averages_data())
    assert not valid({"version": 1, "regions": {}}) and not valid({"version": 2, "regions": averages_data()["regions"]}) and not valid("nope")
    bad = averages_data()
    bad["regions"]["DE"]["electricity"]["values"]["2025-07"] = "0.38"
    assert not valid(bad)


def test_the_bundled_file_is_valid_and_covers_the_places_the_tests_use():
    b = averages.bundled()
    assert b.origin == "bundled" and valid(b.data) and {"US-IL", "US-CA", "DE"} <= set(b.covered("electricity"))
    assert b.price("US-IL", "petrol", "2026-09")["area"] == "Midwest (PADD 2)"


def test_a_download_is_due_when_the_copy_is_old_or_lacks_a_home_region_at_most_daily():
    bundled = Averages(averages_data(), "bundled")
    assert due(bundled, NOW, None, []) is None
    assert due(bundled, NOW + timedelta(days=46), None, []) == "older than 45 days"
    assert due(bundled, NOW + timedelta(days=46), NOW + timedelta(days=45, hours=1), []) is None, "at most once a day"
    assert due(bundled, NOW, None, ["CA"]) is not None
    fetched = Averages(averages_data(), "downloaded", NOW)
    assert due(fetched, NOW + timedelta(days=2), None, ["CA"]) is None, "the published copy lacks it too; asking daily would not change that"
    assert due(fetched, NOW + timedelta(days=46), None, []) == "older than 45 days"


def test_the_later_built_copy_is_the_one_in_force():
    old = averages_data() | {"built": "2026-09-01"}
    assert newer(Averages(averages_data(), "bundled"), Averages(old, "downloaded")).origin == "bundled"
    assert newer(Averages(old, "bundled"), Averages(averages_data(), "downloaded")).origin == "downloaded"
    assert newer(Averages(averages_data(), "bundled"), Averages(averages_data(), "downloaded")).origin == "downloaded"
    assert newer(Averages(averages_data(), "bundled"), None).origin == "bundled"


class Conn:
    """Just enough of a connection for the worker: one status row, recorded statements."""

    def __init__(self, row=None, regions=()):
        self.row, self.regions, self.sql, self.commits = row, list(regions), [], 0

    def execute(self, sql, args=()):
        self.sql.append((sql, args))
        conn = self

        class R:
            def fetchone(self):
                return dict(conn.row) if conn.row and "FROM cc_rate_averages" in sql else None

            def fetchall(self):
                return [{"region": r} for r in conn.regions] if "DISTINCT region" in sql else []

        if sql.lstrip().startswith("INSERT INTO cc_rate_averages"):
            cols = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")][1:]  # id is the literal 1
            self.row = {**(self.row or {}), "error": None, **dict(zip(cols, args))}
        return R()

    def commit(self):
        self.commits += 1


def worker(conn, fetch):
    @contextmanager
    def connect():
        yield conn

    seen = []
    w = averages.RatesWorker(connect=connect, fetch=fetch, url=averages.RATES_URL, after_change=lambda c: seen.append("reopened"), missing=averages.missing_regions)
    return w, seen


def test_the_worker_downloads_the_whole_file_from_one_fixed_url(monkeypatch):
    monkeypatch.setattr(averages, "_ACTIVE", Averages(averages_data() | {"built": "2026-08-01"}, "bundled"))
    monkeypatch.setattr(averages, "bundled", lambda: Averages(averages_data() | {"built": "2026-08-01"}, "bundled"))
    asked = []
    newer_file = averages_data() | {"built": "2026-10-04"}
    newer_file["regions"]["US-IL"]["electricity"]["values"]["2026-08"] = 0.1950
    w, seen = worker(Conn(), lambda url: asked.append(url) or json.dumps(newer_file).encode())
    assert w.round(NOW) == "updated"
    assert asked == [averages.RATES_URL] and urlparse(asked[0]).query == "" and urlparse(asked[0]).scheme == "https", "the same URL for everyone: nothing about a car in it"
    assert averages.active().origin == "downloaded" and averages.active().price("US-IL", "electricity", "2026-09")["month"] == "2026-08"
    assert seen == ["reopened"], "new figures put the charges they can price back in the queue"
    assert w.round(NOW + timedelta(hours=1)) is None, "nothing due an hour later"


def test_a_failed_download_keeps_the_copy_in_force_and_is_recorded(monkeypatch):
    copy = Averages(averages_data() | {"built": "2026-08-01"}, "bundled")
    monkeypatch.setattr(averages, "_ACTIVE", copy)

    def down(url):
        raise OSError("network unreachable")

    conn = Conn()
    w, seen = worker(conn, down)
    assert w.round(NOW) == "failed" and averages.active() is copy and not seen
    assert conn.row["error"] == "network unreachable" and conn.row["tried_at"] == NOW
    w2, _ = worker(conn, lambda url: b'{"version": 1, "regions": {}}')
    w2.request()
    assert w2.round(NOW + timedelta(minutes=5)) == "failed" and averages.active() is copy, "Update now asks at once; a file that is not one is refused"
    assert conn.row["error"] == "the file is not a rate averages file"


def test_a_home_region_the_bundled_copy_lacks_asks_once(monkeypatch):
    monkeypatch.setattr(averages, "_ACTIVE", Averages(averages_data(), "bundled"))
    monkeypatch.setattr(averages, "bundled", lambda: Averages(averages_data(), "bundled"))
    w, _ = worker(Conn(regions=["US-IL", "CA"]), lambda url: json.dumps(averages_data()).encode())
    assert w.round(NOW) == "unchanged"


def test_settings_shows_the_home_region_and_the_last_check():
    a = Averages(averages_data(), "bundled")
    view = settings_view(a, {"tried_at": NOW, "error": "timed out"}, [{"region": "US-IL", "n": 40, "est": 12}, {"region": "", "n": 1, "est": 0}], NOW, averages.RATES_URL)
    (h,) = view["homes"]
    assert h["name"] == "Illinois" and h["estimated"] == 12 and h["price"]["month"] == "2026-07" and h["petrol"]["month"] == "2026-09"
    assert view["error"] == "timed out" and view["built"] == date(2026, 10, 4) and {s["source"] for s in view["sources"]} >= {"US EIA, average residential electricity price"}


def test_after_one_good_download_only_age_counts(monkeypatch):
    """The bundled copy is newer than the published one and stays in force; a home charge
    in Canada must not send a download every day after the first."""
    monkeypatch.setattr(averages, "_ACTIVE", Averages(averages_data(), "bundled"))
    monkeypatch.setattr(averages, "bundled", lambda: Averages(averages_data(), "bundled"))
    older = averages_data() | {"built": "2026-09-01"}
    asked = []
    conn = Conn(regions=["US-IL", "CA"])
    w, _ = worker(conn, lambda url: asked.append(url) or json.dumps(older).encode())
    assert w.round(NOW) in ("updated", "unchanged") and averages.active().origin == "bundled"
    assert w.round(NOW + timedelta(days=1, hours=1)) is None and len(asked) == 1
    assert due(averages.active(), NOW + timedelta(days=2), NOW, ["CA"], fetched_at=NOW) is None


def test_no_transaction_is_open_while_the_file_downloads(monkeypatch):
    monkeypatch.setattr(averages, "_ACTIVE", Averages(averages_data() | {"built": "2026-08-01"}, "bundled"))
    monkeypatch.setattr(averages, "bundled", lambda: Averages(averages_data() | {"built": "2026-08-01"}, "bundled"))
    open_tx, conns = [], []

    class TxConn(Conn):
        def execute(self, sql, args=()):
            open_tx.append(self)
            return super().execute(sql, args)

        def commit(self):
            super().commit()
            while self in open_tx:
                open_tx.remove(self)

    @contextmanager
    def connect():
        c = TxConn()
        conns.append(c)
        yield c

    def fetch(url):
        assert not open_tx, "a transaction is held across the download"
        data = averages_data()
        data["regions"]["US-IL"]["electricity"]["values"]["2026-08"] = 0.1950
        return json.dumps(data).encode()

    w = averages.RatesWorker(connect=connect, fetch=fetch, url=averages.RATES_URL, after_change=lambda c: None, missing=averages.missing_regions)
    assert w.round(NOW) == "updated" and len(conns) == 2 and not open_tx, "one short look before, one short write after, both committed"
