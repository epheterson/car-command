"""The upgrade to per-place prices, fast-charging networks and Tesla bills must change no
stored cost: an install with a home plan and the old single DC price starts up and every
home charge still costs what its plan said, every fast charge what the DC price said.

Two halves. Startup itself writes nothing to the facts: its DDL only adds tables and
columns, and the settings upgrade only copies the DC price. And pricing the same charges
again with the upgraded settings and places gives the same figures as the old ones, so a
charge that does get looked at again (a new one, or one whose energy was zero) is priced
as it would have been."""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import settings
import settingsform
from facts import charge_facts
from places import PlaceIndex, is_home, place_for
from test_settings import _Store

pytestmark = pytest.mark.usefixtures("springfield_plan")
CHI = ZoneInfo("America/Chicago")
HERE = Path(__file__).parent
HOME = {"id": 1, "name": "Home", "latitude": 39.80, "longitude": -89.64, "radius_m": 150}
WORK = {"id": 2, "name": "Work", "latitude": 39.78, "longitude": -89.65, "radius_m": 150}


def sessions():
    """A year of charges: home overnight and in the evening peak, fast charges at two
    networks' worth of places, and a slow charge at work."""
    out, t = [], datetime(2025, 9, 1, 3, tzinfo=timezone.utc)
    for i in range(120):
        start = t + timedelta(days=3 * i, hours=(i % 5) * 3)
        where, kind, minutes = [(HOME, "AC", 300), (HOME, "AC", 200), (HOME, "AC", 420), ({"latitude": 39.70, "longitude": -89.60}, "DC", 35), (WORK, "AC", 240)][i % 5]
        out.append(
            {
                "id": i + 1,
                "session_start_date": start,
                "session_end_date": start + timedelta(minutes=minutes),
                "session_position_latitude": where["latitude"],
                "session_position_longitude": where["longitude"],
                "real_charged": 8.0 + (i % 7) * 3.5,
                "charging_type": kind,
                "real_cost": None,
                "network": "supercharger" if kind == "DC" and i % 2 else None,
            }
        )
    return out


def test_startup_on_an_install_with_a_plan_and_one_dc_price_changes_no_stored_cost():
    import cost

    plan = cost.compile_rows("Springfield EV", [{"from": "2020-01-01", "off_peak": 0.31, "part_peak": 0.45, "peak": 0.52}], [{"period": "peak", "days": "all", "start": 16, "end": 21, "months": None}])
    store = _Store({"units": "imperial", "rate_plan": plan, "dc_price": 0.48, "dc_per_min": None, "public_tiles": True, "public_geocoder": True})
    cost.use_plan(plan)
    idx = PlaceIndex([dict(HOME), dict(WORK)])  # places as they were: no price column yet
    home = lambda a, b: is_home(place_for(a, b, idx))  # noqa: E731
    old = [{k: v for k, v in s.items() if k != "network"} for s in sessions()]  # stored before any network was recorded
    stored = charge_facts(old, [], CHI, home, dc_price=0.48)  # the old path: one DC price, no places, no bills

    st = settings.load(store)  # the upgrade
    assert st["rate_plan"] == plan, "the plan is untouched"
    priced_idx = PlaceIndex([{**HOME, "price": None}, {**WORK, "price": None}])  # the new column, NULL everywhere
    again = charge_facts(sessions(), [], CHI, lambda a, b: is_home(place_for(a, b, priced_idx)), place_of=lambda a, b: place_for(a, b, priced_idx), **settingsform.fast_rates(st))

    def costs(rows, source):
        return [(r[0], r[2], r[3]) for r in rows if r[9] == source]

    assert costs(again, "plan") == costs(stored, "plan") and len(costs(stored, "plan")) == 72
    assert costs(again, "dc") == [(i, c, label.replace("DC at", "Supercharger at") if i % 2 == 0 else label) for i, c, label in costs(stored, "dc")]
    assert [r[2] for r in again] == [r[2] for r in stored], "every cost, to the cent and beyond"
    assert round(sum(r[2] for r in again if r[9] == "plan"), 2) == round(sum(r[2] for r in stored if r[9] == "plan"), 2)


def test_nothing_that_runs_at_startup_rewrites_a_stored_cost():
    """The schema each module applies at startup only creates and adds: no UPDATE, DELETE
    or TRUNCATE of the facts tables hides in a DDL string."""
    from test_ddl import ddl_blocks

    blocks = [b for b in ddl_blocks() if b[0] in ("facts.py", "places.py", "history.py", "billed.py", "settings.py", "averages.py")]
    assert {"facts.py", "places.py", "history.py"} <= {b[0] for b in blocks}
    for module, name, sql in blocks:
        assert not re.search(r"\b(UPDATE|DELETE|TRUNCATE|DROP)\b", sql, re.I), f"{module}.{name} rewrites something at startup"
    src = (HERE / "settings.py").read_text()
    assert "cc_charge_facts" not in src, "the settings upgrade writes settings only"
