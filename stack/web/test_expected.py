"""An accepted command shows on the card at once, and the car's own report takes over."""

from datetime import datetime, timedelta, timezone

from expected import Expected

T0 = datetime(2026, 10, 12, 23, 0, tzinfo=timezone.utc)


def car(**kw):
    return {"vin": "ALPHA", "updated": T0 - timedelta(minutes=1), **kw}


def test_lock_shows_locked_until_the_car_agrees():
    e = Expected()
    assert e.note("ALPHA", "lock", None, car(locked=False), T0)
    assert e.apply(car(locked=False), T0 + timedelta(seconds=5))["locked"] is True
    # the car says so itself: the note is done, and a later unlock by hand shows as it is
    assert e.apply(car(locked=True), T0 + timedelta(seconds=20))["locked"] is True
    assert e.apply(car(locked=False), T0 + timedelta(seconds=25))["locked"] is False


def test_a_report_well_after_the_command_wins():
    e = Expected()
    e.note("ALPHA", "lock", None, car(locked=False), T0)
    spoke = car(locked=False, updated=T0 + timedelta(seconds=45))
    assert e.apply(spoke, T0 + timedelta(seconds=50))["locked"] is False


def test_two_minutes_at_most():
    e = Expected()
    e.note("ALPHA", "sentry_on", None, car(sentry=False), T0)
    assert e.apply(car(sentry=False), T0 + timedelta(seconds=90))["sentry"] is True
    assert e.apply(car(sentry=False), T0 + timedelta(minutes=3))["sentry"] is False


def test_climate_agrees_by_on_or_off_and_limit_by_whole_percent():
    e = Expected()
    e.note("ALPHA", "climate_on", None, car(climate="off"), T0)
    e.note("ALPHA", "charge_limit", 90, car(charge_target=80), T0)
    shown = e.apply(car(climate="off", charge_target=80), T0 + timedelta(seconds=5))
    assert shown["climate"] == "on" and shown["charge_target"] == 90
    caught = e.apply(car(climate="heating", charge_target=90.0), T0 + timedelta(seconds=10))
    assert caught["climate"] == "heating" and caught["charge_target"] == 90.0
    assert e.apply(car(climate="off"), T0 + timedelta(seconds=12))["climate"] == "off"


def test_trunk_toggles_only_from_a_known_state_and_other_cars_are_untouched():
    e = Expected()
    assert not e.note("ALPHA", "trunk", None, car(trunk_open=None), T0)
    assert e.note("ALPHA", "trunk", None, car(trunk_open=True), T0)
    assert e.apply(car(trunk_open=True), T0 + timedelta(seconds=3))["trunk_open"] is False
    assert not e.note("ALPHA", "honk", None, car(), T0)
    other = {"vin": "BRAVO", "trunk_open": True, "updated": T0}
    assert e.apply(other, T0 + timedelta(seconds=3)) == other


def test_a_lucid_trunk_opens_and_closes_by_name():
    e = Expected()
    assert e.note("ALPHA", "trunk", None, car(make="Lucid", trunk_open=None), T0)
    assert e.apply(car(make="Lucid", trunk_open=False), T0 + timedelta(seconds=3))["trunk_open"] is True
    e.note("ALPHA", "frunk_close", None, car(make="Lucid", frunk_open=True), T0)
    assert e.apply(car(make="Lucid", frunk_open=True), T0 + timedelta(seconds=3))["frunk_open"] is False
