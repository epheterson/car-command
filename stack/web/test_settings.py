from settings import parse_rate_rows


def test_rate_rows_parse_sort_and_drop_junk():
    rows = parse_rate_rows(
        [
            {"from": "2026-01-01", "off_peak": "0.40", "part_peak": "0.60", "peak": "0.63"},
            {"from": "2023-06-01", "off_peak": "0.31", "part_peak": "0.45", "peak": "0.50"},
            {"from": "", "off_peak": "", "part_peak": "", "peak": ""},
            {"from": "2024-01-01", "off_peak": "abc", "part_peak": "1", "peak": "1"},
        ]
    )
    assert [r["from"] for r in rows] == ["2023-06-01", "2026-01-01"] and rows[0]["peak"] == 0.50


def test_a_rate_row_survives_the_round_trip_unchanged():
    """The re-price is triggered by comparing parsed rows against what is stored, so the
    parse has to be idempotent: parsing a stored row must give that row back exactly, or
    every save looks like a rate change and re-prices thousands of charges for nothing."""
    import settings as settings_mod

    stored = [{"from": "2000-01-01", "off_peak": 0.4, "part_peak": 0.6, "peak": 0.63}, {"from": "2026-01-01", "off_peak": 0.315, "part_peak": 0.5, "peak": 0.7}]
    as_form = [{"from": r["from"], "off_peak": str(r["off_peak"]), "part_peak": str(r["part_peak"]), "peak": str(r["peak"])} for r in stored]
    assert settings_mod.parse_rate_rows(as_form) == stored


def test_saving_a_string_setting_does_not_break_every_page():
    """psycopg returns JSONB already decoded. load() used to json.loads() any string it got
    back, which fails on "$", "Springfield EV" and "imperial" — so the first time the Settings
    form was ever submitted, Stats, Settings and the startup path all answered 500."""
    import settings

    class Conn:
        def execute(self, sql, *args):
            class R:
                def fetchall(self_inner):
                    return [{"key": "plan", "value": "Springfield EV"}, {"key": "currency", "value": "$"}, {"key": "units", "value": "imperial"}, {"key": "gas_mpg", "value": 28.0}]

            return R()

    st = settings.load(Conn())
    assert st["plan"] == "Springfield EV" and st["rate_plan"]["name"] == "Springfield EV" and st["currency"] == "$" and st["units"] == "imperial" and st["gas_mpg"] == 28.0


def test_a_time_zone_is_accepted_only_if_the_system_knows_it():
    from settings import valid_zone

    assert valid_zone("Europe/London") == "Europe/London"
    assert valid_zone("  America/New_York ") == "America/New_York"
    for bad in ("", None, "Mars/Olympus", "PST", "  "):
        assert valid_zone(bad) is None, repr(bad)


class _Store:
    """A dict-backed cc_settings: enough SQL for load() and save()."""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    def execute(self, sql, args=()):
        import json

        rows = self.rows

        class R:
            def fetchall(self_inner):
                return [{"key": k, "value": v} for k, v in rows.items()] if sql.lstrip().startswith("SELECT") else []

        if sql.lstrip().startswith("INSERT"):
            self.rows[args[0]] = json.loads(args[1])
        return R()

    def commit(self):
        pass


def test_a_fresh_install_stays_opted_out_after_saving_another_section():
    """A fresh install has no rows; saving any section writes only that section's keys.
    The next load must not mistake that for an old install and switch both public paths on."""
    import settings

    db = _Store()
    st = settings.load(db)
    assert (st["public_tiles"], st["public_geocoder"]) == (False, False)
    settings.save(db, "units", "metric")
    st = settings.load(db)
    assert (st["public_tiles"], st["public_geocoder"]) == (False, False)
    assert db.rows["public_tiles"] is False and db.rows["public_geocoder"] is False


def test_an_existing_install_migrates_to_public_once_and_keeps_it():
    import settings

    db = _Store({"units": "imperial"})
    assert (settings.load(db)["public_tiles"], settings.load(db)["public_geocoder"]) == (True, True)
    assert db.rows["public_tiles"] is True and db.rows["public_geocoder"] is True
    settings.save(db, "currency", "€")
    st = settings.load(db)
    assert (st["public_tiles"], st["public_geocoder"]) == (True, True)


def test_weather_is_off_for_a_new_install_and_kept_on_for_an_existing_one():
    """No position goes to Open-Meteo until someone turns weather on; an install from
    before the switch had a default keeps the weather it already had, and a choice stands."""
    import settings

    fresh = _Store()
    assert settings.load(fresh)["weather"] is False
    settings.save(fresh, "units", "metric")
    assert settings.load(fresh)["weather"] is False and fresh.rows["weather"] is False
    old = _Store({"units": "imperial"})
    assert settings.load(old)["weather"] is True and old.rows["weather"] is True
    chosen = _Store({"units": "imperial", "weather": False})
    assert settings.load(chosen)["weather"] is False


def test_explicit_public_choices_are_never_touched():
    import settings

    db = _Store({"units": "imperial", "public_tiles": False, "public_geocoder": True, "rate_plan": None, "sc_price": None, "sc_per_min": None, "weather": True})
    st = settings.load(db)
    assert (st["public_tiles"], st["public_geocoder"]) == (False, True)
    assert db.rows == {"units": "imperial", "public_tiles": False, "public_geocoder": True, "rate_plan": None, "sc_price": None, "sc_per_min": None, "weather": True}


def test_a_fresh_install_has_no_rate_plan_and_keeps_none():
    """No plan for a new install, and none of somebody else's utility either: saving another
    section afterwards must not make it look like an old install that had the built-in rates."""
    import settings

    assert settings.DEFAULTS["rate_plan"] is None and settings.DEFAULTS["rates"] is None and settings.DEFAULTS["schedule"] is None
    db = _Store()
    assert settings.load(db)["rate_plan"] is None
    settings.save(db, "currency", "€")
    assert settings.load(db)["rate_plan"] is None and db.rows["rate_plan"] is None


def test_an_install_from_before_plans_gets_the_plan_it_was_pricing_with():
    import cost
    import settings

    db = _Store({"units": "imperial"})  # never saved Money: the built-in rates and schedule
    plan = settings.load(db)["rate_plan"]
    assert plan == cost.compile_rows(cost.LEGACY_PLAN_NAME, cost.LEGACY_RATES, cost.LEGACY_SCHEDULE) and db.rows["rate_plan"] == plan
    assert plan["name"] == "Earlier rates", "a neutral label; the rates and schedule stay as they were, so no money figure moves"
    assert [pd["price"] for pd in plan["versions"][0]["periods"]] == [0.40, 0.60, 0.63]
    rates = [{"from": "2024-01-01", "off_peak": 0.3, "part_peak": 0.4, "peak": 0.5}]
    sched = [{"period": "peak", "days": "weekdays", "start": 17, "end": 20, "months": None}]
    db = _Store({"plan": "Springfield EV", "rates": rates, "schedule": sched})
    plan = settings.load(db)["rate_plan"]
    assert plan["name"] == "Springfield EV" and plan["source"] == "manual" and cost.plan_rows(plan) == (rates, sched)
    db.rows["rates"] = [{"from": "2025-01-01", "off_peak": 9, "part_peak": 9, "peak": 9}]
    assert settings.load(db)["rate_plan"] == plan, "decided once: the old keys are not read again"


def test_satellite_starts_off_and_a_save_turns_it_on():
    from settingsform import DEFAULTS, parse

    assert DEFAULTS["satellite"] is False
    got = parse("maps", {"public_tiles_shown": "1", "satellite_shown": "1", "satellite": "on"}, DEFAULTS)
    assert got["satellite"] is True and got["public_tiles"] is False
    assert parse("maps", {"satellite_shown": "1"}, dict(DEFAULTS, satellite=True))["satellite"] is False, "an unticked box posts nothing"
