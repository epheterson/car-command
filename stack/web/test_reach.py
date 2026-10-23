from datetime import datetime, timedelta, timezone

import reach

NOW = datetime(2026, 10, 15, 23, 30, tzinfo=timezone.utc)


def ago(**kw):
    return NOW - timedelta(**kw)


def car(status, updated, live=True):
    return {"status": status, "updated": updated, "live": live}


def test_driving_connector_car_is_judged_by_its_last_position_not_the_cloud_stamp():
    # the cloud keeps stamping the old values as new; the recorder saw the car stop moving 80 minutes ago
    c = car("Driving", ago(seconds=20))
    assert reach.seen_if_offline(c, ago(minutes=80), NOW) == ago(minutes=80)
    assert reach.seen_if_offline(c, ago(minutes=2), NOW) is None


def test_driving_connector_car_with_no_position_falls_back_to_its_stamp():
    assert reach.seen_if_offline(car("Driving", ago(minutes=30)), None, NOW) == ago(minutes=30)
    assert reach.seen_if_offline(car("Driving", ago(minutes=3)), None, NOW) is None


def test_other_sources_are_judged_by_their_own_reading():
    # a Tesla whose feed went quiet reads from history, still saying driving
    assert reach.seen_if_offline(car("Driving", ago(minutes=25), live=False), ago(seconds=5), NOW) == ago(minutes=25)
    assert reach.seen_if_offline(car("Driving", ago(minutes=4), live=False), None, NOW) is None


def test_parked_cars_are_allowed_hours():
    assert reach.seen_if_offline(car("Idle", ago(hours=2)), None, NOW) is None
    assert reach.seen_if_offline(car("Sleeping", ago(hours=5)), None, NOW) == ago(hours=5)
    assert reach.seen_if_offline(car("Charging", ago(hours=1), live=False), None, NOW) is None


def test_no_reading_is_not_offline():
    assert reach.seen_if_offline(car("Idle", None), None, NOW) is None


def test_needs_fix_only_for_a_driving_connector_car():
    assert reach.needs_fix(car("Driving", NOW)) and not reach.needs_fix(car("Idle", NOW)) and not reach.needs_fix(car("Driving", NOW, live=False))
