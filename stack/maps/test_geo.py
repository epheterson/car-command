from geo import bbox, contains

POLY = {"type": "Polygon", "coordinates": [[[-89.72, 39.75], [-89.58, 39.75], [-89.58, 39.85], [-89.72, 39.85], [-89.72, 39.75]]]}
MULTIPOLY = {"type": "MultiPolygon", "coordinates": [POLY["coordinates"], [[[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0], [1.0, 1.0]]]]}
POINT = {"type": "Point", "coordinates": [-89.65, 39.8]}
LINE = {"type": "LineString", "coordinates": [[-89.7, 39.8], [-89.6, 39.81], [-89.5, 39.8]]}
MULTILINE = {"type": "MultiLineString", "coordinates": [[[-89.7, 39.8], [-89.6, 39.81]], [[1.0, 1.0], [2.0, 2.0]]]}
MULTIPOINT = {"type": "MultiPoint", "coordinates": [[-89.7, 39.8], [-89.6, 39.81]]}
COLLECTION = {"type": "GeometryCollection", "geometries": [POINT, LINE]}


def test_bbox_handles_a_polygon_by_its_outer_ring():
    assert bbox(POLY) == (39.75, -89.72, 39.85, -89.58)


def test_bbox_handles_a_point():
    assert bbox(POINT) == (39.8, -89.65, 39.8, -89.65)


def test_bbox_handles_a_line():
    assert bbox(LINE) == (39.8, -89.7, 39.81, -89.5)


def test_bbox_handles_a_multiline():
    assert bbox(MULTILINE) == (1.0, -89.7, 39.81, 2.0)


def test_bbox_handles_a_multipoint():
    assert bbox(MULTIPOINT) == (39.8, -89.7, 39.81, -89.6)


def test_bbox_handles_a_multipolygon():
    assert bbox(MULTIPOLY) == (1.0, -89.72, 39.85, 2.0)


def test_bbox_handles_a_geometry_collection():
    assert bbox(COLLECTION) == (39.8, -89.7, 39.81, -89.5)


def test_contains_is_polygon_only_and_false_for_a_line():
    assert contains(LINE, 39.805, -89.6) is False
    assert contains(POINT, 39.8, -89.65) is False
