import json
from pathlib import Path

from geo import contains
from regions import REGION_CAP_BYTES, Index, pick

FIX = json.loads((Path(__file__).parent / "fixtures" / "index-mini.json").read_text())
IDX = Index(FIX["features"])


def test_chain_is_smallest_first():
    assert [r["id"] for r in IDX.chain(39.70, -89.50)] == ["us/illinois/springfield-area", "us/illinois", "us", "north-america"]


def test_a_hole_is_outside_its_polygon():
    area = next(f for f in FIX["features"] if f["properties"]["id"] == "us/illinois/springfield-area")["geometry"]
    assert contains(area, 39.70, -89.50) and not contains(area, 39.80, -89.65)
    assert [r["id"] for r in IDX.chain(39.80, -89.65)][0] == "us/illinois"


def test_pick_takes_the_largest_region_under_the_cap_and_never_a_continent():
    chain = IDX.chain(39.70, -89.50)
    sizes = {"us/illinois/springfield-area": 80_000_000, "us/illinois": 450_000_000, "us": 11_000_000_000, "north-america": 15_000_000_000}
    assert pick(chain, sizes)["id"] == "us/illinois"
    sizes["us/illinois"] = 900_000_000
    assert pick(chain, sizes)["id"] == "us/illinois/springfield-area"


def test_pick_falls_back_to_the_smallest_when_nothing_fits():
    chain = IDX.chain(39.70, -89.50)
    sizes = {r["id"]: REGION_CAP_BYTES + 1 for r in chain}
    assert pick(chain, sizes)["id"] == "us/illinois/springfield-area"


def test_an_unknown_size_never_counts_as_small():
    chain = IDX.chain(39.70, -89.50)
    assert pick(chain, {"us/illinois/springfield-area": 80_000_000, "us": 0, "us/illinois": None})["id"] == "us/illinois/springfield-area"


def test_nowhere_picks_nothing():
    assert IDX.chain(0.0, 0.0) == [] and pick([], {}) is None


def test_a_region_with_an_empty_outline_is_left_out():
    """Geofabrik's index can list a region whose geometry has no coordinates; reading the
    index must skip it, not fail every lookup."""
    empty = {"type": "Feature", "properties": {"id": "nowhere-land", "urls": {"pbf": "https://example.invalid/x.osm.pbf"}}, "geometry": {"type": "MultiPolygon", "coordinates": [[[]]]}}
    idx = Index([*FIX["features"], empty])
    assert "nowhere-land" not in idx.by_id
    assert idx.chain(43.7384, 7.4246) == IDX.chain(43.7384, 7.4246)
