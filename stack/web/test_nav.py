"""Where each page sits, and the rule for switching car without losing your place."""

from nav import PHONE_TABS, SECTIONS, SETTINGS, car_url, section_of


def test_every_active_key_the_app_uses_has_a_section():
    for key in ("now", "timeline", "drives", "charges", "tags", "calendar", "stats", "charging", "battery", "sleep", "software", "service", "map", "places"):
        assert section_of(key) != "", key
    assert section_of("drives") == "timeline" and section_of("battery") == "stats" and section_of("settings") == "settings"
    assert section_of("") == "now" and section_of(None) == "now" and section_of("nonsense") == "now"


def test_sections_are_well_formed():
    keys = [s[0] for s in SECTIONS]
    assert keys == ["now", "timeline", "calendar", "stats", "map", "places"]
    for key, label, path, pages in SECTIONS:
        assert path.startswith("/") and label and pages and pages[0][2] == path, key
    assert set(PHONE_TABS) <= set(keys) and SETTINGS[2] == "/settings" and len(SETTINGS) == 4


def test_switching_car_keeps_the_page_and_its_query():
    assert car_url("/stats?by=month&vin=V1", "V2") == "/stats?by=month&vin=V2"
    assert car_url("/day?date=2026-09-26", "V2") == "/day?date=2026-09-26&vin=V2"
    assert car_url("/", "all") == "/?vin=all"
    assert car_url("", "V2") == "/?vin=V2"


def test_switching_car_on_one_cars_record_goes_to_that_list():
    assert car_url("/drives/123?vin=V1", "V2") == "/drives?vin=V2"
    assert car_url("/charges/45", "all") == "/charges?vin=all"


def test_switching_car_drops_a_query_that_names_one_record():
    # a filter that only means something for the old car (a tag rule id, a readings page's own car) goes
    assert car_url("/data?date=2026-09-26&vin=V1", "V2") == "/data?date=2026-09-26&vin=V2"
    assert "said" not in car_url("/drives?said=ok&vin=V1", "V2")


def test_templates_get_the_nav_globals():
    import jinja2

    import filters

    env = jinja2.Environment()
    filters.register(env)
    assert env.globals["section_of"]("battery") == "stats"
    assert env.globals["car_href"]("/stats?vin=V1", "V2") == "/stats?vin=V2"
    assert env.globals["nav_sections"][0][0] == "now" and env.globals["nav_phone_tabs"][0] == "now"


def test_settings_has_a_page_per_section():
    from settingsform import SECTIONS as SETTING_SECTIONS

    key, label, path, pages = SETTINGS
    assert (key, label, path) == ("settings", "Settings", "/settings")
    assert [p[0] for p in pages] == [f"settings-{k}" for k, _ in SETTING_SECTIONS]
    assert pages[0][2] == "/settings/cars" and all(p[2] == f"/settings/{p[0][len('settings-'):]}" for p in pages)
    assert section_of("settings-money") == "settings" and section_of("settings") == "settings"


def test_the_shell_draws_settings_pages_where_timeline_draws_its_pages():
    from pathlib import Path

    base = (Path(__file__).parent / "templates" / "base.html").read_text()
    assert "nav_settings[3]" in base, "the sidebar indents Settings' pages and the phone bar lists them"
