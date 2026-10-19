"""Golden tests for gamma/GEX units, walls, zero-gamma sweep."""
from datetime import date, datetime, timezone

import numpy as np

from gexlab.engine import gex as G
from gexlab.engine.bsm import gamma
from gexlab.models import Contract

AS_OF = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)


def test_gamma_golden_hand_computed():
    # S=K=100, T=1, sigma=0.2, r=q=0: d1 = 0.5*0.04/0.2 = 0.1
    # N'(0.1) = exp(-0.005)/sqrt(2*pi) = 0.39695254747701
    # Gamma = 0.39695254747701 / (100 * 0.2 * 1) = 0.0198476273738505
    assert abs(gamma(100, 100, 1.0, 0.0, 0.2) - 0.0198476273738505) < 1e-12


def test_gex_units_golden():
    # GEX = sign * Gamma * OI * 100 * S^2 * 0.01
    #     = 0.0198476273738505 * 1000 * 100 * 10000 * 0.01
    #     = 0.0198476273738505 * 1e7 = 198476.273738505 dollars per 1% move
    g = 0.0198476273738505
    assert abs(G.contract_gex(g, 1000, 100, 100.0, +1) - 198476.273738505) < 1e-6
    assert abs(G.contract_gex(g, 1000, 100, 100.0, -1) + 198476.273738505) < 1e-6


def _c(strike, typ, oi, iv=0.2, expiry=date(2026, 10, 19)):
    return Contract(symbol=f"X{strike}{typ}", root="X", expiry=expiry, settlement="PM",
                    strike=float(strike), type=typ, oi=oi, iv=iv)


def test_finalize_marks_invalid_and_drops_expired():
    cs = [_c(100, "call", 10), _c(100, "put", None), _c(101, "call", 5, iv=None),
          _c(99, "put", 5, expiry=date(2026, 10, 15))]
    out = G.finalize(cs, 100.0, AS_OF, None)
    assert len(out) == 3                       # expired one dropped
    assert out[0].valid and out[0].gex > 0
    assert not out[1].valid and out[1].invalid_reason == "no_oi"
    assert not out[2].valid and out[2].invalid_reason == "no_iv"


def test_put_gex_negative_and_matches_formula():
    out = G.finalize([_c(100, "put", 1000)], 100.0, AS_OF, None)
    c = out[0]
    expect = -gamma(100.0, 100.0, c.T, 0.0, 0.2) * 1000 * 100 * 100.0 ** 2 * 0.01
    assert c.gex < 0 and abs(c.gex - expect) < 1e-6


def test_vectorized_sweep_matches_scalar_sum():
    cs = G.finalize([_c(95, "put", 300), _c(100, "call", 200), _c(105, "call", 150, iv=0.3)], 100.0, AS_OF, None)
    arr = G.GexArrays.from_contracts(cs)
    for s in (96.0, 100.0, 104.5):
        expect = sum(G.sign_of(c.type) * gamma(s, c.strike, c.T, 0.0, c.iv) * c.oi * 100 * s * s * 0.01 for c in cs)
        got = G.total_gex_at(np.array([s]), arr)[0]
        assert abs(got - expect) < 1e-6 * max(1.0, abs(expect))


def test_wall_tie_breaks_nearest_then_lower():
    rows = [{"strike": 95.0, "call_gex": 10.0, "put_gex": -50.0},
            {"strike": 105.0, "call_gex": 10.0, "put_gex": -50.0},
            {"strike": 102.0, "call_gex": 10.0, "put_gex": -20.0},
            {"strike": 98.0, "call_gex": 5.0, "put_gex": -50.0}]
    cw, pw = G.walls(rows, spot=100.0)
    assert cw == 102.0          # three tie at 10: 102 is nearest to 100
    assert pw == 98.0           # three tie at 50: 98 is nearest


def test_wall_tie_equal_distance_takes_lower():
    rows = [{"strike": 95.0, "call_gex": 7.0, "put_gex": -3.0},
            {"strike": 105.0, "call_gex": 7.0, "put_gex": -3.0}]
    assert G.walls(rows, 100.0) == (95.0, 95.0)


def test_wall_none_when_empty():
    assert G.walls([{"strike": 100.0, "call_gex": 0.0, "put_gex": 0.0}], 100.0) == (None, None)


def test_zero_crossings_two():
    grid = np.arange(90.0, 110.01, 1.0)
    vals = (grid - 95.5) * (grid - 104.25)       # + then - then +
    zs = G.zero_crossings(grid, vals)
    assert len(zs) == 2
    # linear interpolation between grid points 95 and 96
    assert abs(zs[0] - (95 + vals[5] / (vals[5] - vals[6]))) < 1e-12
    assert 95 < zs[0] < 96 and 104 < zs[1] < 105
    assert G.nearest(zs, 100.0) == min(zs, key=lambda z: abs(z - 100))


def test_zero_crossings_none_is_valid():
    grid = np.linspace(90, 110, 50)
    assert G.zero_crossings(grid, np.ones_like(grid)) == []
    assert G.nearest([], 100.0) is None


def test_zero_crossing_exact_zero_on_grid_counted_once():
    grid = np.array([1.0, 2.0, 3.0, 4.0])
    vals = np.array([-1.0, 0.0, 1.0, 2.0])
    assert G.zero_crossings(grid, vals) == [2.0]


def test_zero_crossing_exact_zero_kept_with_asymmetric_neighbours():
    # The old code skipped the known zero and interpolated -1 .. 3 to 1.5; the crossing is exactly 2.
    grid = np.array([1.0, 2.0, 3.0])
    assert G.zero_crossings(grid, np.array([-1.0, 0.0, 3.0])) == [2.0]
    assert G.zero_crossings(grid, np.array([-3.0, 0.0, 1.0])) == [2.0]
    assert G.zero_crossings(grid, np.array([5.0, 0.0, -0.5])) == [2.0]


def test_zero_crossing_plateau_and_touch():
    grid = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    assert G.zero_crossings(grid, np.array([-1.0, 0.0, 0.0, 2.0, 3.0])) == [2.5]   # plateau midpoint
    assert G.zero_crossings(grid, np.array([1.0, 0.0, 1.0, 2.0, 3.0])) == []       # touch, no sign change
    assert G.zero_crossings(grid, np.array([1.0, 2.0, 3.0, 0.0, 0.0])) == []       # zeros to the edge
    assert G.zero_crossings(grid, np.array([0.0, -1.0, 0.0, 1.0, -1.0])) == [3.0, 4.5]


def test_compute_levels_two_flips_from_chain():
    # Puts at 95 and 105, calls at 100: net goes - / + / - across the band, so two flips.
    cs = G.finalize([_c(95, "put", 500, iv=0.05), _c(100, "call", 900, iv=0.05),
                     _c(105, "put", 500, iv=0.05)], 100.0, AS_OF, None)
    lv, grid, vals = G.compute_levels("SPY", cs, 100.0)
    assert len(lv.zero_gamma) == 2
    assert lv.zero_gamma[0] < 100 < lv.zero_gamma[1]
    assert lv.call_wall == 100.0
    assert lv.put_wall in (95.0, 105.0)
    assert lv.regime == "positive"
    assert abs(grid[1] - grid[0] - 0.01) < 1e-9


def test_compute_levels_no_flip():
    cs = G.finalize([_c(100, "call", 900)], 100.0, AS_OF, None)
    lv, _, _ = G.compute_levels("SPY", cs, 100.0)
    assert lv.zero_gamma == [] and lv.nearest_zero_gamma is None and lv.put_wall is None


def test_sweep_steps():
    assert G.sweep_step("SPY", 700) == 0.01
    assert G.sweep_step("SPX", 7000) == 0.5
    assert G.sweep_step("NDX", 25000) == 1.0
    assert G.sweep_step("NVDA", 187.4) == 1.87
    g = G.sweep_grid("SPX", 7000.0)
    assert g[0] >= 6300 and g[-1] <= 7700 and abs(g[1] - g[0] - 0.5) < 1e-9


def test_regime_neutral_band():
    assert G.regime(1.0, 100.0) == "neutral"
    assert G.regime(10.0, 100.0) == "positive"
    assert G.regime(-10.0, 100.0) == "negative"


def test_coverage_counts_missing_oi_in_band_and_excludes_confirmed_zero():
    cs = G.finalize([_c(100, "call", 10), _c(100, "put", 0), _c(101, "call", None),
                     _c(102, "put", 5, iv=None), _c(130, "call", 5, iv=None)], 100.0, AS_OF, None)
    cov = G.coverage(cs, 100.0)
    # eligible: 100C (OI 10), 101C (NO OI record: unknown exposure, counted and invalid), 102P (OI 5).
    # 100P has a confirmed OI of 0 (no exposure, excluded); 130C sits outside +-10%.
    assert (cov.eligible, cov.valid, cov.pct) == (3, 1, 33.3)
    assert abs(cov.ratio - 1 / 3) < 1e-12
    assert abs(cov.oi_missing_frac - 0.2) < 1e-12
