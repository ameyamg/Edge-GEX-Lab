"""
Black-Scholes European option pricing and Greeks.

Adapted from the author's earlier options project (its wheel_system engine).
Changes for Edge GEX Lab (model v1.0):
  1. REMOVED the one-hour time floor (_T_FLOOR / _eff_T). Edge GEX Lab uses the true
     time to settlement; T <= 0 means expired and is handled explicitly.
  2. Normal CDF/PDF use math.erfc / math.exp instead of scipy.stats.norm. Same
     values to double precision, about 50x faster per scalar call, which matters
     when thousands of IVs are solved every refresh.

Inputs:
  S     : underlying price
  K     : strike
  T     : time to expiry in YEARS
  r     : risk-free rate (decimal, e.g. 0.05)
  sigma : implied volatility (decimal, e.g. 0.25)
  q     : dividend yield (decimal, default 0)

Greek units:
  theta : per CALENDAR DAY (raw / 365)
  vega  : per 1% IV change (raw / 100)
  rho   : per 1% rate change (raw / 100)
"""

import math
from typing import Literal

OptionType = Literal["call", "put"]

_SQRT2 = math.sqrt(2.0)
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


def _N(x: float) -> float:
    return 0.5 * math.erfc(-x / _SQRT2)


def _n(x: float) -> float:
    return _INV_SQRT_2PI * math.exp(-0.5 * x * x)


def _validate(sigma: float) -> None:
    if sigma <= 0:
        raise ValueError(f"sigma must be > 0, got {sigma}")


def _d1_d2(S, K, T, r, sigma, q=0.0):
    # Callers guarantee T > 0 (no floor; see module docstring).
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


# -- Pricing -------------------------------------------------------------------

def call_price(S, K, T, r, sigma, q=0.0) -> float:
    if T <= 0:
        return max(S - K, 0.0)
    _validate(sigma)
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    return S * math.exp(-q * T) * _N(d1) - K * math.exp(-r * T) * _N(d2)


def put_price(S, K, T, r, sigma, q=0.0) -> float:
    if T <= 0:
        return max(K - S, 0.0)
    _validate(sigma)
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    return K * math.exp(-r * T) * _N(-d2) - S * math.exp(-q * T) * _N(-d1)


def option_price(S, K, T, r, sigma, option_type: OptionType, q=0.0) -> float:
    return call_price(S, K, T, r, sigma, q) if option_type == "call" \
        else put_price(S, K, T, r, sigma, q)


# -- First-order Greeks ----------------------------------------------------------

def delta(S, K, T, r, sigma, option_type: OptionType, q=0.0) -> float:
    if T <= 0:
        if option_type == "call":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    _validate(sigma)
    d1, _ = _d1_d2(S, K, T, r, sigma, q)
    if option_type == "call":
        return math.exp(-q * T) * _N(d1)
    return math.exp(-q * T) * (_N(d1) - 1.0)


def gamma(S, K, T, r, sigma, q=0.0) -> float:
    """Identical for calls and puts. Zero once expired."""
    if T <= 0:
        return 0.0
    _validate(sigma)
    d1, _ = _d1_d2(S, K, T, r, sigma, q)
    return math.exp(-q * T) * _n(d1) / (S * sigma * math.sqrt(T))


def theta(S, K, T, r, sigma, option_type: OptionType, q=0.0) -> float:
    """Per calendar day."""
    if T <= 0:
        return 0.0
    _validate(sigma)
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    term1 = -(S * math.exp(-q * T) * _n(d1) * sigma) / (2 * math.sqrt(T))
    if option_type == "call":
        raw = term1 - r * K * math.exp(-r * T) * _N(d2) + q * S * math.exp(-q * T) * _N(d1)
    else:
        raw = term1 + r * K * math.exp(-r * T) * _N(-d2) - q * S * math.exp(-q * T) * _N(-d1)
    return raw / 365.0


def vega(S, K, T, r, sigma, q=0.0) -> float:
    """Per 1% IV change. Identical for calls and puts."""
    if T <= 0:
        return 0.0
    _validate(sigma)
    d1, _ = _d1_d2(S, K, T, r, sigma, q)
    return S * math.exp(-q * T) * _n(d1) * math.sqrt(T) / 100.0


def rho(S, K, T, r, sigma, option_type: OptionType, q=0.0) -> float:
    """Per 1% rate change."""
    if T <= 0:
        return 0.0
    _validate(sigma)
    _, d2 = _d1_d2(S, K, T, r, sigma, q)
    if option_type == "call":
        return K * T * math.exp(-r * T) * _N(d2) / 100.0
    return -K * T * math.exp(-r * T) * _N(-d2) / 100.0


def all_greeks(S, K, T, r, sigma, option_type: OptionType, q=0.0) -> dict:
    """Price + all Greeks in one dict."""
    return {
        "price": option_price(S, K, T, r, sigma, option_type, q),
        "delta": delta(S, K, T, r, sigma, option_type, q),
        "gamma": gamma(S, K, T, r, sigma, q),
        "theta": theta(S, K, T, r, sigma, option_type, q),
        "vega":  vega(S, K, T, r, sigma, q),
        "rho":   rho(S, K, T, r, sigma, option_type, q),
    }


def intrinsic_value(S, K, option_type: OptionType) -> float:
    return max(S - K, 0.0) if option_type == "call" else max(K - S, 0.0)
