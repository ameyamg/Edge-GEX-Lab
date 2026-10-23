"""Trade guidance: determinism, grounding (every price is a map level or a stated computation),
scenarios per regime, the narrative validator, the template fallback and the endpoints (on by
default, demo mode included, no network)."""
from __future__ import annotations

import copy
import json
import math
import re
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gexlab import config
from gexlab.guidance import ai, build, template, validate
from gexlab.guidance import api as gapi
from gexlab import llm

FIX = Path(__file__).resolve().parent.parent / "fixtures"
TICKERS = ["SPY", "QQQ", "SPX", "NDX", "NVDA"]


def fmap(t: str) -> dict:
    return json.loads((FIX / f"map_{t}.json").read_text(encoding="utf-8"))


def g_of(t: str, **kw) -> dict:
    return build.build(fmap(t), **kw)


def trend_map() -> dict:
    """QQQ fixture (negative regime) with a net to gross above the mixed cut: a trend map."""
    m = fmap("QQQ")
    m["magnitude"]["gross"]["net_to_gross"] = 0.4123
    return m


# ----------------------------------------------------------------------------- determinism and grounding

@pytest.mark.parametrize("t", TICKERS)
def test_deterministic_and_serializable(t):
    a, b = g_of(t), g_of(t)
    assert a == b
    assert json.loads(json.dumps(a)) == a


@pytest.mark.parametrize("t", TICKERS)
def test_every_price_is_a_map_level_or_a_stated_computation(t):
    m = fmap(t)
    g = build.build(m)
    levels = build.map_prices(m)
    buf = g["atr"]["buffer"] if g["atr"] else None
    zw = g["atr"]["fade_zone"] if g["atr"] else None
    for lv in g["levels"]:
        assert lv["price"] in levels, lv
        assert lv["distance"] == round(lv["price"] - g["spot"], 2)
    offsets = [d for d in (buf, zw) if d]
    for p in build.guidance_prices(g):
        ok = p in levels or any(abs(p - (a + s * d)) < 0.006 for a in levels for s in (1, -1) for d in offsets)
        assert ok, f"{t}: {p} is neither a map level nor a level +- buffer {buf} or fade zone {zw}"
    for s in g["scenarios"]:                               # targets are map levels only, never spot
        for x in s["targets"]:
            assert x["price"] in levels and x["price"] != g["spot"], s
    if buf:
        atr = next(c for c in g["computations"] if c["name"] == "ATR")["value"]
        if build.is_index(t):     # whole index points, rounded up so the stop is never inside 0.1 ATR
            assert buf == max(1, math.ceil(build.ATR_BUFFER_MULT * atr - 1e-9))
        else:
            assert buf == round(build.ATR_BUFFER_MULT * atr, 2)
        assert "0.1 x ATR" in g["atr"]["buffer_formula"]


@pytest.mark.parametrize("t", TICKERS)
def test_scenario_shape_and_direction(t):
    g = g_of(t)
    assert len(g["scenarios"]) <= build.MAX_SCENARIOS
    assert g["scenarios"] or g["setup_notes"]              # no scenario is always explained
    for s in g["scenarios"]:
        assert s["bias"] in ("long", "short", "flat")
        assert s["trigger"] and s["why"]
        if s["bias"] == "flat":
            assert s["entry"] is None and s["stop"] is None and s["targets"] == []
            continue
        e, stop, tg = s["entry"], s["stop"]["price"], [x["price"] for x in s["targets"]]
        assert tg, s
        gap = g["target_gap"]
        if s["bias"] == "short":
            assert stop > e["high"] and all(x <= e["low"] - gap + 1e-9 for x in tg), s
        else:
            assert stop < e["low"] and all(x >= e["high"] + gap - 1e-9 for x in tg), s
        assert s["rr"] is not None and s["rr"] >= 1.0 and build.rr_of(s) >= config.GUIDANCE_MIN_RR, s


def test_positive_regime_fades_the_walls():
    g = g_of("SPY")
    assert g["environment"]["kind"] == "mean_reversion"
    ids = {s["id"]: s for s in g["scenarios"]}
    assert ids["fade_call"]["bias"] == "short" and ids["fade_put"]["bias"] == "long"
    assert ids["fade_call"]["entry"]["high"] == 705.0 and ids["fade_put"]["entry"]["low"] == 695.0
    assert 697.93 in [x["price"] for x in ids["fade_call"]["targets"]]       # back toward the flip
    assert "flip_loss" in ids
    # the fade zone hugs the wall: 0.25 x ATR 7.7 = 1.93
    assert ids["fade_call"]["entry"]["low"] == 703.07 and ids["fade_put"]["entry"]["high"] == 696.93
    assert all(s["rr"] >= 1 for s in ids.values() if s["bias"] != "flat")


def test_negative_regime_continuation():
    g = build.build(trend_map())
    assert g["environment"]["kind"] == "trend"
    ids = {s["id"]: s for s in g["scenarios"]}
    assert ids["breakout"]["bias"] == "long"
    assert ids["flip_cross"]["bias"] == "flat"
    assert all(s["conviction"] == "normal" for s in g["scenarios"])
    bo = ids["breakout"]                                   # trigger to trigger + 0.1 ATR, stop 0.1 ATR under it
    assert (bo["entry"]["low"], bo["entry"]["high"], bo["stop"]["price"]) == (612.0, 612.86, 611.14)
    assert bo["targets"][0]["price"] == 616.79 and bo["rr"] >= 1
    assert "breakdown" not in ids and any("break below the put wall" in n for n in g["setup_notes"])


def test_mixed_when_net_to_gross_low_or_size_small():
    q = g_of("QQQ")                                        # net to gross 7.76%
    assert q["environment"]["kind"] == "mixed" and "net to gross is 7.76%" in q["environment"]["why"]
    assert q["scenarios"] == []                            # low influence: no trade ideas at all
    assert q["setup_notes"] == ["GEX influence is low for QQQ today (net to gross is 7.76%, calls and puts largely "
                                "offset): levels are for reference only."]
    n = g_of("NVDA")                                       # 0.4% of ADV, below the 1% tick
    assert n["environment"]["kind"] == "mixed" and "below the 1% tick" in n["environment"]["why"]
    assert n["scenarios"] == [] and n["setup_notes"] == [
        "GEX influence is low for NVDA today (hedging is 0.4% of daily dollar volume per 1% move): levels are for "
        "reference only."]
    assert n["levels"] and n["regime_change"]              # levels to watch and what changes it still shown


def test_no_atr_stops_fall_back_to_map_levels():
    m = fmap("SPY")
    m["magnitude"]["per_atr"] = {"atr_basis": None, "atr_usd": None, "atr_pct": None}
    g = build.build(m)
    assert g["atr"] is None and not g["computations"] or all(c["name"] != "buffer" for c in g["computations"])
    levels = build.map_prices(m)
    assert g["target_gap"] == round(0.005 * g["spot"], 2)  # 0.5% of spot without an ATR
    for s in g["scenarios"]:
        if s["stop"]:
            assert s["stop"]["price"] in levels and s["stop"]["basis"].startswith("next level")
        if s["entry"]:
            assert s["entry"]["low"] == s["entry"]["high"] and s["entry"]["low"] in levels


def test_index_atr_from_etf_is_a_stated_computation():
    g = g_of("SPX")
    atr = next(c for c in g["computations"] if c["name"] == "ATR")
    assert "x spot" in atr["formula"] and "/ 100" in atr["formula"]
    assert "no daily volume reference" in g["regime"]["size_text"]


def _all_prices(g: dict) -> list[float]:
    out = [g["spot"], g["target_gap"]] + build.guidance_prices(g) + [lv["distance"] for lv in g["levels"]]
    if g["atr"]:
        out += [g["atr"]["value"], g["atr"]["buffer"], g["atr"]["fade_zone"]]
    return out


def spx_live_like() -> dict:
    """SPX with off-grid numbers seen on screen: spot 7,659.44, gamma flip 7,766.39."""
    m = fmap("SPX")
    m["spot"]["price"] = 7659.44
    m["levels"]["zero_gamma"] = [7766.39]
    m["levels"]["nearest_zero_gamma"] = 7766.39
    m["frozen_today"]["nearest_zero_gamma"] = 7760.61
    return m


def influential(m: dict) -> dict:
    """The same map with a net to gross above the mixed cut, so scenarios are built."""
    m = copy.deepcopy(m)
    m["magnitude"]["gross"]["net_to_gross"] = 0.4123
    return m


@pytest.mark.parametrize("m", [fmap("SPX"), fmap("NDX"), spx_live_like(), influential(fmap("SPX")),
                               influential(fmap("NDX")), influential(spx_live_like())],
                         ids=["SPX", "NDX", "SPX-live", "SPX-scen", "NDX-scen", "SPX-live-scen"])
def test_index_prices_are_whole_points(m):
    g = build.build(m, zero_dte={"expiry": "2026-10-16", "minutes_to_close": 360})
    if m["magnitude"]["gross"]["net_to_gross"] == 0.4123:
        assert any(s["bias"] != "flat" for s in g["scenarios"]), g["setup_notes"]
        assert g["atr"]["fade_zone"] == int(g["atr"]["fade_zone"]) and g["target_gap"] == int(g["target_gap"])
    for p in _all_prices(g):
        assert p == int(p), f"{g['ticker']}: price {p} is not a whole index point"
    # non-price numbers keep their decimals
    assert g["regime"]["net_to_gross_pct"] != int(g["regime"]["net_to_gross_pct"])
    assert any(lv["distance_pct"] not in (None, int(lv["distance_pct"])) for lv in g["levels"])
    text = template.render(g)
    assert not re.search(r"\d,\d{3}\.\d", text), text          # no index level with decimals
    assert validate.validate(text, g) == [], text
    every_string = json.dumps(g)
    assert not re.search(r"\d,\d{3}\.\d", every_string)


def test_spx_live_numbers_render_whole():
    g = build.build(spx_live_like())
    assert g["spot"] == 7659.0
    flip = next(lv for lv in g["levels"] if lv["key"] == "gamma_flip")
    assert flip["price"] == 7766.0 and flip["distance"] == 107.0
    assert g["regime"]["summary"] == "SPX at 7,659 is in positive gamma, 107 below the gamma flip at 7,766."
    assert any("moved from 7,761 at 10:00 to 7,766 now" in c for c in g["regime_change"])
    text = template.render(g)
    assert "7,766.39" not in text and "7,659.44" not in text and "7,766" in text
    # the AI validator now rejects the unrounded figure: it is no longer a number the object states
    bad = validate.validate(text.replace("7,766", "7,766.39", 1), g)
    assert any("number not in the guidance: 7,766.39" in r for r in bad), bad
    # the build stays grounded in the whole-point map prices
    assert 7766.0 in build.map_prices(spx_live_like()) and 7659.0 in build.map_prices(spx_live_like())


def test_index_half_point_rounds_like_the_web():
    m = fmap("SPX")                                        # fixture spot is 7,046.50
    assert build.build(m)["spot"] == 7047.0                # half away from zero, as toLocaleString shows it


def test_stop_rounds_away_from_the_trigger():
    assert build.rp_away(7700.3, "up", True) == 7701.0     # short stop sits above: ceil
    assert build.rp_away(7700.7, "down", True) == 7700.0   # long stop sits below: floor
    assert build.rp_away(7701.0, "up", True) == 7701.0 and build.rp_away(7700.0, "down", True) == 7700.0
    assert build.rp_away(705.774, "up") == 705.77          # SPY and stocks keep cents
    m = influential(fmap("NDX"))
    m["magnitude"]["per_atr"] = {"atr_basis": "own", "atr_usd": 352.4, "atr_label": "test ATR"}   # raw buffer 35.2
    g = build.build(m)
    assert g["atr"]["value"] == 352.0 and g["atr"]["buffer"] == 36.0      # rounded up, never down to 35
    assert "rounded up to a whole index point" in g["atr"]["buffer_formula"]
    raw_buf = build.ATR_BUFFER_MULT * g["atr"]["value"]
    traded = [s for s in g["scenarios"] if s["bias"] != "flat"]
    assert traded
    for s in traded:
        e, stop = s["entry"], s["stop"]["price"]
        assert stop == int(stop)
        if s["bias"] == "short":                           # trigger is the entry high
            assert stop > e["high"] and stop - e["high"] >= raw_buf
        else:                                              # trigger is the entry low
            assert stop < e["low"] and e["low"] - stop >= raw_buf


def test_regime_change_and_confidence():
    g = g_of("SPY", zero_dte={"expiry": "2026-10-16", "minutes_to_close": 360})
    txt = " ".join(g["regime_change"])
    for frag in ("gamma flip at 697.93", "refreshes every 60 s", "0DTE expiry at the close (360 min left)",
                 "Next-day OI update", "below the 10% tick", "stale feed"):
        assert frag in txt
    share = next(c for c in g["computations"] if c["name"] == "0DTE share")["value"]
    rows = fmap("SPY")["by_expiry"]
    assert share == round(abs(rows[0]["net_gex"]) / sum(abs(r["net_gex"]) for r in rows) * 100, 2)
    conf = " ".join(g["confidence"])
    assert "Synthetic fixture" in conf and "one session behind" in conf and "Sign assumption" in conf
    assert "Untested" in conf


def test_frozen_shift_reported():
    m = fmap("SPY")
    m["frozen_today"]["call_wall"] = 710.0
    g = build.build(m)
    assert any(lv["label"] == "10:00 call wall" and lv["price"] == 710.0 for lv in g["levels"])
    assert any("moved from 710 at 10:00 to 705" in c for c in g["regime_change"])
    for s in g["scenarios"]:                               # a frozen level is never a trigger or a stop
        assert "10:00" not in s["trigger"] and "10:00" not in ((s["stop"] or {}).get("basis") or "")


def test_frozen_level_can_be_a_target():
    m = fmap("SPY")
    m["frozen_today"]["put_wall"] = 699.0                  # 10:00 put wall between spot and the flip
    g = build.build(m)
    fc = next(s for s in g["scenarios"] if s["id"] == "fade_call")
    assert [x["price"] for x in fc["targets"]] == [700.0, 699.0]
    assert fc["targets"][1]["label"] == "10:00 put wall"


def test_status_chip_and_footnote():
    g = g_of("SPY")
    assert g["status"]["chip"] == build.CHIP_UNTESTED
    assert g["status"]["footnote"] == "For education only. Not financial advice."
    # A passed Scorecard measures the levels, never these setups: the chip stays Untested (day-1 audit).
    g2 = g_of("SPY", scorecard={"passed": True, "lead": "Measured lead."})
    assert not g2["status"]["tested"] and g2["status"]["chip"] == build.CHIP_UNTESTED
    assert g2["status"]["scorecard_passed"] and g2["status"]["scorecard_lead"] == "Measured lead."
    assert any("not these entry, stop and target rules" in c for c in g2["confidence"])


# ----------------------------------------------------------------------------- template and validator

@pytest.mark.parametrize("t", TICKERS)
def test_template_passes_its_own_validator(t):
    g = g_of(t, zero_dte={"expiry": "2026-10-16", "minutes_to_close": 360})
    text = template.render(g)
    assert validate.validate(text, g) == [], text
    assert 4 <= len(validate.sentences(text)) <= 7
    assert "\u2014" not in text and "\u2013" not in text


GOOD = ("SPY at 702.35 is in positive gamma, 4.42 above the gamma flip at 697.93. The bias is to fade the call wall "
        "at 705 short from 703.07 with a stop at 705.77 and a target at 702. The put wall at 695 is the long side, "
        "stop 694.23. Net GEX is 16.52% of daily volume. A cross below 697.93 changes the picture.")


def test_validator_accepts_grounded_text():
    assert validate.validate(GOOD, g_of("SPY")) == []


@pytest.mark.parametrize("bad,why", [
    (GOOD.replace("705.77", "706.5"), "number not in the guidance: 706.5"),
    (GOOD.replace("16.52%", "21%"), "percentage not in the guidance"),
    (GOOD + " QQQ agrees.", "ticker or acronym not in the guidance: QQQ"),
    (GOOD + " This trade is guaranteed.", "promise"),
    (GOOD + " It is a sure thing.", "promise"),
    (GOOD + " You can't lose here.", "promise"),
    (GOOD.replace(". The put", " \u2014 the put"), "dash character"),
    ("SPY is in positive gamma. Fade 705.", "too short"),
    (GOOD + " Check again at 11:30.", "time not in the guidance"),
    (GOOD + " Size is twelve times normal.", "number word not in the guidance"),
])
def test_validator_rejects(bad, why):
    rs = validate.validate(bad, g_of("SPY"))
    assert any(why in r for r in rs), rs


# ----------------------------------------------------------------------------- AI narrative with a fake runner

def test_ai_accepts_valid_text_and_caches():
    ai.reset_cache()
    g = g_of("SPY")
    calls = []

    def runner(prompt):
        calls.append(prompt)
        assert "<untrusted_data>" in prompt and "computations" not in prompt
        return GOOD, "claude"

    r1 = ai.narrate(g, True, runner)
    r2 = ai.narrate(g, True, runner)
    assert r1["source"] == "ai" and r1["validated"] and not r1["cached"]
    assert r2["cached"] and len(calls) == 1


def test_ai_rejected_text_falls_back_to_template():
    ai.reset_cache()
    g = g_of("SPY")
    r = ai.generate(g, lambda p: (GOOD + " Guaranteed winner at 710.", "claude"))
    assert r["source"] == "template" and r["text"] == template.render(g)
    assert any("promise" in x for x in r["rejected"]) and any("710" in x for x in r["rejected"])


@pytest.mark.parametrize("exc", [llm.CLIUnavailable("no claude"), subprocess.TimeoutExpired("claude", 90),
                                 OSError("boom")])
def test_ai_failures_fall_back(exc):
    def runner(p):
        raise exc
    r = ai.generate(g_of("SPY"), runner)
    assert r["source"] == "template" and r["rejected"] and r["validated"]


def test_cli_is_tool_free_and_keyless(monkeypatch):
    cmd = ai.claude_cmd("claude")
    assert cmd[cmd.index("--tools") + 1] == ""
    assert '{"mcpServers":{}}' in cmd and "--safe-mode" in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == ai.SYSTEM_PROMPT
    assert ai.TIMEOUT_S == 90
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert "ANTHROPIC_API_KEY" not in llm.child_env()


# ----------------------------------------------------------------------------- endpoints (demo, no network)

def client() -> TestClient:
    app = FastAPI()
    app.include_router(gapi.router)
    return TestClient(app)


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv("GEXLAB_DEMO", "1")


def test_guidance_endpoint_on_by_default_in_demo(demo):
    r = client().get("/api/guidance", params={"ticker": "SPY"})
    assert r.status_code == 200
    j = r.json()
    assert j["synthetic"] is True and j["feed"]["label"] == "Synthetic fixture"
    assert j["status"]["chip"] == build.CHIP_UNTESTED
    assert j["narrative"]["source"] == "template" and j["narrative"]["validated"]
    assert j["zero_dte"] == {"expiry": "2026-10-16", "minutes_to_close": 360, "close_et": "16:00"}


def test_guidance_endpoint_expiry_subset_and_bad_input(demo):
    c = client()
    j = c.get("/api/guidance", params={"ticker": "SPY", "expiries": "2026-10-19", "range": 2}).json()
    assert j["expiries"] == ["2026-10-19"] and j["zero_dte"] is None and j["range_pct"] == 2
    assert c.get("/api/guidance", params={"ticker": "SPY", "expiries": "oct19"}).status_code == 400
    assert c.get("/api/guidance", params={"ticker": "SP Y!"}).status_code == 400


def test_narrate_endpoint_demo_serves_template_without_cli(demo, monkeypatch):
    monkeypatch.setattr(ai, "run_cli", lambda *a, **k: pytest.fail("demo mode must not call the CLI"))
    r = client().post("/api/guidance/narrate", json={"ticker": "SPY"})
    assert r.status_code == 200
    j = r.json()
    assert j["source"] == "template" and j["footnote"] == "For education only. Not financial advice."
    assert any("demo mode" in x for x in j["rejected"])


def test_core_app_includes_the_guidance_router(demo):
    from gexlab.api import app
    assert TestClient(app, base_url="http://127.0.0.1").get("/api/guidance", params={"ticker": "QQQ"}).status_code == 200


# ----------------------------------------------------------------------------- targets, fade zones, reward to risk

def synth(spot=100.0, call_wall=110.0, put_wall=95.0, flip=90.0, regime="positive", pct_of_adv=5.0, ntg=0.4,
          atr=4.0, strikes=(), ticker="ZZZ") -> dict:
    """A minimal map payload: walls, one flip, optional strike clusters, an own ATR."""
    return {
        "ticker": ticker, "as_of": "2026-10-19T15:00:00Z", "spot": {"price": spot},
        "levels": {"call_wall": call_wall, "put_wall": put_wall, "zero_gamma": [flip] if flip else [],
                   "nearest_zero_gamma": flip, "regime": regime},
        "totals": {"net_gex": 2e8 if regime == "positive" else -2e8},
        "magnitude": {"pct_of_adv": pct_of_adv, "ticks_pct": [1, 2, 5, 10],
                      "per_atr": {"atr_basis": "own", "atr_usd": atr, "atr_label": f"{ticker}'s 14-session ATR"}
                      if atr else {"atr_basis": None},
                      "gross": {"net_to_gross": ntg}},
        "strikes": [{"strike": k, "net_gex": v} for k, v in strikes],
        "feed": {"kind": "live", "label": "test feed"}, "by_expiry": [], "frozen_today": None,
    }


def mu_case(**kw) -> dict:
    """An MU screen: spot 925.54, put wall 920, positive gamma, ATR 47.3 (buffer 4.73), 0.4% of ADV."""
    args = dict(spot=925.54, call_wall=950.0, put_wall=920.0, flip=905.0, pct_of_adv=0.4, ntg=0.3, atr=47.3,
                ticker="MU")
    args.update(kw)
    return synth(**args)


def test_mu_low_influence_gives_no_scenarios():
    g = build.build(mu_case())
    assert g["environment"]["kind"] == "mixed"
    assert g["scenarios"] == []
    assert g["setup_notes"] == ["GEX influence is low for MU today (hedging is 0.4% of daily dollar volume per 1% "
                                "move): levels are for reference only."]
    assert any(lv["key"] == "put_wall" and lv["price"] == 920.0 for lv in g["levels"])
    assert g["regime_change"]
    text = template.render(g)
    assert validate.validate(text, g) == [], text


def test_mu_map_with_real_influence_hugs_the_wall_and_never_targets_spot():
    g = build.build(mu_case(pct_of_adv=5.0))
    assert g["environment"]["kind"] == "mean_reversion"
    assert g["atr"]["fade_zone"] == 11.82 and g["target_gap"] == 11.82
    fp_ = next(s for s in g["scenarios"] if s["id"] == "fade_put")
    # 920 + 11.82 = 931.82 is past spot, so the zone stops at spot
    assert (fp_["entry"]["low"], fp_["entry"]["high"]) == (920.0, 925.54)
    assert fp_["stop"]["price"] == 915.27
    assert [x["price"] for x in fp_["targets"]] == [950.0]                   # the call wall, not spot
    assert fp_["rr"] == round((950.0 - 925.54) / (925.54 - 915.27), 1)
    for s in g["scenarios"]:
        assert all(x["price"] != g["spot"] for x in s["targets"])


def test_positive_gamma_fade_has_a_level_target_and_rr_at_least_one():
    g = build.build(synth(strikes=[(103.0, 5e7)]))
    assert g["environment"]["kind"] == "mean_reversion"
    ids = {s["id"]: s for s in g["scenarios"]}
    fc, fpu = ids["fade_call"], ids["fade_put"]
    # zone 0.25 x ATR 4 = 1: call 109 to 110, put 95 to 96; stops 0.1 ATR beyond the wall
    assert (fc["entry"]["low"], fc["entry"]["high"], fc["stop"]["price"]) == (109.0, 110.0, 110.4)
    assert (fpu["entry"]["low"], fpu["entry"]["high"], fpu["stop"]["price"]) == (95.0, 96.0, 94.6)
    assert [x["price"] for x in fc["targets"]] == [103.0, 95.0]           # nearest level, then the next one
    assert [x["price"] for x in fpu["targets"]] == [103.0, 110.0]
    assert fc["rr"] == round((109.0 - 103.0) / (110.4 - 109.0), 1) and fc["rr"] >= 1
    assert "R:R" not in fc["rr_basis"] and "entry edge 109" in fc["rr_basis"]
    assert validate.validate(template.render(g), g) == []


def test_reward_smaller_than_risk_drops_the_scenario_with_a_line():
    g = build.build(synth(strikes=[(97.1, 5e7)]))           # put fade: zone 95 to 96, T1 97.1, stop 94.6
    ids = {s["id"] for s in g["scenarios"]}
    assert "fade_put" not in ids and "fade_call" in ids
    assert "No setup at the put wall: reward smaller than risk (0.8 to 1)" in g["setup_notes"]


def test_target_closer_than_the_gap_is_skipped():
    g = build.build(synth(strikes=[(96.5, 5e7)]))           # 96.5 is 0.5 above the zone top: under the 1.0 gap
    fpu = next(s for s in g["scenarios"] if s["id"] == "fade_put")
    assert fpu["targets"][0]["price"] == 110.0


def test_a_cluster_at_spot_is_never_a_target():
    g = build.build(synth(strikes=[(100.0, 9e7)]))
    for s in g["scenarios"]:
        assert all(x["price"] != 100.0 for x in s["targets"]), s


def test_no_surviving_scenario_says_so():
    g = build.build(synth(call_wall=101.0, put_wall=99.0, flip=None, strikes=[]))   # walls 1 ATR/4 from spot
    assert g["scenarios"] == []
    assert g["setup_notes"][0] == "No trade setups from this map right now"
    assert g["levels"] and g["regime_change"]
    assert validate.validate(template.render(g), g) == []


def test_negative_gamma_breakouts_still_built():
    g = build.build(synth(regime="negative", flip=120.0, strikes=[(90.0, -5e7)]), range_pct=10)
    assert g["environment"]["kind"] == "trend"
    ids = {s["id"]: s for s in g["scenarios"]}
    bo, bd = ids["breakout"], ids["breakdown"]
    assert (bo["entry"]["low"], bo["entry"]["high"], bo["stop"]["price"]) == (110.0, 110.4, 109.6)
    assert bo["targets"][0]["price"] == 120.0 and bo["rr"] >= 1          # the gamma flip
    assert (bd["entry"]["low"], bd["entry"]["high"], bd["stop"]["price"]) == (94.6, 95.0, 95.4)
    assert bd["targets"][0]["price"] == 90.0 and bd["rr"] >= 1
    assert validate.validate(template.render(g), g) == []


# ----------------------------------------------------------------------------- day-1 audit (gate 1.3 batch)

def test_invalid_frozen_capture_is_never_a_target():
    m = fmap("SPY")
    m["frozen_today"]["put_wall"] = 699.0
    m["frozen_today"]["status"] = "invalid"
    g = build.build(m)
    assert all(not lv.get("frozen") for lv in g["levels"])
    assert all(t["price"] != 699.0 for s in g["scenarios"] for t in s["targets"])
    assert any("10:00 capture is invalid" in c for c in g["confidence"])


def test_frozen_levels_reference_only_when_the_view_is_not_all_expiries():
    m = fmap("SPY")
    m["frozen_today"]["put_wall"] = 699.0
    m["expiry_selection"] = {"all": False, "selected": 1, "available": 5}
    g = build.build(m)
    ref = [lv for lv in g["levels"] if lv.get("frozen")]
    assert ref and all(lv["reference_only"] and "(all expiries)" in lv["label"] for lv in ref)
    assert all(t["price"] != 699.0 for s in g["scenarios"] for t in s["targets"])
    assert not any("moved from" in c for c in g["regime_change"])
    assert any("reference only" in c for c in g["confidence"])


@pytest.mark.parametrize("patch, needle", [
    (lambda m: m["feed"].__setitem__("stale", True), "the feed is stale"),
    (lambda m: m["coverage"].update(gex_pct=96.5, gate_pct=98.0, eligible=100), "is usable"),
    (lambda m: m.__setitem__("oi_freshness", {"expected": "2026-10-15", "stale_pct": 40.0, "fresh": False}),
     "open interest is not from"),
])
def test_no_setups_from_a_stale_or_failed_map(patch, needle):
    m = fmap("SPY")
    patch(m)
    g = build.build(m)
    assert g["scenarios"] == []
    assert g["setup_notes"][0].startswith(build.NO_SETUPS) and needle in g["setup_notes"][0]
    assert g["levels"]                                       # levels stay, for reference


def test_crossing_an_overhead_flip_from_positive_gamma_reads_as_negative():
    c = build._Ctx(100.0, [{"key": "gamma_flip", "label": "gamma flip", "price": 102.0},
                           {"key": "call_wall", "label": "call wall", "price": 106.0}], 0.5, [102.0], gap=0.5)
    flip = {"key": "gamma_flip", "label": "gamma flip", "price": 102.0}
    s = build._flip_cross(c, "positive", flip, "negative")
    assert s["id"] == "flip_break_up" and s["bias"] == "long"
    assert "turns negative" in s["why"] and "damped" not in s["why"]
    assert build._flip_cross(c, "positive", flip, "positive") is None      # no sign change: no scenario


def test_curve_sign_reads_the_profile_beyond_a_price():
    m = {"profile_curve": [{"spot": 98.0, "net_gex": 5.0}, {"spot": 100.0, "net_gex": 1.0},
                           {"spot": 102.0, "net_gex": -2.0}, {"spot": 104.0, "net_gex": -4.0}]}
    assert build.curve_sign(m, 101.0, "up") == "negative"
    assert build.curve_sign(m, 101.0, "down") == "positive"
    assert build.curve_sign({}, 101.0, "up") is None


def test_guidance_reads_the_authoritative_gate_not_the_rounded_share():
    m = fmap("SPY")
    m["coverage"].update(gex_pct=98.0, gate_pct=98.0, eligible=100, gate_pass=False)   # 97.999% rounds to 98.00
    g = build.build(m)
    assert g["scenarios"] == [] and "is usable" in g["setup_notes"][0]


def test_old_vendor_data_blocks_setups_while_the_market_is_open():
    m = fmap("SPY")
    m["feed"].update(market_open=True, data_age_s=900.0)
    g = build.build(m)
    assert g["scenarios"] == [] and "vendor data is 15 min old" in g["setup_notes"][0]


@pytest.mark.parametrize("patch, note", [
    (lambda f: f.update(excluded="stale_oi"), "excluded from the studies"),
    (lambda f: f.update(oi_source="alpaca"), "different map"),
    (lambda f: f.update(expiries=["2026-10-16"]), "different map"),
])
def test_frozen_levels_need_an_unexcluded_comparable_capture(patch, note):
    m = fmap("SPY")
    m["oi_source"] = {"kind": "cboe"}
    m["expiry_selection"] = {"all": True, "expiries": ["2026-10-16", "2026-10-19"]}
    m["frozen_today"].update(oi_source="cboe", expiries=["2026-10-16", "2026-10-19"])
    m["frozen_today"]["put_wall"] = 699.0
    patch(m["frozen_today"])
    g = build.build(m)
    assert all(t["price"] != 699.0 for s in g["scenarios"] for t in s["targets"])
    assert any(note in c for c in g["confidence"])
