from discover import cluster, name_from, town_from_display


def test_cluster_merges_neighbouring_bins_and_respects_existing_places():
    home = {"id": 1, "name": "Home", "latitude": 40.7120, "longitude": -74.0060, "radius_m": 150}
    pts = [(40.7120, -74.0060)] * 40 + [(40.7113, -74.0056)] * 10  # home and its neighbour bin
    pts += [(40.7580, -73.9855)] * 6 + [(40.7584, -73.9849)] * 2  # work, two bins 60 m apart
    pts += [(40.6892, -74.0445)] * 2  # a rare stop
    pts += [(None, None)]
    new, seen = cluster(pts, [home], min_visits=3)
    assert seen == {1: 50}
    assert len(new) == 1 and new[0]["visits"] == 8
    assert abs(new[0]["latitude"] - 40.7581) < 0.0005


def test_names_prefer_a_poi_then_road_and_town():
    assert name_from({"amenity": "Tesla Supercharger", "road": "Main St", "city": "Gilroy"}) == "Tesla Supercharger"
    assert name_from({"road": "Elm St", "city": "Springfield"}) == "Elm St, Springfield"
    assert name_from({"road": "Industrial Way", "town": "Springfield"}, "the office") == "the office"
    assert name_from({"state": "California"}) == "California"
    assert name_from({}) == "Unnamed"


def test_town_comes_out_of_display_name_when_the_dict_hides_it():
    assert town_from_display("Oak Avenue, Springfield, Sangamon County, Illinois, 62701, United States") == "Springfield"
    assert town_from_display("Second Street, Old Town, Downtown, Capital City, California, 94143, United States") == "Capital City"
    assert town_from_display("Ridge Circle, Hillside Estates, Springfield, Sangamon County, Illinois, 62701, United States") == "Springfield"
    assert town_from_display("Zone C, Springfield International Airport, Shelby County, California, 94128, United States") == "Springfield International Airport"
    assert name_from({"road": "Oak Avenue"}, None, "Oak Avenue, Springfield, Sangamon County, Illinois, 62701, United States") == "Oak Avenue, Springfield"


def test_geocoder_prefers_local_and_never_goes_public_unless_allowed():
    from discover import NOMINATIM, geocoder_url

    assert geocoder_url("http://maps:8090", True, True) == "http://maps:8090/reverse"
    assert geocoder_url("http://maps:8090", False, True) == NOMINATIM
    assert geocoder_url("http://maps:8090", False, False) is None
    assert geocoder_url(None, False, False) is None


class FakeConn:
    """Records every statement it runs; cc_geocells is created at startup now, never here."""

    def __init__(self, rows=None):
        self.log, self.rows = [], rows or []

    def execute(self, sql, params=()):
        self.log.append(sql)
        self.params = params
        return self

    def fetchall(self):
        return self.rows

    def commit(self):
        pass


def test_pending_cells_never_runs_the_table_ddl_itself():
    from discover import pending_cells

    conn = FakeConn(rows=[(39.801, -89.654, 5)])
    cells = pending_cells(conn, 10)
    assert cells == [(39.801, -89.654)]
    assert not any("ALTER TABLE" in s or "CREATE TABLE" in s for s in conn.log), "cc_geocells is created once, at startup, not on every call"
    assert len(conn.log) == 1, "exactly the SELECT, nothing else"


def test_requeue_public_geocells_runs_one_statement():
    from discover import requeue_public_geocells

    conn = FakeConn()
    requeue_public_geocells(conn)
    assert len(conn.log) == 1
    assert "requeue = TRUE" in conn.log[0] and "source = 'public'" in conn.log[0]
    assert "OR name IS NULL" in conn.log[0], "a cell nothing could name gets another try on the new maps"


def test_save_cell_keeps_the_existing_name_when_a_requeued_lookup_fails():
    from discover import _save_cell

    conn = FakeConn()
    _save_cell(conn, 39.801, -89.654, None, None, None)  # local tried a requeued public cell, found nothing
    sql, params = conn.log[0], conn.params
    assert "COALESCE(EXCLUDED.name, cc_geocells.name)" in sql
    assert "requeue = FALSE" in sql
    assert params[2] is None  # the lookup's own name is NULL; COALESCE is what keeps the old one


def test_geocoder_route_switches_to_local_at_once_and_away_only_after_a_long_outage():
    from discover import DOWN_STREAK_LIMIT, geocoder_route

    # public not allowed: local is always the answer, up or down, so a dead local
    # endpoint never restarts the connector (it just names nothing, per privacy._Redirect)
    assert geocoder_route("http://maps:8090", True, False, 0, None) == ("http://maps:8090/", 0)
    assert geocoder_route("http://maps:8090", False, False, 0, "http://maps:8090/") == ("http://maps:8090/", 0)

    # no local add-on configured at all
    assert geocoder_route(None, False, True, 0, None) == ("public", 0), "public is stored explicitly: a missing row means no decision yet, which the connector reads as off"
    assert geocoder_route(None, False, False, 0, "") == ("", 0)

    # public allowed: switching TO local happens on the first confirmed-up tick
    assert geocoder_route("http://maps:8090", True, True, 3, None) == ("http://maps:8090/", 0)

    # switching AWAY from local waits out DOWN_STREAK_LIMIT consecutive down ticks
    current, streak = "http://maps:8090/", 0
    changes = 0
    for _ in range(DOWN_STREAK_LIMIT - 1):
        want, streak = geocoder_route("http://maps:8090", False, True, streak, current)
        if want != current:
            changes += 1
        current = want
    assert current == "http://maps:8090/" and changes == 0, "not yet — still inside the streak"
    want, streak = geocoder_route("http://maps:8090", False, True, streak, current)
    assert want == "public", "switched away to the public geocoder after enough consecutive downs"


def test_geocoder_route_flapping_changes_at_most_once():
    """Alternating up/down never re-triggers once local is on: switching to local is
    immediate, but switching away needs DOWN_STREAK_LIMIT consecutive downs, which
    alternation never reaches."""
    from discover import geocoder_route

    current, streak, changes = None, 0, 0
    for i in range(10):
        up = i % 2 == 0
        want, streak = geocoder_route("http://maps:8090", up, True, streak, current)
        if want != current:
            changes += 1
        current = want
    assert changes <= 1


def test_reverse_geocode_raises_on_an_error_body():
    from discover import reverse_geocode

    class Client:
        def get(self, *a, **k):
            return self

        def raise_for_status(self):
            pass

        def json(self):
            return {"error": "Unable to geocode"}

    import pytest

    with pytest.raises(ValueError):
        reverse_geocode(39.801, -89.654, Client(), "http://maps:8090/reverse")


class GeoConn:
    """Enough of a connection for geocode_cells: the cell query, a cell's stored name, the
    connector's stored road and city for it, and what got saved."""

    def __init__(self, cells, stored=None, connector=None):
        self.cells, self.saved, self.stored, self.connector = cells, [], stored, connector
        self.asked_connector = 0

    def execute(self, sql, params=()):
        s = sql.strip()
        self._one = None
        if s.startswith("WITH ends"):
            self._rows = self.cells
        elif s.startswith("INSERT INTO cc_geocells"):
            self.saved.append(params)
        elif s.startswith("SELECT name, address FROM cc_geocells"):
            self._one = self.stored
        elif "locations" in s:
            self.asked_connector += 1
            self._one = self.connector
        return self

    def fetchall(self):
        return getattr(self, "_rows", [])

    def fetchone(self):
        return self._one

    def commit(self):
        pass


class Resp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeHttpxClient:
    def __init__(self, responder):
        self.responder = responder

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, url, **kw):
        return self.responder(url)


def test_geocode_cells_falls_back_to_public_when_local_says_unable(monkeypatch):
    import httpx

    import discover

    calls = []

    def responder(url):
        calls.append(url)
        if url.startswith("http://maps"):
            return Resp({"error": "unable"})
        return Resp({"address": {"road": "Elm St", "city": "Springfield"}, "display_name": "Elm St, Springfield"})

    monkeypatch.setattr(httpx, "Client", lambda: FakeHttpxClient(responder))
    conn = GeoConn(cells=[(39.801, -89.654, 1)])
    sleeps = []
    n = discover.geocode_cells(conn, limit=5, pause_s=0.0, url="http://maps:8090/reverse", source="local", fallback_url=discover.NOMINATIM, sleep=sleeps.append)
    assert n == 1 and len(calls) == 2, "one local try, then the public fallback"
    assert calls[0].startswith("http://maps") and calls[1] == discover.NOMINATIM
    lat, lon, name, address, source = conn.saved[0]
    assert name == "Elm St, Springfield" and source == "public", "the fallback is recorded as public, not local"
    assert sleeps == [discover.PUBLIC_PAUSE_S], "a public request (even a fallback) is paced, regardless of pause_s"


def test_geocode_cells_leaves_a_cell_pending_when_nothing_can_answer(monkeypatch):
    import httpx

    import discover

    monkeypatch.setattr(httpx, "Client", lambda: FakeHttpxClient(lambda url: Resp({"error": "unable"})))
    conn = GeoConn(cells=[(39.801, -89.654, 1)])
    sleeps = []
    n = discover.geocode_cells(conn, limit=5, pause_s=0.0, url="http://maps:8090/reverse", source="local", fallback_url=None, sleep=sleeps.append)
    assert n == 1, "it still counts as processed (throttled by tried_at), not retried every pass"
    lat, lon, name, address, source = conn.saved[0]
    assert name is None and source is None, "unnamed: pending_cells offers it again after RETRY_AFTER_H, never named wrongly"
    assert sleeps == [0.0], "no public request was made, so the local-fast pause applies"


def test_geocode_cells_paces_every_public_hit_not_just_the_primary(monkeypatch):
    """Local-fast mode (pause_s=0.0) must not let the fallback hammer the public
    Nominatim: every cell local can't name used to trigger an immediate public request,
    up to `limit` a minute against OSM's 1 req/s policy."""
    import httpx

    import discover

    monkeypatch.setattr(httpx, "Client", lambda: FakeHttpxClient(lambda url: Resp({"error": "unable"})))
    conn = GeoConn(cells=[(39.801 + i * 0.001, -89.654, 1) for i in range(5)])
    sleeps = []
    n = discover.geocode_cells(conn, limit=5, pause_s=0.0, url="http://maps:8090/reverse", source="local", fallback_url=discover.NOMINATIM, sleep=sleeps.append)
    assert n == 5
    assert sleeps == [discover.PUBLIC_PAUSE_S] * 5, "every cell fell back to public, so every cell is paced at >= 1.1s"


def test_geocode_cells_caps_public_fallbacks_per_pass(monkeypatch):
    """A pass of local-fast cells (limit can be 200) must not turn into hundreds of
    public requests: at most MAX_PUBLIC_FALLBACKS fall back per call."""
    import httpx

    import discover

    calls = []

    def responder(url):
        calls.append(url)
        if url == discover.NOMINATIM:
            return Resp({"address": {"road": "Elm St", "city": "Springfield"}, "display_name": "Elm St, Springfield"})
        return Resp({"error": "unable"})

    monkeypatch.setattr(httpx, "Client", lambda: FakeHttpxClient(responder))
    n_cells = discover.MAX_PUBLIC_FALLBACKS + 5
    conn = GeoConn(cells=[(39.801 + i * 0.001, -89.654, 1) for i in range(n_cells)])
    n = discover.geocode_cells(conn, limit=n_cells, pause_s=0.0, url="http://maps:8090/reverse", source="local", fallback_url=discover.NOMINATIM, sleep=lambda _s: None)
    assert n == n_cells
    public_calls = sum(1 for c in calls if c == discover.NOMINATIM)
    assert public_calls == discover.MAX_PUBLIC_FALLBACKS, "no more than the cap falls back, even though every cell asked"
    assert all(name is None for _, _, name, _, _ in conn.saved[discover.MAX_PUBLIC_FALLBACKS :]), "past the cap, a cell stays pending rather than skip the pace limit"


def test_geocode_cells_stops_the_pass_when_local_itself_is_down(monkeypatch):
    """A ConnectionError/timeout talking to local means local itself is down, not "this
    one cell is unknown" — that must not fall back cell by cell (which would hammer the
    public Nominatim for every remaining cell); it stops the pass and lets the next tick's
    debounce (geocoder_route) decide whether to keep trying local at all."""
    import httpx

    import discover

    def responder(url):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "Client", lambda: FakeHttpxClient(responder))
    conn = GeoConn(cells=[(39.801, -89.654, 1), (39.802, -89.655, 1), (39.803, -89.656, 1)])
    sleeps = []
    n = discover.geocode_cells(conn, limit=5, pause_s=0.0, url="http://maps:8090/reverse", source="local", fallback_url=discover.NOMINATIM, sleep=sleeps.append)
    assert n == 0 and conn.saved == [], "the whole pass stopped at the first cell"
    assert sleeps == [], "no public call was made, so nothing to pace"


class DiscoverConn:
    """Enough of a connection for discover(): no existing places, a handful of endpoints
    at one spot, and what got inserted."""

    def __init__(self, pts):
        self.pts, self.inserted = pts, []

    def execute(self, sql, params=()):
        s = sql.strip()
        if s.startswith("SELECT id, name"):
            self._rows = []
        elif "trips" in s:
            self._rows = self.pts
        elif s.startswith("INSERT INTO cc_places"):
            self.inserted.append(params)
        return self

    def fetchall(self):
        return getattr(self, "_rows", [])

    def commit(self):
        pass


def test_discover_names_nothing_and_never_calls_the_geocoder_when_url_is_none(monkeypatch):
    import discover

    def boom(*_a, **_k):
        raise AssertionError("reverse_geocode must not be called when Settings > Maps allows nothing")

    monkeypatch.setattr(discover, "reverse_geocode", boom)
    conn = DiscoverConn(pts=[(39.801, -89.654)] * 3)
    n = discover.discover(conn, min_visits=3, dry_run=False, pause_s=0.0, url=None)
    assert n == 1 and conn.inserted
    name = conn.inserted[0][0]
    assert name.startswith("39.801"), "named by coordinates, still clustered and saved, just unnamed"


def test_discover_never_prints_a_position(monkeypatch, capsys):
    """Its output lands in logs; a position, rounded or not, does not."""
    import discover

    def fails(*_a, **_k):
        raise RuntimeError("Client error for url http://geo/reverse?lat=39.801&lon=-89.654")

    monkeypatch.setattr(discover, "reverse_geocode", fails)
    for dry in (True, False):
        discover.discover(DiscoverConn(pts=[(39.801, -89.654)] * 3), min_visits=3, dry_run=dry, pause_s=0.0, url="http://geo/reverse")
    out = capsys.readouterr()
    assert "39.8" not in out.out + out.err and "89.6" not in out.out + out.err, out


class StateConn:
    def __init__(self, value=None, has_row=True):
        self.value, self.has_row = value, has_row

    def execute(self, sql, params=()):
        return self

    def fetchone(self):
        return (self.value,) if self.has_row else None


def test_resolve_url_prefers_the_stored_decision_over_live_settings():
    from discover import resolve_url

    assert resolve_url(StateConn("http://maps:8090/")) == "http://maps:8090/reverse"
    assert resolve_url(StateConn("")) is None
    assert resolve_url(StateConn("public")) == __import__("discover").NOMINATIM


def test_resolve_url_falls_back_to_live_settings_when_nothing_is_stored(monkeypatch):
    import discover
    import settings as settings_mod
    from mapsclient import MapsClient

    monkeypatch.setattr(settings_mod, "load", lambda conn: {"public_geocoder": True})
    monkeypatch.setattr(MapsClient, "status", lambda self: None)  # no local maps configured or it is down
    monkeypatch.delenv("MAPS_URL", raising=False)
    assert discover.resolve_url(StateConn(has_row=False)) == discover.NOMINATIM


def test_specificity_ranks_a_road_or_place_over_a_bare_town():
    from discover import specificity

    assert specificity("Evergreen Terrace, Springfield", "Evergreen Terrace, Springfield, Sangamon County, Illinois") == 2
    assert specificity("Bravo Market", "Bravo Market, 742 Evergreen Terrace, Springfield, Sangamon County, Illinois") == 2
    assert specificity("Springfield", "Springfield, Sangamon County, Illinois") == 1
    assert specificity("Springfield", "Springfield, Sangamon County, Illinois, 62701, United States") == 1
    assert specificity("Springfield", None) == 1
    assert specificity(None) == 0 and specificity("") == 0 and specificity("Unnamed") == 0


def _local(address):
    def responder(url):
        return Resp({"address": address, "display_name": ", ".join(address.values())})

    return responder


def _run(monkeypatch, responder, conn):
    import httpx

    import discover

    monkeypatch.setattr(httpx, "Client", lambda: FakeHttpxClient(responder))
    discover.geocode_cells(conn, limit=5, pause_s=0.0, url="http://maps:8090/reverse", source="local", sleep=lambda _s: None)
    return conn.saved[0]


def test_a_town_only_answer_never_replaces_a_road_name(monkeypatch):
    stored = ("Evergreen Terrace, Springfield", "Evergreen Terrace, Springfield, Illinois")
    conn = GeoConn(cells=[(39.801, -89.654, 1)], stored=stored)
    lat, lon, name, address, source = _run(monkeypatch, _local({"city": "Springfield", "state": "Illinois"}), conn)
    assert name is None and address is None and source is None, "NULLs: the upsert's COALESCE keeps the stored road name"


def test_an_answer_at_least_as_specific_replaces_the_stored_name(monkeypatch):
    stored = ("Elm Street, Springfield", "Elm Street, Springfield, Illinois")
    conn = GeoConn(cells=[(39.801, -89.654, 1)], stored=stored)
    lat, lon, name, address, source = _run(monkeypatch, _local({"road": "Evergreen Terrace", "city": "Springfield"}), conn)
    assert name == "Evergreen Terrace, Springfield" and source == "local"


def test_a_town_only_cell_falls_back_to_the_connectors_stored_road(monkeypatch):
    # The regression: a local town-only name overwrote the public "Road, City", which survives only
    # in the connector's own locations table
    stored = ("Springfield", "Springfield, Sangamon County, Illinois")
    conn = GeoConn(cells=[(39.801, -89.654, 1)], stored=stored, connector=("Evergreen Terrace", "Springfield"))
    lat, lon, name, address, source = _run(monkeypatch, _local({"city": "Springfield", "state": "Illinois"}), conn)
    assert name == "Evergreen Terrace, Springfield" and source == "public"


def test_a_road_answer_never_asks_the_connector(monkeypatch):
    conn = GeoConn(cells=[(39.801, -89.654, 1)], connector=("Elm Street", "Springfield"))
    lat, lon, name, address, source = _run(monkeypatch, _local({"road": "Evergreen Terrace", "city": "Springfield"}), conn)
    assert name == "Evergreen Terrace, Springfield" and conn.asked_connector == 0


def test_local_cells_go_back_to_the_namer_once():
    from discover import RENAME_LOCAL_ONCE

    assert "source = 'local'" in RENAME_LOCAL_ONCE and "requeue = TRUE" in RENAME_LOCAL_ONCE
    assert "NOT EXISTS (SELECT 1 FROM cc_state" in RENAME_LOCAL_ONCE and "ON CONFLICT (key) DO NOTHING" in RENAME_LOCAL_ONCE
