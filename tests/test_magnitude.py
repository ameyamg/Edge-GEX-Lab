"""Net GEX magnitude reference: ADV and ATR math, the daily caches, per-ATR scaling, gross and net to gross,
the percentile gate, no index volume reference, ATR% from the ETF for the indexes."""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from gexlab import config, magnitude as M
from gexlab.engine.calendar import TradingCalendar
from gexlab.sources.base import SourceError

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _clean():
    M.reset_cache()
    yield
    M.reset_cache()


def _bars(last: date, n: int, v=1_000_000.0, vw=500.0, partial_today: date | None = None):
    out, d = [], last
    while len(out) < n:
        if d.weekday() < 5:
            out.append({"t": f"{d.isoformat()}T04:00:00Z", "v": v, "vw": vw, "c": vw})
        d -= timedelta(days=1)
    if partial_today:
        out.append({"t": f"{partial_today.isoformat()}T04:00:00Z", "v": 1.0, "vw": 1.0})
    return list(reversed(out))


def _cal():
    return TradingCalendar.weekdays(date(2026, 6, 1), date(2026, 12, 31))


# ---------------------------------------------------------------- math

def test_adv_is_mean_dollar_volume_of_last_20_completed_sessions():
    last = date(2026, 10, 16)
    bars = _bars(last, 25)
    bars[-1] = {**bars[-1], "v": 3_000_000.0}          # the newest session trades 3x
    r = M.adv_from_bars(bars, last)
    assert r["sessions"] == 20 and r["last"] == "2026-10-16"
    assert r["adv_usd"] == pytest.approx((19 * 1e6 * 500 + 3e6 * 500) / 20)


def test_adv_ignores_a_partial_session_after_the_last_completed_one():
    last = date(2026, 10, 15)
    r = M.adv_from_bars(_bars(last, 20, partial_today=date(2026, 10, 16)), last)
    assert r["last"] == "2026-10-15" and r["adv_usd"] == pytest.approx(5e8)


def test_adv_null_with_reason_under_20_sessions():
    r = M.adv_from_bars(_bars(date(2026, 10, 16), 12), date(2026, 10, 16))
    assert r["adv_usd"] is None and "12 of 20" in r["reason"]


def test_percentile_mid_rank_and_null_under_20():
    hist = [float(i) for i in range(1, 21)]           # 20 values
    assert M.percentile_of(10.5, hist) == 50.0
    assert M.percentile_of(10.0, hist) == pytest.approx(47.5)   # 9 below, 1 tie counted half
    assert M.percentile_of(100.0, hist) == 100.0
    assert M.percentile_of(10.0, hist[:19]) is None
    assert M.percentile_of(1.0, []) is None


def test_hedge_for_move_is_linear():
    rows = M.hedge_for_move(2e9)
    assert [r["move_pct"] for r in rows] == [0.5, 1.0, 2.0]
    assert [r["usd"] for r in rows] == [1e9, 2e9, 4e9]


def test_last_completed_session():
    cal = _cal()
    fri = date(2026, 10, 16)
    assert M.last_completed_session(cal, datetime(2026, 10, 16, 15, 0, tzinfo=UTC)) == date(2026, 10, 15)  # 11:00 ET
    assert M.last_completed_session(cal, datetime(2026, 10, 16, 20, 30, tzinfo=UTC)) == fri                # 16:30 ET
    assert M.last_completed_session(cal, datetime(2026, 10, 18, 15, 0, tzinfo=UTC)) == fri                 # Sunday


# ---------------------------------------------------------------- the daily cache

def test_adv_fetched_once_per_completed_session_memory_then_disk(tmp_path):
    calls = []
    last = date(2026, 10, 15)

    def fetch(sym, start, end):
        calls.append((sym, start, end))
        return _bars(end, 25)

    path = tmp_path / "adv.json"
    a = M.get_adv("spy", last, fetch=fetch, cache_path=path)
    b = M.get_adv("SPY", last, fetch=fetch, cache_path=path)
    assert a["adv_usd"] == b["adv_usd"] == pytest.approx(5e8) and len(calls) == 1
    assert calls[0][2] == last and calls[0][1] == last - timedelta(days=config.ADV_LOOKBACK_DAYS)
    M.reset_cache()                                    # a restart: the disk copy answers
    c = M.get_adv("SPY", last, fetch=fetch, cache_path=path)
    assert c["adv_usd"] == pytest.approx(5e8) and len(calls) == 1
    M.get_adv("SPY", last + timedelta(days=1), fetch=fetch, cache_path=path)   # next session: refetch
    assert len(calls) == 2


def test_adv_failure_is_not_retried_until_the_retry_window(tmp_path):
    calls = []
    now = [1000.0]

    def fetch(sym, start, end):
        calls.append(sym)
        raise SourceError("HTTP 403", status=403)

    kw = dict(fetch=fetch, cache_path=tmp_path / "adv.json", clock=lambda: now[0])
    r = M.get_adv("SPY", date(2026, 10, 15), **kw)
    assert r["adv_usd"] is None and "HTTP 403" in r["reason"]
    M.get_adv("SPY", date(2026, 10, 15), **kw)
    assert len(calls) == 1
    now[0] += config.ADV_RETRY_S + 1
    M.get_adv("SPY", date(2026, 10, 15), **kw)
    assert len(calls) == 2


# ---------------------------------------------------------------- the payload

ATR_OK = {"atr_usd": 7.0, "last_close": 700.0, "atr_pct_of_close": 1.0, "sessions": 14,
          "first": "2026-09-25", "last": "2026-10-15", "source": "test"}


def _build(ticker, *, net=-1.27e9, window=None, spot=700.0, adv=3.25e10, hist=None, calls=None,
           atr=None, atr_calls=None, call_gex=None, put_gex=None):
    def atr_fn(s, d):
        if atr_calls is not None:
            atr_calls.append(s)
        return dict(ATR_OK) if atr is None else atr
    return M.build(ticker, net_signed=net, window_net_signed=net if window is None else window, spot=spot,
                   cal=_cal(), now=datetime(2026, 10, 16, 15, 0, tzinfo=UTC),
                   call_gex=call_gex, put_gex=put_gex,
                   adv_fn=lambda s, d: (calls.append(s) if calls is not None else None) or {
                       "adv_usd": adv, "sessions": 20, "first": "2026-09-17", "last": "2026-10-15",
                       "source": "test"},
                   atr_fn=atr_fn,
                   history_fn=lambda t, d: list(hist or []))


def test_build_own_adv():
    m = _build("SPY")
    assert m["adv_basis"] == "own" and m["adv_label"] == "SPY's average daily dollar volume" and "proxy" not in m
    assert m["hedge_per_1pct_usd"] == pytest.approx(1.27e9)
    assert m["pct_of_adv"] == pytest.approx(1.27e9 / 3.25e10 * 100)
    assert [r["usd"] for r in m["hedge_for_move"]] == pytest.approx([0.635e9, 1.27e9, 2.54e9])
    assert m["percentile"] is None and m["history_n"] == 0 and m["history_min"] == 20
    assert m["ticks_pct"] == [1, 2, 5, 10]


def test_build_percentile_after_20_captures_uses_the_window_net():
    hist = [float(x) * 1e8 for x in range(-10, 10)]   # 20 captures
    m = _build("SPY", net=-5e9, window=0.5e8, hist=hist)  # the window net, not the selection net, is ranked
    assert m["history_n"] == 20 and m["percentile"] == 55.0   # 11 of 20 captures (-1e9 .. 0) sit below 0.5e8
    m19 = _build("SPY", hist=hist[:19])
    assert m19["percentile"] is None and m19["history_n"] == 19


NO_REF = "No free volume reference: index hedging trades mostly in ES and NQ futures"


@pytest.mark.parametrize("index", ["SPX", "NDX"])
def test_index_has_no_volume_reference_and_fetches_none(index):
    calls = []
    m = _build(index, spot=7000.0, adv=3e10, net=-9e9, calls=calls)
    assert calls == []                                   # no SPY/QQQ (or index) ADV is fetched
    assert m["adv_usd"] is None and m["pct_of_adv"] is None and m["adv_sessions"] is None
    assert m["adv_window"] is None and m["adv_source"] is None and m["adv_reason"] is None
    assert m["adv_basis"] == "none" and m["adv_label"] == NO_REF and "proxy" not in m


@pytest.mark.parametrize("index", ["SPX", "NDX"])
def test_index_keeps_the_dollar_rows_and_the_percentile(index):
    hist = [float(x) * 1e9 for x in range(-10, 10)]
    m = _build(index, net=-9e9, window=0.5e9, hist=hist)
    assert m["hedge_per_1pct_usd"] == pytest.approx(9e9)
    assert [r["usd"] for r in m["hedge_for_move"]] == pytest.approx([4.5e9, 9e9, 18e9])
    assert m["percentile"] == 55.0 and m["history_n"] == 20


def test_no_reference_sentence_has_no_banned_words_or_dashes():
    import re
    banned = re.compile(r"\b(" + "|".join(config.BANNED_WORDS) + r")\b", re.I)
    assert not banned.search(NO_REF) and chr(0x2013) not in NO_REF and chr(0x2014) not in NO_REF


def test_no_adv_leaves_share_null_but_keeps_the_dollar_rows():
    m = M.build("TSLA", net_signed=-4.89e6, window_net_signed=-4.89e6, spot=350.0, cal=_cal(),
                now=datetime(2026, 10, 16, 15, 0, tzinfo=UTC),
                adv_fn=lambda s, d: {"adv_usd": None, "reason": "only 3 of 20 sessions have daily bars"},
                atr_fn=lambda s, d: dict(ATR_OK), history_fn=lambda t, d: [])
    assert m["adv_usd"] is None and m["pct_of_adv"] is None and "3 of 20" in m["adv_reason"]
    assert m["hedge_for_move"][1]["usd"] == pytest.approx(4.89e6)


def test_explainer_has_no_banned_words_or_dashes():
    import re
    banned = re.compile(r"\b(" + "|".join(config.BANNED_WORDS) + r")\b", re.I)
    for s in (M.EXPLAINER, M.MOVE_METHOD, M.PERCENTILE_BASIS, M.PER_ATR_METHOD, M.GROSS_NOTE, M.INDEX_NOTE,
              M.ATR_FROM_ETF_NOTE.format(index="SPX", etf="SPY")):
        assert not banned.search(s)
        assert chr(0x2013) not in s and chr(0x2014) not in s


# ---------------------------------------------------------------- ATR math

def _ohlc(last: date, n: int, h=101.0, lo=99.0, c=100.0, partial_today: date | None = None):
    out, d = [], last
    while len(out) < n:
        if d.weekday() < 5:
            out.append({"t": f"{d.isoformat()}T04:00:00Z", "o": c, "h": h, "l": lo, "c": c, "v": 1e6, "vw": c})
        d -= timedelta(days=1)
    out = list(reversed(out))
    if partial_today:
        out.append({"t": f"{partial_today.isoformat()}T04:00:00Z", "o": 1, "h": 500.0, "l": 1.0, "c": 1.0})
    return out


def test_atr_is_mean_true_range_of_last_14_sessions_with_gaps():
    last = date(2026, 10, 16)
    bars = _ohlc(last, 20)                              # every day: range 2, close 100
    bars[-1] = {**bars[-1], "h": 106.0, "l": 104.0, "c": 105.0}   # gap up: TR = 106 - 100 = 6, not 2
    r = M.atr_from_bars(bars, last)
    assert r["sessions"] == 14 and r["last"] == "2026-10-16"
    assert r["atr_usd"] == pytest.approx((13 * 2 + 6) / 14)
    assert r["last_close"] == 105.0
    assert r["atr_pct_of_close"] == pytest.approx((13 * 2 + 6) / 14 / 105.0 * 100)


def test_atr_gap_down_uses_the_prior_close():
    last = date(2026, 10, 16)
    bars = _ohlc(last, 15)
    bars[-1] = {**bars[-1], "h": 96.0, "l": 95.0, "c": 95.5}       # TR = 100 - 95 = 5
    assert M.atr_from_bars(bars, last)["atr_usd"] == pytest.approx((13 * 2 + 5) / 14)


def test_atr_ignores_a_partial_session_and_needs_15_bars():
    last = date(2026, 10, 15)
    r = M.atr_from_bars(_ohlc(last, 15, partial_today=date(2026, 10, 16)), last)
    assert r["atr_usd"] == pytest.approx(2.0) and r["last"] == "2026-10-15"
    short = M.atr_from_bars(_ohlc(last, 14), last)        # 14 bars give only 13 true ranges
    assert short["atr_usd"] is None and "13 of 14" in short["reason"]


# ---------------------------------------------------------------- ATR cache

def test_atr_fetched_once_per_session_split_adjusted_and_separate_from_adv(tmp_path, monkeypatch):
    seen = []

    def fake(sym, start, end, adjustment="raw"):
        seen.append((sym, adjustment))
        return _ohlc(end, 25)
    monkeypatch.setattr(M, "fetch_daily_bars", fake)
    last = date(2026, 10, 15)
    atr_path, adv_path = tmp_path / "atr.json", tmp_path / "adv.json"
    a = M.get_atr("tsla", last, cache_path=atr_path)
    b = M.get_atr("TSLA", last, cache_path=atr_path)
    assert a["atr_usd"] == b["atr_usd"] == pytest.approx(2.0) and seen == [("TSLA", "split")]
    M.get_adv("TSLA", last, cache_path=adv_path)           # ADV keeps raw bars and its own cache entry
    assert seen[-1] == ("TSLA", "raw") and len(seen) == 2
    M.reset_cache()                                         # restart: disk answers, no refetch
    assert M.get_atr("TSLA", last, cache_path=atr_path)["atr_usd"] == pytest.approx(2.0) and len(seen) == 2
    M.get_atr("TSLA", last + timedelta(days=1), cache_path=atr_path)   # next session: refetch
    assert len(seen) == 3


def test_atr_failure_waits_for_the_retry_window(tmp_path):
    calls, now = [], [1000.0]

    def fetch(sym, start, end):
        calls.append(sym)
        raise SourceError("HTTP 403", status=403)
    kw = dict(fetch=fetch, cache_path=tmp_path / "atr.json", clock=lambda: now[0])
    r = M.get_atr("NVDA", date(2026, 10, 15), **kw)
    assert r["atr_usd"] is None and "daily range unavailable" in r["reason"]
    M.get_atr("NVDA", date(2026, 10, 15), **kw)
    assert len(calls) == 1
    now[0] += config.ADV_RETRY_S + 1
    M.get_atr("NVDA", date(2026, 10, 15), **kw)
    assert len(calls) == 2


# ---------------------------------------------------------------- per ATR and gross in the payload

def test_per_atr_scales_the_hedge_by_atr_pct_of_spot():
    m = _build("TSLA", net=-2e9, spot=400.0, adv=4e10,
               atr={"atr_usd": 16.0, "last_close": 380.0, "atr_pct_of_close": 4.21, "sessions": 14,
                    "first": "2026-09-25", "last": "2026-10-15", "source": "test"})
    a = m["per_atr"]
    assert a["atr_pct"] == pytest.approx(4.0) and a["atr_pct_basis"] == "spot"   # 16 / 400, not / the close
    assert a["atr_basis"] == "own" and a["atr_label"] == "TSLA's 14-session ATR" and a["atr_note"] is None
    assert a["hedge_per_atr_usd"] == pytest.approx(8e9)
    assert m["pct_of_adv"] == pytest.approx(5.0) and a["pct_of_adv_per_atr"] == pytest.approx(20.0)
    assert a["atr_symbol"] == "TSLA" and a["atr_sessions"] == 14 and a["atr_reason"] is None


@pytest.mark.parametrize("index,etf", [("SPX", "SPY"), ("NDX", "QQQ")])
def test_index_per_atr_uses_the_etf_atr_pct_and_stays_dollars_only(index, etf):
    seen, adv_calls = [], []
    m = _build(index, net=-9e9, spot=7000.0, calls=adv_calls, atr_calls=seen,
               atr={"atr_usd": 7.7, "last_close": 700.0, "atr_pct_of_close": 1.1, "sessions": 14,
                    "first": "2026-09-25", "last": "2026-10-15", "source": "test"},
               call_gex=2e11, put_gex=-2.09e11)
    a = m["per_atr"]
    assert seen == [etf] and adv_calls == []                     # ATR from the ETF; still no ADV fetched
    assert a["atr_pct"] == pytest.approx(1.1) and a["atr_pct_basis"] == "etf_close"   # not 7.7 / 7000
    assert a["atr_basis"] == "etf" and a["atr_label"] == f"ATR% from {etf}" and a["atr_symbol"] == etf
    assert etf in a["atr_note"] and index in a["atr_note"]
    assert a["hedge_per_atr_usd"] == pytest.approx(9.9e9) and a["pct_of_adv_per_atr"] is None
    g = m["gross"]
    assert g["per_1pct_usd"] == pytest.approx(4.09e11) and g["pct_of_adv"] is None
    assert g["pct_of_adv_per_atr"] is None and g["hedge_per_atr_usd"] == pytest.approx(4.09e11 * 1.1)
    assert g["net_to_gross"] == pytest.approx(9e9 / 4.09e11)


def test_gross_is_unsigned_calls_plus_puts_and_net_to_gross():
    m = _build("SPY", net=1e9, adv=5e10, call_gex=3e9, put_gex=-2e9)
    g = m["gross"]
    assert g["per_1pct_usd"] == pytest.approx(5e9)
    assert g["pct_of_adv"] == pytest.approx(10.0)
    assert g["net_to_gross"] == pytest.approx(0.2)
    assert g["hedge_per_atr_usd"] == pytest.approx(5e9) and g["pct_of_adv_per_atr"] == pytest.approx(10.0)  # ATR 1%
    assert m["gross"]["note"] == M.GROSS_NOTE


def test_gross_null_without_side_totals_and_ratio_null_at_zero():
    assert _build("SPY")["gross"] is None
    assert _build("SPY", net=0.0, call_gex=0.0, put_gex=0.0)["gross"]["net_to_gross"] is None


def test_atr_unavailable_leaves_per_atr_null_with_reason():
    m = _build("NVDA", atr={"atr_usd": None, "reason": "only 9 of 14 sessions have a daily range"},
               call_gex=1e8, put_gex=-5e7)
    a = m["per_atr"]
    assert a["atr_pct"] is None and a["hedge_per_atr_usd"] is None and a["pct_of_adv_per_atr"] is None
    assert a["atr_label"] is None and "9 of 14" in a["atr_reason"]
    assert m["gross"]["hedge_per_atr_usd"] is None and m["gross"]["pct_of_adv"] is not None
    assert m["pct_of_adv"] is not None                          # the per 1% view is untouched


# ---------------------------------------------------------------- DEMO API

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GEXLAB_DEMO", "1")
    from gexlab import api, service
    service._CHAINS.clear()
    service._LEVEL_CACHE.clear()
    return TestClient(api.app)


@pytest.mark.parametrize("ticker", ["SPY", "QQQ", "SPX", "NDX", "NVDA"])
def test_demo_map_carries_magnitude(client, ticker):
    m = client.get("/api/map", params={"ticker": ticker}).json()
    g = m["magnitude"]
    assert g["hedge_per_1pct_usd"] == pytest.approx(abs(m["totals"]["net_gex"]))
    if ticker in ("SPX", "NDX"):
        assert g["adv_basis"] == "none" and g["adv_usd"] is None and g["pct_of_adv"] is None
        assert g["adv_label"] == NO_REF
    else:
        assert g["adv_basis"] == "own"
        assert g["pct_of_adv"] == pytest.approx(g["hedge_per_1pct_usd"] / g["adv_usd"] * 100)
    a, gr, t = g["per_atr"], g["gross"], m["totals"]
    assert a["atr_pct"] > 0 and a["hedge_per_atr_usd"] == pytest.approx(g["hedge_per_1pct_usd"] * a["atr_pct"])
    assert gr["per_1pct_usd"] == pytest.approx(abs(t["call_gex"]) + abs(t["put_gex"]))
    assert gr["net_to_gross"] == pytest.approx(abs(t["net_gex"]) / gr["per_1pct_usd"])
    if ticker in ("SPX", "NDX"):
        assert a["atr_label"] == f"ATR% from {'SPY' if ticker == 'SPX' else 'QQQ'}" and a["atr_pct_basis"] == "etf_close"
        assert a["pct_of_adv_per_atr"] is None and gr["pct_of_adv"] is None
    else:
        assert a["atr_basis"] == "own" and a["atr_pct_basis"] == "spot"
        assert a["pct_of_adv_per_atr"] == pytest.approx(g["pct_of_adv"] * a["atr_pct"])
        assert gr["pct_of_adv"] == pytest.approx(gr["per_1pct_usd"] / g["adv_usd"] * 100)
    if ticker in ("SPY", "QQQ", "SPX", "NDX"):
        assert g["history_n"] == 18 and g["percentile"] is None       # fixture history: 18 eligible before Oct 16
    else:
        assert g["history_n"] == 0


def test_unsigned_view_keeps_the_signed_magnitude(client):
    s = client.get("/api/map", params={"ticker": "SPY", "signed": "true"}).json()["magnitude"]
    u = client.get("/api/map", params={"ticker": "SPY", "signed": "false"}).json()["magnitude"]
    assert s["hedge_per_1pct_usd"] == pytest.approx(u["hedge_per_1pct_usd"])


def test_map_survives_a_magnitude_failure(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(M, "build", boom)
    m = client.get("/api/map", params={"ticker": "SPY"}).json()
    assert m["magnitude"] is None and m["totals"]["net_gex"]
