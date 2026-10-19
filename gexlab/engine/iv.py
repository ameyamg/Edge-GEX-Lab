"""
Implied volatility back-solver.
Newton-Raphson with Brenner-Subrahmanyam initial guess, Brent fallback.

Adapted from the author's earlier options project (its wheel_system engine).
Unchanged logic; imports point at gexlab.engine.bsm (which has no time floor).
Edge GEX Lab calls it with r = q = 0 on European BSM, a documented approximation for
American-style equity options (model v1.0).
"""

import math

from scipy.optimize import brentq

from .bsm import option_price, vega, intrinsic_value, OptionType

_IV_MIN = 0.001
_IV_MAX = 5.0
_VEGA_FLOOR = 1e-10


def _initial_guess(market_price, S, K, T) -> float:
    """Brenner-Subrahmanyam ATM approximation."""
    if T <= 0:
        return 0.30
    return max(_IV_MIN, min(math.sqrt(2 * math.pi / T) * (market_price / S), _IV_MAX))


def implied_volatility(
    market_price: float, S: float, K: float, T: float, r: float,
    option_type: OptionType, q: float = 0.0,
    precision: float = 1e-6, max_iter: int = 100,
) -> float:
    """
    Back-solve IV from a market price.
    Returns NaN if price <= 0, below intrinsic, or expired.
    """
    intrinsic = intrinsic_value(S, K, option_type)
    if market_price <= 0 or market_price < intrinsic - precision or T <= 0:
        return float("nan")

    market_price = max(market_price, intrinsic + 1e-8)

    # Newton-Raphson
    sigma = _initial_guess(market_price, S, K, T)
    for _ in range(max_iter):
        price = option_price(S, K, T, r, sigma, option_type, q)
        v = vega(S, K, T, r, sigma, q) * 100.0   # convert back from per-1% to raw
        diff = price - market_price
        if abs(diff) < precision:
            return sigma
        if abs(v) < _VEGA_FLOOR:
            break
        sigma -= diff / v
        sigma = max(_IV_MIN, min(sigma, _IV_MAX))

    # Brent fallback
    try:
        def objective(s):
            return option_price(S, K, T, r, s, option_type, q) - market_price
        lo, hi = objective(_IV_MIN), objective(_IV_MAX)
        if lo > 0 or hi < 0:
            return float("nan")
        return brentq(objective, _IV_MIN, _IV_MAX, xtol=precision, maxiter=max_iter)
    except Exception:
        return float("nan")
