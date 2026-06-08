"""Bot-side consumer for the Context Engine microservice.

Stdlib-only (urllib) HTTP client that fetches an external-conviction score for a
symbol from the decoupled context_engine service over localhost. This is the
ONLY contract across the bot↔service boundary — the bot NEVER imports anything
from ``context_engine/``; it only talks HTTP.

Design invariants (live-trade-path safety):
  * FLAG-GATED. ``CONTEXT_ENGINE_URL`` unset (the default) => ``get_context``
    returns None on its first line: no HTTP, no log, no latency, no behaviour
    delta. This is the clean A/B baseline (claude-blind) and the instant revert.
  * FAIL-OPEN. ANY non-200, timeout, malformed body, missing key, or unexpected
    error => None. The bot proceeds with NO context exactly as it does today.
  * NEVER RAISES. A context fetch can never break or block a paper trade.
  * SHORT TIMEOUT. ``CONTEXT_ENGINE_TIMEOUT_S`` hard-caps added latency on the
    inline scan→decide path; on timeout we fail open.
  * STALE == OFF. A ``stale=true`` (or empty-rationale) response is treated
    exactly like the flag being off — no context is injected.

Observability via ``helm.instrument.log_event("context_engine", ...)``: events
hit / miss / fail_open, append-only JSONL, never raises. NO event is emitted
when the flag is unset — flag-off is observably silent.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from helm.config import CONTEXT_ENGINE_TIMEOUT_S
from helm.instrument import log_event

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def get_context(symbol: str, *, signal_id: int | None = None) -> dict | None:
    """Fetch external context for ``symbol`` from the context engine.

    Returns ``{"score": float, "rationale": str}`` on a fresh hit, or ``None``
    when the flag is unset, the service is unreachable/slow, the response is
    malformed, or the context is stale. Never raises.
    """
    # The inline decide path (scan_signals.py) does NOT call load_dotenv, so the
    # env var must be visible to that process. Load .env idempotently here
    # (override=False, cheap) so both the inline and cron-catch-up paths agree on
    # the flag — otherwise the A/B is inconsistent across the two entry points.
    if "CONTEXT_ENGINE_URL" not in os.environ:
        try:
            from dotenv import load_dotenv

            load_dotenv(ENV_PATH, override=False)
        except Exception:
            pass  # dotenv missing or unreadable → treat flag as unset

    url = os.environ.get("CONTEXT_ENGINE_URL")
    if not url:
        # Flag off => zero code path: no HTTP, no log, no latency.
        return None

    endpoint = url.rstrip("/") + f"/context/{symbol}"
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(endpoint, timeout=CONTEXT_ENGINE_TIMEOUT_S) as r:
            status = getattr(r, "status", None) or r.getcode()
            if status != 200:
                log_event("context_engine", "fail_open", symbol=symbol,
                          signal_id=signal_id, status=status)
                return None
            data = json.loads(r.read().decode("utf-8"))

        if data.get("stale", True) or data.get("rationale") in (None, ""):
            log_event("context_engine", "miss", symbol=symbol,
                      signal_id=signal_id, stale=data.get("stale"))
            return None

        ctx = {"score": float(data["score"]), "rationale": str(data["rationale"])[:500]}
        log_event("context_engine", "hit", symbol=symbol, signal_id=signal_id,
                  score=ctx["score"], latency_ms=round((time.monotonic() - t0) * 1000))
        return ctx
    except Exception as e:  # urllib.error.URLError, TimeoutError, ValueError, KeyError, ...
        log_event("context_engine", "fail_open", symbol=symbol,
                  signal_id=signal_id, error=str(e)[:200])
        return None
