from autotag import match, plan

RULES = [
    {"id": 1, "from_name": "Home", "to_name": "Power Plant", "tag": "Commute", "both_ways": True, "active": True},
    {"id": 2, "from_name": "Home", "to_name": "Springfield Monorail", "tag": "Monorail Commute", "both_ways": False, "active": True},
    {"id": 3, "from_name": "", "to_name": "Springfield Airport", "tag": "Airport", "both_ways": False, "active": True},
    {"id": 4, "from_name": "Home", "to_name": "Home", "tag": "Loop", "both_ways": False, "active": False},
    {"id": 5, "from_name": "", "to_name": "", "tag": "Everything", "both_ways": True, "active": True},
]


def test_rules_match_by_name_both_ways_or_one_way_and_a_blank_side_is_anywhere():
    assert match(RULES, "Home", "Power Plant") == "Commute"
    assert match(RULES, "power plant", "HOME") == "Commute", "both ways, case-blind"
    assert match(RULES, "Home", "Springfield Monorail") == "Monorail Commute"
    assert match(RULES, "Springfield Monorail", "Home") is None, "one way only"
    assert match(RULES, "Maple Street, Shelbyville", "Springfield Airport") == "Airport"
    assert match(RULES, "Home", "Home") is None, "inactive rule; and the blank-blank rule never fires"
    assert match(RULES, "Home", "Maple Street, Shelbyville") is None


def test_plan_skips_drives_that_already_carry_a_tag():
    drives = [{"id": 1, "from": "Home", "to": "Power Plant", "tag": None}, {"id": 2, "from": "Power Plant", "to": "Home", "tag": "Errand"}, {"id": 3, "from": "Home", "to": "Nowhere", "tag": None}]
    assert plan(RULES, drives) == [(1, "Commute")]
