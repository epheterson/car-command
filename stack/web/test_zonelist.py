"""The time zone picker's list: pure."""

from datetime import datetime, timezone

import zonelist

SUMMER = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
WINTER = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)


def label_of(name, now):
    return dict(i for _, items in zonelist.grouped(now) for i in items)[name]


def test_each_zone_shows_its_city_and_its_offset_now():
    assert label_of("America/Los_Angeles", SUMMER) == "Los Angeles (UTC−7)"
    assert label_of("America/Los_Angeles", WINTER) == "Los Angeles (UTC−8)", "the offset follows the date"
    assert label_of("Asia/Kolkata", SUMMER) == "Kolkata (UTC+5:30)"
    assert label_of("Europe/London", WINTER) == "London (UTC)"
    assert label_of("America/Argentina/Buenos_Aires", SUMMER) == "Argentina / Buenos Aires (UTC−3)"


def test_zones_are_grouped_by_region_and_only_real_regions_are_listed():
    groups = dict(zonelist.grouped(SUMMER))
    assert "America/New_York" in dict(groups["America"]) and "Europe/Paris" in dict(groups["Europe"]) and groups["UTC"] == (("UTC", "UTC"),)
    assert not any(n.startswith(("Etc/", "US/", "posix", "SystemV")) for n in zonelist.names(SUMMER))


def test_an_old_alias_in_force_stays_selectable_and_junk_is_never_offered():
    got = zonelist.with_current("US/Pacific", SUMMER)
    assert got[0] == ("Current", (("US/Pacific", "US/Pacific"),))
    assert zonelist.with_current("America/Denver", SUMMER) == zonelist.grouped(SUMMER)
    assert zonelist.with_current("Mars/Olympus", SUMMER) == zonelist.grouped(SUMMER)
    assert zonelist.with_current("", SUMMER) == zonelist.grouped(SUMMER)
