"""The comparison with a petrol car, the estimate footnote, and the empty states, all from
recorded figures."""

import pytest

from averages import Averages
from test_rates_data import averages_data
from stats import charging_summary, efficiency_by_year, estimate_note, petrol_comparison, place_savings, saving_cell, saving_note
from statspage import cost_mark

RATES = Averages(averages_data())
EFF = {("ALPHA", 2026): 300.0, ("ALPHA", None): 320.0, ("BRAVO", None): 250.0}


def g(**kw):
    base = {"vin": "ALPHA", "year": 2026, "ym": "2026-09", "region": "US-IL", "cost_source": "average", "n": 1, "kwh": 30.0, "cost": 5.77, "rate_lo": "2026-07", "rate_hi": "2026-07"}
    return base | kw


def test_each_charge_is_compared_at_its_own_regions_petrol_price_and_month():
    p = petrol_comparison([g(), g(ym="2026-08", vin="BRAVO", year=2025, kwh=25.0, cost=5.0)], EFF, None, RATES)
    # 30 kWh at 300 Wh/mi is 100 mi; 25 kWh at BRAVO's all-time 250 Wh/mi is 100 mi. 30 mpg.
    assert p["ok"] and p["mpg"] == 30.0 and p["miles"] == pytest.approx(200.0) and p["gallons"] == pytest.approx(200 / 30)
    assert p["petrol"] == pytest.approx(100 / 30 * 4.166 + 100 / 30 * 3.893) and p["charging"] == pytest.approx(10.77)
    assert p["saved"] == pytest.approx(p["petrol"] - 10.77) and p["areas"] == ["Midwest (PADD 2)"] and (p["from"], p["to"]) == ("2026-08", "2026-09")


def test_berlin_petrol_is_per_litre_and_converted_once():
    p = petrol_comparison([g(region="DE", kwh=30.0, cost=11.6)], EFF, 30.0, RATES)
    assert p["petrol"] == pytest.approx(100 / 30 * 3.78541 * 2.345) and p["l_per_100km"] == pytest.approx(7.84, abs=0.01)


def test_what_is_missing_is_counted_not_hidden():
    p = petrol_comparison([g(cost=None, n=3), g(region="FR"), g(vin="CHARLIE")], EFF, 30.0, RATES)
    assert not p["ok"] and p["skipped"] == {"unpriced": 3, "petrol": 1, "efficiency": 1} and p["petrol"] == 0.0
    p = petrol_comparison([g(region="FR")], EFF, 25.0, RATES, own_price=5.0)
    assert p["ok"] and p["petrol"] == pytest.approx(100 / 25 * 5.0), "a price on Settings is used for every charge"


def test_efficiency_by_year_needs_miles_behind_it():
    eff = efficiency_by_year([("ALPHA", 2025, 1000.0, 300.0), ("ALPHA", 2026, 50.0, 20.0)])
    assert eff[("ALPHA", 2025)] == pytest.approx(300.0) and ("ALPHA", 2026) not in eff and eff[("ALPHA", None)] == pytest.approx(320 / 1050 * 1000)


def test_the_footnote_names_the_average_its_source_and_months():
    note = estimate_note([g(), g(ym="2026-06", rate_lo="2026-06", rate_hi="2026-06"), g(cost_source="plan")], RATES)
    assert note["areas"] == ["Illinois"] and note["sources"] == ["US EIA, average residential electricity price"] and note["charges"] == 2
    assert (note["from"], note["to"]) == ("2026-06", "2026-07") and note["lagged"], "September was priced on July's figure"
    assert estimate_note([g(cost_source="plan")], RATES) is None


def test_the_charging_page_knows_what_is_priced_and_what_is_an_estimate():
    s = charging_summary([{"session_start_date": 1, "where": "Home", "kwh": 30.0, "cost": 5.77, "cost_source": "average", "rate_plan": "Illinois average", "at_home": True}])
    assert s["priced"] == 1 and s["estimate_cost"] == pytest.approx(5.77) and s["estimate_areas"] == ["Illinois"] and s["places"][0]["estimate"]
    s = charging_summary([{"session_start_date": 1, "where": "Home", "kwh": 30.0, "cost": None, "at_home": True}])
    assert s["priced"] == 0 and s["total_cost"] == 0.0


def test_a_month_of_charges_with_no_price_is_marked_not_zero():
    assert cost_mark("cost", {"charges": 3, "priced": 0, "cost": 0.0, "avg_cost": 0.0}) == "none"
    assert cost_mark("cost", {"charges": 3, "priced": 3, "cost": 9.0, "avg_cost": 4.0}) == "est"
    assert cost_mark("cost", {"charges": 0, "priced": 0, "cost": 0.0}) is None, "no charges is a true zero"
    assert cost_mark("miles", {"charges": 3, "priced": 0}) is None


def test_each_place_saves_its_own_share_and_a_place_with_no_price_saves_nothing():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    chi = ZoneInfo("America/Chicago")
    at = datetime(2026, 9, 12, 3, tzinfo=chi)
    row = lambda where, kwh, cost: {"session_start_date": at, "where": where, "vin": "ALPHA", "region": "US-IL", "kwh": kwh, "cost": cost}  # noqa: E731
    rows = [row("Home", 30.0, 5.0), row("Home", 30.0, 5.0), row("Springfield Supercharger", 30.0, 12.0), row("Elm Street", 30.0, None)]
    saved = place_savings(rows, chi, EFF, 30.0, RATES)
    whole = petrol_comparison([g(kwh=30.0, cost=5.0), g(kwh=30.0, cost=5.0), g(kwh=30.0, cost=12.0)], EFF, 30.0, RATES)
    assert saved["Elm Street"]["saved"] is None and saved["Elm Street"]["skipped"]["unpriced"] == 1, "no priced charge: no figure, and the reason kept, not $0"
    assert saved["Home"]["saved"] == pytest.approx(2 * 100 / 30 * 4.166 - 10.0) and (saved["Home"]["compared"], saved["Home"]["of"]) == (2, 2)
    every = saving_cell(list(saved.values()))
    assert every["saved"] == pytest.approx(whole["saved"]) and (every["compared"], every["of"]) == (3, 4), "the places add up to the page's whole, and say one was left out"
    assert saving_note(every) == "3 of 4 charges compared: 1 with no price" and saving_note(saved["Home"]) is None
    assert saving_note(saved["Elm Street"]) == "Nothing to compare: 1 with no price"
    away = place_savings([dict(row("Paris", 30.0, 9.0), region="FR"), dict(row("Home", 30.0, 5.0), vin="CHARLIE")], chi, EFF, 30.0, RATES)
    assert saving_note(away["Paris"]) == "Nothing to compare: 1 with no petrol price for where and when"
    assert saving_note(away["Home"]) == "Nothing to compare: 1 with no Wh a mile for that year", "a car with no measured efficiency is named as the reason, not the price"
