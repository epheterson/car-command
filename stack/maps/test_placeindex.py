import json
from pathlib import Path

import pytest

from placeindex import Places, build, parse_query

FIX = Path(__file__).parent / "fixtures" / "places.geojsonseq"
LINES_FIX = Path(__file__).parent / "fixtures" / "lines.geojsonseq"


@pytest.fixture()
def places(tmp_path):
    db = tmp_path / "places.sqlite"
    build(FIX, db)
    return Places(db)


def test_city_county_state(places):
    assert places.city_for(39.8010, -89.6500) == {
        "city": "Springfield",
        "county": "Sangamon County",
        "state": "Illinois",
        "kind": "city",
    }


def test_address_only_when_close(places):
    assert places.address_for(39.8010, -89.6500) == {
        "house_number": "742",
        "road": "Evergreen Terrace",
    }
    assert places.address_for(39.8100, -89.6500) is None


def test_candidates_put_the_building_you_are_in_first_then_near_before_far(places):
    c = places.candidates(39.8010, -89.6500)
    names = [x["name"] for x in c]
    assert names[0] == "Bravo Market" and c[0]["inside"] is True
    assert names.index("Alpha Coffee") < names.index("Charlie Fuel")
    assert "Far Cafe" not in names and len(c) <= 10
    assert c[0]["kind"] == "supermarket" and c[0]["osm_id"] == "w10"


def test_boundaries_and_bare_addresses_are_not_candidates(places):
    names = {x["name"] for x in places.candidates(39.8010, -89.6500)}
    assert not names & {"Springfield", "Sangamon County", "Illinois", "742"}


def test_build_does_not_raise_on_real_osmium_line_geometries(tmp_path):
    """Osmium's geojsonseq export carries LineString/MultiLineString roads and ways (named
    streets, addr:* on an open way, a GeometryCollection) alongside Points -- build() must
    walk all of them, not just the Polygon/MultiPolygon shapes the fixture used to assume."""
    db = tmp_path / "lines.sqlite"
    build(LINES_FIX, db)  # must not raise
    places = Places(db)
    # the address on a line is indexed at its midpoint
    assert places.address_for(39.801, -89.65) == {"house_number": "742", "road": "Evergreen Terrace"}
    # a named way (MultiLineString) and a named GeometryCollection both get a centre
    names = {c["name"] for c in places.candidates(39.715, -89.685)}
    assert "Alpha Trail" in names
    names = {c["name"] for c in places.candidates(39.605, -89.595)}
    assert "Bravo Loop" in names
    # an ordinary Point POI still works alongside them
    names = {c["name"] for c in places.candidates(39.50, -89.50)}
    assert "Charlie Bench" in names


def test_build_skips_an_unhandleable_feature_instead_of_raising(tmp_path, caplog):
    """A Polygon with coordinates: null (or any other malformed geometry) must not fail the
    whole build -- it's counted and logged, and every other feature is still indexed."""
    db = tmp_path / "lines.sqlite"
    with caplog.at_level("WARNING"):
        build(LINES_FIX, db)  # fixture's last line, "Broken Shape", has coordinates: null
    assert any("skipped" in r.message for r in caplog.records)
    places = Places(db)
    assert "Charlie Bench" in {c["name"] for c in places.candidates(39.50, -89.50)}


def test_a_wider_reach_finds_an_address_beyond_the_default_box(places):
    # ~130 m south of 742: outside the old 0.001-degree box, inside 150 m
    assert places.address_for(39.79973, -89.6501) is None
    assert places.address_for(39.79973, -89.6501, 150) == {"house_number": "742", "road": "Evergreen Terrace"}


# ---- forward search: typed text to places in the index, for the address field ----

SEARCH_SEQ = [
    {
        "type": "Feature",
        "id": "a1",
        "geometry": {"type": "Point", "coordinates": [-89.6501, 39.8009]},
        "properties": {"addr:housenumber": "742", "addr:street": "Evergreen Terrace"},
    },
    {
        "type": "Feature",
        "id": "a2",
        "geometry": {"type": "Point", "coordinates": [-89.6503, 39.8011]},
        "properties": {"addr:housenumber": "744", "addr:street": "Evergreen Terrace"},
    },
    {
        "type": "Feature",
        "id": "a3",
        "geometry": {"type": "Point", "coordinates": [-89.6400, 39.8100]},
        "properties": {"addr:housenumber": "12", "addr:street": "Elm Street"},
    },
    {
        "type": "Feature",
        "id": "a4",
        "geometry": {"type": "Point", "coordinates": [-89.6420, 39.8120]},
        "properties": {"addr:housenumber": "30", "addr:street": "North Elm Street"},
    },
    {
        "type": "Feature",
        "id": "a5",
        "geometry": {"type": "Point", "coordinates": [-88.0000, 41.0000]},
        "properties": {"addr:housenumber": "12", "addr:street": "Elm Street"},
    },
]


@pytest.fixture()
def searchable(tmp_path):
    seq = tmp_path / "s.geojsonseq"
    lines = FIX.read_text().splitlines() + [json.dumps(f) for f in SEARCH_SEQ]
    seq.write_text("\n".join(lines) + "\n")
    db = tmp_path / "s.sqlite"
    build(seq, db)
    return Places(db)


def test_a_house_number_and_the_start_of_a_street_find_the_address_with_its_city(
    searchable,
):
    got = searchable.search("742 everg", 39.80, -89.65)
    assert got[0] == {
        "label": "742 Evergreen Terrace",
        "city": "Springfield",
        "kind": "address",
        "lat": 39.8009,
        "lon": -89.6501,
    }
    assert [x["label"] for x in got] == ["742 Evergreen Terrace"], "another number on the street is not it"


def test_a_finished_abbreviation_is_spelled_out_and_one_being_typed_is_a_prefix(
    searchable,
):
    assert [x["label"] for x in searchable.search("12 Elm St", 39.80, -89.65)] == ["12 Elm Street"]
    assert [x["label"] for x in searchable.search("12 elm st ", 39.80, -89.65)] == ["12 Elm Street"]
    assert parse_query("742 Evergreen Ter, Spring") == (
        "742",
        "evergreen terrace",
        "spring",
    )
    assert parse_query("Evergreen Ter") == (None, "evergreen ter", None)
    assert parse_query("7") is None and parse_query("  ") is None


def test_a_street_alone_and_a_named_place_are_found_nearest_first(searchable):
    got = searchable.search("elm", 39.81, -89.64)
    labels = [x["label"] for x in got]
    assert labels[:2] == [
        "Elm Street",
        "North Elm Street",
    ], "a word inside the name matches too; nearest first"
    assert [x["kind"] for x in got[:2]] == ["street", "street"]
    shop = searchable.search("bravo mar", 39.80, -89.65)
    assert shop[0]["label"] == "Bravo Market" and shop[0]["kind"] == "supermarket"


def test_near_the_map_first_then_anywhere(searchable):
    far = searchable.search("12 elm", 41.0, -88.0)
    assert len(far) == 1 and far[0]["lat"] == 41.0, "the one within reach of the map, not both"
    assert searchable.search("12 elm", None, None)[0]["label"] == "12 Elm Street"


def test_a_town_after_a_comma_narrows_it(searchable):
    assert searchable.search("742 evergreen, spring", 39.80, -89.65)[0]["city"] == "Springfield"
    assert searchable.search("742 evergreen, shelbyville", 39.80, -89.65) == []


def test_typed_wildcards_are_text(searchable):
    assert searchable.search("%%", 39.80, -89.65) == [] and searchable.search("e_m", 39.80, -89.65) == []
