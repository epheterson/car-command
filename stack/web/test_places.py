from places import DDL, distance_m, label, place_for


def test_the_icon_column_is_added_nullable():
    assert "ADD COLUMN IF NOT EXISTS icon VARCHAR" in DDL


HOME = {"name": "Home", "latitude": 40.7120, "longitude": -74.0060, "radius_m": 150}
WORK = {"name": "Work", "latitude": 40.7484, "longitude": -73.9857, "radius_m": 300}


def test_distance_is_about_right():
    assert distance_m(40.7120, -74.0060, 40.7120, -74.0060) == 0
    assert abs(distance_m(HOME["latitude"], HOME["longitude"], WORK["latitude"], WORK["longitude"]) - 4_394) < 50


def test_place_for_uses_radius_and_nearest():
    assert place_for(40.7121, -74.0059, [HOME, WORK])["name"] == "Home"
    assert place_for(40.80, -74.00, [HOME, WORK]) is None
    near_both = [{**HOME, "radius_m": 100_000}, {**WORK, "radius_m": 100_000}]
    assert place_for(40.75, -73.99, near_both)["name"] == "Work"
    assert place_for(None, None, [HOME]) is None


def test_label_falls_back_to_the_address():
    assert label(40.7121, -74.0059, [HOME], "Elm Street, Springfield") == "Home"
    assert label(40.80, -74.00, [HOME], "Somewhere St, Springfield") == "Somewhere St, Springfield"
    assert label(40.80, -74.00, [HOME], None) == "Unnamed spot"


def test_index_finds_the_same_place_as_the_flat_scan_and_skips_far_cells():
    from places import PlaceIndex, place_for
    import random

    rnd = random.Random(7)
    places = [{"id": i, "name": f"p{i}", "latitude": 39.5 + rnd.random(), "longitude": -90.0 + rnd.random(), "radius_m": 150} for i in range(600)]
    idx = PlaceIndex(places)
    for _ in range(300):
        lat, lon = 39.5 + rnd.random(), -90.0 + rnd.random()
        assert place_for(lat, lon, idx) == place_for(lat, lon, places)
    p = places[0]
    assert place_for(p["latitude"], p["longitude"], idx) is p
    assert len(idx.near(p["latitude"], p["longitude"])) < len(places)
