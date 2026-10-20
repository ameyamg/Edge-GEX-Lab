"""Core endpoints in DEMO mode (synthetic fixtures, no network). Shapes per the v1 API contract."""
import re

import pytest
from fastapi.testclient import TestClient

from gexlab import config


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GEXLAB_DEMO", "1")
    from gexlab import api, service
    service._CHAINS.clear()
    service._LEVEL_CACHE.clear()
    return TestClient(api.app)


BANNED = re.compile(r"\b(buy|sell|bullish|bearish|signal|entry|should)\b", re.I)
DASHES = re.compile("[" + chr(0x2013) + chr(0x2014) + "]")   # en and em dash, built by code point


def test_health_and_tickers(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["demo"] is True and h["rate"]["cap"] == 60
    assert client.get("/api/tickers").json() == {"pinned": ["SPY", "QQQ", "SPX", "NDX"]}


def test_search(client):
    r = client.get("/api/search", params={"q": "nvd"}).json()
    assert r[0]["symbol"] == "NVDA" and r[0]["source"] == "alpaca"
    assert client.get("/api/search", params={"q": "nasdaq"}).json()[0]["symbol"] == "NDX"
    assert client.get("/api/search", params={"q": ""}).json() == []


def test_expirations(client):
    e = client.get("/api/expirations", params={"ticker": "SPX"}).json()
    assert e["window_days"] == 7
    dates = [x["date"] for x in e["expirations"]]
    assert dates == sorted(dates) and all(0 <= x["dte"] <= 7 for x in e["expirations"])
    assert {x["kind"] for x in e["expirations"]} <= {"daily", "weekly", "monthly"}
    # settles_at: the last settlement instant of the date, in UTC (PM 16:00 ET wins over AM 09:30 ET)
    for x in e["expirations"]:
        assert x["settles_at"].endswith("Z") and x["settles_at"][:10] == x["date"]
        assert x["settles_at"][11:16] in ({"20:00", "21:00", "17:00", "18:00"} if "PM" in x["settlements"]
                                          else {"13:30", "14:30"})


def test_map_coverage_reports_gate_1_2(client):
    c = client.get("/api/map", params={"ticker": "SPY"}).json()["coverage"]
    assert c["gate_version"] == "1.3" and c["gate_pct"] == 98.0 and c["oi_band_pct"] == 5
    assert c["gex_pct"] is not None and 0 <= c["gex_pct"] <= 100 and "oi_pct" in c


@pytest.mark.parametrize("ticker", ["SPY", "QQQ", "SPX", "NDX", "NVDA"])
def test_map_shape(client, ticker):
    m = client.get("/api/map", params={"ticker": ticker}).json()
    for k in ("ticker", "as_of", "model_version", "spot", "feed", "oi_date", "coverage", "signed", "strikes",
              "by_expiry", "totals", "levels", "profile_curve", "frozen_today", "lead", "warnings"):
        assert k in m, k
    assert m["feed"]["kind"] == "demo" and m["feed"]["label"] == "Synthetic fixture"
    assert 195 <= len(m["profile_curve"]) <= 205
    spot = m["spot"]["price"]
    assert all(spot * 0.9 - 1e-9 <= s["strike"] <= spot * 1.1 + 1e-9 for s in m["strikes"])
    assert all(s["put_gex"] <= 0 for s in m["strikes"])
    assert m["levels"]["regime"] in ("positive", "negative", "neutral")
    assert not BANNED.search(m["lead"]) and not DASHES.search(m["lead"])
    assert all(not DASHES.search(w) for w in m["warnings"])
    assert "Synthetic fixture" in m["warnings"]


def test_map_unsigned_and_expiry_filter(client):
    e = client.get("/api/expirations", params={"ticker": "SPY"}).json()["expirations"]
    first = e[0]["date"]
    m = client.get("/api/map", params={"ticker": "SPY", "expiries": first, "signed": "false"}).json()
    assert m["signed"] is False
    assert all(s["put_gex"] >= 0 for s in m["strikes"])
    assert [b["expiry"] for b in m["by_expiry"]] == [first]
    full = client.get("/api/map", params={"ticker": "SPY"}).json()
    assert m["coverage"]["eligible"] < full["coverage"]["eligible"]


def test_map_bad_input(client):
    assert client.get("/api/map", params={"ticker": "SPY", "expiries": "nope"}).status_code == 400
    assert client.get("/api/map", params={"ticker": "ZZZZ"}).status_code == 404


@pytest.mark.parametrize("ticker", ["SPY", "QQQ", "SPX", "NDX", "NVDA"])
def test_by_expiry_rows_reconcile_with_coverage(client, ticker):
    # by_expiry counts the same +-10% population as coverage, so the rows add up to it.
    m = client.get("/api/map", params={"ticker": ticker}).json()
    assert sum(r["contracts"] for r in m["by_expiry"]) == m["coverage"]["eligible"]
    assert sum(r["valid"] for r in m["by_expiry"]) == m["coverage"]["valid"]
    assert sum(r["net_gex"] for r in m["by_expiry"]) == pytest.approx(m["totals"]["net_gex"], rel=1e-9, abs=1e-6)


def test_map_lead_is_short_and_says_gamma_flip(client):
    m = client.get("/api/map", params={"ticker": "SPY"}).json()
    lead = m["lead"]
    assert re.fullmatch(r"SPY at [\d,.]+ sits (above|below|at) the gamma flip \([\d.,]+\) and "
                        r"(above|below|at) the call wall \([\d.,]+\)\. Built from \d+\.\d% of the chain "
                        r"within 10% of spot\.", lead), lead
    assert "zero OI" not in lead and "zero gamma" not in lead
