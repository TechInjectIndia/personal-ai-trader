"""Context Engine FastAPI app — localhost-only (127.0.0.1:8601).

Routes:
  GET  /health             liveness + db probe (always 200 so PM2 doesn't flap)
  GET  /context/{symbol}   latest DECAYED conviction for a watchlist symbol
  POST /refresh            manual/cron trigger of ingest+score (safe to repeat)

The bot uses ONLY GET /context/{symbol}, over localhost, fail-open. Decay and
staleness math live here (server-side) so the bot stays dumb. Startup runs the
idempotent schema init.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from helm.config import WATCHLIST

from context_engine.db import db_ok, init_context_schema
from context_engine.ingest import run_ingest
from context_engine.read import current_context, to_response

_WATCHLIST_SET = set(WATCHLIST)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Idempotent: CREATE TABLE/INDEX IF NOT EXISTS. Tolerate DB-down at boot so
    # the process still starts (health will report db=false).
    try:
        init_context_schema()
    except Exception:
        pass
    yield


app = FastAPI(title="context_engine", version="0.1.0", lifespan=lifespan)


class RefreshRequest(BaseModel):
    symbols: list[str] | None = None
    force: bool = False


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "context_engine", "db": db_ok()}


@app.get("/context/{symbol}")
def get_context(symbol: str) -> dict:
    sym = symbol.upper()
    if sym not in _WATCHLIST_SET:
        raise HTTPException(status_code=404, detail="unknown symbol")
    return to_response(sym, current_context(sym))


@app.post("/refresh")
def refresh(req: RefreshRequest | None = None) -> dict:
    body = req or RefreshRequest()
    syms = [s.upper() for s in body.symbols] if body.symbols else None
    result = run_ingest(syms, force=body.force)
    return {
        "ran_at": result.ran_at,
        "symbols": result.symbols,
        "items_fetched": result.items_fetched,
        "items_new": result.items_new,
        "scored": result.scored,
        "skipped_window": result.skipped_window,
        "errors": result.errors,
    }
