from datetime import datetime, timedelta, timezone
import re

from thumbs import route_svg, thin


def test_route_fits_the_box_and_marks_both_ends():
    svg = route_svg([[39.80, -89.69], [39.79, -89.68], [39.78, -89.66]], 96, 48)
    nums = [float(v) for v in re.findall(r"(-?\d+\.\d)", svg.split("points=")[1].split('"')[1].replace(",", " "))]
    xs, ys = nums[0::2], nums[1::2]
    assert 0 <= min(xs) and max(xs) <= 96 and 0 <= min(ys) and max(ys) <= 48
    assert svg.count("<circle") == 2 and 'fill="currentColor"' in svg
    assert ys[0] < ys[-1], "north is up: the start, further north, is drawn higher"


def test_a_straight_east_west_road_stays_a_line_not_a_box():
    svg = route_svg([[39.80, -89.69], [39.80, -89.60]])
    assert svg and "NaN" not in svg and "inf" not in svg


def test_too_few_points_draw_nothing():
    assert route_svg([]) == "" and route_svg([[1, 2]]) == "" and route_svg([[None, 2], [1, None]]) == ""


def test_thin_keeps_the_ends_and_the_count():
    pts = [[i, i] for i in range(200)]
    out = thin(pts, 40)
    assert len(out) == 40 and out[0] == [0, 0] and out[-1] == [199, 199]
    assert thin(pts[:10], 40) == pts[:10]


def test_routes_for_hands_each_trip_its_own_positions():
    from datetime import datetime, timedelta, timezone

    from thumbs import routes_for

    t0 = datetime(2026, 9, 16, 16, 0, tzinfo=timezone.utc)
    m = lambda k: t0 + timedelta(minutes=k)
    trips = [{"id": 1, "start_date": m(0), "destination_date": m(10)}, {"id": 2, "start_date": m(60), "destination_date": m(70)}, {"id": 3, "start_date": m(200), "destination_date": m(201)}]
    rows = [(m(k), 37.0 + k / 1000, -122.0) for k in range(0, 12)] + [(m(65), 37.5, -122.1), (m(66), 37.6, -122.2)] + [(m(200), 38.0, None)]
    out = routes_for(trips, rows)
    assert len(out[1]) == 12 and out[1][0] == [37.0, -122.0] and out[1][-1] == [37.011, -122.0], "ends at the next fix, where it parked"
    assert out[2] == [[37.5, -122.1], [37.6, -122.2]]
    assert 3 not in out, "one usable point is not a route"


def test_a_route_starts_and_ends_where_the_car_was_parked():
    """The car reports every ~45 s while it drives, so the first driving fix can be a few hundred
    metres from the spot it left. The parked fix either side is the true start and end."""
    from thumbs import anchored

    parked_a, parked_b = [39.8000, -89.6000], [39.8100, -89.6100]
    route = [[39.8010, -89.6010], [39.8050, -89.6050], [39.8090, -89.6090]]
    assert anchored(route, parked_a, parked_b) == [parked_a, *route, parked_b]
    assert anchored(route, None, None) == route


def test_a_parked_fix_far_from_the_route_is_not_drawn_to():
    """A stale fix (positions missing, a different day's spot) would be a straight line across the map."""
    from thumbs import anchored

    route = [[39.8010, -89.6010], [39.8050, -89.6050]]
    far = [39.9000, -89.6010]  # about 11 km north
    assert anchored(route, far, far) == route
    assert anchored(route, route[0], route[-1]) == route, "the same spot twice adds nothing"


def test_routes_for_adds_the_parked_fixes_either_side():
    from thumbs import routes_for

    t0 = datetime(2026, 10, 3, 15, 50, tzinfo=timezone.utc)
    rows = [
        (t0 - timedelta(minutes=5), 39.8000, -89.6000),
        (t0 + timedelta(seconds=45), 39.8010, -89.6010),
        (t0 + timedelta(minutes=5), 39.8050, -89.6050),
        (t0 + timedelta(minutes=9), 39.8090, -89.6090),
        (t0 + timedelta(minutes=11), 39.8100, -89.6100),
    ]
    trip = {"id": 1, "start_date": t0, "destination_date": t0 + timedelta(minutes=10)}
    pts = routes_for([trip], rows)[1]
    assert pts[0] == [39.8000, -89.6000] and pts[-1] == [39.8100, -89.6100] and len(pts) == 5
