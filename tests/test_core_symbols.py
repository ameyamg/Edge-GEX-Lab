import json
from datetime import date, datetime, timezone

import pytest

from gexlab.engine.calendar import TradingCalendar
from gexlab.models import parse_occ
from gexlab.sources import cboe

UTC = timezone.utc


def test_parse_alpaca_occ():
    s = parse_occ("SPY261019P00755000")
    assert (s.root, s.expiry, s.type, s.strike) == ("SPY", date(2026, 10, 19), "put", 755.0)


def test_parse_cboe_weekly_and_half_strike():
    s = parse_occ("SPXW261029C07460000")
    assert (s.root, s.expiry, s.type, s.strike) == ("SPXW", date(2026, 10, 29), "call", 7460.0)
    assert parse_occ("QQQ261019C00612500").strike == 612.5
    assert parse_occ("NDXP261020P25400000").root == "NDXP"


def test_parse_bad_symbol():
    with pytest.raises(ValueError):
        parse_occ("SPY 2610 C 755")


def test_cboe_settlement_roots():
    assert cboe.settlement_for_root("SPX", "SPX") == "AM"
    assert cboe.settlement_for_root("SPX", "SPXW") == "PM"
    assert cboe.settlement_for_root("NDX", "NDX") == "AM"
    assert cboe.settlement_for_root("NDX", "NDXP") == "PM"
    assert cboe.settlement_for_root("SPX", "XSP") is None


def test_cboe_timestamp_is_utc():
    assert cboe.parse_cboe_timestamp("2026-10-17 13:32:46") == datetime(2026, 10, 17, 13, 32, 46, tzinfo=UTC)


def test_cboe_parse_chain_filters_window_band_and_roots():
    payload = {"timestamp": "2026-10-16 14:00:00", "data": {"current_price": 7000.0, "options": [
        {"option": "SPXW261016C07000000", "bid": 10, "ask": 11, "iv": 0.12, "open_interest": 100},
        {"option": "SPX261016C07000000", "bid": 10, "ask": 11, "iv": 0.12, "open_interest": 100},
        {"option": "SPXW261120C07000000", "iv": 0.12, "open_interest": 5},   # outside 7 days
        {"option": "SPXW261019P04000000", "iv": 0.5, "open_interest": 5},    # outside strike band
        {"option": "XSP261019P00700000", "iv": 0.5, "open_interest": 5},     # other root
        {"option": "SPXW261019P06950000", "iv": 0.0, "open_interest": 7},    # no iv
    ]}}
    cal = TradingCalendar.weekdays(date(2026, 9, 1), date(2026, 11, 30))
    ch = cboe.parse_chain("SPX", json.dumps(payload).encode(), datetime(2026, 10, 16, 14, 1, tzinfo=UTC), cal)
    syms = {c.symbol: c for c in ch.contracts}
    assert set(syms) == {"SPXW261016C07000000", "SPX261016C07000000", "SPXW261019P06950000"}
    assert syms["SPX261016C07000000"].settlement == "AM"
    assert syms["SPXW261019P06950000"].iv is None
    assert ch.spot.price == 7000.0 and ch.feed == "cboe_delayed"
    assert ch.oi_date == "2026-10-15"
    assert ch.source_ts == datetime(2026, 10, 16, 14, 0, tzinfo=UTC)


def test_cboe_am_contract_dropped_after_0930_by_finalize():
    from gexlab.engine import gex as G
    payload = {"timestamp": "2026-10-16 14:00:00", "data": {"current_price": 7000.0, "options": [
        {"option": "SPXW261016C07000000", "iv": 0.12, "open_interest": 100},
        {"option": "SPX261016C07000000", "iv": 0.12, "open_interest": 100}]}}
    as_of = datetime(2026, 10, 16, 14, 1, tzinfo=UTC)          # 10:01 ET
    ch = cboe.parse_chain("SPX", json.dumps(payload).encode(), as_of, None)
    out = G.finalize(ch.contracts, ch.spot.price, as_of, None)
    assert [c.root for c in out] == ["SPXW"]
