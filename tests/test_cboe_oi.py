"""Model 1.1: Cboe open interest for Alpaca-quoted tickers (join, fallback, labels, digest, storage). No network."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from gexlab import config, service, store
from gexlab.engine.calendar import TradingCalendar
from gexlab.engine.digest import input_digest
from gexlab.models import Chain, Contract, Spot
from gexlab.sources import cboe
from gexlab.sources.base import Hasher, SourceError
from tests import test_core_feed as F

UTC = timezone.utc
AS_OF = datetime(2026, 10, 19, 15, 50, tzinfo=UTC)            # Mon Oct 19, 11:50 ET
CAL = TradingCalendar.weekdays(date(2026, 9, 1), date(2026, 11, 30))


def _c(sym: str, oi: int | None, oi_date: str | None = "2026-10-15") -> Contract:
    occ = __import__("gexlab.models", fromlist=["parse_occ"]).parse_occ(sym)
    return Contract(symbol=sym, root=occ.root, expiry=occ.expiry, settlement="PM", strike=occ.strike,
                    type=occ.type, oi=oi, oi_date=oi_date if oi is not None else None,
                    oi_basis="reported" if oi is not None else None)


def _payload(oi: dict[str, float], ts: str | None = "2026-10-19 15:48:22") -> bytes:
    body = {"data": {"symbol": "QQQ", "current_price": 724.05,
                     "options": [{"option": k, "open_interest": v, "bid": 1.0, "ask": 1.1} for k, v in oi.items()]},
            "symbol": "QQQ"}
    if ts:
        body["timestamp"] = ts
    return json.dumps(body).encode()


def _fetch(body: bytes):
    return lambda t: (body, False)


def _chain():
    return [_c("QQQ261019C00725000", 2495), _c("QQQ261019P00720000", 8357), _c("QQQ261019C00730000", 100)]


# ----------------------------------------------------------------------------- symbols and fetch

def test_cboe_symbol_index_underscore_etf_plain():
    assert cboe.cboe_symbol("SPX") == "_SPX" and cboe.cboe_symbol("NDX") == "_NDX"
    assert cboe.cboe_symbol("qqq") == "QQQ" and cboe.cboe_symbol("SPY") == "SPY"
    with pytest.raises(SourceError):
        cboe.cboe_symbol("BRK.B")


def test_fetch_raw_url_and_oi_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    paths = []

    def h(req):
        paths.append(req.url.path)
        return httpx.Response(200, content=b'{"x": 1}')
    client = httpx.Client(transport=httpx.MockTransport(h))
    cboe.fetch_raw("QQQ", client)
    cboe.fetch_raw("SPX", client)
    assert paths == ["/api/global/delayed_quotes/options/QQQ.json", "/api/global/delayed_quotes/options/_SPX.json"]
    import time
    later = time.time() + 120                                # past the 60 s floor, inside the 600 s OI cache
    _, cached = cboe.fetch_raw("QQQ", client, now=later, max_age_s=config.CBOE_OI_CACHE_S)
    assert cached and len(paths) == 2
    _, cached = cboe.fetch_raw("QQQ", client, now=later)    # quote path: 60 s
    assert not cached and len(paths) == 3
    _, cached = cboe.fetch_raw("QQQ", client, now=time.time() + 30, max_age_s=1)   # never below the 60 s floor
    assert cached


def test_parse_oi_normalizes_option_code():
    body = _payload({"QQQ 261019C00725000": 6815.0, "qqq261019p00720000": 6769})
    oi, ts = cboe.parse_oi(body)
    assert oi == {"QQQ261019C00725000": 6815, "QQQ261019P00720000": 6769}
    assert ts == datetime(2026, 10, 19, 15, 48, 22, tzinfo=UTC)
    with pytest.raises(ValueError):
        cboe.parse_oi(b"<html>blocked</html>")


# ----------------------------------------------------------------------------- join and labels

def test_join_replaces_oi_and_infers_prior_session():
    cs = _chain()
    h0, h1 = Hasher(), Hasher()
    body = _payload({"QQQ261019C00725000": 6815, "QQQ261019P00720000": 6769, "QQQ261019C00730000": 100})
    info = cboe.apply_oi("QQQ", cs, AS_OF, CAL, h1, fetch=_fetch(body))
    assert [c.oi for c in cs] == [6815, 6769, 100]
    assert all(c.oi_source == "cboe" and c.oi_date == "2026-10-16" and c.oi_basis == "reported" for c in cs)
    assert info["source"] == "cboe" and info["basis"] == "inferred"
    assert info["label"] == "OI: Cboe (prior session, inferred)"
    assert info["oi_date"] == "2026-10-16" and info["alpaca_oi_date"] == "2026-10-15"
    assert info["matched"] == 3 and info["total"] == 3 and info["differ_frac"] == pytest.approx(2 / 3, abs=1e-4)
    assert h1.count == 1 and h1.hexdigest() != h0.hexdigest()      # the Cboe body is bound in response_hash


def test_join_matching_alpaca_keeps_alpaca_date():
    cs = _chain()
    body = _payload({"QQQ261019C00725000": 2495, "QQQ261019P00720000": 8357, "QQQ261019C00730000": 100})
    info = cboe.apply_oi("QQQ", cs, AS_OF, CAL, fetch=_fetch(body))
    assert info["source"] == "cboe" and info["basis"] == "matches_alpaca"
    assert info["oi_date"] == "2026-10-15" and info["label"] == "OI: Cboe (matches Alpaca, Oct 15)"
    assert all(c.oi_date == "2026-10-15" for c in cs)


def test_contract_missing_from_cboe_keeps_alpaca_oi():
    old = config.CBOE_OI_MIN_MATCH
    config.CBOE_OI_MIN_MATCH = 0.5
    try:
        cs = _chain()
        body = _payload({"QQQ261019C00725000": 6815, "QQQ261019P00720000": 6769})
        info = cboe.apply_oi("QQQ", cs, AS_OF, CAL, fetch=_fetch(body))
    finally:
        config.CBOE_OI_MIN_MATCH = old
    assert info["source"] == "cboe" and info["matched"] == 2
    assert cs[2].oi == 100 and cs[2].oi_source == "alpaca" and cs[2].oi_date == "2026-10-15"


def test_fallback_when_cboe_unavailable():
    cs = _chain()

    def boom(t):
        raise SourceError("Cboe HTTP 403", status=403)
    info = cboe.apply_oi("QQQ", cs, AS_OF, CAL, fetch=boom)
    assert info["source"] == "alpaca" and info["label"] == "OI: Alpaca (Oct 15)"
    assert "Cboe unavailable" in info["fallback_reason"]
    assert [c.oi for c in cs] == [2495, 8357, 100] and all(c.oi_source == "alpaca" for c in cs)


def test_fallback_when_cboe_lacks_the_chain_or_is_older():
    cs = _chain()
    info = cboe.apply_oi("QQQ", cs, AS_OF, CAL, fetch=_fetch(_payload({"QQQ261019C00725000": 6815})))
    assert info["source"] == "alpaca" and "Cboe lists 1 of 3" in info["fallback_reason"]
    assert cs[0].oi == 2495
    # A payload from Thu Oct 15 carries Oct 14 OI: older than Alpaca's Oct 15, so Alpaca wins.
    cs = _chain()
    body = _payload({"QQQ261019C00725000": 1, "QQQ261019P00720000": 2, "QQQ261019C00730000": 3},
                    ts="2026-10-15 19:00:00")
    info = cboe.apply_oi("QQQ", cs, AS_OF, CAL, fetch=_fetch(body))
    assert info["source"] == "alpaca" and "older than Alpaca" in info["fallback_reason"]
    assert [c.oi for c in cs] == [2495, 8357, 100]
    # Unreadable body
    cs = _chain()
    info = cboe.apply_oi("QQQ", cs, AS_OF, CAL, fetch=_fetch(b"not json"))
    assert info["source"] == "alpaca" and "unreadable" in info["fallback_reason"]


def test_alpaca_chain_with_overlay_end_to_end(tmp_path):
    body = _payload({"SPY261019C00100000": 5000, "SPY261019P00100000": 4000, "SPY261019C00105000": 0,
                     "SPY261019C00110000": 70}, ts="2026-10-16 14:00:00")
    overlay = lambda t, cs, at, cl, h: cboe.apply_oi(t, cs, at, cl, h, fetch=_fetch(body))
    with_cboe = F._source(tmp_path, F.make_handler()).fetch_chain("SPY", F.AS_OF, CAL, oi_overlay=overlay)
    F.A._CONTRACT_CACHE.clear()
    plain = F._source(tmp_path, F.make_handler()).fetch_chain("SPY", F.AS_OF, CAL)
    assert with_cboe.oi_source == "cboe" and plain.oi_source == "alpaca"
    assert with_cboe.oi_info["label"] == "OI: Cboe (prior session, inferred)"
    assert with_cboe.oi_date == "2026-10-15" and plain.oi_info["label"] == "OI: Alpaca (Oct 15)"
    by = {c.symbol: c for c in with_cboe.contracts}
    assert by["SPY261019C00100000"].oi == 5000 and by["SPY261019C00105000"].oi == 0
    assert by["SPY261019C00105000"].oi_basis == "reported"           # Cboe reports the zero; not a decoded null
    assert with_cboe.raw_hash != plain.raw_hash
    assert input_digest(with_cboe, CAL) != input_digest(plain, CAL)


def test_fallback_label_reaches_chain_warnings(tmp_path):
    def boom(t):
        raise SourceError("Cboe HTTP 503")
    overlay = lambda t, cs, at, cl, h: cboe.apply_oi(t, cs, at, cl, h, fetch=boom)
    ch = F._source(tmp_path, F.make_handler()).fetch_chain("SPY", F.AS_OF, CAL, oi_overlay=overlay)
    assert ch.oi_source == "alpaca" and ch.oi_info["fallback_reason"]
    assert any(w.startswith("OI: Alpaca (Oct 15): Cboe open interest not used") for w in ch.warnings)


def test_index_chain_is_cboe_oi():
    body = json.dumps({"timestamp": "2026-10-19 15:48:00", "data": {"current_price": 6700.0, "options": [
        {"option": "SPXW261019C06700000", "open_interest": 10, "iv": 0.2, "bid": 5, "ask": 6}]}}).encode()
    ch = cboe.parse_chain("SPX", body, AS_OF, CAL)
    assert ch.oi_source == "cboe" and ch.oi_info["label"] == "OI: Cboe (prior session, inferred)"
    assert ch.contracts[0].oi_source == "cboe"


# ----------------------------------------------------------------------------- digest, storage, scorecard

def _mini_chain(oi_source: str) -> Chain:
    c = _c("QQQ261019C00725000", 6815, "2026-10-16")
    c.oi_source = oi_source
    c.iv, c.iv_source, c.iv_ts = 0.2, "solved_mid", AS_OF
    return Chain(ticker="QQQ", as_of=AS_OF, spot=Spot(724.05, AS_OF, "alpaca_sip"), feed="opra", contracts=[c],
                 oi_date="2026-10-16", raw_hash="h", oi_source=oi_source,
                 oi_info={"source": oi_source, "basis": "inferred", "label": "x"})


def test_digest_covers_oi_source():
    a, b = _mini_chain("cboe"), _mini_chain("cboe")
    assert input_digest(a, CAL) == input_digest(b, CAL)
    assert input_digest(a, CAL) != input_digest(_mini_chain("alpaca"), CAL)
    before = input_digest(a, CAL)
    old = config.OI_SOURCE
    config.OI_SOURCE = "alpaca"                     # the policy is a model setting in the digest
    try:
        assert input_digest(a, CAL) != before
    finally:
        config.OI_SOURCE = old


def test_map_payload_exposes_oi_source():
    ch = _mini_chain("cboe")
    ch.oi_info.update(label="OI: Cboe (prior session, inferred)", alpaca_oi_date="2026-10-15")
    out = service._oi_source_payload(ch)
    assert out["kind"] == "cboe" and out["label"] == "OI: Cboe (prior session, inferred)"
    assert out["alpaca_oi_date"] == "2026-10-15"
    ch.oi_info = {}
    assert service._oi_source_payload(ch) is None
