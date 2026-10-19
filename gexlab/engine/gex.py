"""GEX math, model v1.0 (see SPEC_GEX.md "Math").

GEX per contract = sign * Gamma * OI * multiplier * S^2 * 0.01
  units: dollars of dealer delta change per 1% move in the underlying.
  sign: +1 call, -1 put. This is an ASSUMPTION about dealer positioning
  (dealers long calls, short puts from the customer's side), shown as such.
Gamma = N'(d1) / (S * IV * sqrt(T)), r = q = 0 (stated approximation).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

from gexlab import config
from gexlab.engine import bsm
from gexlab.engine.calendar import TradingCalendar, time_to_expiry
from gexlab.models import Contract

_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


def contract_gex(gamma: float, oi: float, multiplier: float, spot: float, sign: int) -> float:
    return sign * gamma * oi * multiplier * spot * spot * 0.01


def sign_of(option_type: str) -> int:
    return 1 if option_type == "call" else -1


def iv_ok(iv: float | None) -> bool:
    return iv is not None and math.isfinite(iv) and config.IV_MIN <= iv <= config.IV_MAX


def finalize(contracts: list[Contract], spot: float, as_of: datetime,
             cal: TradingCalendar | None) -> list[Contract]:
    """Compute T, gamma and GEX at spot. Drops expired contracts. Marks validity.

    A contract is valid when it has OI (0 counts) and a usable IV. Invalid
    contracts stay in the list (they count against coverage) with gex = None.
    """
    out = []
    for c in contracts:
        T = time_to_expiry(as_of, c.expiry, c.settlement, cal)
        if T <= 0:
            continue                       # expired: not eligible
        c.T = T
        reason = None
        if c.oi is None:
            reason = "no_oi"
        elif not iv_ok(c.iv):
            reason = c.invalid_reason or "no_iv"
        if reason:
            c.valid, c.invalid_reason, c.gamma, c.gex = False, reason, None, None
        else:
            c.gamma = bsm.gamma(spot, c.strike, T, 0.0, c.iv)
            c.gex = contract_gex(c.gamma, c.oi, c.multiplier, spot, sign_of(c.type))
            c.valid, c.invalid_reason = True, None
        out.append(c)
    return out


def is_eligible(c: Contract, spot: float) -> bool:
    """Counts toward coverage: inside the +-10% band and not CONFIRMED zero open interest.

    A confirmed OI of 0 carries no exposure, so it is left out. A contract with NO OI
    record is unknown exposure, not zero exposure: it stays in the denominator and,
    being invalid (no_oi), pulls coverage down. Strikes beyond the band still feed the
    sweep when valid but do not gate it.
    """
    return c.oi != 0 and abs(c.strike / spot - 1.0) <= config.COVERAGE_STRIKE_BAND + 1e-12


@dataclass
class Coverage:
    eligible: int
    valid: int
    pct: float                 # rounded to 0.1 for display only; never gate on it
    oi_missing_frac: float     # share of all contracts with no OI record
    ratio: float = 0.0         # unrounded valid / eligible, the number the quality gate reads


def coverage(contracts: list[Contract], spot: float) -> Coverage:
    el = [c for c in contracts if is_eligible(c, spot)]
    v = sum(1 for c in el if c.valid)
    miss = sum(1 for c in contracts if c.oi is None)
    ratio = v / len(el) if el else 0.0
    return Coverage(len(el), v, round(100.0 * ratio, 1),
                    miss / len(contracts) if contracts else 0.0, ratio)


def coverage_ok(cov: Coverage) -> bool:
    """Gate 1.0 test on the UNROUNDED ratio (94.96% must fail even though it displays as 95.0).
    Since gate 1.1 this is a reported diagnostic only; gex_gate_ok() gates captures (1.2)."""
    return cov.eligible > 0 and cov.ratio * 100.0 >= config.QUALITY_MIN_PCT


@dataclass
class OiCoverage:
    """Gate 1.1 (a stored diagnostic since 1.2): open interest inside +-GATE_OI_BAND held by valid contracts."""
    oi_total: float            # band OI, unknown OI imputed (GATE_OI_UNKNOWN_WEIGHT)
    oi_valid: float
    ratio: float               # unrounded oi_valid / oi_total, the number the gate reads
    pct: float                 # rounded to 0.01 for display only
    contracts: int             # contracts inside the band
    invalid: int               # invalid contracts inside the band
    unknown_oi: int            # invalid for lack of an OI record (weight imputed)
    band: float


def in_band(c: Contract, spot: float, band: float) -> bool:
    return abs(c.strike / spot - 1.0) <= band + 1e-12


def oi_coverage(contracts: list[Contract], spot: float, band: float | None = None) -> OiCoverage:
    band = config.GATE_OI_BAND if band is None else band
    b = [c for c in contracts if in_band(c, spot, band)]
    known = sorted(c.oi for c in b if c.oi is not None and c.oi > 0)
    impute = float(known[len(known) // 2]) if known else 0.0
    unknown = [c for c in b if c.oi is None]
    total = sum(float(c.oi) for c in b if c.oi is not None) + impute * len(unknown)
    valid = sum(float(c.oi) for c in b if c.valid and c.oi is not None)
    ratio = valid / total if total > 0 else 0.0
    return OiCoverage(total, valid, ratio, round(100.0 * ratio, 2), len(b),
                      sum(1 for c in b if not c.valid), len(unknown), band)


def oi_gate_ok(oc: OiCoverage) -> bool:
    """Gate 1.1 test on the UNROUNDED ratio. A reported diagnostic since 1.2."""
    return oc.oi_total > 0 and oc.ratio * 100.0 >= config.GATE_OI_MIN_PCT


@dataclass
class GexCoverage:
    """Gate 1.2/1.3: estimated |GEX| inside +-GATE_OI_BAND held by valid contracts."""
    ratio: float | None        # unrounded valid / total, the number the gate reads; None = cannot be computed or bounded
    pct: float | None          # rounded to 0.01 for display and storage only
    left_out: int              # invalid contracts in the band whose gamma could not be estimated from a donor IV
    total: float               # estimated |GEX| in the band (valid + estimated invalid + charged left-out)
    band: float
    charged: float = 0.0       # |GEX| charged for the left-out contracts at the conservative bound (gate 1.3)


def gex_cov(contracts: list[Contract], spot: float, band: float | None = None) -> GexCoverage:
    """Share of estimated |GEX| inside the band held by valid contracts (gate 1.2, bounded in 1.3).

    An invalid contract's gamma is estimated with the IV of the nearest valid strike of the same
    expiry and type, else of the same expiry (any type). A contract with NO OI record is unknown
    exposure, not zero: its OI is weighted as the median OI of the band's known contracts, as in
    oi_coverage() (GATE_OI_UNKNOWN_WEIGHT), so a feed that drops OI records pulls the share down.
    A confirmed OI of 0 carries no exposure.

    Gate 1.3: a band contract carrying OI (or no OI record) with no donor IV (a whole expiry with no
    valid quote, say) is no longer dropped from the denominator: the share is None (unbounded) and the
    capture fails. No safe charge exists: a missing 0DTE contract near the close can carry far more
    gamma than any valid contract (day-1 verification: a 99.01% "bound" against 91.03% real).
    `charged` stays 0 (kept for the stored column).
    """
    band = config.GATE_OI_BAND if band is None else band
    b = [c for c in contracts if in_band(c, spot, band)]
    known = sorted(c.oi for c in b if c.oi is not None and c.oi > 0)
    impute = float(known[len(known) // 2]) if known else 0.0
    by_type: dict[tuple, list[Contract]] = {}
    by_exp: dict[object, list[Contract]] = {}
    for d in contracts:
        if d.valid and iv_ok(d.iv) and d.T:
            by_type.setdefault((d.expiry, d.type), []).append(d)
            by_exp.setdefault(d.expiry, []).append(d)
    tot = val = charged = 0.0
    left_out = 0
    unbounded = False
    for c in b:
        if c.valid and c.gex is not None:
            g = abs(c.gex)
            tot += g
            val += g
            continue
        if c.oi == 0:
            continue                       # confirmed zero OI: no exposure to miss
        oi = c.oi if c.oi is not None else impute
        pool = by_type.get((c.expiry, c.type)) or by_exp.get(c.expiry)
        if oi and c.T and c.T > 0 and pool:
            iv = min(pool, key=lambda d: (abs(d.strike - c.strike), d.strike)).iv
            tot += abs(contract_gex(bsm.gamma(spot, c.strike, c.T, 0.0, iv), oi, c.multiplier, spot, 1))
            continue
        left_out += 1
        unbounded = True
    ratio = val / tot if tot > 0 and not unbounded else None
    return GexCoverage(ratio, round(100.0 * ratio, 2) if ratio is not None else None, left_out, tot, band, charged)


@dataclass
class OiFreshness:
    """Gate 1.3: open interest inside +-GATE_OI_BAND dated other than the expected (previous) session."""
    expected: str | None       # the previous completed session, ISO date; None = cannot be judged (no calendar)
    stale_frac: float          # share of band OI on contracts whose OI date is not `expected` (older, newer or none)
    dates: dict                # {oi_date or "none": band OI on it}
    sources: dict              # {oi_source or "unknown": contracts in the band}


def oi_freshness(contracts: list[Contract], spot: float, expected: str | None,
                 band: float | None = None) -> OiFreshness:
    """Weighted by open interest over the band contracts that carry exposure (OI > 0, or no OI record:
    imputed like gex_cov, because an undated unknown cannot be called fresh). Evaluated per contract,
    so a chain's newest date never hides an older contributor."""
    band = config.GATE_OI_BAND if band is None else band
    b = [c for c in contracts if in_band(c, spot, band) and c.oi != 0]
    known = sorted(c.oi for c in b if c.oi is not None and c.oi > 0)
    impute = float(known[len(known) // 2]) if known else 1.0
    dates: dict[str, float] = {}
    sources: dict[str, int] = {}
    tot = stale = 0.0
    for c in b:
        w = float(c.oi) if c.oi is not None else impute
        k = c.oi_date or "none"
        dates[k] = dates.get(k, 0.0) + w
        s = getattr(c, "oi_source", None) or "unknown"
        sources[s] = sources.get(s, 0) + 1
        tot += w
        if expected is None or c.oi_date != expected:
            stale += w
    frac = stale / tot if tot > 0 else 0.0
    return OiFreshness(expected, frac, dict(sorted(dates.items())), dict(sorted(sources.items())))


def gex_coverage(contracts: list[Contract], spot: float, band: float | None = None) -> tuple[float | None, int]:
    """(pct rounded to 0.01 or None, contracts left out): the gex_cov() numbers as stored."""
    gc = gex_cov(contracts, spot, band)
    return gc.pct, gc.left_out


def gex_gate_ok(gc: GexCoverage) -> bool:
    """Gate 1.2 on the UNROUNDED ratio. No computable share fails."""
    return gc.ratio is not None and gc.ratio * 100.0 >= config.GATE_GEX_MIN_PCT


@dataclass
class GexArrays:
    K: np.ndarray
    T: np.ndarray
    iv: np.ndarray
    w: np.ndarray        # sign * OI * multiplier

    @classmethod
    def from_contracts(cls, contracts: list[Contract]) -> "GexArrays":
        v = [c for c in contracts if c.valid]
        return cls(np.array([c.strike for c in v], dtype=float),
                   np.array([c.T for c in v], dtype=float),
                   np.array([c.iv for c in v], dtype=float),
                   np.array([sign_of(c.type) * c.oi * c.multiplier for c in v], dtype=float))


def total_gex_at(spots: np.ndarray, arr: GexArrays, chunk: int | None = None) -> np.ndarray:
    """Total signed GEX at each hypothetical spot, IV/OI/T frozen. Chunked for memory."""
    spots = np.asarray(spots, dtype=float)
    out = np.zeros(spots.shape[0])
    if arr.K.size == 0:
        return out
    chunk = chunk or config.SWEEP_CHUNK
    sig_sqrt_t = arr.iv * np.sqrt(arr.T)
    half_var_t = 0.5 * arr.iv * arr.iv * arr.T
    lnK = np.log(arr.K)
    for i in range(0, spots.shape[0], chunk):
        S = spots[i:i + chunk][:, None]
        d1 = (np.log(S) - lnK + half_var_t) / sig_sqrt_t
        g = _INV_SQRT_2PI * np.exp(-0.5 * d1 * d1) / (S * sig_sqrt_t)
        out[i:i + chunk] = (g * arr.w).sum(axis=1) * spots[i:i + chunk] ** 2 * 0.01
    return out


def zero_crossings(grid: np.ndarray, values: np.ndarray) -> list[float]:
    """Every sign change of values along grid. No crossing returns [] (valid).

    Between two nonzero neighbours of opposite sign the crossing is linearly
    interpolated. An exact zero on the grid is a known location and is kept as is:
    a single zero point whose nonzero neighbours differ in sign is reported at that
    grid point. A plateau (a run of exact zeros) between opposite signs is reported
    once, at the plateau midpoint. A zero run whose neighbours share a sign (a touch)
    or that sits at either end of the grid (no sign change observable) is not a crossing.
    """
    grid = np.asarray(grid, dtype=float)
    values = np.asarray(values, dtype=float)
    n = values.shape[0]
    out: list[float] = []
    i = 0
    while i < n - 1:
        va = values[i]
        if va == 0:
            i += 1
            continue
        j = i + 1
        while j < n and values[j] == 0:
            j += 1
        if j >= n:
            break                                   # zeros run to the end: no sign change observable
        vb = values[j]
        if (va > 0) != (vb > 0):
            if j == i + 1:
                out.append(float(grid[i] + (grid[j] - grid[i]) * va / (va - vb)))
            else:
                out.append(float(0.5 * (grid[i + 1] + grid[j - 1])))   # exact zero point or plateau midpoint
        i = j
    return out


def sweep_step(ticker: str, spot: float) -> float:
    step = config.SWEEP_STEP.get(ticker.upper())
    if step:
        return step
    return max(round(config.STOCK_STEP_FRAC * spot, 2), 0.01)


def sweep_grid(ticker: str, spot: float) -> np.ndarray:
    step = sweep_step(ticker, spot)
    lo, hi = spot * (1 - config.SWEEP_BAND), spot * (1 + config.SWEEP_BAND)
    start = math.ceil(lo / step) * step
    n = int(math.floor((hi - start) / step + 1e-9)) + 1
    return start + step * np.arange(n)


def nearest(values: list[float], spot: float) -> float | None:
    if not values:
        return None
    return min(values, key=lambda z: (abs(z - spot), z))


def _pick_wall(rows: list[dict], key: str, spot: float) -> float | None:
    """Strike with the max value; ties go to the strike nearest spot, then the lower strike."""
    best = None
    for r in rows:
        v = abs(r[key])
        if v <= 0:
            continue
        if best is None:
            best = (v, r["strike"])
            continue
        bv, bk = best
        tol = 1e-9 * max(v, bv)
        if v > bv + tol:
            best = (v, r["strike"])
        elif abs(v - bv) <= tol:
            d_new, d_old = abs(r["strike"] - spot), abs(bk - spot)
            if d_new < d_old - 1e-12 or (abs(d_new - d_old) <= 1e-12 and r["strike"] < bk):
                best = (v, r["strike"])
    return best[1] if best else None


def walls(rows: list[dict], spot: float) -> tuple[float | None, float | None]:
    """Call wall = max summed call GEX; put wall = max summed abs put GEX."""
    return _pick_wall(rows, "call_gex", spot), _pick_wall(rows, "put_gex", spot)


def strike_rows(contracts: list[Contract], lo: float | None = None,
                hi: float | None = None) -> list[dict]:
    """Signed per-strike sums over valid contracts (put_gex negative)."""
    by: dict[float, dict] = {}
    for c in contracts:
        if lo is not None and c.strike < lo:
            continue
        if hi is not None and c.strike > hi:
            continue
        r = by.setdefault(c.strike, {"strike": c.strike, "call_gex": 0.0, "put_gex": 0.0,
                                     "net_gex": 0.0, "call_oi": 0, "put_oi": 0})
        if c.type == "call":
            r["call_oi"] += c.oi or 0
            if c.valid:
                r["call_gex"] += c.gex
        else:
            r["put_oi"] += c.oi or 0
            if c.valid:
                r["put_gex"] += c.gex
    for r in by.values():
        r["net_gex"] = r["call_gex"] + r["put_gex"]
    return [by[k] for k in sorted(by)]


def regime(net: float, gross: float) -> str:
    if gross <= 0 or abs(net) < config.NEUTRAL_FRAC * gross:
        return "neutral"
    return "positive" if net > 0 else "negative"


@dataclass
class Levels:
    call_wall: float | None
    put_wall: float | None
    call_wall_gex: float | None
    put_wall_gex: float | None
    zero_gamma: list[float]
    nearest_zero_gamma: float | None
    net_gex: float
    gross_gex: float
    regime: str


def compute_levels(ticker: str, contracts: list[Contract], spot: float) -> tuple[Levels, np.ndarray, np.ndarray]:
    """Walls, zero-gamma crossings, regime. Returns (levels, sweep grid, sweep values)."""
    lo, hi = spot * (1 - config.WALL_STRIKE_BAND), spot * (1 + config.WALL_STRIKE_BAND)
    rows = strike_rows(contracts, lo, hi)
    cw, pw = walls(rows, spot)
    rmap = {r["strike"]: r for r in rows}
    arr = GexArrays.from_contracts(contracts)
    grid = sweep_grid(ticker, spot)
    vals = total_gex_at(grid, arr)
    zs = [round(z, 4) for z in zero_crossings(grid, vals)]
    valid = [c for c in contracts if c.valid]
    net = float(sum(c.gex for c in valid))
    gross = float(sum(abs(c.gex) for c in valid))
    lv = Levels(cw, pw,
                rmap[cw]["call_gex"] if cw is not None else None,
                rmap[pw]["put_gex"] if pw is not None else None,
                zs, nearest(zs, spot), net, gross, regime(net, gross))
    return lv, grid, vals


def profile_curve(contracts: list[Contract], spot: float, points: int | None = None) -> list[dict]:
    points = points or config.PROFILE_POINTS
    grid = np.linspace(spot * (1 - config.SWEEP_BAND), spot * (1 + config.SWEEP_BAND), points)
    vals = total_gex_at(grid, GexArrays.from_contracts(contracts))
    return [{"spot": round(float(s), 4), "net_gex": float(v)} for s, v in zip(grid, vals)]


def filter_expiries(contracts: list[Contract], expiries: set[date] | None) -> list[Contract]:
    if not expiries:
        return list(contracts)
    return [c for c in contracts if c.expiry in expiries]
