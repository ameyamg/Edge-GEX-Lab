"""Magnitude reference for net GEX: the modeled hedge next to what normally trades.

Net GEX is dollars of the underlying that dealers would trade, under the sign assumption, for
each 1% move. A raw dollar figure is not comparable across tickers, so the map also reports it
as a share of the underlying's average daily dollar volume (ADV) and, once enough history
exists, as a percentile among this ticker's own eligible 10:00 frozen captures.

ADV = mean of v * vw (Alpaca daily SIP bars: volume times the bar VWAP) over the last
ADV_SESSIONS completed regular sessions. Cached per symbol per completed session, in memory
and on disk. REST only, never a websocket.

SPX and NDX have no volume reference. Index options have no share volume, and the hedging mostly
trades in ES and NQ futures, which have no free data source. Scaling SPY or QQQ dollar volume by the
index/ETF price ratio would be a units error (dollar volume is already in dollars), so their share
of ADV is null with adv_basis "none" and a plain sentence; the dollar rows and percentile stay.

Two more views of the same number:
- per typical day: the hedge for a 1-ATR move, abs(net) x ATR%, where ATR% is the simple mean true
  range of the last ATR_SESSIONS completed sessions over spot (split adjusted SIP daily bars, cached
  daily like ADV). SPX/NDX use SPY/QQQ's ATR% (a percentage, so no units problem), labeled.
- gross: abs(call GEX) + abs(put GEX) of the same expiries, and net as a share of it.
"""
from __future__ import annotations

import json
import threading
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from gexlab import config, store
from gexlab.engine.calendar import NY, TradingCalendar, iso_z, ny_today, utc_now
from gexlab.sources.base import SourceError

MOVE_METHOD = ("Linear: the hedge per 1% times the move. Gamma itself changes as price moves, "
               "so the larger the move, the rougher the estimate.")
PERCENTILE_BASIS = ("Signed net GEX over every expiry in the 0 to 7 day window, against this ticker's "
                    "eligible 10:00 frozen captures before today.")
EXPLAINER = ("Net GEX estimates how many dollars of the underlying dealers would have to trade, under the sign "
             "assumption, for each 1% move. Compare it to how much normally trades: a few percent of daily volume "
             "is noticeable; a fraction of a percent is usually lost in normal flow. The size is a model estimate, "
             "not a measured flow.")

PER_ATR_METHOD = ("Linear: the hedge per 1% times the ATR in percent. ATR is the simple mean true range of the last "
                  "14 completed sessions, divided by spot (SPX and NDX: the ETF's last close). Gamma changes as price "
                  "moves, so this is a rough size for a typical day, not a forecast.")
ATR_FROM_ETF_NOTE = ("Alpaca has no index bars, so {index} uses {etf}'s 14-session ATR as a percent of {etf}'s last "
                     "close. A percentage carries over from the ETF to the index; dollar volume does not.")
GROSS_NOTE = ("Gross adds call GEX and put GEX without signs, same expiries. Net to gross near 100% means one side "
              "dominates. A low ratio means calls and puts largely offset each other: check the Unsigned view to see "
              "how much exposure sits on each side.")
INDEX_NOTE = ("Index products read larger than single stocks for four reasons: daily expiries keep a lot of short "
              "dated gamma open, index positioning is put heavy so less of it nets out, implied volatility is lower "
              "so each contract carries more gamma, and a 1% move is a large day for an index but a small one for a "
              "volatile stock. The per typical day view (1 ATR) puts them on a more equal footing.")

_LOCK = threading.Lock()
_MEM: dict[tuple[str, str], dict] = {}            # (kind, symbol) -> cached entry (for one completed session)
_FAIL: dict[tuple[str, str], tuple[float, str]] = {}   # (kind, symbol) -> (time of last failure, reason)


def reset_cache() -> None:
    with _LOCK:
        _MEM.clear()
        _FAIL.clear()


# ---------------------------------------------------------------- pure math

def last_completed_session(cal: TradingCalendar, now: datetime) -> date:
    """Today once its regular close has passed, else the prior session."""
    d = ny_today(now)
    if cal.is_session(d) and cal.close_dt(d) <= now:
        return d
    return cal.prev_session(d)


def adv_from_bars(bars: list[dict], last_completed: date, n: int | None = None) -> dict:
    """Average daily dollar volume from Alpaca daily bars ({t, v, vw}).

    Uses the newest n bars dated on or before last_completed (a partial bar for a session still
    trading is never used). Fewer than n usable bars gives adv_usd None and a reason."""
    n = n or config.ADV_SESSIONS
    rows = []
    for b in bars:
        try:
            d = datetime.fromisoformat(str(b["t"]).replace("Z", "+00:00")).astimezone(NY).date()
            v, vw = float(b["v"]), float(b["vw"])
        except (KeyError, TypeError, ValueError):
            continue
        if d <= last_completed and v > 0 and vw > 0:
            rows.append((d, v * vw))
    rows = sorted(dict(rows).items())[-n:]          # one bar per date, newest n
    if len(rows) < n:
        return {"adv_usd": None, "sessions": len(rows), "first": None, "last": None,
                "reason": f"only {len(rows)} of {n} sessions have daily bars"}
    return {"adv_usd": sum(x for _, x in rows) / n, "sessions": n,
            "first": rows[0][0].isoformat(), "last": rows[-1][0].isoformat(), "reason": None}


def atr_from_bars(bars: list[dict], last_completed: date, n: int | None = None) -> dict:
    """Average true range from daily bars ({t, h, l, c}): simple mean of the last n true ranges.

    TR = max(high - low, |high - prior close|, |low - prior close|), so n sessions need n + 1 bars
    dated on or before last_completed. atr_pct_of_close is ATR over the newest close, in percent."""
    n = n or config.ATR_SESSIONS
    rows = []
    for b in bars:
        try:
            d = datetime.fromisoformat(str(b["t"]).replace("Z", "+00:00")).astimezone(NY).date()
            h, lo, c = float(b["h"]), float(b["l"]), float(b["c"])
        except (KeyError, TypeError, ValueError):
            continue
        if d <= last_completed and h > 0 and lo > 0 and c > 0 and h >= lo:
            rows.append((d, (h, lo, c)))
    rows = sorted(dict(rows).items())[-(n + 1):]
    if len(rows) < n + 1:
        got = max(len(rows) - 1, 0)
        return {"atr_usd": None, "last_close": None, "atr_pct_of_close": None, "sessions": got,
                "first": None, "last": None, "reason": f"only {got} of {n} sessions have a daily range"}
    trs = []
    for (_, (_, _, pc)), (_, (h, lo, _)) in zip(rows[:-1], rows[1:]):
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    atr = sum(trs) / n
    close = rows[-1][1][2]
    return {"atr_usd": atr, "last_close": close, "atr_pct_of_close": atr / close * 100.0, "sessions": n,
            "first": rows[1][0].isoformat(), "last": rows[-1][0].isoformat(), "reason": None}


def percentile_of(value: float, history: list[float], min_n: int | None = None) -> float | None:
    """Mid-rank percentile of value within history (ties count half). None below min_n."""
    min_n = config.PERCENTILE_MIN_HISTORY if min_n is None else min_n
    if len(history) < min_n or not history:
        return None
    below = sum(1 for h in history if h < value)
    equal = sum(1 for h in history if h == value)
    return round(100.0 * (below + 0.5 * equal) / len(history), 1)


def hedge_for_move(per_1pct: float) -> list[dict]:
    return [{"move_pct": m, "usd": per_1pct * m} for m in config.MAGNITUDE_MOVES]


# ---------------------------------------------------------------- ADV with a daily cache

def _read_disk(path: Path) -> dict:
    try:
        j = json.loads(path.read_text(encoding="utf-8"))
        return j if isinstance(j, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_disk(path: Path, symbol: str, entry: dict) -> None:
    from gexlab.sources.base import atomic_write_bytes
    j = _read_disk(path)
    j[symbol] = entry
    try:
        atomic_write_bytes(path, json.dumps(j, indent=1, sort_keys=True).encode("utf-8"))
    except OSError:
        pass                                          # the memory cache still holds it


def fetch_daily_bars(symbol: str, start: date, end: date, adjustment: str = "raw") -> list[dict]:
    """Alpaca daily SIP bars. raw for ADV (v * vw is the dollars actually traded), split for ATR."""
    from gexlab.sources.alpaca import AlpacaSource
    src = AlpacaSource(priority="low", who="adv")
    r = src._get(f"{config.ALPACA_DATA_BASE}/v2/stocks/{symbol}/bars",
                 {"feed": config.ADV_FEED, "timeframe": "1Day", "start": start.isoformat(),
                  "end": end.isoformat(), "limit": 100, "adjustment": adjustment})
    if r.status_code != 200:
        raise SourceError(f"daily {config.ADV_FEED.upper()} bars for {symbol}: HTTP {r.status_code}",
                          status=r.status_code)
    return r.json().get("bars") or []


def _cached_daily(kind: str, symbol: str, last_completed: date, *, value_key: str, what: str,
                  fetch: Callable[[str, date, date], list[dict]],
                  compute: Callable[[list[dict], date], dict], path: Path,
                  clock: Callable[[], float]) -> dict:
    """One entry per symbol per completed session: memory, then disk, then one fetch. A failure is
    retried after ADV_RETRY_S, never on every refresh."""
    symbol = symbol.upper()
    key = last_completed.isoformat()
    ck = (kind, symbol)
    with _LOCK:
        e = _MEM.get(ck)
        if e and e.get("as_of_session") == key:
            return e
        fail = _FAIL.get(ck)
        if fail and clock() - fail[0] < config.ADV_RETRY_S:
            return {value_key: None, "as_of_session": key, "reason": fail[1]}
    disk = _read_disk(path).get(symbol)
    if isinstance(disk, dict) and disk.get("as_of_session") == key and disk.get(value_key):
        with _LOCK:
            _MEM[ck] = disk
        return disk
    try:
        bars = fetch(symbol, last_completed - timedelta(days=config.ADV_LOOKBACK_DAYS), last_completed)
        res = compute(bars, last_completed)
    except SourceError as ex:
        reason = f"{what} unavailable ({ex})"
        with _LOCK:
            _FAIL[ck] = (clock(), reason)
        return {value_key: None, "as_of_session": key, "reason": reason}
    entry = {"symbol": symbol, "as_of_session": key, "source": f"alpaca_{config.ADV_FEED}_daily",
             "fetched_at": iso_z(utc_now()), **res}
    if entry[value_key] is None:
        with _LOCK:
            _FAIL[ck] = (clock(), entry["reason"])
        return entry
    with _LOCK:
        _MEM[ck] = entry
        _FAIL.pop(ck, None)
    _write_disk(path, symbol, entry)
    return entry


def get_adv(symbol: str, last_completed: date, *,
            fetch: Callable[[str, date, date], list[dict]] | None = None,
            cache_path: Path | None = None, clock: Callable[[], float] = _time.time) -> dict:
    """ADV entry for symbol as of last_completed (raw daily bars)."""
    return _cached_daily("adv", symbol, last_completed, value_key="adv_usd", what="daily volume",
                         fetch=fetch or fetch_daily_bars, compute=adv_from_bars,
                         path=cache_path or config.ADV_CACHE_FILE, clock=clock)


def _fetch_split_adjusted(symbol: str, start: date, end: date) -> list[dict]:
    return fetch_daily_bars(symbol, start, end, adjustment=config.ATR_ADJUSTMENT)


def get_atr(symbol: str, last_completed: date, *,
            fetch: Callable[[str, date, date], list[dict]] | None = None,
            cache_path: Path | None = None, clock: Callable[[], float] = _time.time) -> dict:
    """ATR entry for symbol as of last_completed (split adjusted daily bars), cached like ADV."""
    return _cached_daily("atr", symbol, last_completed, value_key="atr_usd", what="daily range",
                         fetch=fetch or _fetch_split_adjusted, compute=atr_from_bars,
                         path=cache_path or config.ATR_CACHE_FILE, clock=clock)


def demo_adv(symbol: str) -> dict:
    p = config.FIXTURES_DIR / "adv.json"
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        j = {}
    e = j.get(symbol.upper())
    if not e:
        return {"adv_usd": None, "reason": f"no demo ADV fixture for {symbol.upper()}"}
    return {**e, "source": "demo"}


def demo_atr(symbol: str) -> dict:
    p = config.FIXTURES_DIR / "atr.json"
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        j = {}
    e = j.get(symbol.upper())
    if not e:
        return {"atr_usd": None, "reason": f"no demo ATR fixture for {symbol.upper()}"}
    return {**e, "source": "demo"}


# ---------------------------------------------------------------- history (percentile)

def history(ticker: str, before: date) -> list[float]:
    """Signed net GEX of this ticker's eligible frozen captures dated before `before`."""
    if config.is_demo():
        p = config.FIXTURES_DIR / f"captures_{ticker.upper()}.json"
        try:
            rows = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [float(r["net_gex"]) for r in rows if r.get("status") == "eligible"
                and r.get("net_gex") is not None and r.get("date", "") < before.isoformat()]
    try:
        with store.session() as conn:
            rows = conn.execute(
                "SELECT net_gex FROM captures WHERE kind='frozen' AND ticker=? AND status='eligible' "
                "AND net_gex IS NOT NULL AND session_date < ?", (ticker.upper(), before.isoformat())).fetchall()
    except Exception:
        return []
    return [float(r[0]) for r in rows]


# ---------------------------------------------------------------- the payload

def _per_atr(ticker: str, per_1pct: float, pct_adv: float | None, spot: float, atr_entry: dict,
             atr_symbol: str) -> dict:
    via_etf = atr_symbol != ticker
    atr = atr_entry.get("atr_usd")
    atr_pct = None
    pct_basis = None
    if atr:
        if not via_etf and spot and spot > 0:
            atr_pct, pct_basis = atr / spot * 100.0, "spot"
        else:
            close = atr_entry.get("last_close")
            atr_pct = atr_entry.get("atr_pct_of_close") or (atr / close * 100.0 if close else None)
            pct_basis = ("etf_close" if via_etf else "last_close") if atr_pct is not None else None
    ok = atr_pct is not None
    return {
        "atr_pct": atr_pct,                          # percent of price, null when unavailable
        "atr_usd": atr if ok else None,              # in atr_symbol's dollars
        "atr_symbol": atr_symbol,
        "atr_basis": ("etf" if via_etf else "own") if ok else None,
        "atr_pct_basis": pct_basis,                  # spot | etf_close | last_close | null
        "atr_label": (f"ATR% from {atr_symbol}" if via_etf else f"{ticker}'s 14-session ATR") if ok else None,
        "atr_note": ATR_FROM_ETF_NOTE.format(index=ticker, etf=atr_symbol) if via_etf else None,
        "atr_sessions": atr_entry.get("sessions") if ok else None,
        "atr_window": {"first": atr_entry.get("first"), "last": atr_entry.get("last")} if ok else None,
        "atr_source": atr_entry.get("source") if ok else None,
        "atr_reason": None if ok else (atr_entry.get("reason") or "daily range unavailable"),
        "hedge_per_atr_usd": per_1pct * atr_pct if ok else None,
        "pct_of_adv_per_atr": pct_adv * atr_pct if (pct_adv is not None and ok) else None,
        "method": PER_ATR_METHOD,
    }


def _gross(per_1pct: float, call_gex: float | None, put_gex: float | None, adv: float | None,
           atr_pct: float | None) -> dict | None:
    if call_gex is None or put_gex is None:
        return None
    g = abs(call_gex) + abs(put_gex)
    pct = (g / adv * 100.0) if adv else None
    return {
        "per_1pct_usd": g,
        "pct_of_adv": pct,
        "hedge_per_atr_usd": g * atr_pct if atr_pct is not None else None,
        "pct_of_adv_per_atr": pct * atr_pct if (pct is not None and atr_pct is not None) else None,
        "net_to_gross": (per_1pct / g) if g > 0 else None,   # 0..1
        "note": GROSS_NOTE,
    }


def build(ticker: str, *, net_signed: float, window_net_signed: float, spot: float,
          cal: TradingCalendar, now: datetime,
          call_gex: float | None = None, put_gex: float | None = None,
          adv_fn: Callable[[str, date], dict] | None = None,
          atr_fn: Callable[[str, date], dict] | None = None,
          history_fn: Callable[[str, date], list[float]] | None = None) -> dict:
    """The `magnitude` block of /api/map.

    net_signed: signed net GEX of the selected expiries (the NET GEX tile).
    window_net_signed: signed net GEX of every expiry in the window (the frozen-capture basis).
    call_gex, put_gex: raw call and put GEX of the selected expiries (put negative), for gross."""
    ticker = ticker.upper()
    demo = config.is_demo()
    last = last_completed_session(cal, now)
    adv_fn = adv_fn or ((lambda s, d: demo_adv(s)) if demo else (lambda s, d: get_adv(s, d)))
    atr_fn = atr_fn or ((lambda s, d: demo_atr(s)) if demo else (lambda s, d: get_atr(s, d)))
    history_fn = history_fn or history

    per_1pct = abs(net_signed)
    if ticker in config.ADV_NO_REFERENCE:
        adv_entry, adv = {}, None                    # never fetched: there is no honest reference
        basis, label = "none", config.ADV_NO_REFERENCE[ticker]
    else:
        adv_entry = adv_fn(ticker, last)
        adv = adv_entry.get("adv_usd")
        basis = "own" if adv else None
        label = f"{ticker}'s average daily dollar volume" if adv else None

    hist = history_fn(ticker, ny_today(now))
    pct_adv = (per_1pct / adv * 100.0) if adv else None
    atr_symbol = config.ATR_FROM_ETF.get(ticker, ticker)
    per_atr = _per_atr(ticker, per_1pct, pct_adv, spot, atr_fn(atr_symbol, last), atr_symbol)
    return {
        "adv_usd": adv,
        "adv_basis": basis,                          # own | none | null
        "adv_label": label,
        "adv_sessions": adv_entry.get("sessions") if adv else None,
        "adv_window": ({"first": adv_entry.get("first"), "last": adv_entry.get("last")} if adv else None),
        "adv_source": adv_entry.get("source") if adv else None,
        "adv_reason": (None if adv or basis == "none"
                       else (adv_entry.get("reason") or "average daily dollar volume unavailable")),
        "hedge_per_1pct_usd": per_1pct,
        "pct_of_adv": pct_adv,
        "hedge_for_move": hedge_for_move(per_1pct),
        "hedge_for_move_method": MOVE_METHOD,
        "per_atr": per_atr,
        "gross": _gross(per_1pct, call_gex, put_gex, adv, per_atr["atr_pct"]),
        "percentile": percentile_of(window_net_signed, hist),
        "percentile_value": window_net_signed,
        "percentile_basis": PERCENTILE_BASIS,
        "history_n": len(hist),
        "history_min": config.PERCENTILE_MIN_HISTORY,
        "ticks_pct": list(config.MAGNITUDE_TICKS),
        "explainer": EXPLAINER,
        "index_note": INDEX_NOTE,
    }
