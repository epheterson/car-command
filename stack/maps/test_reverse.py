import json
from pathlib import Path

from placeindex import Places, build
from reverse import answer

HERE = Path(__file__).parent
LOCATE = json.loads((HERE / "fixtures" / "locate.json").read_text())


def places(tmp_path):
    db = tmp_path / "p.sqlite"
    build(HERE / "fixtures" / "places.geojsonseq", db)
    return Places(db)


def test_the_road_skips_driveways_and_parking_aisles(tmp_path):
    a = answer(39.801, -89.65, LOCATE, places(tmp_path))
    assert a["address"]["road"] == "Evergreen Terrace" and a["osm_id"] == 113 and a["osm_type"] == "way"
    assert a["address"]["city"] == "Springfield" and a["address"]["county"] == "Sangamon County" and a["address"]["state"] == "Illinois"
    assert a["address"]["house_number"] == "742"


def test_a_point_inside_a_named_place_carries_it(tmp_path):
    a = answer(39.801, -89.65, LOCATE, places(tmp_path))
    assert a["name"] == "Bravo Market" and a["address"]["amenity"] == "Bravo Market"
    assert a["display_name"].startswith("Bravo Market, 742 Evergreen Terrace, Springfield")


def test_no_index_still_names_the_road(tmp_path):
    a = answer(39.801, -89.65, LOCATE, None)
    assert a["address"] == {"road": "Evergreen Terrace"} and a["display_name"] == "Evergreen Terrace"


def test_nothing_known_is_empty():
    assert answer(39.801, -89.65, [{"edges": []}], None) == {}


def test_a_place_named_after_its_road_is_not_said_twice(tmp_path):
    # We're inside Bravo Market (a POI) at (39.801, -89.6500)
    # and the road name from locate is also "Bravo Market"
    # display_name should contain "Bravo Market" once, not twice
    locate = [
        {
            "input_lat": 39.801,
            "input_lon": -89.6500,
            "edges": [
                {
                    "distance": 5.0,
                    "edge": {"classification": {"classification": "residential", "use": "road", "link": False}},
                    "edge_info": {"way_id": 300, "names": ["Bravo Market"]},
                }
            ],
        }
    ]
    p = places(tmp_path)
    a = answer(39.801, -89.6500, locate, p)
    display = a["display_name"]
    # Bravo Market should appear once, not twice
    assert display.count("Bravo Market") == 1, f"display_name has duplicate: {display}"


def _edge(dist, cls, use, names, way_id, link=False):
    # The shape Valhalla 3.9's verbose /locate returns: the road class and use sit under edge.classification
    return {"distance": dist, "edge": {"classification": {"classification": cls, "use": use, "link": link}}, "edge_info": {"way_id": way_id, "names": names}}


def test_a_parking_lot_stop_reaches_the_named_street_beyond_the_aisles():
    locate = [{"edges": [_edge(10.0, "service_other", "parking_aisle", ["Lot Aisle"], 1), _edge(120.0, "residential", "road", ["Evergreen Terrace"], 2)]}]
    assert answer(39.801, -89.65, locate, None)["address"]["road"] == "Evergreen Terrace"


def test_a_parked_car_is_never_on_a_freeway_or_its_ramps():
    locate = [
        {
            "edges": [
                _edge(20.0, "motorway", "road", ["I 55"], 1),
                _edge(30.0, "trunk", "road", ["Route 4"], 2),
                _edge(40.0, "secondary", "ramp", ["I 55 South"], 3, link=True),
                _edge(50.0, "tertiary", "turn_channel", ["Bravo Bend"], 4, link=True),
                _edge(90.0, "residential", "road", ["Evergreen Terrace"], 5),
            ]
        }
    ]
    a = answer(39.801, -89.65, locate, None)
    assert a["address"]["road"] == "Evergreen Terrace" and a["osm_id"] == 5


def test_no_named_road_falls_back_to_the_nearest_address_points_street(tmp_path):
    # ~60 m north of 742 Evergreen Terrace: too far for its house number, close enough for its street
    a = answer(39.80144, -89.6501, [{"edges": [_edge(10.0, "service_other", "parking_aisle", [], 1)]}], places(tmp_path))
    assert a["address"]["road"] == "Evergreen Terrace" and "house_number" not in a["address"]


def test_an_address_point_beyond_150_m_names_nothing(tmp_path):
    a = answer(39.8030, -89.6501, [{"edges": []}], places(tmp_path))
    assert "road" not in a["address"]
