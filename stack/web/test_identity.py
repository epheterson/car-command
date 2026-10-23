"""What each car is: the year from its VIN, Tesla's config, paint and chart colours, the
drawings, and the rule that Tesla is asked only about a car that is already awake."""

import itertools
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import jinja2
import pytest

import identity
from tesla_fleet import TeslaError

HERE = Path(__file__).parent
ALPHA = "5YJ3E1EA0JF000001"  # demo.py's Model 3: position 10 is J


# ---- the model year ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "vin,seen,year",
    [
        (ALPHA, date(2026, 10, 1), 2018),
        ("5YJYGDEE0MF000002", date(2026, 10, 1), 2021),  # M
        ("5YJYGDEE0NF000002", date(2026, 10, 1), 2022),  # N
        ("7SAYGDEE0PF000003", date(2026, 10, 1), 2023),  # P
        ("5YJ3E1EA0RF000004", date(2026, 10, 1), 2024),  # R
        ("5YJ3E1EA0SF000004", date(2026, 10, 1), 2025),  # S
        ("5YJ3E1EA0TF000004", date(2026, 10, 1), 2026),  # T
        ("5YJ3E1EA0VF000004", date(2026, 10, 1), 2027),  # V: a car a year ahead of the calendar
        ("5YJ3E1EA0WF000004", date(2026, 10, 1), None),  # W: 2028 is too far ahead, and 1998 is outside a letter-at-7 VIN's band
        ("5YJ3E1EA01F000004", date(2026, 10, 1), None),  # 1: 2031 is too far ahead, 2001 outside the band
        ("5YJ3E1EA0WF000004", date(2027, 3, 1), 2028),  # the same W, seen a few months on
        ("5YJ3E1EA0AF000004", date(2026, 10, 1), 2010),  # A
        ("1HGCM8263AA000005", date(2026, 10, 1), 1980),  # a digit at 7: 1980-2009, so A is 1980
        ("1G1ZT5ST5LF000006", date(2026, 10, 1), 2020),  # letter at 7: L is 2020
        ("1G1ZT5355LF000006", date(2026, 10, 1), 1990),  # digit at 7: L is 1990
        ("1G1ZT5359YF000006", date(2026, 10, 1), 2000),  # Y, digit at 7
        ("5yj3e1ea0jf000001", date(2026, 10, 1), 2018),  # lower case is the same VIN
    ],
)
def test_the_year_is_read_from_position_ten(vin, seen, year):
    assert identity.model_year(vin, seen) == year


def test_the_cycle_turns_with_the_date_first_seen():
    """Outside North America there is no position-7 rule: a car first seen in 2040 with an A is
    a 2040, the same VIN seen in 2015 a 2010."""
    berlin = "XP7YGCEK0AB000007"
    assert identity.model_year(berlin, date(2015, 6, 1)) == 2010
    assert identity.model_year(berlin, date(2041, 6, 1)) == 2040
    assert identity.model_year("LRW3E7EK0RC000008", date(2026, 1, 1)) == 2024


@pytest.mark.parametrize(
    "vin",
    [
        None,
        "",
        "5YJ3E1EA0JF00000",
        "5YJ3E1EA0JF0000011",
        "5YJ3E1EA0OF000001",
        "5YJ3E1EA0QF000001",
        "5YJ3E1EA0IF000001",
        "5YJ3E1EA0UF000001",
        "5YJ3E1EA0ZF000001",
        "5YJ3E1EA00F000001",
        "5YJ3E1EA0J-000001",
    ],
)
def test_an_invalid_vin_has_no_year(vin):
    assert identity.model_year(vin, date(2026, 1, 1)) is None


# ---- make, model, trim -----------------------------------------------------------------
def test_make_and_model_come_from_the_source_then_tesla_then_the_vin():
    assert identity.make_of(ALPHA, None) == "Tesla" and identity.make_of("50EA1TEA0PA000009", None) == "Lucid"
    assert identity.make_of(ALPHA, " Tesla ") == "Tesla" and identity.make_of("WVWZZZ1KZ6W000010", None) is None
    assert identity.model_of(ALPHA, "Tesla", "Model 3 Long Range", None) == ("Model 3", "Long Range"), "TeslaMate keeps the trim in the model"
    assert identity.model_of(ALPHA, "Tesla", None, "modely") == ("Model Y", None)
    assert identity.model_of(ALPHA, "Tesla", None, "ModelY") == ("Model Y", None), "the stream's CarType, its prefix gone"
    assert identity.model_of(ALPHA, "Tesla", None, None) == ("Model 3", None)
    assert identity.model_of("50EA1TEA0PA000009", "Lucid", "Air", None) == ("Air", None)


@pytest.mark.parametrize(
    "model,badge,name",
    [
        ("Model Y", "74d", "Long Range"),
        ("Model 3", "p74d", "Performance"),
        ("Model 3", "50", "Standard Range"),
        ("Model S", "100d", "100D"),
        ("Model S", "plaid", "Plaid"),
        ("Cybertruck", "awd", "All-Wheel Drive"),
        ("Cybertruck", "cyberbeast", "Cyberbeast"),
        ("Model Y", "", None),
        ("Model Y", None, None),
    ],
)
def test_trims_read_as_sold(model, badge, name):
    assert identity.trim_name(model, badge) == name


# ---- Tesla's config, and its colours ---------------------------------------------------------
def test_only_the_identity_fields_of_vehicle_data_are_kept():
    body = {
        "response": {
            "vin": ALPHA,
            "vehicle_config": {
                "car_type": "modely",
                "trim_badging": "74d",
                "exterior_color": "MidnightSilver",
                "wheel_type": "Apollo19",
                "roof_color": "RoofColorGlass",
                "perf_config": "P2",
                "plg": True,
                "rhd": False,
            },
            "vehicle_state": {"vehicle_name": "Alpha", "odometer": 1234.5, "car_version": "2026.20.6"},
            "drive_state": {"latitude": 39.78, "longitude": -89.6},
        }
    }
    got = identity.tesla_config(body)
    assert got == {"car_type": "modely", "trim_badging": "74d", "exterior_color": "MidnightSilver", "wheel_type": "Apollo19", "roof_color": "RoofColorGlass", "perf_config": "P2"}
    fitted = identity.tesla_config({"response": {"vehicle_config": {"has_seat_cooling": False, "rear_seat_heaters": 1}}})
    assert fitted == {"seat_cooling": False, "rear_seat_heaters": True}
    assert identity.describe("5YJ3E1EA0SF000001", "Tesla", "Model 3", {"detected": fitted})["fitted"] == fitted
    assert identity.tesla_config({}) == {} and identity.tesla_config(None) == {} and identity.tesla_config({"response": {"vehicle_config": {"exterior_color": ""}}}) == {}


@pytest.mark.parametrize(
    "code,make,name",
    [
        ("MidnightSilver", "Tesla", "Midnight Silver"),
        ("PearlWhite", "Tesla", "Pearl White"),
        ("DeepBlue", "Tesla", "Deep Blue"),
        ("SolidBlack", "Tesla", "Solid Black"),
        ("RedMulticoat", "Tesla", "Red Multi-Coat"),
        ("StealthGrey", "Tesla", "Stealth Grey"),
        ("Stealth Gray", "Tesla", "Stealth Grey"),
        ("Quicksilver", "Tesla", "Quicksilver"),
        ("UltraRed", "Tesla", "Ultra Red"),
        ("Silver", "Tesla", "Silver"),
        ("silver", "Volkswagen", "Silver"),
        ("StellarWhite", "Lucid", "Stellar White"),
    ],
)
def test_tesla_colour_codes_have_names_and_a_hex(code, make, name):
    p = identity.paint_of(code, make)
    assert p["name"] == name and re.match(r"^#[0-9A-F]{6}$", p["hex"])


def test_tesla_silver_is_teslas_and_an_unknown_code_is_nothing():
    assert identity.paint_of("Silver", "Tesla")["key"] == "silvermetallic" and identity.paint_of("Silver", "Volvo")["key"] == "silver"
    assert identity.paint_of("PlasmaPurple", "Tesla") is None and identity.paint_of(None) is None
    custom = identity.paint_of("#12abEF")
    assert custom == {"key": "#12ABEF", "name": None, "hex": "#12ABEF"}


def test_a_car_is_described_in_one_line():
    row = {"detected": {"car_type": "modely", "trim_badging": "74d", "exterior_color": "MidnightSilver"}}
    d = identity.describe("7SAYGDEE0NF000003", "Tesla", None, row, date(2026, 10, 1))
    assert d["line"] == "2022 Tesla Model Y Long Range · Midnight Silver" and d["body"] == "modely" and not d["paint_set"]
    own = identity.describe("7SAYGDEE0NF000003", "Tesla", None, dict(row, paint="pearlwhite", body="suv"), date(2026, 10, 1))
    assert own["paint"]["name"] == "Pearl White" and own["paint_set"] and own["body"] == "suv" and own["detected_paint"]["name"] == "Midnight Silver"
    custom = identity.describe(ALPHA, "Tesla", "Model 3", {"paint": "#336699"}, date(2026, 10, 1))
    assert custom["line"] == "2018 Tesla Model 3" and custom["paint"]["hex"] == "#336699", "a custom paint has no name to print"
    bare = identity.describe("50EA1TEA0PA000009", "Lucid", "Gravity", None, date(2026, 10, 1))
    assert bare["line"] == "2023 Lucid Gravity" and bare["body"] == "lucidgravity" and bare["paint"] is None
    nothing = identity.describe("V1", None, None, None)
    assert nothing["line"] == "" and nothing["body"] == "sedan"


def test_the_now_cards_line_leaves_out_the_trim_so_it_fits_on_one_line():
    row = {"detected": {"car_type": "modely", "trim_badging": "74d", "exterior_color": "MidnightSilver"}}
    d = identity.describe("7SAYGDEE0NF000003", "Tesla", None, row, date(2026, 10, 1))
    assert d["short"] == "2022 Tesla Model Y · Midnight Silver" and "Long Range" in d["line"]
    assert identity.describe("V1", None, None, None)["short"] == ""


@pytest.mark.parametrize(
    "make,model,body",
    [
        ("Tesla", "Model 3", "model3"),
        ("Tesla", "Model Y", "modely"),
        ("Tesla", "Model S", "models"),
        ("Tesla", "Model X", "modelx"),
        ("Tesla", "Cybertruck", "cybertruck"),
        ("Lucid", "Air", "lucidair"),
        ("Lucid", "Gravity", "lucidgravity"),
        ("Ford", "F-150 Lightning", "pickup"),
        ("Volkswagen", "ID. Buzz", "van"),
        ("Volvo", "EX90", "suv"),
        ("Chevrolet", "Bolt", "hatchback"),
        ("Polestar", "2", "sedan"),
        (None, None, "sedan"),
    ],
)
def test_the_drawing_follows_the_model(make, model, body):
    assert identity.body_for(make, model) == body


# ---- chart colours ------------------------------------------------------------------------
def test_every_chart_colour_reads_on_both_themes():
    for name, light, dark in identity.PALETTE:
        for theme, hx in (("light", light), ("dark", dark)):
            for surface in identity.SURFACES[theme]:
                assert identity.contrast(hx, surface) >= identity.MIN_CONTRAST, (name, theme, surface, round(identity.contrast(hx, surface), 2))


def test_no_two_chart_colours_are_near_each_other_or_the_states():
    for theme, col in (("light", 1), ("dark", 2)):
        colours = [p[col] for p in identity.PALETTE]
        for a, b in itertools.combinations(colours, 2):
            assert identity.apart(a, b) >= identity.MIN_APART, (theme, a, b, round(identity.apart(a, b), 1))
        for c in colours:
            for state in identity.STATES[theme]:
                assert identity.apart(c, state) >= identity.MIN_APART, (theme, c, state)


def test_the_stylesheet_and_the_maps_carry_the_palette():
    """One source: identity.PALETTE. The CSS's --cN (light, and both dark blocks) and the
    maps' fallback are held to it."""
    css = (HERE / "static" / "style.css").read_text()
    light = dict(re.findall(r"--c(\d+):(#[0-9A-F]{6})", re.search(r"^:root \{ --c0:.*$", css, re.M).group(0)))
    assert [light[str(i)] for i in range(len(identity.PALETTE))] == [p[1] for p in identity.PALETTE]
    darks = [dict(re.findall(r"--c(\d+):(#[0-9A-F]{6})", m)) for m in re.findall(r"\{ --c0:#D9A82A;[^}]*\}", css)]
    assert len(darks) == 2 and darks[0] == darks[1]
    for i, (_, lt, dk) in enumerate(identity.PALETTE):
        assert darks[0].get(str(i), lt) == dk, i
    for sel in (".chip.c{}", ".dot.c{}", ".mbars .mb.c{}", ".mbars figcaption i.c{}", ".chip-t i.c{}", ".c{} {{ --car"):
        for i in range(len(identity.PALETTE)):
            assert sel.format(i).replace("{{", "{") in css, sel.format(i)
    maps = (HERE / "static" / "maps.js").read_text()
    assert re.findall(r'"(#[0-9A-F]{6})"', re.search(r"var fallback = \[(.*?)\]", maps).group(1)) == [p[1] for p in identity.PALETTE]


def test_a_car_with_no_colour_takes_one_by_its_place_in_the_garage_and_never_gold():
    vins = ["A", "B", "C", "D", "E", "F", "G"]
    got = identity.chart_colors(vins, {})
    assert [got[v] for v in vins[:3]] == [1, 2, 3], "from Violet on, in the garage's order"
    assert 0 not in [got[v] for v in vins[:5]], "Gold is the battery's and the mark's, not a default"
    assert len(set(got.values())) == 7, "a seventh car gets a colour of its own, not the first car's again"
    _all_distinct({v: got[v] for v in vins[:5]})
    assert identity.chart_colors(["A"], {})["A"] != 0 and identity.chart_colors(["A"], {"A": 99})["A"] != 0, "an unusable choice does not fall back to Gold either"


def _all_distinct(colors):
    cs = list(colors.values())
    for n, a in enumerate(cs):
        for b in cs[n + 1 :]:
            for theme in (1, 2):
                d = identity.de2000(identity.PALETTE[a][theme], identity.PALETTE[b][theme])
                assert d >= identity.MIN_DISTINCT, (identity.PALETTE[a][0], identity.PALETTE[b][0], round(d, 1))


def test_ciede2000_reads_as_a_person_sees():
    """Identical is 0, red against green is far, and the pair that started this sits under 20."""
    assert identity.de2000("#000000", "#000000") == 0
    assert 16.0 < identity.de2000("#2A4E90", "#41464D") < 17.0, "blue beside charcoal: the pair the owner found too close"
    assert identity.de2000("#FF0000", "#00FF00") > 80


def test_too_close_is_symmetric_and_every_car_colour_reads_on_both_themes():
    for i, near in identity.TOO_CLOSE.items():
        for j in near:
            assert i in identity.TOO_CLOSE[j]
    for _, lt, dk in identity.PALETTE:
        for theme, hx in (("light", lt), ("dark", dk)):
            for surface in identity.SURFACES[theme]:
                assert identity.contrast(hx, surface) >= identity.MIN_CONTRAST


def test_a_car_with_no_choice_takes_the_colour_of_its_reported_paint():
    deep = identity.PAINTS["deepblue"][1]
    got = identity.chart_colors(["A", "B"], {}, {"A": deep, "B": None})
    assert identity.PALETTE[got["A"]][0] == "Slate" and got["B"] not in (got["A"], None), "the paint picks A's; B takes a free one by its place"
    black = identity.PAINTS["solidblack"][1]
    got = identity.chart_colors(["A", "B"], {}, {"A": deep, "B": black})
    assert identity.PALETTE[got["A"]][0] == "Slate" and got["B"] != got["A"], "a black car beside a blue one: Slate is taken, so its place in the garage"
    _all_distinct(got)


def test_a_choice_wins_over_the_paint_unless_it_sits_beside_an_earlier_cars():
    deep = identity.PAINTS["deepblue"][1]
    assert identity.chart_colors(["A"], {"A": 3}, {"A": deep})["A"] == 3, "picked by hand beats the paint"
    got = identity.chart_colors(["A", "B"], {"A": 10, "B": 13})
    assert got["A"] == 10 and got["B"] != 13, "the owner's blue and charcoal were too close: the later car moves"
    _all_distinct(got)
    assert identity.too_close(13, {"A": 10, "B": 5}, "B") == "A" and identity.too_close(3, {"A": 10}, "B") is None
    assert identity.blocked({10}) >= {10, 13, 9, 1}
    chosen = identity.chart_colors(["A", "B", "C"], {"C": 0})
    assert chosen["C"] == 0 and chosen["A"] != 0 and len(set(chosen.values())) == 3, "a choice wins; the car it displaced moves to a free one"
    clash = identity.chart_colors(["A", "B"], {"A": 3, "B": 3})
    assert clash["A"] == 3 and clash["B"] != 3, "two cars never share; the first in the garage keeps it"
    assert identity.chart_colors(["A"], {"A": 99})["A"] != 0 and identity.chart_colors(["A"], {"A": True})["A"] != 0


def test_a_taken_colour_is_named_and_the_paint_suggests_the_nearest():
    colors = {"A": 0, "B": 2}
    assert identity.chart_taken("A", 2, colors) == "B" and identity.chart_taken("A", 0, colors) is None
    assert identity.PALETTE[identity.nearest_chart("#A11722")][0] in ("Coral", "Raspberry", "Rust"), "a red car, a red line"
    red = identity.nearest_chart("#A11722")
    assert identity.nearest_chart("#A11722", {red}) != red, "a colour another car holds is never suggested"


def name_of(i):
    return identity.PALETTE[i][0]


@pytest.mark.parametrize(
    "paint, chart",
    [
        ("deepblue", "Slate"),
        ("sapphireblue", "Violet"),
        ("blue", "Slate"),
        ("green", "Teal"),
        ("auroragreen", "Teal"),
        ("pearlwhite", "Slate"),
        ("silvermetallic", "Slate"),
        ("stellarwhite", "Slate"),
        ("midnightsilver", "Slate"),
        ("grey", "Slate"),
        ("quantumgrey", "Slate"),
        ("solidblack", "Slate"),
        ("infiniteblack", "Slate"),
        ("ultrared", None),  # a red: Coral, Raspberry or Rust
    ],
)
def test_every_common_paint_has_a_matching_chart_colour(paint, chart):
    got = name_of(identity.nearest_chart(identity.PAINTS[paint][1]))
    assert got == chart if chart else got in ("Coral", "Raspberry", "Rust"), (paint, got)


def test_a_paint_whose_match_is_taken_gets_the_next_nearest_of_its_kind():
    blue = identity.nearest_chart("#23417A")
    assert name_of(blue) == "Slate" and identity.nearest_chart("#23417A", {blue}) not in (blue, None)
    assert identity.nearest_chart("#EDEDEA", {identity.NEUTRAL}) is None, "white with Slate taken: its place in the garage"
    assert identity.nearest_chart("#A11722", set(range(len(identity.PALETTE)))) is None and identity.nearest_chart(None) is None


def test_a_paint_never_picks_gold_or_a_muted_match():
    for key, (_name, hx, *_rest) in identity.PAINTS.items():
        got = identity.nearest_chart(hx)
        assert got is None or got in identity.AUTO, key
    assert 0 not in identity.AUTO, "gold is the battery's and the mark's"


def test_the_new_colours_keep_off_the_sites_own_greys():
    for theme, col in (("light", 1), ("dark", 2)):
        for p in identity.PALETTE:
            for grey in identity.UI_GREYS[theme]:
                assert identity.apart(p[col], grey) >= identity.MIN_FROM_UI, (theme, p[0], grey, round(identity.apart(p[col], grey), 1))


def test_cars_keep_their_colours_when_the_palette_grows():
    assert [p[0] for p in identity.PALETTE[:10]] == ["Gold", "Violet", "Coral", "Teal", "Orchid", "Olive", "Rust", "Raspberry", "Moss", "Slate"], "a stored index means what it meant"
    assert [p[0] for p in identity.PALETTE[10:]] == ["Blue", "Green", "Steel", "Charcoal"]


BLUE, GREEN = 10, 11


def test_a_new_paint_with_the_chart_left_alone_takes_its_match():
    deep = identity.paint_of("deepblue")
    assert identity.chart_after_look(3, 3, False, "midnightsilver", deep, set()) == identity.NEUTRAL, "paint changed, chart untouched: the match"
    assert identity.chart_after_look(3, 3, False, "deepblue", deep, set()) == 3, "same paint: the colour stays"
    assert identity.chart_after_look(None, 3, False, "deepblue", deep, set()) is None, "nothing changed, nothing chosen"
    assert identity.chart_after_look(3, 3, False, "midnightsilver", deep, {BLUE}) not in (3, BLUE), "another car's Blue: the next nearest free"


def test_a_chart_colour_picked_by_hand_wins_over_the_paint():
    deep = identity.paint_of("deepblue")
    assert identity.chart_after_look(GREEN, 3, False, "midnightsilver", deep, set()) == GREEN, "a different chart colour is a pick"
    assert identity.chart_after_look(3, 3, True, "midnightsilver", deep, set()) == 3, "the same one, touched (chart_set), is a pick too"


# ---- settings -----------------------------------------------------------------------------
def test_the_look_form_parses_to_what_is_stored():
    f = {"paint_V1": "pearlwhite", "paint_hex_V1": "#888888", "paint_hex0_V1": "#888888", "chart_V1": "7", "body_V1": "suv"}
    assert identity.parse_choice(f, "V1", "Tesla") == ("pearlwhite", 7, "suv")
    assert identity.parse_choice({"paint_V1": "auto", "chart_V1": "", "body_V1": "auto"}, "V1", "Tesla") == (None, None, None)
    moved = {"paint_V1": "auto", "paint_hex_V1": "#336699", "paint_hex0_V1": "#888888"}
    assert identity.parse_choice(moved, "V1", None)[0] == "#336699", "moving the picker is choosing it"
    assert identity.parse_choice({"paint_V1": "custom", "paint_hex_V1": "#336699", "paint_hex0_V1": "#336699"}, "V1", None)[0] == "#336699"
    bad = {"paint_V1": "custom", "paint_hex_V1": "red;}", "chart_V1": "99", "body_V1": "spaceship"}
    assert identity.parse_choice(bad, "V1", None) == (None, None, None)
    assert identity.parse_choice({"paint_V1": "<script>"}, "V1", None)[0] is None
    assert identity.parse_choice({"chart_V1": "\u0663"}, "V1", None)[1] is None and identity.parse_choice({"chart_V1": "\u00b2"}, "V1", None)[1] is None, "ASCII digits only"


class FakeConn:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        return self

    def fetchall(self):
        return []

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_choice_and_a_detection_are_stored_without_overwriting_each_other():
    conn = FakeConn()
    identity.save_choice(conn, "V1", "pearlwhite", 3, None)
    identity.save_detected(conn, "V1", {"exterior_color": "MidnightSilver"}, datetime(2026, 10, 1, tzinfo=timezone.utc))
    choice, detected = conn.sql
    assert "detected" not in choice[0].split("DO UPDATE")[1] and choice[1] == ("V1", "pearlwhite", 3, None)
    assert "paint" not in detected[0].split("DO UPDATE")[1] and "detected || EXCLUDED.detected" in detected[0]


def test_the_table_is_created_at_startup():
    src = (HERE / "app.py").read_text()
    assert '("car identity", identity.DDL)' in src and "CREATE TABLE IF NOT EXISTS cc_car_identity" in identity.DDL


# ---- never waking the car ----------------------------------------------------------------------
T1, T2, T3 = (
    "5YJ3E1EA0JF000011",
    "7SAYGDEE0NF000012",
    "5YJYGDEE0MF000013",
)  # three Teslas in the garage


class FakeFleet:
    def __init__(self, cars, configs=None, fail=None):
        self.cars, self.configs, self.fail = cars, configs or {}, dict(fail or {})
        self.asked: list[str] = []
        self.listed = 0

    def vehicles(self, token, base):
        self.listed += 1
        return self.cars

    def vehicle_config(self, token, base, vin):
        self.asked.append(vin)
        if vin in self.fail:
            status = self.fail[vin]
            raise TeslaError(
                (f"Tesla refused ({status})" if status else "Tesla did not answer (ConnectError)."),
                status,
            )
        return {"response": {"vehicle_config": self.configs.get(vin, {})}}


class Store:
    """cc_car_identity in memory, through the module's own load and save calls."""

    def __init__(self, rows=None):
        self.rows = rows or {}

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        pass


def worker(fleet, store, monkeypatch, streamed=None, garage=(T1, T2, T3)):
    saved = []

    def save_detected(conn, vin, found, checked_at=None):
        row = store.rows.setdefault(vin, {"vin": vin, "detected": {}, "checked_at": None})
        row["detected"] = {**row["detected"], **found}
        row["checked_at"] = checked_at or row["checked_at"]
        saved.append(vin)

    monkeypatch.setattr(identity, "load", lambda conn: {k: dict(v) for k, v in store.rows.items()})
    monkeypatch.setattr(identity, "save_detected", save_detected)
    after = []
    w = identity.Worker(
        connect=store.connect,
        access=lambda: ("tok", "https://fleet.example.test"),
        fleet=lambda: fleet,
        streamed=lambda: streamed or {},
        after=lambda: after.append(1),
        vins=lambda: list(garage),
    )
    return w, saved, after


NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
LATER = NOW + identity.LIST_GAP  # the next round that may ask the list again


def test_an_asleep_car_is_never_asked(monkeypatch):
    fleet = FakeFleet(
        [
            {"vin": T1, "state": "asleep"},
            {"vin": T2, "state": "offline"},
            {"vin": T3, "state": "online"},
        ],
        {T3: {"exterior_color": "PearlWhite"}},
    )
    store = Store()
    w, saved, after = worker(fleet, store, monkeypatch)
    assert w.round(NOW) == [T3]
    assert fleet.asked == [T3], "only the car already awake"
    assert store.rows[T3]["detected"] == {"exterior_color": "PearlWhite"} and store.rows[T3]["checked_at"] == NOW and after


def test_a_car_read_today_is_not_read_again_and_the_list_is_not_asked_every_hour(
    monkeypatch,
):
    fleet = FakeFleet(
        [{"vin": T1, "state": "online"}, {"vin": T2, "state": "asleep"}],
        {T1: {"car_type": "model3"}},
    )
    store = Store(
        {
            T1: {
                "vin": T1,
                "detected": {"car_type": "model3"},
                "checked_at": NOW - timedelta(hours=5),
            }
        }
    )
    w, saved, _ = worker(fleet, store, monkeypatch, garage=(T1, T2))
    assert w.round(NOW) == [] and fleet.asked == [] and fleet.listed == 1, "T2 is due, so the list is asked; T2 is asleep"
    assert w.round(NOW + timedelta(hours=1)) == [] and fleet.listed == 1, "the list waits LIST_GAP between asks"
    assert w.round(NOW + timedelta(hours=20)) == [T1] and fleet.asked == [T1], "a day on, read again"


def test_the_list_is_not_asked_when_no_garage_tesla_is_due(monkeypatch):
    fleet = FakeFleet([{"vin": T1, "state": "online"}])
    store = Store({vin: {"vin": vin, "detected": {}, "checked_at": NOW - timedelta(hours=2)} for vin in (T1, T2)})
    w, _, _ = worker(fleet, store, monkeypatch, garage=(T1, T2, "50EA1TEA0PA000009", "V9"))
    assert w.round(NOW) == [] and fleet.listed == 0, "both Teslas read today; the Lucid and the unknown VIN are never Tesla's"


def test_only_cars_in_the_garage_are_read(monkeypatch):
    stranger = "5YJ3E1EA0JF000099"
    fleet = FakeFleet(
        [{"vin": stranger, "state": "online"}, {"vin": T1, "state": "online"}],
        {T1: {"car_type": "model3"}, stranger: {"car_type": "modely"}},
    )
    w, _, _ = worker(fleet, Store(), monkeypatch, garage=(T1,))
    assert w.round(NOW) == [T1] and fleet.asked == [T1], "the account's other car is not ours to read"


def test_a_refusal_waits_a_day(monkeypatch):
    fleet = FakeFleet([{"vin": T1, "state": "online"}], fail={T1: 403})
    store = Store()
    w, saved, after = worker(fleet, store, monkeypatch, garage=(T1,))
    assert w.round(NOW) == [] and fleet.asked == [T1] and store.rows[T1]["checked_at"] == NOW, "kept as the day's read"
    assert w.round(LATER) == [] and fleet.asked == [T1] and fleet.listed == 1, "not asked again, and nothing due means no list"
    assert w.round(NOW + timedelta(hours=25)) == [] and fleet.asked == [
        T1,
        T1,
    ], "a day on, tried again"


def test_too_many_requests_ends_the_round(monkeypatch):
    fleet = FakeFleet(
        [{"vin": T1, "state": "online"}, {"vin": T2, "state": "online"}],
        {T2: {"car_type": "modely"}},
        fail={T1: 429},
    )
    store = Store()
    w, _, _ = worker(fleet, store, monkeypatch, garage=(T1, T2))
    assert w.round(NOW) == [] and fleet.asked == [T1], "the second car is not asked after a 429"
    assert T1 not in store.rows, "a 429 is not the car's answer: no day's wait"
    fleet.fail = {}
    assert sorted(w.round(LATER)) == [T2] and fleet.asked == [T1, T1, T2]


@pytest.mark.parametrize("status", [500, 503, None])
def test_a_failure_on_teslas_side_is_retried_twice_a_day(monkeypatch, status):
    fleet = FakeFleet([{"vin": T1, "state": "online"}], {T1: {"car_type": "model3"}}, fail={T1: status})
    store = Store()
    w, _, _ = worker(fleet, store, monkeypatch, garage=(T1,))
    assert w.round(NOW) == [] and T1 not in store.rows
    assert w.round(LATER) == [] and fleet.asked == [
        T1,
        T1,
    ], "tried again at the next round"
    assert w.round(LATER + identity.LIST_GAP) == [] and fleet.asked == [T1, T1] and fleet.listed == 2, "two tries a day, then it waits (and so does the list)"
    fleet.fail = {}
    assert w.round(LATER + timedelta(hours=25)) == [T1] and fleet.asked == [
        T1,
        T1,
        T1,
    ], "the next day it is tried again"


def test_a_car_that_fell_asleep_since_the_list_is_left_for_next_time(monkeypatch):
    fleet = FakeFleet([{"vin": T1, "state": "online"}], fail={T1: 408})
    store = Store()
    w, saved, after = worker(fleet, store, monkeypatch, garage=(T1,))
    assert w.round(NOW) == [] and fleet.asked == [T1] and saved == [] and not after
    assert w.round(LATER) == [] and fleet.asked == [T1, T1] and w.round(LATER + identity.LIST_GAP) == [] and len(fleet.asked) == 3, "an asleep car spends no try"


def test_no_tesla_account_asks_nothing(monkeypatch):
    w, _, _ = worker(None, Store(), monkeypatch)
    assert w.round(NOW) == []


def test_what_the_stream_carried_is_kept_without_asking_tesla(monkeypatch):
    fleet = FakeFleet([])
    store = Store()
    w, saved, after = worker(
        fleet,
        store,
        monkeypatch,
        streamed={"V1": {"CarType": "ModelY", "ExteriorColor": "DeepBlue", "Trim": "74D"}},
        garage=(),
    )
    assert w.round(NOW) == ["V1"] and fleet.asked == []
    assert store.rows["V1"]["detected"] == {"car_type": "ModelY", "exterior_color": "DeepBlue", "trim_badging": "74D"} and store.rows["V1"]["checked_at"] is None
    assert w.round(NOW + timedelta(hours=4)) == [], "the same values again change nothing"


def test_the_fleet_client_asks_only_for_config_and_state():
    from test_tesla_fleet import fleet

    f, fake = fleet((200, {"response": {"vehicle_config": {"car_type": "modely"}}}))
    assert identity.tesla_config(f.vehicle_config("acc", "https://fleet.example.test", ALPHA)) == {"car_type": "modely"}
    url = fake.sent[0]["url"]
    assert url == f"https://fleet.example.test/api/1/vehicles/{ALPHA}/vehicle_data?endpoints=vehicle_config%3Bvehicle_state" and fake.sent[0]["method"] == "GET"
    assert "wake" not in url and "location" not in url
    f, _ = fleet((408, {"error": "vehicle unavailable: vehicle is offline or asleep"}))
    with pytest.raises(TeslaError) as e:
        f.vehicle_config("acc", "https://fleet.example.test", ALPHA)
    assert e.value.status == 408


def test_the_stream_asks_for_the_identity_fields_and_reads_the_car_type():
    import tesla_net
    import tesla_stream

    for k in identity.STREAM_KEYS:
        assert k in tesla_net.FIELDS, k
    assert tesla_stream.value_of({"carTypeValue": "CarTypeModelY"}) == "ModelY"
    assert tesla_stream.value_of({"stringValue": "MidnightSilver"}) == "MidnightSilver"


def test_the_stream_worker_keeps_the_identity_fields_it_sees():
    import tesla_live

    s = tesla_live.TeslaStream(connect=None, url=None)
    rec = {"data": [{"key": "ExteriorColor", "value": {"stringValue": "PearlWhite"}}, {"key": "Trim", "value": {"stringValue": "74"}}, {"key": "CarType", "value": {"carTypeValue": "CarTypeModel3"}}]}
    s.carry("V1", NOW, rec)
    assert s.identity["V1"] == {"ExteriorColor": "PearlWhite", "Trim": "74", "CarType": "Model3"}


@pytest.mark.parametrize("body", sorted(identity.BODIES))
def test_each_body_renders_on_the_now_card(body):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(HERE / "templates")), autoescape=True)
    import filters

    filters.register(env)
    ident = identity.describe(ALPHA, "Tesla", "Model 3", {"paint": "midnightsilver", "body": body}, date(2026, 10, 1))
    html = env.from_string('{% import "_parts.html" as parts %}{{ parts.carpic(ident, "/cc") }}').render(ident=ident)
    assert 'class="paintsw" style="--paint:#5D6168" title="Midnight Silver"' in html and "<svg" not in html, "a paint swatch, no drawing"


# ---- the pages -----------------------------------------------------------------------------
def _env():
    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(HERE / "templates")))
    filters.register(env)
    return env


T0 = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
BRAVO = "7SAYGDEE0NF000003"
IDENTS = {
    ALPHA: identity.describe(ALPHA, "Tesla", "Model 3", {"detected": {"exterior_color": "PearlWhite", "trim_badging": "74d"}}, date(2026, 10, 1)),
    BRAVO: identity.describe(BRAVO, "Tesla", None, {"detected": {"car_type": "modely", "exterior_color": "MidnightSilver"}}, date(2026, 10, 1)),
}
VEHICLES = [{"vin": ALPHA, "name": "Alpha"}, {"vin": BRAVO, "name": "Bravo"}]
BASE = {"root": "/cc", "now": T0, "active": "now", "vehicles": VEHICLES, "q": "", "colors": {ALPHA: 0, BRAVO: 7}, "idents": IDENTS, "names": {ALPHA: "Alpha", BRAVO: "Bravo"}, "cur": "$"}


def test_the_garage_cards_and_the_switcher_show_each_car():
    rib = [{"label": "sleeping", "width": 1.0, "start": T0 - timedelta(hours=24), "end": T0}]
    cars = [
        {
            "vin": v["vin"],
            "name": v["name"],
            "status": "Sleeping",
            "soc": 60.0,
            "range_mi": 200.0,
            "place": "Home",
            "live": True,
            "updated_age": "just now",
            "ribbon": rib,
            "last_drive": None,
            "ident": IDENTS[v["vin"]],
        }
        for v in VEHICLES
    ]
    html = _env().get_template("garage.html").render(**BASE, car={"vin": "all", "name": "All cars", "garage": True}, cars=cars, today={}, line=[], ticks=[])
    # the year, make, model and paint belong to the car's own page; All cars stays a glance
    assert "Pearl White" not in html and "Midnight Silver" not in html and 'class="cc-id"' not in html and "cars.svg" not in html
    assert '<span class="dot c7"></span>' in html and "paintdot" not in html, "the switcher's dot is the chart colour alone"
    assert "<symbol" not in html, "the sprite is linked, never inlined"


def test_the_now_card_leads_with_the_car():
    car = {"vin": BRAVO, "name": "Bravo", "status": "Idle", "soc": 50.0, "range_mi": 150.0, "live": True, "updated_age": "just now", "lat": None, "lon": None}
    kw = dict(BASE, car=car, today=None, due=[], route=None, tread_last=None, u={"dist": "mi", "temp": "°F"})
    html = _env().get_template("_now_hero.html").render(**kw, ident=IDENTS[BRAVO])
    assert 'class="hero-id"' in html and 'class="paintsw"' in html and "2022 Tesla Model Y · Midnight Silver" in html
    assert 'class="hero-id"' not in _env().get_template("_now_hero.html").render(**kw), "no identity, no empty row"


def test_settings_offers_paint_chart_colour_and_drawing():
    cards = [
        {"vin": v["vin"], "name": v["name"], "model": "", "sources": [], "newest": None, "pack": {"kwh": None, "origin": None, "text": "unknown", "figure": None}, "choices": []} for v in VEHICLES
    ]
    for c in cards:
        ident = IDENTS[c["vin"]]
        taken = {i: "Other" for v, i in BASE["colors"].items() if v != c["vin"]}
        near = identity.nearest_chart((ident["paint"] or {}).get("hex"), set(taken))
        charts = [{"i": i, "name": p[0], "taken": taken.get(i), "near": i == near} for i, p in enumerate(identity.PALETTE)]
        c.update(ident=ident, paints=identity.paints_for("Tesla"), auto_body=ident["body"], charts=charts)
    html = (
        _env()
        .get_template("settings_cars.html")
        .render(
            **dict(BASE, active="cars"),
            car={"vin": "all", "name": "All cars", "garage": True},
            st={},
            conv={},
            saved=False,
            section="cars",
            label="Cars and sources",
            cars=cards,
            accounts=[],
            sources=[],
            env_sources=[],
            vault_ready=True,
            bodies=identity.BODIES,
        )
    )
    assert html.count('name="look"') == 2 and f'name="paint_{ALPHA}" value="midnightsilver"' in html and f'name="paint_hex_{ALPHA}"' in html
    assert f'name="chart_{ALPHA}" value="7" aria-label="Raspberry, used by Other" disabled' in html, "another car's colour cannot be picked"
    assert f'name="chart_{ALPHA}" value="0" aria-label="Gold" checked' in html
    assert f'name="body_{BRAVO}"' not in html, "the drawing picker is off while the drawings are"
    assert "2018 Tesla Model 3 Long Range · Pearl White" in html and html.count('class="paintsw"') >= 2


# ---- the app's use of it -----------------------------------------------------------------------
def _app(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    monkeypatch.setenv("REST_USER", "u")
    monkeypatch.setenv("REST_PASSWORD", "p")
    import app

    return app


def test_the_map_cells_name_the_car_and_the_page_picks_its_colour(monkeypatch):
    """A colour chosen in Settings shows on the map at once: the cached cells carry each car's
    place in the garage, never a colour, so they need no rebuilding when one changes."""
    import json

    app = _app(monkeypatch)
    cars = [{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}]
    monkeypatch.setattr(app, "all_vehicles", lambda: list(cars))
    built = []

    def driveprint(vin):
        built.append(vin)
        return [(39.78, -89.6, 3, "x")] if vin == "V1" else [(39.8, -89.65, 1, "x")]

    monkeypatch.setattr(app, "driveprint", driveprint)
    monkeypatch.setattr(app, "_driveprint_body", {})
    colors = {"V1": 0, "V2": 1}
    monkeypatch.setattr(app, "car_colors", lambda vehicles: dict(colors))
    body = json.loads(app.driveprint_body(app.GARAGE))
    assert body == [[39.78, -89.6, 3, 0], [39.8, -89.65, 1, 1]]
    colors.update(V1=7)
    assert json.loads(app.driveprint_body(app.GARAGE)) == body and built == [
        "V1",
        "V2",
    ], "a new colour: the same cached cells"
    cars.insert(0, {"vin": "V0", "name": "Zulu"})
    assert [c[3] for c in json.loads(app.driveprint_body(app.GARAGE)) if c[0] == 39.78] == [1] and built[-1] == "V2", "a new car moves the others: built again, Alpha now second"
    page = (HERE / "templates" / "map.html").read_text()
    assert "fillColor: CCMap.palette[colours[v] || 0]" in page and "order[c[3] || 0]" in page


def test_saving_a_look_forgets_the_identities_after_the_commit(monkeypatch):
    import asyncio

    app = _app(monkeypatch)
    events = []

    class Conn:
        def execute(self, *a, **k):
            return self

        def commit(self):
            events.append("commit")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Form(dict):
        pass

    class Req:
        scope = {"root_path": ""}
        query_params = {}
        headers = {}

        async def form(self):
            return Form(look="V1", csrf_token="x")

    real = app._cache.pop

    class Cache(dict):
        def pop(self, key, *d):
            if key == "identity":
                events.append("forget")
            return real(key, *d) if key in app._cache else (d[0] if d else None)

    monkeypatch.setattr(app, "_cache", Cache(app._cache))
    monkeypatch.setattr(app, "db", lambda timeout=None: Conn())
    monkeypatch.setattr(app, "current_settings", lambda: {})
    monkeypatch.setattr(app, "settings_changed", lambda: None)
    monkeypatch.setattr(app, "capacities", lambda: [])
    monkeypatch.setattr(app, "save_look", lambda conn, form: events.append("save"))
    asyncio.run(app.settings_save(Req(), "cars"))
    assert events.index("save") < events.index("commit") < events.index("forget")


def test_saving_a_new_paint_picks_its_chart_colour_unless_one_was_picked(monkeypatch):
    app = _app(monkeypatch)
    cars = [{"vin": ALPHA, "name": "Alpha"}, {"vin": BRAVO, "name": "Bravo"}]
    monkeypatch.setattr(app, "all_vehicles", lambda: list(cars))
    monkeypatch.setattr(app, "car_identities", lambda vehicles: dict(IDENTS))
    monkeypatch.setattr(app, "car_colors", lambda vehicles: {ALPHA: 0, BRAVO: 3})
    monkeypatch.setattr(app, "identity_rows", lambda: ({}, {}))
    saved = []
    monkeypatch.setattr(app.identity, "save_choice", lambda conn, vin, paint, chart, body: saved.append((vin, paint, chart)))

    def save(**form):
        app.save_look(None, {"look": ALPHA, f"paint_hex_{ALPHA}": "#888888", f"paint_hex0_{ALPHA}": "#888888", **form})
        return saved[-1]

    assert save(**{f"paint_{ALPHA}": "deepblue", f"chart_{ALPHA}": "0"}) == (ALPHA, "deepblue", 9), "Deep Blue, chart left at Gold: Slate"
    assert save(**{f"paint_{ALPHA}": "deepblue", f"chart_{ALPHA}": "8"}) == (ALPHA, "deepblue", 8), "Moss picked in the same save stays"
    assert save(**{f"paint_{ALPHA}": "deepblue", f"chart_{ALPHA}": "0", f"chart_set_{ALPHA}": "1"}) == (ALPHA, "deepblue", 0), "Gold picked again by hand stays"
    assert save(**{f"paint_{ALPHA}": "auto", f"chart_{ALPHA}": "0"}) == (ALPHA, None, 0), "the paint the car reports, unchanged: the colour stays"
    monkeypatch.setattr(app, "car_colors", lambda vehicles: {ALPHA: 0, BRAVO: 10})
    got = save(**{f"paint_{ALPHA}": "deepblue", f"chart_{ALPHA}": "0"})[2]
    assert got not in identity.blocked({10}), "Bravo holds Blue: the nearest colour not too close to it"
    n = len(saved)
    said = app.save_look(None, {"look": ALPHA, f"chart_{ALPHA}": "13", f"chart_set_{ALPHA}": "1", f"paint_hex_{ALPHA}": "#888888", f"paint_hex0_{ALPHA}": "#888888"})
    assert said == "Charcoal is too close to Bravo's colour; pick another." and len(saved) == n, "a hand pick beside another car's colour is refused, nothing saved"


def test_the_look_row_shows_one_swatch_when_paint_and_chart_match_and_each_paint_names_its_match():
    card = {"vin": ALPHA, "name": "Alpha", "model": "", "sources": [], "newest": None, "pack": {"kwh": None, "origin": None, "text": "unknown", "figure": None}, "choices": []}
    ident = IDENTS[ALPHA]
    paints = [{**p, "chart": identity.nearest_chart(p["hex"])} for p in identity.paints_for("Tesla")]
    charts = [{"i": i, "name": p[0], "taken": None, "near": False} for i, p in enumerate(identity.PALETTE)]

    def render(**kw):
        c = dict(card, ident=ident, paints=paints, charts=charts, auto_chart=12, **kw)
        ctx = dict(BASE, active="cars", colors={ALPHA: kw.get("mine", 0)})
        return (
            _env()
            .get_template("settings_cars.html")
            .render(
                **ctx,
                car={"vin": "all", "name": "All cars", "garage": True},
                st={},
                conv={},
                saved=False,
                section="cars",
                label="Cars",
                cars=[c],
                accounts=[],
                sources=[],
                env_sources=[],
                vault_ready=True,
            )
        )

    apart_html = render(matched=False, chart_name="Gold")
    assert "on charts" in apart_html and 'class="dot c0"' in apart_html, "paint and chart differ: both"
    one = render(matched=True, chart_name="Steel", mine=12)
    assert "on charts</span>" not in one, "they match: one swatch"
    assert f'name="paint_{ALPHA}" value="deepblue" aria-label="Deep Blue" data-chart="9"' in one and 'data-chart-set' in one and 'data-chart="12"' in one


def test_the_now_card_says_nothing_of_a_reading_it_does_not_have():
    car = {
        "vin": BRAVO,
        "name": "Bravo",
        "status": "Idle",
        "live": True,
        "updated_age": "just now",
        "lat": None,
        "lon": None,
        "meters": {"trip": 10.0},
    }
    html = _env().get_template("_now_hero.html").render(**BASE, car=car, today=None, due=[], route={"name": "Work"}, tread_last=None, u={"dist": "mi", "temp": "°F"})
    assert "°F" not in html and "FSD" not in html and " min" not in html, "a missing field is not a reading"
    assert "—<" in html or "—" in html


def test_the_guard_mode_is_named_by_make():
    assert identity.guard_name("Tesla") == "Sentry" and identity.guard_name("lucid") == "Halo Secure"
    assert identity.guard_name("Rivian") == "Guard mode" and identity.guard_name(None) == "Guard mode"
    assert identity.guard_label(["Tesla", "Lucid", "Tesla"]) == "Sentry / Halo Secure"
    assert identity.guard_label([]) == "Guard mode"


def test_model_code_from_a_source_reads_as_the_model_name():
    # Some sources report Tesla's internal code ('model3', 'lychee') in the model field.
    assert identity.model_of("5YJ3E1EA0JF000001", "Tesla", "model3", None) == ("Model 3", None)
    assert identity.model_of("5YJSA1E40LF000001", "Tesla", "lychee", None) == ("Model S", None)
    assert identity.describe("5YJ3E1EA0JF000001", "Tesla", "model3", None)["title"].endswith("Tesla Model 3")
    assert identity.model_of(None, "Lucid", "Air", None) == ("Air", None)


def test_the_map_cells_are_kept_warm_in_the_background_never_built_on_a_request(monkeypatch):
    """Cold, the Map's cells took 0.8-1 s after every deploy and every hour; the warmer builds
    them at start and every 45 minutes, inside the hour a request keeps them."""
    app = _app(monkeypatch)
    monkeypatch.setattr(app, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}])
    calls = []
    monkeypatch.setattr(app, "driveprint", lambda vin, fresh=False: calls.append(("cells", vin, fresh)) or [])
    monkeypatch.setattr(app, "driveprint_body", lambda key, fresh=False: calls.append(("body", key, fresh)) or b"[]")

    class Stop(Exception):
        pass

    def sleep(s):
        calls.append(("sleep", s))
        raise Stop

    monkeypatch.setattr(app.time, "sleep", sleep)
    with pytest.raises(Stop):
        app.warm_driveprints()
    assert calls == [("cells", "V1", True), ("cells", "V2", True), ("body", app.GARAGE, True), ("body", "V1", True), ("body", "V2", True), ("sleep", 2700)]
    src = (HERE / "app.py").read_text()
    route = src[src.index('@site.get("/api/driveprint")') :][:400]
    assert "fresh=True" not in route, "a request reads the cache"


def test_a_car_joining_the_garage_never_recolours_one_already_drawn():
    """The walkthrough's case: a third car sorted before the second and its auto colour moved from coral to teal."""
    deep = identity.PAINTS["deepblue"][1]
    first = identity.chart_colors(["Bravo", "Comet"], {}, {"Bravo": deep, "Comet": None})
    kept = dict(first)
    after = identity.chart_colors(["Bravo", "Charlie", "Comet"], {}, {"Bravo": deep, "Charlie": deep}, kept)
    assert after["Bravo"] == first["Bravo"] and after["Comet"] == first["Comet"], "kept colours stay"
    assert after["Charlie"] not in (after["Bravo"], after["Comet"])


def test_a_hand_picked_colour_still_beats_a_kept_one():
    got = identity.chart_colors(["A", "B"], {"B": 3}, None, {"A": 3})
    assert got["B"] == 3 and got["A"] != 3


def test_editing_a_cars_look_forgets_its_kept_colour():
    src = (HERE / "identity.py").read_text()
    assert "chart_auto = NULL" in src and "ADD COLUMN IF NOT EXISTS chart_auto" in src


def test_the_page_keeps_each_cars_first_colour_once(monkeypatch):
    app = _app(monkeypatch)
    rows = {"V1": {"chart": None, "chart_auto": None}, "V2": {"chart": 5, "chart_auto": None}}
    monkeypatch.setattr(app, "identity_rows", lambda: (rows, {}))
    monkeypatch.setattr(app, "car_identities", lambda vehicles: {v["vin"]: {"paint": None} for v in vehicles})
    wrote = []
    monkeypatch.setattr(app, "_keep_colors", lambda fresh: wrote.append(dict(fresh)))
    cars = [{"vin": "V1"}, {"vin": "V2"}]
    got = app.car_colors(cars)
    assert wrote == [{"V1": got["V1"]}], "only the car with no choice and nothing kept is written down"
    rows["V1"]["chart_auto"] = got["V1"]
    wrote.clear()
    assert app.car_colors([{"vin": "V0"}, *cars])["V1"] == got["V1"] and wrote == [{"V0": wrote[0]["V0"]}], "a car joining first in the list leaves V1 alone"
