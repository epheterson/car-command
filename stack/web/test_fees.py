"""Session and idle fees: Tesla's bill split into charging, idle and congestion; a rule on a
place or a network (a fee per charge, an idle price per minute after a grace) for charges no
bill covers; idle minutes from the charge finishing to the plug coming out; the charge page."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import billed
import fees
import placeprice
import settingsform
from facts import charge_facts
from places import PlaceIndex, is_home, place_for
from test_billed import ALPHA, PAGE

CHI = ZoneInfo("America/Chicago")
T0 = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
AWAY = lambda lat, lon: False  # noqa: E731
COLS = ("id", "kwh", "cost", "plan", "home", "off", "part", "peak", "split", "source", "region", "month", "session_fee", "idle_fee", "idle_min", "congestion_fee")


def fast(network="supercharger", minutes=60, **kw):
    return {
        "id": 31,
        "session_start_date": T0,
        "session_end_date": T0 + timedelta(minutes=minutes),
        "session_position_latitude": 39.80,
        "session_position_longitude": -89.65,
        "real_charged": 40.0,
        "charging_type": "DC",
        "real_cost": None,
        "network": network,
        **kw,
    }


def row(sessions, powers=(), **kw):
    return dict(zip(COLS, charge_facts(sessions, list(powers), CHI, AWAY, **kw)[0]))


# 40 kWh drawn for the first 30 minutes, then the car sits to the hour
RUN = [(T0, T0 + timedelta(minutes=30), 80.0)]
RULES = {"sc": {"session": 1.0, "idle": 0.50, "grace": 5.0}, "dc": {"session": None, "idle": None, "grace": None}}


# --- the arithmetic --------------------------------------------------------------------


def test_idle_is_the_time_from_the_last_real_power_to_the_unplugging():
    runs = [(T0, T0 + timedelta(minutes=20), 60.0), (T0 + timedelta(minutes=20), T0 + timedelta(minutes=40), 0.4)]
    end = T0 + timedelta(minutes=50)
    assert fees.charge_finished(runs, end) == T0 + timedelta(minutes=20), "a trickle under IDLE_KW is not charging"
    assert fees.charge_finished(runs, T0 + timedelta(minutes=10)) == T0 + timedelta(minutes=10), "never after the session's end"
    assert fees.charge_finished([], end) is None
    assert fees.idle_minutes(T0, T0 + timedelta(minutes=12)) == 12 and fees.idle_minutes(None, T0) is None and fees.idle_minutes(T0, None) is None
    assert fees.idle_minutes(T0 + timedelta(minutes=5), T0) == 0, "never negative"


def test_the_grace_is_free_and_only_the_minutes_past_it_are_billed():
    assert fees.idle_fee(30, 0.5, 10) == 10.0
    assert fees.idle_fee(8, 0.5, 10) == 0.0
    assert fees.idle_fee(30, 0.5, None) == 15.0
    assert fees.idle_fee(None, 0.5, 5) == 0.0 and fees.idle_fee(30, None, 5) == 0.0


def test_settings_become_a_rule_per_network_blank_parts_none():
    st = {"sc_session_fee": 1.5, "sc_idle_per_min": 0.4, "sc_idle_grace": 5, "dc_idle_per_min": 0.2}
    assert fees.rules(st) == {"sc": {"session": 1.5, "idle": 0.4, "grace": 5.0}, "dc": {"session": None, "idle": 0.2, "grace": None}}


# --- the bill -------------------------------------------------------------------------


def test_a_tesla_bill_keeps_its_idle_and_congestion_fees_apart():
    rows, _more = billed.parse_page(PAGE)
    assert rows[0]["total"] == 19.95 and rows[0]["idle_fee"] == 1.0 and rows[0]["idle_min"] == 2 and rows[0]["congestion_fee"] == 0.0
    assert rows[1]["idle_fee"] == 0.0 and rows[1]["idle_min"] == 0.0
    item = {**PAGE["data"][1], "fees": PAGE["data"][1]["fees"] + [{"feeType": "CONGESTION", "totalDue": 3.25, "uom": "min", "usageBase": 4}]}
    got = billed.parse_item(item)
    assert got["total"] == 12.6 + 3.25 and got["congestion_fee"] == 3.25 and got["idle_fee"] == 0.0


def test_a_billed_charge_stores_the_bills_split_and_the_bill_beats_a_rule():
    bill = {"id": "77", "vin": ALPHA, "started_at": T0, "site": "Springfield, IL", "total": 19.95, "invoice_id": None, "idle_fee": 1.0, "idle_min": 2.0, "congestion_fee": 0.0}
    got = row([fast(plug_disconnected_date=T0 + timedelta(minutes=60))], RUN, bills=[bill], sc_price=0.40, fee_rules=RULES)
    assert got["source"] == "billed" and got["cost"] == 19.95 and got["idle_fee"] == 1.0 and got["idle_min"] == 2.0 and got["session_fee"] is None


def test_a_bill_read_before_the_fees_were_kept_has_no_split_not_a_zero():
    bill = {"id": "77", "vin": ALPHA, "started_at": T0, "site": None, "total": 19.95, "invoice_id": None}
    got = row([fast()], RUN, bills=[bill])
    assert got["cost"] == 19.95 and got["idle_fee"] is None and got["idle_min"] is None


# --- a network's rule -----------------------------------------------------------------


def test_a_supercharger_idle_fee_runs_from_the_charge_finishing_to_the_car_leaving():
    got = row([fast(plug_disconnected_date=T0 + timedelta(minutes=60))], RUN, sc_price=0.40, fee_rules=RULES)
    # 40 kWh at 0.40, a 1.00 session fee, 30 idle minutes less 5 free at 0.50
    assert got["idle_min"] == 30 and got["idle_fee"] == 12.5 and got["session_fee"] == 1.0
    assert got["cost"] == pytest.approx(16.0 + 1.0 + 12.5)
    assert got["plan"] == "Supercharger at 0.40/kWh + 1.00/session + 0.50/min idle"


def test_an_earlier_departure_ends_the_idle_sooner():
    s = fast(plug_disconnected_date=T0 + timedelta(minutes=40))
    got = row([s], RUN, sc_price=0.40, fee_rules=RULES)
    assert got["idle_min"] == 10 and got["idle_fee"] == 2.5


def test_no_power_profile_means_no_idle_fee_rather_than_a_guess():
    got = row([fast(plug_disconnected_date=T0 + timedelta(minutes=60))], [], sc_price=0.40, fee_rules=RULES)
    assert got["idle_min"] is None and got["idle_fee"] is None and got["cost"] == pytest.approx(16.0 + 1.0)


def test_a_car_that_has_not_left_has_unknown_idle_not_zero_idle():
    got = row([fast()], RUN, sc_price=0.40, fee_rules=RULES)
    assert got["idle_min"] is None and got["idle_fee"] is None and got["session_fee"] == 1.0 and got["cost"] == pytest.approx(17.0)
    assert "min idle" in got["plan"], "the label still says an idle rule applied, which is how the page knows it is unknown"


def test_other_networks_take_their_own_rule_and_none_means_no_fees():
    rules = {"sc": RULES["sc"], "dc": {"session": 2.0, "idle": None, "grace": None}}
    got = row([fast(None, plug_disconnected_date=T0 + timedelta(minutes=60))], RUN, dc_price=0.56, fee_rules=rules)
    assert got["session_fee"] == 2.0 and got["idle_fee"] is None and got["cost"] == pytest.approx(40 * 0.56 + 2.0)
    plain = row([fast(None)], RUN, dc_price=0.56)
    assert plain["cost"] == pytest.approx(40 * 0.56) and plain["session_fee"] is None


# --- a place's rule -------------------------------------------------------------------


def test_a_places_price_carries_an_idle_price_and_grace():
    price = placeprice.clean("kwh", "0.30", "1", "0.25", "10")
    assert price == {"kind": "kwh", "rate": 0.30, "fee": 1.0, "idle": 0.25, "grace": 10.0} and placeprice.valid(price)
    assert placeprice.clean("kwh", "0.30", "", "", "10") == {"kind": "kwh", "rate": 0.30}, "a grace with no idle price is dropped"
    assert placeprice.from_form({"price_kind": "paid", "price_per": "kwh", "price_rate": "0.3", "price_idle": "0.2", "price_grace": "15"}) == (
        True,
        {"kind": "kwh", "rate": 0.3, "idle": 0.2, "grace": 15.0},
    )
    assert placeprice.describe(price) == "$0.30/kWh + $1.00 a session + $0.25/min idle after 10 min"
    with pytest.raises(placeprice.BadPrice):
        placeprice.clean("kwh", "0.30", "", "99")


def test_a_charge_at_a_place_with_an_idle_price_is_billed_for_the_idle():
    place = {"id": 1, "name": "Garage Alpha", "latitude": 39.80, "longitude": -89.65, "radius_m": 100, "price": {"kind": "kwh", "rate": 0.30, "fee": 1.0, "idle": 0.20, "grace": 10.0}}
    idx = PlaceIndex([place])
    s = fast(None, charging_type="AC", plug_disconnected_date=T0 + timedelta(minutes=60))
    (r,) = charge_facts([s], RUN, CHI, lambda a, b: is_home(place_for(a, b, idx)), place_of=lambda a, b: place_for(a, b, idx))
    got = dict(zip(COLS, r))
    assert got["source"] == "place" and got["idle_min"] == 30 and got["idle_fee"] == pytest.approx(4.0) and got["session_fee"] == 1.0
    assert got["cost"] == pytest.approx(40 * 0.30 + 1.0 + 4.0)


# --- Settings and the page -------------------------------------------------------------


def test_the_money_page_owns_the_fee_fields_and_a_change_reprices_fast_charges():
    was = dict(settingsform.DEFAULTS)
    values = settingsform.parse("money", {"sc_session_fee": "1.5", "sc_idle_per_min": "0.4", "sc_idle_grace": "5", "dc_session_fee": "", "dc_idle_per_min": "9", "dc_idle_grace": "x"}, was)
    assert (values["sc_session_fee"], values["sc_idle_per_min"], values["sc_idle_grace"]) == (1.5, 0.4, 5.0)
    assert values["dc_session_fee"] is None and values["dc_idle_per_min"] is None and values["dc_idle_grace"] is None, "blank and out of range are unset"
    assert settingsform.fast_rate_changed(values, was)
    assert not settingsform.fast_rate_changed({"sc_idle_per_min": None}, was)


def test_the_next_drive_stands_in_for_an_unrecorded_unplugging_and_is_asked_only_when_an_idle_price_applies():
    asked = []

    def departure_of(s):
        asked.append(s["id"])
        return T0 + timedelta(minutes=50)

    got = row([fast()], RUN, sc_price=0.40, fee_rules=RULES, departure_of=departure_of)
    assert got["idle_min"] == 20 and got["idle_fee"] == 7.5 and asked == [31]
    asked.clear()
    row([fast(None)], RUN, dc_price=0.56, fee_rules=RULES, departure_of=departure_of)
    assert asked == [], "no idle price on this network: the drives are not looked at"
