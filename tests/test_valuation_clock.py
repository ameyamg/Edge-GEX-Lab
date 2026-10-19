"""Valuation clock (model v1.0): T is measured from one explicit valuation time per map or capture.

In regular hours that is the option quote time (never later than now). With the market closed it
is the last regular-session close, and the spot is that session's official close, never an
extended-hours print. Found 2026-10-17 against Cboe's published gamma: measuring T from a Saturday
wall clock against Friday's IVs overstated near-the-money gamma by about 14% and moved the SPX
put wall.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

import test_core_feed as F
from gexlab import config, service
from gexlab.engine import gex as G
from gexlab.engine.bsm import gamma as bsm_gamma
from gexlab.engine.calendar import (NY, TradingCalendar, time_to_expiry, valuation_label,
                                    valuation_time)
from gexlab.engine.digest import canonical_inputs, input_digest
from gexlab.models import Spot
from gexlab.sources import alpaca as A
from gexlab.sources import cboe
from conftest import exchange_cal

UTC = timezone.utc
CAL = exchange_cal()
EXP = date(2026, 10, 19)                                         # Monday, PM settled
FRI_CLOSE = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)            # Fri Oct 16 16:00 ET
FRI_1615 = datetime(2026, 10, 16, 20, 15, tzinfo=UTC)            # last Cboe payload of the session
SAT = datetime(2026, 10, 17, 18, 48, tzinfo=UTC)                 # Sat 14:48 ET, when the bug was found


def nyt(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=NY).astimezone(UTC)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("GEXLAB_DEMO", raising=False)
    A.FEED_STATE.update(options=None, options_checked=0.0, stocks=None)
    A._CONTRACT_CACHE.clear()
    yield
    A.FEED_STATE.update(options=None, options_checked=0.0, stocks=None)
    A._CONTRACT_CACHE.clear()


# ----------------------------------------------------------------------------- the rule

def test_closed_market_valuation_is_the_last_session_close():
    assert valuation_time(CAL, SAT) == FRI_CLOSE                          # weekend
    assert valuation_time(CAL, nyt(2026, 10, 16, 20)) == FRI_CLOSE        # after hours, same day
    assert valuation_time(CAL, nyt(2026, 10, 19, 8)) == FRI_CLOSE         # Monday pre-market
    assert valuation_time(CAL, SAT, FRI_1615) == FRI_CLOSE                # a 16:15 payload is valued at 16:00
    older = nyt(2026, 10, 16, 15, 40)
    assert valuation_time(CAL, SAT, older) == older                       # data older than the close: its own time


def test_in_session_valuation_is_the_quote_time_never_later_than_now():
    now = nyt(2026, 10, 16, 11, 0)
    q = now - timedelta(minutes=15)
    assert valuation_time(CAL, now, q) == q                               # Cboe: 15 min delayed payload
    assert valuation_time(CAL, now, now + timedelta(seconds=5)) == now    # a future vendor stamp is capped
    assert valuation_time(CAL, now) == now
    assert valuation_time(None, now, q) == q                              # no calendar: in-session clock


def test_holiday_and_early_close():
    rows = [{"date": "2026-11-25", "open": "09:30", "close": "16:00"},     # Wed
            {"date": "2026-11-27", "open": "09:30", "close": "13:00"},     # Fri after Thanksgiving: early close
            {"date": "2026-11-30", "open": "09:30", "close": "16:00"}]     # Thanksgiving (Thu 26) absent
    cal = TradingCalendar.from_alpaca(rows)
    wed_close = nyt(2026, 11, 25, 16)
    assert valuation_time(cal, nyt(2026, 11, 26, 12)) == wed_close        # holiday: prior session close
    assert cal.market_open(nyt(2026, 11, 27, 12, 30))
    assert valuation_time(cal, nyt(2026, 11, 27, 12, 30), nyt(2026, 11, 27, 12, 29)) == nyt(2026, 11, 27, 12, 29)
    fri_early = nyt(2026, 11, 27, 13)
    assert valuation_time(cal, nyt(2026, 11, 27, 14)) == fri_early        # after an early close
    assert valuation_time(cal, nyt(2026, 11, 28, 10)) == fri_early        # the weekend after it
    assert valuation_label(fri_early) == "Valued at Fri Nov 27 close"
    assert valuation_label(nyt(2026, 11, 27, 12, 45), at_close=False) == "Valued at Fri Nov 27 12:45 ET"


# ----------------------------------------------------------------------------- Cboe: weekend == Friday close

def _cboe_payload(ts: datetime, price: float, rows: list[tuple]) -> bytes:
    """rows: (type, strike, oi, iv). SPXW weeklies settle PM."""
    opts = []
    for typ, k, oi, iv in rows:
        sym = f"SPXW{EXP:%y%m%d}{'C' if typ == 'call' else 'P'}{int(round(k * 1000)):08d}"
        opts.append({"option": sym, "open_interest": oi, "iv": iv, "bid": 1.0, "ask": 1.1})
    return json.dumps({"timestamp": ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                       "data": {"current_price": price, "options": opts}}).encode()


def _finalized(body: bytes, now: datetime):
    ch = cboe.parse_chain("SPX", body, now, CAL)
    ch.contracts = G.finalize(ch.contracts, ch.spot.price, ch.valuation, CAL)    # what service.fetch_chain does
    return ch


ROWS = [(t, k, 1000, 0.15) for k in (7600.0, 7650.0, 7660.0, 7700.0, 7750.0) for t in ("call", "put")]


def test_weekend_valuation_matches_friday_close_exactly():
    body = _cboe_payload(FRI_1615, 7659.44, ROWS)
    fri = _finalized(body, FRI_1615 + timedelta(seconds=30))      # Friday 16:15, after the close
    sat = _finalized(body, SAT)                                   # same payload, Saturday afternoon
    assert fri.valuation == sat.valuation == FRI_CLOSE
    assert [c.T for c in fri.contracts] == [c.T for c in sat.contracts]
    assert [c.gamma for c in fri.contracts] == [c.gamma for c in sat.contracts]
    assert [c.gex for c in fri.contracts] == [c.gex for c in sat.contracts]
    assert fri.contracts[0].T == pytest.approx(time_to_expiry(FRI_CLOSE, EXP, "PM", CAL))
    # The old wall clock: T shrinks by most of a day and near-the-money gamma rises well over 10%.
    atm = next(c for c in sat.contracts if c.strike == 7660.0 and c.type == "put")
    t_wall = time_to_expiry(SAT, EXP, "PM", CAL)
    g_wall = bsm_gamma(7659.44, 7660.0, t_wall, 0.0, 0.15)
    assert g_wall / atm.gamma > 1.14


def test_cboe_in_session_uses_the_payload_time_not_the_request_time():
    payload = nyt(2026, 10, 16, 14, 45)
    now = nyt(2026, 10, 16, 15, 0)
    ch = _finalized(_cboe_payload(payload, 7659.44, ROWS), now)
    assert ch.valuation == payload and ch.spot.basis == "payload"
    assert ch.contracts[0].T == pytest.approx(time_to_expiry(payload, EXP, "PM", CAL))


def test_cboe_closed_market_spot_is_the_regular_close_payload_time_kept():
    """Saturday: the spot caption reads "Regular close (Cboe)", not the payload fetch time."""
    ch = _finalized(_cboe_payload(FRI_1615, 7659.44, ROWS), SAT)
    assert ch.spot.basis == "regular_close" and ch.spot.source == "cboe"
    assert ch.spot.ts == FRI_CLOSE and ch.spot.price == 7659.44
    assert ch.source_ts == FRI_1615                    # the payload time stays available (feed.data_ts)


def test_cboe_closed_market_older_payload_keeps_the_payload_basis():
    """A payload from before the close (a stale cache) is not relabeled as the close."""
    early = nyt(2026, 10, 16, 15, 30)
    ch = _finalized(_cboe_payload(early, 7650.0, ROWS), SAT)
    assert ch.spot.basis == "payload" and ch.spot.ts == early


def _wall_clock_put_wall(body: bytes, now: datetime) -> float:
    ch = cboe.parse_chain("SPX", body, now, CAL)
    cs = G.finalize(ch.contracts, ch.spot.price, now, CAL)        # the bug: T from the wall clock
    return G.compute_levels("SPX", cs, ch.spot.price)[0].put_wall


def test_regression_wall_clock_flips_the_put_wall_valuation_clock_does_not():
    """Small version of the SPX Oct 19 finding: a near-the-money put strike against a larger one
    further away. Shorter T sharpens gamma near spot, so the wall clock hands the put wall to the
    near strike; Friday-close T (what Cboe and QuantWheel show) keeps it at the far one."""
    S, near, far, iv = 7659.44, 7660.0, 7700.0, 0.12

    def ratio(t):
        return bsm_gamma(S, near, t, 0.0, iv) / bsm_gamma(S, far, t, 0.0, iv)

    r_fri = ratio(time_to_expiry(FRI_CLOSE, EXP, "PM", CAL))
    r_sat = ratio(time_to_expiry(SAT, EXP, "PM", CAL))
    assert r_sat > r_fri * 1.05                                  # the effect is material, not a rounding tie
    oi_near = 1000
    oi_far = round(oi_near * (r_fri * r_sat) ** 0.5)             # between the two: the clock decides the wall
    body = _cboe_payload(FRI_1615, S, [("put", near, oi_near, iv), ("put", far, oi_far, iv),
                                       ("call", 7800.0, 10, iv)])
    ch = _finalized(body, SAT)
    assert G.compute_levels("SPX", ch.contracts, S)[0].put_wall == far
    assert _wall_clock_put_wall(body, SAT) == near


# ----------------------------------------------------------------------------- Alpaca: regular close spot

def _handler(daily_bars=True, calls=None):
    base = F.make_handler(calls=calls)

    def handler(req: httpx.Request) -> httpx.Response:
        p, q = req.url.path, req.url.params
        if p == "/v2/stocks/SPY/trades/latest":                   # an after-hours print, never used when closed
            return httpx.Response(200, json={"symbol": "SPY", "trade": {"p": 98.5, "t": "2026-10-16T23:59:59Z"}})
        if p == "/v2/stocks/SPY/bars":
            if q["timeframe"] == "1Day":
                bars = [{"t": f"{q['start']}T04:00:00Z", "c": 100.0}] if daily_bars else []
                return httpx.Response(200, json={"bars": bars, "symbol": "SPY"})
            # 1-minute bars: 15:58 and 15:59 are regular hours; a 16:00 bar would include after-hours trades.
            return httpx.Response(200, json={"symbol": "SPY", "bars": [
                {"t": "2026-10-16T19:58:00Z", "c": 99.8}, {"t": "2026-10-16T19:59:00Z", "c": 99.9},
                {"t": "2026-10-16T20:00:00Z", "c": 98.7}]})
        return base(req)
    return handler


def test_after_hours_spot_ignored_closed_market_uses_the_official_close(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(F, "QUOTE_T", "2026-10-16T20:14:59Z")    # closing quotes, stamped 16:14:59 ET
    ch = F._source(tmp_path, _handler(calls=calls)).fetch_chain("SPY", SAT, CAL)
    assert ch.spot.price == 100.0 and ch.spot.basis == "regular_close" and ch.spot.ts == FRI_CLOSE
    assert ch.valuation == FRI_CLOSE
    assert not any(p.endswith("/trades/latest") for p, _ in calls)
    # The closing OPRA quote is solved at the close, not sent to the vendor's wall-clock IV.
    atm = {c.symbol: c for c in ch.contracts}["SPY261019C00100000"]
    assert atm.iv_source == "solved_mid"


def test_closed_market_falls_back_to_the_last_regular_minute_bar(tmp_path):
    ch = F._source(tmp_path, _handler(daily_bars=False)).fetch_chain("SPY", SAT, CAL)
    assert ch.spot.price == 99.9 and ch.spot.basis == "regular_close"   # the 16:00 bar is extended hours


def test_in_session_alpaca_valuation_is_the_quote_time_and_feeds_the_digest(tmp_path):
    ch = F._source(tmp_path, F.make_handler()).fetch_chain("SPY", F.AS_OF, None)
    assert ch.spot.basis == "last_trade" and ch.spot.price == 100.0
    assert ch.valuation == A.parse_ts(F.QUOTE_T) and ch.valuation <= ch.as_of
    assert canonical_inputs(ch, None)["valuation_time"] == ch.valuation.isoformat(timespec="microseconds")
    d0 = input_digest(ch, None)
    assert input_digest(dataclasses.replace(ch, valuation_ts=ch.as_of), None) != d0


def test_frozen_capture_t_uses_the_valuation_not_the_request_time(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "get_calendar", lambda now=None: CAL)
    monkeypatch.setattr(service, "AlpacaSource", lambda **kw: F._source(tmp_path, F.make_handler()))
    monkeypatch.setattr(config, "is_demo", lambda: False)
    ch = service.fetch_chain("SPY", "high", now=F.AS_OF, cal=CAL, strict=True)
    c = next(c for c in ch.contracts if c.T is not None)
    assert c.T == pytest.approx(time_to_expiry(ch.valuation, c.expiry, c.settlement, CAL))
    assert ch.valuation == A.parse_ts(F.QUOTE_T) != ch.as_of


# ----------------------------------------------------------------------------- map labels

def test_map_labels_when_closed(monkeypatch):
    body = _cboe_payload(FRI_1615, 7659.44, ROWS)
    ch = _finalized(body, SAT)
    slot = service._Entry(ch)
    monkeypatch.setattr(service, "cached_chain", lambda t: (ch, slot, False))
    monkeypatch.setattr(service, "get_calendar", lambda now=None: CAL)
    monkeypatch.setattr(service, "utc_now", lambda: SAT)
    monkeypatch.setattr(service, "frozen_today", lambda t, d: None)
    m = service.build_map("SPX", ["2026-10-19"], signed=True)
    assert m["feed"]["market_open"] is False
    assert m["feed"]["label"] == "Valued at Fri Oct 16 close"
    assert m["valuation"] == {"ts": "2026-10-16T20:00:00Z", "basis": "session_close",
                              "label": "Valued at Fri Oct 16 close"}
    # Closed market: the Cboe spot is the regular close (caption "Regular close (Cboe)"), stamped at the
    # close; the payload time stays in feed.data_ts for the method drawer.
    assert m["spot"]["basis"] == "regular_close" and m["spot"]["source"] == "cboe"
    assert m["spot"]["ts"] == "2026-10-16T20:00:00Z" and m["feed"]["data_ts"] == "2026-10-16T20:15:00Z"
