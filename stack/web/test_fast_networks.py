"""Fast charging away from home, priced by network: Superchargers at their own prices,
every other or unknown network at the other pair, and an install that had one DC price
keeps every fast-charge cost when the two are split."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from facts import charge_facts
from test_settings import _Store

CHI = ZoneInfo("America/Chicago")
T0 = datetime(2026, 9, 20, 18, tzinfo=timezone.utc)
AWAY = lambda lat, lon: False  # noqa: E731


def fast(network=None, **kw):
    return {
        "id": 21,
        "session_start_date": T0,
        "session_end_date": T0 + timedelta(minutes=30),
        "session_position_latitude": 39.80,
        "session_position_longitude": -89.65,
        "real_charged": 40.0,
        "charging_type": "DC",
        "real_cost": None,
        "network": network,
        **kw,
    }


def test_a_supercharger_takes_the_supercharger_price_and_anything_else_the_other():
    rates = {"dc_price": 0.56, "dc_per_min": None, "sc_price": 0.40, "sc_per_min": 0.10}
    (sc,) = charge_facts([fast("supercharger")], [], CHI, AWAY, **rates)
    assert sc[2] == 40 * 0.40 + 30 * 0.10 and sc[3] == "Supercharger at 0.40/kWh + 0.10/min" and sc[9] == "dc"
    (other,) = charge_facts([fast(None)], [], CHI, AWAY, **rates)
    assert other[2] == 40 * 0.56 and other[3] == "DC at 0.56/kWh", "an unknown network is Other"


def test_a_supercharger_with_no_supercharger_price_is_unpriced_not_priced_as_other():
    (row,) = charge_facts([fast("supercharger")], [], CHI, AWAY, dc_price=0.56)
    assert row[2] is None and row[9] == "none"


def test_an_install_with_one_dc_price_gets_it_for_superchargers_too_once():
    import settings

    db = _Store({"units": "imperial", "dc_price": 0.48, "dc_per_min": 0.1})
    st = settings.load(db)
    assert (st["sc_price"], st["sc_per_min"], st["dc_price"], st["dc_per_min"]) == (0.48, 0.1, 0.48, 0.1)
    assert db.rows["sc_price"] == 0.48 and db.rows["sc_per_min"] == 0.1, "written down, so it is decided once"
    settings.save(db, "dc_price", 0.60)
    assert settings.load(db)["sc_price"] == 0.48, "a later change to Other leaves Superchargers alone"


def test_a_new_install_has_no_fast_prices():
    import settings

    db = _Store()
    st = settings.load(db)
    assert st["sc_price"] is None and st["sc_per_min"] is None and db.rows["sc_price"] is None


def test_the_split_changes_no_fast_charge_cost():
    """Every fast charge priced before the split, by any network, costs the same after the
    migration copies the one price into both pairs."""
    import settings
    import settingsform

    st = settings.load(_Store({"units": "imperial", "dc_price": 0.48, "dc_per_min": 0.1}))
    before = charge_facts([fast(None)], [], CHI, AWAY, dc_price=0.48, dc_per_min=0.1)[0][2]  # one price, whatever the network
    after = [charge_facts([fast(n)], [], CHI, AWAY, **settingsform.fast_rates(st))[0][2] for n in (None, "supercharger")]
    assert after == [before, before] and before == 40 * 0.48 + 30 * 0.1


def test_the_money_page_owns_both_pairs_and_a_change_to_either_reprices_fast_charges():
    import settingsform

    was = dict(settingsform.DEFAULTS, dc_price=0.48, sc_price=0.48)
    form = {"sc_price": "0.52", "sc_per_min": "", "dc_price": "0.48", "dc_per_min": ""}
    values = settingsform.parse("money", form, was)
    assert values["sc_price"] == 0.52 and values["dc_price"] == 0.48
    assert settingsform.fast_rate_changed(values, was)
    assert not settingsform.fast_rate_changed({"dc_price": 0.48, "sc_price": 0.48}, was)
