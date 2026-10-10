from maps_check import (
    agrees,
    cell_key,
    format_candidates,
    format_line,
    in_named_place,
    is_freeway,
    local_display,
    normalize,
    run,
    public_display,
    sample_stops,
    totals_line,
)

HOME = {"latitude": 39.80, "longitude": -89.65, "radius_m": 150}


def test_normalize_is_case_and_space_insensitive():
    assert normalize("  Oak Ave  ") == "oak ave"
    assert normalize(None) == ""
    assert normalize("") == ""


def test_in_named_place_uses_the_radius():
    assert in_named_place(39.80, -89.65, [HOME])
    assert not in_named_place(39.90, -89.65, [HOME])


def test_sample_stops_picks_busiest_distinct_cells_and_skips_named_places():
    endpoints = (
        [{"lat": 39.904, "lon": -89.704, "road": "Elm St", "city": "Springfield"}] * 5
        + [{"lat": 39.908, "lon": -89.708, "road": "Elm St", "city": "Springfield"}] * 2  # same 0.01 cell
        + [{"lat": 39.95, "lon": -89.60, "road": "Pine Rd", "city": "Shelbyville"}] * 3
        + [{"lat": 39.80, "lon": -89.65, "road": "Home St", "city": "Springfield"}] * 9  # inside the named place
    )
    stops = sample_stops(endpoints, [HOME], limit=20)
    assert [s["count"] for s in stops] == [7, 3]
    assert stops[0]["road"] == "Elm St"
    assert all(s["cell"] != cell_key(39.80, -89.65) for s in stops)


def test_sample_stops_is_deterministic_on_a_count_tie():
    endpoints = [
        {"lat": 39.95, "lon": -89.60, "road": "Pine Rd", "city": "Shelbyville"},
        {"lat": 39.90, "lon": -89.70, "road": "Elm St", "city": "Springfield"},
    ]
    stops = sample_stops(endpoints, [], limit=20)
    assert [s["road"] for s in stops] == ["Elm St", "Pine Rd"], "tied counts break on the cell key, not arrival order"


def test_sample_stops_skips_missing_coordinates():
    assert sample_stops([{"lat": None, "lon": None}], [], limit=20) == []


def test_public_display_is_the_connectors_road_and_city():
    assert public_display("Elm Street", "Springfield") == {"road": "Elm Street", "city": "Springfield", "raw": "Elm Street, Springfield"}
    assert public_display(None, "Springfield") == {"road": None, "city": "Springfield", "raw": "Springfield"}
    assert public_display(None, None) == {"road": None, "city": None, "raw": None}


def test_is_freeway_spots_interstates_and_routes_not_streets():
    assert is_freeway("I 55 South") and is_freeway("I-55") and is_freeway("US 101") and is_freeway("Route 4")
    assert is_freeway("Springfield Freeway") and is_freeway("Bravo Expressway")
    assert not is_freeway("Evergreen Terrace") and not is_freeway("Industrial Way") and not is_freeway(None)


def test_local_display_reads_a_reverse_answer():
    assert local_display({"address": {"road": "Oak Ave", "city": "Springfield"}}) == {"road": "Oak Ave", "city": "Springfield"}
    assert local_display({"address": {"road": "Oak Ave", "town": "Shelbyville"}}) == {"road": "Oak Ave", "city": "Shelbyville"}
    assert local_display(None) == {"road": None, "city": None}
    assert local_display({}) == {"road": None, "city": None}


def test_agrees_is_case_insensitive_and_never_true_on_nothing():
    assert agrees("Elm Street", "elm street")
    assert not agrees(None, None)
    assert not agrees("", "")
    assert not agrees("Elm Street", "Oak Ave")


def test_format_candidates():
    assert format_candidates([]) == "(none)"
    cands = [{"name": "Joe's Diner", "kind": "restaurant", "distance_m": 12}, {"name": "Lot A", "kind": "parking", "distance_m": 40}]
    assert format_candidates(cands) == "Joe's Diner · restaurant · 12m, Lot A · parking · 40m"


def test_format_line_never_prints_a_coordinate():
    public = {"road": "Elm Street", "city": "Springfield", "raw": "Elm Street, Springfield"}
    local = {"road": "Elm Street", "city": "Springfield"}
    line = format_line(3, public, local, [{"name": "Joe's Diner", "kind": "restaurant", "distance_m": 12}])
    assert line == "#3   public: Elm Street, Springfield   local: Elm Street, Springfield   top5: Joe's Diner · restaurant · 12m"
    assert "39." not in line and "-89." not in line and "89.65" not in line


def test_totals_line_counts_agreement():
    rows = [
        {"road_agree": True, "city_agree": True, "freeway": False, "no_road": False},
        {"road_agree": False, "city_agree": True, "freeway": True, "no_road": False},
        {"road_agree": False, "city_agree": False, "freeway": False, "no_road": True},
    ]
    assert totals_line(rows) == "3 stops: road agrees 1/3, city agrees 2/3, freeway named 1, no road 1"


class FakeConn:
    """Mimics a raw psycopg connection enough for `run`: `.execute(sql, params)` returns an
    object with `.fetchall()`/`.fetchone()`, rows as tuples."""

    def __init__(self, endpoints=(), places=()):
        self.endpoints, self.places = list(endpoints), list(places)

    def execute(self, sql, params=()):
        if "FROM trips" in sql or "start_position_latitude" in sql:
            return FakeResult(self.endpoints)
        if "FROM cc_places" in sql:
            return FakeResult(self.places)
        raise AssertionError(f"unexpected query: {sql}")


class FakeResult:
    def __init__(self, rows, one=False):
        self.rows, self.one = rows, one

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeClient:
    def __init__(self, status=None, reverse=None, candidates=None):
        self._status, self._reverse, self._candidates = status, reverse or {}, candidates or {}

    def status(self):
        return self._status

    def reverse(self, lat, lon):
        return self._reverse.get((lat, lon))

    def candidates(self, lat, lon, limit=10):
        return self._candidates.get((lat, lon))


def test_run_exits_2_and_says_so_when_maps_is_down(capsys):
    assert run(FakeConn(), FakeClient(status=None)) == 2
    assert "maps not answering" in capsys.readouterr().err


def test_run_compares_local_with_the_connectors_public_name_and_counts_misses(capsys):
    conn = FakeConn(
        endpoints=[(39.90, -89.70, "Elm St", "Springfield")] * 4
        + [(39.95, -89.75, "Oak Ave", "Springfield")] * 3
        + [(39.97, -89.77, "Pine Rd", "Springfield")] * 2
        + [(39.99, -89.79, None, None)] * 9,
        places=[],
    )
    client = FakeClient(
        status={"version": 1},
        reverse={
            (39.90, -89.70): {"address": {"road": "Elm St", "city": "Springfield"}},
            (39.95, -89.75): {"address": {"road": "I 55 South", "city": "Springfield"}},
            (39.97, -89.77): {"address": {"city": "Springfield"}},
        },
        candidates={(39.90, -89.70): {"candidates": []}},
    )
    code = run(conn, client)
    out = capsys.readouterr().out
    assert code == 0
    assert "#1   public: Elm St, Springfield   local: Elm St, Springfield" in out
    assert "#2   public: Oak Ave, Springfield   local: I 55 South, Springfield" in out
    assert "#3   public: Pine Rd, Springfield   local: Springfield" in out
    assert "#4" not in out, "a stop the connector never named has nothing to compare against"
    assert "3 stops: road agrees 1/3, city agrees 3/3, freeway named 1, no road 1" in out
    assert "39." not in out and "-89." not in out
