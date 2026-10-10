"""Which price region a point is in, from the bundled outlines, with no network."""

from regions import FILE, name_of, region_of
from test_rates_data import BERLIN, MID_ATLANTIC, SPRINGFIELD


def test_springfield_berlin_and_the_open_sea():
    assert region_of(*SPRINGFIELD) == "US-IL" and name_of("US-IL") == "Illinois"
    assert region_of(*BERLIN) == "DE" and name_of("DE") == "Germany"
    assert region_of(*MID_ATLANTIC) is None


def test_neighbouring_states_and_a_point_just_off_the_coast():
    assert region_of(39.10, -94.58) == "US-MO" and region_of(39.10, -94.65) == "US-KS", "Kansas City, either side of the line"
    assert region_of(41.88, -87.60) == "US-IL", "a charger on the lakefront, just off the simplified shore"
    assert region_of(48.85, 2.35) == "FR" and region_of(21.30, -157.85) == "US-HI"


def test_no_position_is_no_region():
    assert region_of(None, -89.6) is None and region_of(39.8, None) is None and region_of("x", 1) is None and region_of(95, 0) is None


def test_the_bundled_outlines_stay_small():
    assert FILE.stat().st_size <= 600 * 1024
