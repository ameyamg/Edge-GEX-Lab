"""pair_iv_from_otm keys by root: two series with the same expiry and strike never share an IV."""
from datetime import date

from gexlab.models import Contract
from gexlab.sources.alpaca import pair_iv_from_otm


def _c(root, typ, iv):
    return Contract(symbol=f"{root}261120{typ[0].upper()}06700000", root=root, expiry=date(2026, 11, 20),
                    settlement="AM" if root == "SPX" else "PM", strike=6700.0, type=typ, multiplier=100,
                    oi=10, iv=iv, iv_source="solved_mid")


def test_same_expiry_strike_different_roots_pair_separately():
    am_call, am_put = _c("SPX", "call", 0.20), _c("SPX", "put", None)
    pm_call, pm_put = _c("SPXW", "call", 0.30), _c("SPXW", "put", None)
    pair_iv_from_otm([am_call, am_put, pm_call, pm_put], spot=6650.0)   # strike above spot: the call is OTM
    assert am_put.iv == 0.20 and am_put.iv_donor == am_call.symbol
    assert pm_put.iv == 0.30 and pm_put.iv_donor == pm_call.symbol


def test_order_does_not_mix_series():
    am_call, am_put = _c("SPX", "call", 0.20), _c("SPX", "put", None)
    pm_call, pm_put = _c("SPXW", "call", 0.30), _c("SPXW", "put", None)
    pair_iv_from_otm([pm_put, am_call, pm_call, am_put], spot=6650.0)
    assert (am_put.iv, pm_put.iv) == (0.20, 0.30)
