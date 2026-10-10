"""The icon a place shows: pure default/override logic, and the glyph set it's drawn from."""

from placeicons import ICON_KEYS, PLACE_ICONS, resolve_icon, svg


def test_the_name_guess_is_the_default_when_nothing_is_stored():
    assert resolve_icon("Home", None) == "home"
    assert resolve_icon("SF Home", None) == "home", "home as a whole word anywhere"
    assert resolve_icon("WORK", None) == "work"
    assert resolve_icon("  home  ", None) == "home", "trimmed, matched case-insensitively"
    assert resolve_icon("Homeward Lane", None) is None, "not a substring match"
    assert resolve_icon("Home Depot", None) == "store", "a business with Home in its name is not home"
    assert resolve_icon("Gym", None) == "gym"
    assert resolve_icon("Alpha's Daycare", None) == "kids"
    assert resolve_icon("Evergreen Terrace, Springfield", None) is None


def test_a_stored_key_that_is_not_an_icon_draws_nothing():
    assert resolve_icon("Home", "not-a-key") is None


def test_an_explicit_icon_overrides_the_default_including_for_home_and_work():
    assert resolve_icon("Gym", "gym") == "gym"
    assert resolve_icon("Home", "star") == "star"
    assert resolve_icon("Work", "gym") == "gym"


def test_the_stored_value_none_means_no_icon_even_for_home():
    assert resolve_icon("Home", "none") is None
    assert resolve_icon("Work", "none") is None
    assert resolve_icon("Gym", "none") is None


def test_the_icon_set_is_defined_once_and_every_key_draws_something():
    assert len(ICON_KEYS) == len(PLACE_ICONS) >= 30
    for k in ("kids", "doctor", "dentist", "pharmacy", "coffee", "bar", "beach", "mountain", "store", "gas", "parking", "friend", "hotel", "worship", "library", "pet", "service", "transit", "venue"):
        assert k in PLACE_ICONS, k
    assert "home" in ICON_KEYS and "work" in ICON_KEYS
    for key in ICON_KEYS:
        assert "<path" in PLACE_ICONS[key] or "<rect" in PLACE_ICONS[key] or "<circle" in PLACE_ICONS[key], key


def test_svg_wraps_a_known_key_and_is_blank_for_an_unknown_one():
    out = svg("home")
    assert out.startswith("<svg") and 'class="pl-ic pl-home"' in out and "stroke=\"currentColor\"" in out
    assert svg("not-a-key") == "" and svg(None) == ""


def test_every_icon_has_a_title_and_the_old_keys_are_kept():
    """Keys are stored in cc_places.icon: none of the ones already stored may go."""
    from placeicons import title

    assert {"home", "work", "school", "gym", "shop", "food", "charge", "park", "family", "star", "travel", "pin"} <= set(PLACE_ICONS)
    assert title("food") == "Restaurant" and title("kids") == "Daycare, kids" and title("gym") == "Gym"
