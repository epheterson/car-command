"""A small averages file for tests: figures recorded from the real rates/averages.json
built on 2026-10-04, for Springfield's state and Berlin's country and a few others."""

from __future__ import annotations

EIA_POWER = "US EIA, average residential electricity price"
EIA_PETROL = "US EIA, regular gasoline retail price"
EUROSTAT = "Eurostat nrg_pc_204, household electricity price with all taxes"
BULLETIN = "EU Weekly Oil Bulletin, Euro-super 95 with taxes"


def _s(area, source, unit, currency, values):
    return {"area": area, "source": source, "unit": unit, "currency": currency, "as_of": max(values), "values": values}


def averages_data() -> dict:
    de_power = {f"2025-{m:02d}": 0.3835 for m in range(1, 7)} | {f"2025-{m:02d}": 0.3869 for m in range(7, 13)}
    return {
        "version": 1,
        "built": "2026-10-04",
        "regions": {
            "US-IL": {
                "name": "Illinois",
                "electricity": _s("Illinois", EIA_POWER, "kWh", "USD", {"2026-05": 0.2385, "2026-06": 0.1989, "2026-07": 0.1922}),
                "petrol": _s("Midwest (PADD 2)", EIA_PETROL, "gal", "USD", {"2026-08": 3.893, "2026-09": 4.166}),
            },
            "US-CA": {"name": "California", "electricity": _s("California", EIA_POWER, "kWh", "USD", {"2026-07": 0.3361})},
            "US": {"name": "United States", "electricity": _s("United States", EIA_POWER, "kWh", "USD", {"2026-07": 0.1831})},
            "DE": {"name": "Germany", "electricity": _s("Germany", EUROSTAT, "kWh", "EUR", de_power), "petrol": _s("Germany", BULLETIN, "L", "EUR", {"2026-08": 2.209, "2026-09": 2.345})},
            "FR": {"name": "France", "electricity": _s("France", EUROSTAT, "kWh", "EUR", {"2025-12": 0.2561})},
        },
    }


SPRINGFIELD = (39.80, -89.64)
BERLIN = (52.52, 13.40)
MID_ATLANTIC = (30.0, -40.0)


def test_the_recorded_excerpt_has_the_shape_pricing_reads():
    from averages import valid

    assert valid(averages_data())
