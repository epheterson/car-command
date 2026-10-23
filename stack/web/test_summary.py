from datetime import date, datetime, timedelta, timezone

from summary import daily_summary_text, drive_announcement_due, summary_due


def test_summary_text_reads_like_a_person_wrote_it():
    drives = [{"miles": 12.4, "duration": timedelta(minutes=19), "level_start": 80.0, "level_end": 74.0}, {"miles": 12.1, "duration": timedelta(minutes=21), "level_start": 74.0, "level_end": 69.0}]
    charges = [{"kwh": 10.8, "cost": 4.32, "gained": 15.0}]
    breakdown = {"known_h": 24, "hours": {"sleeping": 20.5, "idle": 2.1, "driving": 0.7, "charging": 1.0, "unknown": 0.0}, "asleep_pct": 85.4, "wakeups": 3}
    text = daily_summary_text("Alpha", date(2026, 9, 4), breakdown, drives, charges, 89.0, 344.0)
    assert text.splitlines() == [
        "Alpha, Friday Sep 4:",
        "- 2 drives, 24.5 mi in 40 min, 11% of battery.",
        "- 1 charge: +15%, 10.8 kWh, $4.32 at home.",
        "- Asleep 85% of the day (20.5 h), idle awake 2.1 h, 3 wake-ups.",
        "- Now 89% and 344 mi.",
    ]
    assert daily_summary_text("Alpha", date(2026, 9, 4), None, [], [], 74.0, 286.0) is None, "nothing happened: nothing sent"
    parked = {"known_h": 24, "asleep_pct": 100, "hours": {"sleeping": 24, "idle": 0}, "wakeups": 0}
    assert daily_summary_text("Alpha", date(2026, 9, 4), parked, [], [], None, None).splitlines()[1:] == ["- No drives.", "- No charging.", "- Asleep 100% of the day (24.0 h), idle awake 0.0 h, 0 wake-ups."], "no '?%' without a reading"
    one = daily_summary_text("Alpha", date(2026, 9, 4), None, [{"miles": 3.0}], [], 50.0, None)
    assert one.splitlines()[-1] == "- Now 50%."


def test_summary_due_once_per_day_after_seven():
    assert summary_due(datetime(2026, 9, 4, 6, 59), None) is None
    assert summary_due(datetime(2026, 9, 4, 7, 0), None) == date(2026, 9, 3)
    assert summary_due(datetime(2026, 9, 4, 9, 0), date(2026, 9, 4)) is None
    assert summary_due(datetime(2026, 9, 5, 7, 30), date(2026, 9, 4)) == date(2026, 9, 4)


def test_a_restart_does_not_re_send_yesterdays_summary():
    """The bug seen live: the same 'Sunday Sep 6' summary at 07:00 and again at 14:27,
    because the marker for 'already sent today' lived in memory and every deploy reset it.
    summary_due is the pure half — given the date it was last sent, it must stay quiet."""
    from datetime import date, datetime, timedelta, timezone

    from summary import summary_due

    tz = timezone(timedelta(hours=-7))
    seven_am = datetime(2026, 9, 7, 7, 0, tzinfo=tz)
    assert summary_due(seven_am, None) == date(2026, 9, 6), "the first tick after 07:00 sends yesterday"
    # Every later tick that day, restart or not, has to say no.
    for hour in (7, 11, 14, 23):
        assert summary_due(seven_am.replace(hour=hour), date(2026, 9, 7)) is None, hour
    # And the next morning it is due again, for the new yesterday.
    assert summary_due(datetime(2026, 9, 8, 7, 1, tzinfo=tz), date(2026, 9, 7)) == date(2026, 9, 7)


def test_nothing_goes_out_before_the_hour():
    from datetime import datetime, timedelta, timezone

    from summary import summary_due

    tz = timezone(timedelta(hours=-7))
    assert summary_due(datetime(2026, 9, 7, 6, 59, tzinfo=tz), None) is None


def _drive(ended_min_ago, miles=8.2, id=417):
    return {"id": id, "destination_date": NOW - timedelta(minutes=ended_min_ago), "miles": miles}


NOW = datetime(2026, 9, 11, 18, 0, tzinfo=timezone.utc)


def test_a_finished_drive_is_announced_once():
    assert drive_announcement_due(_drive(2), None, NOW) is True
    assert drive_announcement_due(_drive(2), "417", NOW) is False


def test_a_drive_that_finished_before_a_restart_still_goes_out():
    """The defect the in-process dict had, and the reason this moved to the database.

    A deploy at 18:00 threw away the memory of what had been announced. The next tick was
    the first tick, and the old code suppressed whatever it found on a first tick so that
    a restart could not repeat yesterday's drive. So a drive that ended at 17:58 was never
    announced by anyone: not before the restart, because it had not been noticed yet, and
    not after, because it was found on the tick that swallows things."""
    assert drive_announcement_due(_drive(2), None, NOW) is True


def test_an_old_drive_is_not_announced_after_a_restart():
    """The other half: a restart must not narrate a drive that finished this morning."""
    assert drive_announcement_due(_drive(16), None, NOW) is False
    assert drive_announcement_due(_drive(60 * 9), None, NOW) is False


def test_a_drive_still_under_way_is_not_announced():
    assert drive_announcement_due({"id": 417, "destination_date": None, "miles": 3.0}, None, NOW) is False
    assert drive_announcement_due(None, None, NOW) is False


def test_shuffling_the_car_around_the_driveway_is_not_news():
    assert drive_announcement_due(_drive(2, miles=0.2), None, NOW) is False
    assert drive_announcement_due(_drive(2, miles=None), None, NOW) is False


def test_a_feed_drive_is_announced_only_once_it_is_whole():
    from datetime import datetime, timedelta, timezone

    from summary import drive_whole

    stopped = datetime(2026, 3, 1, 18, 40, tzinfo=timezone.utc)  # the last moment the car was seen driving
    half = {"destination_date": stopped - timedelta(minutes=25), "miles": 4.1}  # the hourly replay, mid-drive
    whole = {"destination_date": stopped - timedelta(minutes=1), "miles": 30.2}
    assert not drive_whole(whole, "Driving", stopped), "never while it drives"
    assert not drive_whole(half, "Idle", stopped), "4 miles of a 30-mile drive is not the drive"
    assert drive_whole(whole, "Idle", stopped), "ends with the stop, less the feed's minute behind it"
    assert drive_whole(whole, "Idle", None), "a car never seen driving since the start has nothing to wait for"
