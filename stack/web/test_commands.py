"""The command catalogue, its tiers and guards, the limits and the connector paths: pure."""

import pytest

import commands as cm


def test_parse_checks_the_argument_range_and_step():
    cmd, arg = cm.parse("charge_limit", "80")
    assert cmd.key == "charge_limit" and arg == 80 and isinstance(arg, int)
    assert cm.parse("climate_temp", "21.3")[1] == 21.5
    with pytest.raises(ValueError, match="between 50 and 100"):
        cm.parse("charge_limit", 101)
    with pytest.raises(ValueError, match="a number"):
        cm.parse("charge_amps", "lots")
    with pytest.raises(ValueError, match="a number"):
        cm.parse("charge_amps", "nan")
    with pytest.raises(ValueError, match=r"no command called 'summon'. The names are: charge_start, .*flash.*honk") as said:
        cm.parse("summon", None)
    assert all(name in str(said.value) for name in cm.CATALOGUE), "every valid name is listed"
    assert cm.parse("charge_stop", "ignored") == (cm.CATALOGUE["charge_stop"], None)


def test_route_tesla_through_the_proxy_others_through_the_connector():
    assert cm.route("Tesla", live=False, proxy=True) == "tesla"
    assert cm.route("Tesla", live=False, proxy=False) is None
    assert cm.route("Lucid", live=True, proxy=True) == "connector"
    assert cm.route("Rivian", live=False, proxy=True) is None
    assert cm.route("Tesla", live=False, proxy=True, demo=True) is None
    assert "signing proxy" in cm.no_way("Tesla")


def _refuse(key="charge_stop", make="Tesla", way="tesla", **kw):
    args = {"enabled": True, "signed": True, "allowed_tier": 1, "stepped_up": False, "offered": None, **kw}
    return cm.refusal(cm.CATALOGUE[key], make, way, **args)


def test_refusals_in_order():
    assert "off" in _refuse(enabled=False, way=None)
    assert "proxy" in _refuse(way=None)
    assert "sign-in" in _refuse(signed=False)
    assert "off for this car" in _refuse("sentry_on")
    assert _refuse("sentry_on", allowed_tier=2) is None
    assert "Lucid" in _refuse("climate_temp", "Lucid", "connector")
    assert "Tesla" in _refuse("max_ac_on", "Tesla", "tesla")
    assert "doesn't offer" in _refuse("charge_stop", "Lucid", "connector", offered={"lock"})
    assert _refuse("charge_stop", "Lucid", "connector", offered={"charge_stop"}) is None
    assert _refuse("charge_stop", "Lucid", "connector", offered=None) is None, "not known: the connector answers"
    assert "password" in _refuse("unlock", allowed_tier=3)
    assert _refuse("unlock", allowed_tier=3, stepped_up=True) is None
    assert _refuse("charge_amps") is None


def test_every_tier_is_one_two_or_three_and_tier_three_holds_what_opens_the_car():
    assert {c.tier for c in cm.CATALOGUE.values()} == {1, 2, 3}
    for key in ("unlock", "trunk", "frunk", "windows_vent", "windows_close", "remote_start"):
        assert cm.CATALOGUE[key].tier == 3
    assert cm.CATALOGUE["lock"].tier == 1 and cm.CATALOGUE["sentry_on"].tier == 2


def test_nothing_that_moves_the_car_or_touches_its_keys_or_software():
    assert not set(cm.NEVER) & set(cm.CATALOGUE)
    for key in cm.CATALOGUE:
        assert not any(word in key for word in ("summon", "update", "pin", "speed", "key", "valet"))


def test_tier_of_is_bounded():
    assert cm.tier_of({}, "V1") == 1
    assert cm.tier_of({"V1": 3}, "V1") == 3
    assert cm.tier_of({"V1": "9"}, "V1") == 3
    assert cm.tier_of({"V1": "x"}, "V1") == 1


def test_every_connector_command_has_a_path_and_takes_its_argument():
    assert cm.cc_request(cm.CATALOGUE["charge_start"], "V1", None) == ("POST", "garage/V1/charging/commands/start-stop", {"value": "start"})
    assert cm.cc_request(cm.CATALOGUE["charge_limit"], "V1", 80) == ("PUT", "garage/V1/charging/settings/target_level", {"value": 80})
    for key in cm.CC_PATHS:
        assert key in cm.CATALOGUE


def test_offered_reads_the_command_nodes_the_connector_put_in_the_tree():
    read_only = {"charging": {"settings": {"target_level": {"val": 80}}}, "doors": {"lock_state": {"val": "locked"}}}
    assert cm.cc_offered(read_only) == set(), "a charge limit alone is a reading, not a command"
    commanding = {
        "charging": {"commands": {"start-stop": {"val": None}}, "settings": {"target_level": {"val": 80}}},
        "doors": {"commands": {"lock-unlock": {"val": None}}},
    }
    assert cm.cc_offered(commanding) == {"charge_start", "charge_stop", "charge_limit", "lock", "unlock"}
    assert cm.cc_offered(None) == set()
    lucid = {"doors": {"commands": {"lock-unlock": {"val": None}, "trunk": {"val": None}, "frunk": {"val": None}}}}
    assert cm.cc_offered(lucid) == {"lock", "unlock", "trunk", "trunk_close", "frunk", "frunk_close"}
    assert cm.CC_PATHS["trunk_close"] == ("POST", "doors/commands/trunk", "close")


def test_limiter_ten_a_car_a_minute_and_three_tier_three_an_hour():
    t = [0.0]
    lim = cm.Limiter(clock=lambda: t[0])
    assert all(lim.admit("V1") is None for _ in range(10))
    assert "10 commands" in lim.admit("V1") and lim.admit("V2") is None
    t[0] = 60.0
    assert lim.admit("V1") is None
    assert all(lim.admit("V3", 3) is None for _ in range(3))
    t[0] = 200.0
    assert "in an hour" in lim.admit("V3", 3)
    assert lim.admit("V3", 1) is None, "the hourly cap is tier 3's alone"
    t[0] = 3700.0
    assert lim.admit("V3", 3) is None


def test_the_result_line_says_what_and_when():
    limit = cm.CATALOGUE["charge_limit"]
    assert cm.said(limit, 80, True, None, "2:14 PM") == "Charge limit set to 80% 2:14 PM"
    assert cm.said(cm.CATALOGUE["sched_charge"], 75, True, None, "9 PM") == "Scheduled charging set to 1:15 AM 9 PM"
    assert cm.said(limit, 80, False, "Tesla is limiting commands.", "") == "Tesla is limiting commands."


def test_only_the_connectors_own_refusal_reaches_the_page():
    assert cm.cc_refusal({"error": "Not sent: this car's steering wheel is not heated"}) == "Not sent: this car's steering wheel is not heated."
    assert cm.cc_refusal({"error": "Not sent: a reason."}) == "Not sent: a reason."
    for other in ({"error": "Frunk failed: RESOURCE_EXHAUSTED"}, {"error": None}, {}, None, [], "Not sent: x"):
        assert cm.cc_refusal(other) is None


def test_defrost_reaches_a_lucid_through_the_connector_as_window_heating():
    assert cm.cc_request(cm.CATALOGUE["defrost_on"], "V1", None) == ("POST", "garage/V1/window_heating/commands/start-stop", {"value": "start"})
    assert cm.cc_request(cm.CATALOGUE["defrost_off"], "V1", None)[2] == {"value": "stop"}
    assert cm.CATALOGUE["defrost_on"].makes == ("Tesla", "Lucid")
    tree = {"window_heating": {"commands": {"start-stop": {"val": None}}}}
    assert {"defrost_on", "defrost_off"} <= cm.cc_offered(tree)


def test_home_assistant_and_teslafi_controls_are_here_for_each_make():
    """What either one's controls do for a Tesla or a Lucid, by key; NEVER holds the rest."""
    tesla = {k for k, c in cm.CATALOGUE.items() if c.makes is None or "Tesla" in c.makes}
    lucid = {k for k, c in cm.CATALOGUE.items() if c.makes is None or "Lucid" in c.makes}
    assert {
        "charge_port_open",
        "charge_port_close",
        "keep_on",
        "dog_on",
        "camp_on",
        "keeper_off",
        "overheat_on",
        "overheat_fan",
        "overheat_off",
        "bioweapon_on",
        "seat_heat_passenger",
        "seat_heat_rear_left",
        "seat_heat_rear_center",
        "seat_heat_rear_right",
        "seat_vent",
        "seat_vent_passenger",
        "wheel_heat_on",
        "wheel_heat_off",
        "media_toggle",
        "media_next",
        "media_prev",
        "volume",
        "homelink",
        "sentry_on",
        "remote_start",
    } <= tesla
    assert {
        "sentry_on",
        "sentry_home_on",
        "sentry_work_on",
        "deterrence_on",
        "alarm_on",
        "alarm_silent",
        "alarm_off",
        "alarm_silence",
        "windows_vent",
        "windows_close",
        "lights_on",
        "lights_off",
        "max_ac_on",
        "dog_on",
        "keeper_off",
        "seat_heat_rear_right",
        "seat_vent_passenger",
        "wheel_heat",
        "precondition_on",
        "charge_amps",
        "remote_start",
    } <= lucid
    assert cm.CATALOGUE["homelink"].tier == 3 and cm.CATALOGUE["overheat_off"].tier == 2 and cm.CATALOGUE["alarm_off"].tier == 2


def test_every_lucid_command_has_a_connector_path_and_every_tesla_one_an_endpoint():
    import tesla_cmd

    for k, c in cm.CATALOGUE.items():
        if c.makes is None or "Lucid" in c.makes:
            assert k in cm.CC_PATHS or k in ("climate_temp",), k
        if (c.makes is None or "Tesla" in c.makes) and k != "wake":
            assert k in tesla_cmd.ENDPOINTS, k


def test_connector_bodies_name_the_seat_and_the_level():
    assert cm.cc_request(cm.CATALOGUE["seat_heat_rear_left"], "V1", 2) == ("POST", "garage/V1/climatization/commands/seat-climate", {"value": "rear-left heat medium"})
    assert cm.cc_request(cm.CATALOGUE["seat_vent"], "V1", 0)[2] == {"value": "driver vent off"}
    assert cm.cc_request(cm.CATALOGUE["wheel_heat"], "V1", 3)[2] == {"value": "high"}
    assert cm.cc_request(cm.CATALOGUE["alarm_silence"], "V1", None) == ("POST", "garage/V1/commands/sentry-alarm", {"value": "off"})
    assert cm.cc_request(cm.CATALOGUE["charge_amps"], "V1", 32) == ("PUT", "garage/V1/charging/settings/maximum_current", {"value": 32})
    assert cm.cc_request(cm.CATALOGUE["remote_start"], "V1", None)[1] == "garage/V1/commands/keyless-driving"


def test_a_setting_counts_as_offered_only_beside_commands():
    tree = {"charging": {"settings": {"target_level": {"val": 80}, "maximum_current": {"val": 80}}}}
    assert cm.cc_offered(tree) == set(), "read-only: no command objects"
    tree["commands"] = {"sentry": {"val": None}}
    assert cm.cc_offered(tree) == {"sentry_on", "sentry_off", "charge_limit", "charge_amps"}


def test_a_tesla_is_not_offered_what_it_is_not_fitted_with():
    m3 = {"model": "Model 3", "fitted": {"seat_cooling": False, "rear_seat_heaters": True}}
    assert cm.not_fitted("Tesla", m3) == {"seat_vent", "seat_vent_passenger", "bioweapon_on", "bioweapon_off"}
    assert cm.not_fitted("Tesla", {"model": "Model Y", "fitted": {"seat_cooling": None}}) == set(), "not said: offered"
    assert cm.not_fitted("Tesla", {"model": "Model S", "fitted": {"rear_seat_heaters": False}}) == {"seat_heat_rear_left", "seat_heat_rear_center", "seat_heat_rear_right"}
    assert {"wheel_heat_on", "wheel_heat_off"} <= cm.not_fitted("Tesla", {**m3, "year": 2018})
    assert "wheel_heat_on" not in cm.not_fitted("Tesla", {**m3, "year": 2021})
    assert cm.not_fitted("Lucid", m3) == set() and cm.not_fitted("Tesla", None) == set()
