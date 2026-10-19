"""Trade guidance endpoints. gexlab/api.py includes `router` (one guarded include line).

GET  /api/guidance?ticker&expiries&signed&range   the structured object + the template narrative
POST /api/guidance/narrate                         the AI narrative (validated) or the template with
                                                   rejection reasons

On for everyone, demo mode included (demo data carries the synthetic label, and the narrate call
serves the template there: demo mode makes no network calls). Not financial advice.
"""
from __future__ import annotations

import json
import logging
import time as _time
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from gexlab import config, service
from gexlab.engine.calendar import NY, utc_now
from gexlab.sources.base import SourceError

from . import ai, build, template, validate

log = logging.getLogger("gexlab.guidance")
router = APIRouter(prefix="/api", tags=["guidance"])

SCORECARD_TTL_S = 600            # The scorecard moves once a day (after-close processing); 10 min is plenty fresh.
_SC: dict = {"at": 0.0, "value": None}


def _ticker(t: str) -> str:
    t = (t or "").strip().upper()
    if not t or len(t) > 10 or not t.replace(".", "").isalnum():
        raise HTTPException(400, "invalid ticker")
    return t


def _expiries(expiries: str | None) -> list[str] | None:
    if not expiries:
        return None
    try:
        return [date.fromisoformat(x.strip()).isoformat() for x in expiries.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "expiries must be YYYY-MM-DD, comma separated")


def scorecard_status() -> dict:
    """The public edition has no Scorecard: the setups are always untested."""
    return {"passed": False, "lead": None}


def _sc_summary(sc: dict) -> dict:
    passed = bool((sc.get("study1") or {}).get("passed") or (sc.get("study2") or {}).get("passed"))
    return {"passed": passed, "lead": sc.get("lead") if passed else None}


def zero_dte(m: dict) -> dict | None:
    """Today's expiry, when selected, with the minutes left to today's close (None after the close)."""
    try:
        cal = service.get_calendar()
        now = (datetime.fromisoformat(m["as_of"].replace("Z", "+00:00")) if config.is_demo() else utc_now())
        today = now.astimezone(NY).date()
        if not any(r.get("expiry") == today.isoformat() for r in m.get("by_expiry") or []):
            return None
        close = cal.close_dt(today)
        if now >= close:
            return None
        return {"expiry": today.isoformat(), "minutes_to_close": int((close - now).total_seconds() // 60),
                "close_et": close.astimezone(NY).strftime("%H:%M")}
    except Exception as e:  # noqa: BLE001
        log.warning("0DTE timing unavailable: %s", e)
        return None


def guidance_for(ticker: str, expiries: list[str] | None, signed: bool, range_pct: float | None) -> dict:
    t = _ticker(ticker)
    try:
        m = service.build_map(t, expiries, True)          # guidance always reads the signed map
    except SourceError as e:
        if e.status == 404:
            raise HTTPException(404, str(e))
        raise HTTPException(503, f"Data source unavailable: {e}")
    return build.build(m, zero_dte=zero_dte(m), scorecard=scorecard_status(), range_pct=range_pct,
                       signed_view=signed)


@router.get("/guidance")
def get_guidance(ticker: str, expiries: str | None = None, signed: bool = True,
                 range: float | None = Query(None, ge=0.5, le=10)):  # noqa: A002 (query name)
    g = guidance_for(ticker, _expiries(expiries), signed, range)
    text = template.render(g)
    return {**g, "narrative": {"text": text, "source": "template", "model": "template",
                               "validated": not validate.validate(text, g), "rejected": []}}


class NarrateBody(BaseModel):
    ticker: str
    expiries: str | None = None
    signed: bool = True
    range: float | None = None


@router.post("/guidance/narrate")
def narrate(body: NarrateBody):
    rng = body.range if body.range is None else min(max(body.range, 0.5), 10.0)
    g = guidance_for(body.ticker, _expiries(body.expiries), body.signed, rng)
    if config.is_demo():
        res = {**ai.template_result(g, ["demo mode: template narrative, no AI call"]), "cached": False}
    else:
        res = ai.narrate(g, body.signed)
    return {"ticker": g["ticker"], "valuation_ts": g.get("valuation_ts"), "footnote": build.FOOTNOTE, **res}
