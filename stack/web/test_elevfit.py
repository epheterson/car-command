"""Fitting the Lucid's raw GPS height to metres: the line, the matching, and that nothing else reads it."""

from pathlib import Path

import elevfit


def test_a_line_through_exact_points_has_no_residual():
    fit = elevfit.linear_fit([(x, 0.0137 * x + 0.5) for x in (8000.0, 8932.0, 9500.0, 12000.0)])
    assert abs(fit["slope"] - 0.0137) < 1e-9 and abs(fit["intercept"] - 0.5) < 1e-6 and fit["rmse_m"] < 1e-6 and fit["n"] == 4 and abs(fit["r2"] - 1) < 1e-9


def test_noise_shows_in_the_residual():
    fit = elevfit.linear_fit([(1000.0, 10.0), (2000.0, 22.0), (3000.0, 28.0), (4000.0, 42.0)])
    assert 0.009 < fit["slope"] < 0.011 and 1.0 < fit["rmse_m"] < 3.0 and fit["r2"] < 1


def test_too_little_to_fit_is_none():
    assert elevfit.linear_fit([]) is None and elevfit.linear_fit([(1.0, 1.0), (2.0, 2.0)]) is None
    assert elevfit.linear_fit([(5.0, 1.0), (5.0, 2.0), (5.0, 3.0)]) is None, "one raw value says nothing about the slope"


def test_a_raw_point_takes_the_nearest_reference_within_reach():
    # Springfield: a raw fix, a Tesla fix 10 m north and one 20 m south, and one 300 m away
    raw = [(39.7817, -89.6501, 8932.0), (39.9, -89.9, 9000.0)]
    ref = [(39.78179, -89.6501, 123.0), (39.78152, -89.6501, 130.0), (39.7844, -89.6501, 200.0)]
    assert elevfit.match(raw, ref) == [(8932.0, 123.0)], "the far raw fix matches nothing"
    assert elevfit.match(raw, ref, within_m=5) == []
    across = [(39.78174, -89.65001, 8932.0)]  # either side of a grid line
    assert elevfit.match(across, [(39.78176, -89.64999, 123.0)]) == [(8932.0, 123.0)]


def test_both_fits_and_their_words():
    raw = [(39.78 + i * 0.01, -89.65, 8000.0 + 100 * i) for i in range(5)]
    ref = [(lat, lon, v / 70) for lat, lon, v in raw]
    fits = elevfit.fit_both(raw, ref, height=lambda lat, lon: None)
    assert abs(fits["cars"]["slope"] - 1 / 70) < 1e-9 and fits["terrain"] is None
    assert elevfit.fit_both(raw, ref)["terrain"] is None, "no model on this box"
    assert elevfit.describe("Against the Teslas", fits["cars"]).startswith("Against the Teslas: metres = 0.0142857 × raw + ")
    assert elevfit.describe("x", {"slope": 0.01, "intercept": -4.6, "n": 3, "rmse_m": 1.0, "r2": None}) == "x: metres = 0.01 × raw − 4.6  (rmse 1.0 m over 3 points)"
    assert elevfit.describe("Against the terrain model", None) == "Against the terrain model: not enough matched points yet"


def test_the_raw_height_is_kept_beside_each_position_and_never_read_as_metres():
    here = Path(__file__).parent
    import recorder

    assert "ADD COLUMN IF NOT EXISTS elevation_raw DOUBLE PRECISION" in recorder.DDL
    readers = [p.name for p in here.glob("*.py") if not p.name.startswith("test_") and "elevation_raw" in p.read_text()]
    assert sorted(readers) == ["app.py", "elevfit.py", "recorder.py"], "the pages never read it"
    assert "elevation_raw" not in "".join(p.read_text() for p in (here / "templates").glob("*.html"))
