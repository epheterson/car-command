from filters import place_city, place_road, short_address, short_place


def test_a_long_auto_named_street_drops_its_city_and_a_named_place_stays():
    assert short_place("Springfield Valley Boulevard, Springfield") == "Springfield Valley Boulevard"
    assert short_place("Home") == "Home" and short_place("Power Plant") == "Power Plant"
    assert short_place("Maple Street, Shelbyville") == "Maple Street, Shelbyville", "short enough to keep whole"
    assert short_place(None) == ""


def test_a_place_splits_into_road_and_city_tail():
    assert place_road("Maple Street, Shelbyville") == "Maple Street" and place_city("Maple Street, Shelbyville") == ", Shelbyville"
    assert place_road("Home") == "Home" and place_city("Home") == "" and place_city(None) == ""


def test_short_address_cuts_a_nominatim_string_to_road_city():
    """A named place's card shows this, not the raw Nominatim display string — a long one
    (county, state, postcode, country all tacked on) used to overflow the card at phone
    widths instead of being cut down to the two parts that matter."""
    assert short_address("Evergreen Terrace, Springfield, Sangamon County, Illinois, 62701, United States") == "Evergreen Terrace, Springfield"
    assert short_address("742, Evergreen Terrace, Springfield, Sangamon County, Illinois, 62701, United States") == "Evergreen Terrace, Springfield"
    assert short_address("Elm Street, Springfield") == "Elm Street, Springfield", "already short, left whole"
    assert short_address(None) == "" and short_address("") == ""


def test_the_clock_and_date_order_rewrite_every_pattern_the_templates_ask_for():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from filters import adapt, when

    assert adapt("%a %b %-d, %-I:%M %p", "24", "dmy") == "%a %-d %b, %H:%M"
    assert adapt("%b %-d %Y, %-I:%M %p", "24", "mdy") == "%b %-d %Y, %H:%M"
    assert adapt("%-I:%M %p", "12", "mdy") == "%-I:%M %p"
    tz = ZoneInfo("UTC")
    at = datetime(2026, 3, 4, 17, 5, tzinfo=timezone.utc)
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    assert when(at, tz, now) == "Mar 4, 5:05 PM"
    assert when(at, tz, now, clock="24", date_order="dmy") == "4 Mar, 17:05"
    assert when(now, tz, now, clock="24") == "12:00"


def test_home_and_work_get_a_glyph_where_they_are_the_place():
    """A house before Home and a briefcase before Work, so the two stand out at a glance; only
    where the word is the whole place name, never inside one, and the text stays escaped."""
    from filters import places

    title = str(places("Home → Elm Street, Springfield"))
    assert title.count("<svg") == 1 and title.index('class="pl-ic pl-home"') < title.index("Home") and "&rarr;" not in title
    assert str(places("Maple Street → Home")).count("pl-home") == 1
    assert str(places("Parked 18 min at Home · lost 0.2 kWh")).count("pl-home") == 1
    assert str(places("Asleep 2 h at work · cabin 77°F")).count("pl-work") == 1, "a place named in lower case still counts"
    assert str(places("Work")).count("pl-work") == 1 and str(places("Home 9.4 h")).count("pl-home") == 1
    for plain in ("Home Depot", "Homeward Lane", "Network Plaza → Shelbyville", "Workshop Lane"):
        assert "<svg" not in str(places(plain)), plain
    assert "&lt;b&gt;" in str(places("<b>Home</b>")) and "<b>" not in str(places("<b>Home</b>"))
    assert str(places(None)) == "" and 'aria-hidden="true"' in title


def test_a_named_place_shows_its_own_icon_from_the_map():
    """The filter's lookup: a place with a stored icon gets that glyph, by name, wherever
    it renders — and the map may be a plain dict or a callable (the registered filter
    passes the app's cached lookup as a callable)."""
    from filters import places

    gym = str(places("Parked at Gym", icon_map={"gym": "gym"}))
    assert gym.count("<svg") == 1 and 'class="pl-ic pl-gym"' in gym and "Gym" in gym
    assert str(places("Gym", icon_map=lambda: {"gym": "gym"})).count("<svg") == 1, "a callable icon_map works too"
    assert str(places("Gymnasium Road", icon_map={"gym": "gym"})).count("<svg") == 0, "still a whole-name match only"


def test_home_still_defaults_to_the_house_glyph_with_an_icon_map_present():
    from filters import places

    out = str(places("Home", icon_map={"gym": "gym"}))
    assert out.count("<svg") == 1 and 'class="pl-ic pl-home"' in out


def test_a_stored_none_icon_suppresses_even_homes_default():
    from filters import places

    assert "<svg" not in str(places("Home", icon_map={"home": "none"}))
    assert "Home" in str(places("Home", icon_map={"home": "none"}))


def test_an_overridden_home_icon_shows_instead_of_the_house():
    from filters import places

    out = str(places("Home", icon_map={"home": "star"}))
    assert out.count("<svg") == 1 and 'class="pl-ic pl-star"' in out and 'class="pl-ic pl-home"' not in out


def test_icon_map_names_are_escaped_before_they_become_a_pattern():
    from filters import places

    # a place literally named with regex-special characters must not blow up the match,
    # and the rendered text stays escaped
    out = str(places("<b>Gym</b>", icon_map={"gym": "gym"}))
    assert "&lt;b&gt;" in out and "<b>" not in out


def test_a_place_name_with_regex_metacharacters_never_raises():
    """A place literally named "Mom (" used to blow up every `|places` call on the page:
    _place_pattern joined HTML-escaped names into a regex alternation without re.escape,
    so an unbalanced "(" became an invalid pattern and `re.compile` raised PatternError —
    not just for that place's own text, but for every other name sharing the call, since
    one bad name poisons the whole alternation."""
    from filters import places

    tricky = ["Mom (", "C++ Club", "3.5 Sons", "[Draft] HQ", "A|B Diner", "Re*Store", "Wh?at Now", "$ Store", "^Top^", "Back\\slash"]
    icon_map = {n.lower(): "star" for n in tricky}
    for name in tricky:
        out = str(places(name, icon_map=icon_map))  # must not raise
        assert out.count("<svg") == 1, name
    # one bad name must not break matching an unrelated one in the same map
    out = str(places("Home", icon_map=icon_map))
    assert out.count("<svg") == 1 and 'class="pl-ic pl-home"' in out
