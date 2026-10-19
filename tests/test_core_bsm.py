"""Ported from Options/wheel_system/tests/test_engine.py (pricing + IV parts), adapted to gexlab.engine.
Plus checks that the one-hour time floor is gone."""
import math

from gexlab.engine.bsm import all_greeks, call_price, delta, gamma, put_price, rho, theta, vega
from gexlab.engine.iv import implied_volatility

S, K, T, r, sig = 100.0, 100.0, 1.0, 0.05, 0.20


def test_atm_call_known_value():
    assert abs(call_price(S, K, T, r, sig) - 10.4506) < 0.01


def test_put_call_parity():
    c, p = call_price(S, K, T, r, sig), put_price(S, K, T, r, sig)
    assert abs((c - p) - (S - K * math.exp(-r * T))) < 1e-8


def test_iv_round_trip():
    for sigma in (0.10, 0.20, 0.35, 0.60):
        price = call_price(S, K, T, r, sigma)
        assert abs(implied_volatility(price, S, K, T, r, "call") - sigma) < 1e-4


def test_iv_round_trip_r0_short_dated_put():
    # Edge GEX Lab solves with r = q = 0 on 0 to 7 DTE contracts.
    T2 = 2.5 / 365
    for sigma in (0.08, 0.15, 0.40):
        for k in (95.0, 100.0, 103.0):
            price = put_price(S, k, T2, 0.0, sigma)
            if price - max(k - S, 0.0) < 1e-3:
                continue        # no time value left: IV is not identifiable
            assert abs(implied_volatility(price, S, k, T2, 0.0, "put") - sigma) < 1e-4


def test_iv_below_intrinsic_nan():
    assert math.isnan(implied_volatility(0.01, 110, 100, T, r, "call"))


def test_iv_expired_nan():
    assert math.isnan(implied_volatility(1.0, 100, 100, 0.0, 0.0, "call"))


def test_delta_bounds():
    assert 0 <= delta(S, K, T, r, sig, "call") <= 1
    assert -1 <= delta(S, K, T, r, sig, "put") <= 0


def test_greeks_signs():
    assert gamma(S, K, T, r, sig) > 0
    assert theta(S, K, T, r, sig, "put") < 0
    assert vega(S, K, T, r, sig) > 0
    assert rho(S, K, T, r, sig, "call") > 0
    assert rho(S, K, T, r, sig, "put") < 0


def test_all_greeks_keys():
    assert set(all_greeks(S, K, T, r, sig, "put")) == {"price", "delta", "gamma", "theta", "vega", "rho"}


def test_no_time_floor():
    # 30 minutes to settlement uses the true T, not the old one-hour floor.
    t30 = 0.5 / (365 * 24)
    t60 = 1.0 / (365 * 24)
    g30, g60 = gamma(S, K, t30, 0.0, sig), gamma(S, K, t60, 0.0, sig)
    # Near-ATM gamma scales with 1/sqrt(T): halving T multiplies it by about sqrt(2).
    assert abs(g30 / g60 - math.sqrt(2)) < 1e-3


def test_expired_gamma_zero():
    assert gamma(S, K, 0.0, 0.0, sig) == 0.0
    assert gamma(S, K, -1e-6, 0.0, sig) == 0.0
