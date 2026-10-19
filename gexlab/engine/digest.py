"""Canonical calculation-input digest stored on every capture (captures.input_digest).

response_hash proves which vendor bodies arrived. input_digest proves which numbers the
levels were computed from: the valuation time, the spot, every contract input (OI and its
date, basis and source vendor, IV with its provenance and quote time, quote and its time), the calendar provenance with the
settlement instant of every expiry, and the model settings. Two captures with the same
digest were computed from identical inputs under identical settings; change any one of
them and the digest changes.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime

from gexlab import config
from gexlab.engine.calendar import TradingCalendar, settlement_dt
from gexlab.models import Chain


def _ts(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="microseconds") if dt is not None else None


def model_settings(ticker: str, spot: float) -> dict:
    from gexlab.engine.gex import sweep_step
    return {
        "model_version": config.MODEL_VERSION, "r": 0.0, "q": 0.0,
        "gex_units": "sign*gamma*oi*multiplier*S^2*0.01", "sign": "call +1, put -1",
        "sweep_band": config.SWEEP_BAND, "sweep_step": sweep_step(ticker, spot),
        "wall_strike_band": config.WALL_STRIKE_BAND, "coverage_strike_band": config.COVERAGE_STRIKE_BAND,
        "fetch_strike_band": config.FETCH_STRIKE_BAND, "iv_min": config.IV_MIN, "iv_max": config.IV_MAX,
        "wide_spread_frac": config.WIDE_SPREAD_FRAC, "neutral_frac": config.NEUTRAL_FRAC,
        "quote_spot_max_skew_s": config.QUOTE_SPOT_MAX_SKEW_S, "window_days": config.WINDOW_DAYS,
        "indicative_iv_max_age_s": config.INDICATIVE_IV_MAX_AGE_S,
        "oi_null_ambiguous_max_frac": config.OI_NULL_AMBIGUOUS_MAX_FRAC,
        "gate_version": config.GATE_VERSION, "gate_oi_band": config.GATE_OI_BAND,
        "gate_oi_min_pct": config.GATE_OI_MIN_PCT, "gate_gex_min_pct": config.GATE_GEX_MIN_PCT,
        "oi_source_policy": config.OI_SOURCE, "cboe_oi_min_match": config.CBOE_OI_MIN_MATCH,
        "cboe_oi_same_max_frac": config.CBOE_OI_SAME_MAX_FRAC,
    }


def canonical_inputs(chain: Chain, cal: TradingCalendar | None) -> dict:
    expiries = sorted({(c.expiry, c.settlement) for c in chain.contracts})
    return {
        "ticker": chain.ticker,
        "valuation_time": _ts(chain.valuation),
        "spot": {"price": chain.spot.price, "ts": _ts(chain.spot.ts), "source": chain.spot.source,
                 "basis": chain.spot.basis},
        "feed": chain.feed, "source_ts": _ts(chain.source_ts), "oi_date": chain.oi_date,
        "oi_source": chain.oi_source, "oi_basis": (chain.oi_info or {}).get("basis"),
        "response_hash": chain.raw_hash,
        "calendar": {**(cal.provenance() if cal is not None else {"source": None}),
                     "settlements": [[d.isoformat(), s, _ts(settlement_dt(d, s, cal))] for d, s in expiries]},
        "model": model_settings(chain.ticker, chain.spot.price),
        "contracts": [[c.symbol, c.expiry.isoformat() if isinstance(c.expiry, date) else c.expiry, c.settlement,
                       c.strike, c.type, c.multiplier, c.oi, c.oi_date, c.iv, c.iv_source, bool(c.iv_shared),
                       c.iv_own, c.iv_donor, c.bid, c.ask, _ts(c.quote_ts), _ts(c.iv_ts), c.oi_basis,
                       c.oi_source]
                      for c in sorted(chain.contracts, key=lambda c: c.symbol)],
    }


def input_digest(chain: Chain, cal: TradingCalendar | None) -> str:
    blob = json.dumps(canonical_inputs(chain, cal), sort_keys=True, separators=(",", ":"), allow_nan=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
