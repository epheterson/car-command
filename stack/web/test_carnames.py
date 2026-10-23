"""A car's shown name: the rename, else the source's, duplicates told apart."""

import carnames


def test_a_rename_is_the_shown_name_and_the_source_is_untouched():
    sources = {"VIN-A-1111": "Comet", "VIN-B-2222": "Nova"}
    shown = carnames.display_names(sources, {"VIN-A-1111": "  Family   car "})
    assert shown == {"VIN-A-1111": "Family car", "VIN-B-2222": "Nova"} and sources["VIN-A-1111"] == "Comet"


def test_a_blank_or_junk_rename_falls_back_to_the_source():
    assert carnames.display_names({"V1": "Comet"}, {"V1": "   "}) == {"V1": "Comet"}
    assert carnames.display_names({"V1": "Comet"}, {"V1": 7}) == {"V1": "Comet"}
    assert carnames.display_names({"V1": "Comet"}, None) == {"V1": "Comet"}
    assert carnames.display_names({"V1": None}, {}) == {"V1": "V1"}, "a car with no name at all shows its VIN"


def test_a_long_rename_is_cut():
    assert len(carnames.clean("x" * 100)) == carnames.MAX_LEN


def test_two_cars_with_one_name_are_told_apart_by_their_vin_tails():
    shown = carnames.display_names({"VINAAAA1111": "Comet", "VINBBBB2222": "comet", "VINCCCC3333": "Nova"}, {})
    assert shown == {"VINAAAA1111": "Comet (1111)", "VINBBBB2222": "comet (2222)", "VINCCCC3333": "Nova"}


def test_tails_lengthen_until_they_differ():
    shown = carnames.display_names({"AAA1234567": "Comet", "BBB9234567": "Comet"}, {})
    assert shown == {"AAA1234567": "Comet (1234567)", "BBB9234567": "Comet (9234567)"}


def test_a_rename_that_collides_is_told_apart_too_and_one_that_does_not_is_left_alone():
    shown = carnames.display_names({"V-AAAA1": "Comet", "V-BBBB2": "Nova"}, {"V-BBBB2": "Comet"})
    assert shown == {"V-AAAA1": "Comet (AAA1)", "V-BBBB2": "Comet (BBB2)"}
    assert carnames.display_names({"V-AAAA1": "Comet", "V-BBBB2": "Comet"}, {"V-BBBB2": "Nova"}) == {"V-AAAA1": "Comet", "V-BBBB2": "Nova"}
