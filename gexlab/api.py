"""Edge GEX Lab FastAPI app (port 8240). Shapes follow the v1 API contract.

Run: launch.bat, or
  .venv\\Scripts\\python -m uvicorn gexlab.api:app --host 127.0.0.1 --port 8240
(launch.bat takes the port from GEXLAB_PORT, 8240 by default.)
GEXLAB_DEMO=1 serves synthetic fixtures with no keys and no network.
"""
from __future__ import annotations

import logging
from datetime import date

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from gexlab import config, service
from gexlab.sources.base import SourceError

log = logging.getLogger("gexlab.api")

app = FastAPI(title="Edge GEX Lab", version=config.MODEL_VERSION)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5240", "http://localhost:5240"],
                   allow_methods=["GET", "POST"], allow_headers=["*"])


def _ticker(t: str) -> str:
    t = (t or "").strip().upper()
    if not t or len(t) > 10 or not t.replace(".", "").isalnum():
        raise HTTPException(400, "invalid ticker")
    return t


def _vendor_error(e: SourceError) -> HTTPException:
    if e.status == 404:
        return HTTPException(404, str(e))
    if e.status == 401:
        return HTTPException(503, "Alpaca rejected the configured keys (HTTP 401).")
    return HTTPException(503, f"Data source unavailable: {e}")


@app.get("/api/health")
def health():
    return service.health()


@app.get("/api/tickers")
def tickers():
    return {"pinned": config.PINNED_TICKERS}


@app.get("/api/search")
def search(q: str = Query("", max_length=40)):
    return service.search(q)


@app.get("/api/expirations")
def expirations(ticker: str):
    try:
        return service.expirations(_ticker(ticker))
    except SourceError as e:
        raise _vendor_error(e)


@app.get("/api/map")
def gex_map(ticker: str, expiries: str | None = None, signed: bool = True):
    exp = None
    if expiries:
        try:
            exp = [date.fromisoformat(x.strip()).isoformat() for x in expiries.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "expiries must be YYYY-MM-DD, comma separated")
    try:
        return service.build_map(_ticker(ticker), exp, signed)
    except SourceError as e:
        raise _vendor_error(e)


try:   # trade guidance (gexlab/guidance); optional, must never take down the core API
    from gexlab.guidance.api import router as _guidance_router
    app.include_router(_guidance_router)
except Exception as _e:  # noqa: BLE001
    log.warning("guidance router not loaded: %s", _e)

_dist = config.ROOT / "web" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="web")
