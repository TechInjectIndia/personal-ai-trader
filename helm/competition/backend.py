"""Shared backend-invocation path for the competition league.

Both the per-cycle runner (helm.competition.runner) and the weekly mandate
planner (helm.competition.mandate) need the same thing: call a competitor's
backend for a schema-typed JSON object, while (a) respecting the per-backend
quota throttle and (b) tracing every call to `agent_invocations` (the "how it
thinks" + cost/quota ledger). `call_backend` is that single chokepoint.

Quota is checked *before* the call (a paused backend is skipped without spending
anything); a quota/rate-limit error *returned* by the backend force-pauses it
via quota.note_error so we back off even when the vendor's real limit is tighter
than our local estimate. Transport/parse failures are logged but not fatal — the
caller decides what a failed cycle means.
"""

from __future__ import annotations

import json
import time
from typing import Any, NamedTuple

from helm.competition import quota
from helm.data.store import conn
from helm.llm import LLMError, complete_json

# agent_invocations.prompt / raw_output are TEXT; keep traces bounded.
_MAX_FIELD = 20000


class BackendCall(NamedTuple):
    ok: bool
    parsed: dict | None
    raw: str | None
    latency_ms: int
    error: str | None
    quota_blocked: bool
    reason: str


def log_invocation(
    competitor_id: str, backend: str, model: str | None, prompt: str,
    raw_output: str | None, latency_ms: int, ok: bool, error: str | None,
) -> None:
    """Append one row to agent_invocations (the per-call audit/quota ledger)."""
    with conn() as c:
        c.execute(
            """
            INSERT INTO agent_invocations
                (competitor_id, backend, model, prompt, raw_output, latency_ms, ok, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (competitor_id, backend, model, prompt[:_MAX_FIELD],
             (raw_output or "")[:_MAX_FIELD], latency_ms, ok,
             (error or None) and error[:1000]),
        )


def call_backend(
    *, competitor_id: str, backend: str, model: str | None,
    system: str, user: str, schema: dict[str, Any],
) -> BackendCall:
    """Quota-gated, traced backend call returning a parsed JSON dict.

    Precedence of outcomes:
      * quota-paused  → no call made, nothing logged to the invocation ledger
        (the pause is already audited); BackendCall(quota_blocked=True).
      * LLMError      → logged ok=False; if it reads like a quota/rate-limit
        error the backend is force-paused; BackendCall(ok=False).
      * success       → logged ok=True; BackendCall(ok=True, parsed=...).
    """
    decision = quota.check_and_reserve(backend)
    if not decision.allowed:
        return BackendCall(False, None, None, 0, decision.reason,
                           quota_blocked=True, reason=decision.reason)

    started = time.monotonic()
    try:
        parsed = complete_json(system, user, schema=schema, model=model, backend=backend)
    except LLMError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        log_invocation(competitor_id, backend, model, user, None, latency_ms,
                       ok=False, error=str(exc))
        quota.note_error(backend, str(exc))
        return BackendCall(False, None, None, latency_ms, str(exc),
                           quota_blocked=False, reason="llm_error")

    latency_ms = int((time.monotonic() - started) * 1000)
    raw = json.dumps(parsed)
    log_invocation(competitor_id, backend, model, user, raw, latency_ms,
                   ok=True, error=None)
    return BackendCall(True, parsed, raw, latency_ms, None,
                       quota_blocked=False, reason="ok")
