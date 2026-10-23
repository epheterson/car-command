import pytest

import commandcheck as cc


def tree(**vals):
    out = {}
    for path, v in vals.items():
        node = out
        parts = path.split("__")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = {"val": v}
    return out


def test_nothing_that_moves_or_sounds_is_a_step():
    paths = " ".join(s.path for s in cc.STEPS)
    for loud in ("trunk", "frunk", "alarm"):
        assert loud not in paths
    flash = cc.BY_KEY["flash"]
    assert cc.value_for(flash, "do", None) == "flash", "the lights, never the horn"
    with pytest.raises(ValueError):
        cc.value_for(flash, "undo", None)


def test_a_switch_tries_the_other_way_and_puts_back_what_it_read():
    halo = cc.BY_KEY["halo"]
    assert cc.value_for(halo, "do", True) == "off" and cc.value_for(halo, "undo", True) == "on"
    assert cc.value_for(halo, "do", False) == "on" and cc.value_for(halo, "undo", False) == "off"
    with pytest.raises(ValueError):
        cc.value_for(halo, "do", None)


def test_the_charging_current_goes_back_to_what_it_was():
    amps = cc.BY_KEY["amps"]
    assert cc.value_for(amps, "do", 48.0) == 32 and cc.value_for(amps, "undo", 48.0) == 48
    assert amps.method == "PUT" and cc.BY_KEY["wheel"].method == "POST"


def test_readings_and_moving():
    t = tree(sentry=True, steering_wheel_heater="low", state="parked", speed=0.0)
    now = cc.readings(t)
    assert now["halo"] is True and now["wheel"] == "low" and now["lights"] is None and now["_state"] == "parked"
    assert not cc.moving(t) and cc.moving(tree(state="driving", speed=0.0)) and cc.moving(tree(state="parked", speed=12.0))


def test_a_limit_steps_five_down_and_goes_back():
    lim = cc.BY_KEY["charge_limit"]
    assert cc.value_for(lim, "do", 80.0) == 75 and cc.value_for(lim, "undo", 90.00000134110451) == 90
    assert cc.value_for(lim, "do", 52.0) == 50, "never under 50"


def test_the_windows_wait_to_finish_before_they_are_put_back():
    assert cc.BY_KEY["windows"].settle >= 15 and cc.BY_KEY["lights"].settle == 0


def test_keyless_driving_is_always_put_back_off():
    """Left on, anyone can drive the car away: Put back turns it off whatever it read before."""
    keyless = cc.BY_KEY["keyless"]
    assert keyless.method == "POST"
    assert cc.value_for(keyless, "do", None) == "on"
    for before in (True, False, None):
        assert cc.value_for(keyless, "undo", before) == "off"
    assert cc.readings(tree(keyless_driving=True))["keyless"] is True


def test_the_windows_reading_names_each_window():
    """A close once read back closed while three windows were down: each one is shown beside the whole."""
    now = cc.readings(tree(windows__open_state="closed", windows__front_left__open_state="closed", windows__front_right__open_state="unknown"))
    assert now["windows"] == "closed · FL closed, FR unknown, RL —, RR —"
