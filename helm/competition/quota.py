"""Per-backend quota throttle for the competition league.

Each league backend (claude / gemini / qwen / codex / opencode) runs on a
different vendor's free or subscription tier. To keep $0 spend and stay inside
free allowances we cap each backend to a rolling per-window call ceiling
(see helm.config.BACKEND_QUOTAS). State lives in `backend_quota_state`:

    backend       PK
    window_start  start of the current rolling window
    calls_used    calls reserved in the current window
    paused_until  when set and in the future, the backend is benched

Lifecycle, all driven from `check_and_reserve()` (called once per intended
backend invocation):

  * window roll  — when now − window_start ≥ window_minutes, the window resets
                   (window_start = now, calls_used = 0).
  * reserve      — under the ceiling: increment calls_used, allow the call.
  * exhaustion   — at/over the ceiling: set paused_until = window_start +
                   window_minutes, audit it, and block the call.
  * auto-resume  — once now ≥ paused_until the next check clears the pause and
                   rolls a fresh window, so the agent comes back on its own.

`note_error()` lets callers force a pause when a backend *returns* a quota /
rate-limit error mid-window even though our local counter hadn't tripped yet
(the vendor's real limit is tighter than our estimate).

All money is irrelevant here; all timestamps are timezone-aware IST, matching
the rest of the competition code. Single-process cron, so the read-modify-write
needs no locking.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

from helm.config import backend_quota
from helm.data.store import conn, insert_audit

IST = ZoneInfo("Asia/Kolkata")

# Substrings that mark a backend response as a quota / rate-limit refusal.
# Matched case-insensitively against the CLI error text.
_QUOTA_ERROR_PATTERNS = re.compile(
    r"quota|rate.?limit|resource.?exhausted|too many requests|"
    r"\b429\b|usage limit|over.?loaded|insufficient_quota|out of credits",
    re.IGNORECASE,
)


class QuotaDecision(NamedTuple):
    allowed: bool
    reason: str
    paused_until: datetime | None
    calls_used: int
    max_calls: int


def looks_like_quota_error(text: str | None) -> bool:
    """True if a backend error string reads like a quota / rate-limit refusal."""
    return bool(text) and bool(_QUOTA_ERROR_PATTERNS.search(text or ""))


def _load(c, backend: str) -> dict:
    """Fetch (creating if absent) the quota row for `backend`."""
    row = c.execute(
        "SELECT backend, window_start, calls_used, paused_until "
        "FROM backend_quota_state WHERE backend = %s",
        (backend,),
    ).fetchone()
    if row is None:
        c.execute(
            """
            INSERT INTO backend_quota_state (backend, window_start, calls_used)
            VALUES (%s, %s, 0)
            ON CONFLICT (backend) DO NOTHING
            """,
            (backend, datetime.now(IST)),
        )
        row = c.execute(
            "SELECT backend, window_start, calls_used, paused_until "
            "FROM backend_quota_state WHERE backend = %s",
            (backend,),
        ).fetchone()
    return row


def check_and_reserve(backend: str, *, now: datetime | None = None) -> QuotaDecision:
    """Reserve one call against `backend`'s quota.

    Returns a QuotaDecision: `allowed=True` reserves the slot (calls_used++),
    `allowed=False` means the backend is paused or exhausted and the caller must
    NOT invoke it this cycle. Handles window rolls and auto-resume transparently.
    """
    now = now or datetime.now(IST)
    cfg = backend_quota(backend)
    window = timedelta(minutes=cfg.window_minutes)

    with conn() as c:
        row = _load(c, backend)
        window_start: datetime = row["window_start"] or now
        calls_used: int = int(row["calls_used"] or 0)
        paused_until: datetime | None = row["paused_until"]

        # Still benched? Block without touching the counter.
        if paused_until is not None and now < paused_until:
            return QuotaDecision(False, f"paused until {paused_until.astimezone(IST):%H:%M}",
                                 paused_until, calls_used, cfg.max_calls)

        # Pause elapsed, or window rolled → start a fresh window.
        if (paused_until is not None and now >= paused_until) or (now - window_start >= window):
            window_start, calls_used, paused_until = now, 0, None

        if calls_used >= cfg.max_calls:
            paused_until = window_start + window
            c.execute(
                "UPDATE backend_quota_state "
                "SET window_start = %s, calls_used = %s, paused_until = %s WHERE backend = %s",
                (window_start, calls_used, paused_until, backend),
            )
            insert_audit("competition_quota", "exhausted",
                         {"backend": backend, "calls_used": calls_used,
                          "max_calls": cfg.max_calls,
                          "paused_until": paused_until.isoformat()})
            return QuotaDecision(False,
                                 f"quota exhausted ({calls_used}/{cfg.max_calls}); "
                                 f"paused until {paused_until.astimezone(IST):%H:%M}",
                                 paused_until, calls_used, cfg.max_calls)

        calls_used += 1
        c.execute(
            "UPDATE backend_quota_state "
            "SET window_start = %s, calls_used = %s, paused_until = NULL WHERE backend = %s",
            (window_start, calls_used, backend),
        )
        return QuotaDecision(True, "ok", None, calls_used, cfg.max_calls)


def note_error(backend: str, error: str | None, *, now: datetime | None = None) -> bool:
    """Record a backend error; pause the backend if it reads like a quota hit.

    Returns True if the error tripped a pause. Non-quota errors are ignored
    here (the call was already counted by check_and_reserve).
    """
    if not looks_like_quota_error(error):
        return False
    now = now or datetime.now(IST)
    cfg = backend_quota(backend)
    paused_until = now + timedelta(minutes=cfg.window_minutes)
    with conn() as c:
        _load(c, backend)
        c.execute(
            "UPDATE backend_quota_state SET paused_until = %s WHERE backend = %s",
            (paused_until, backend),
        )
    insert_audit("competition_quota", "error_pause",
                 {"backend": backend, "paused_until": paused_until.isoformat(),
                  "error": (error or "")[:300]})
    return True


def reset(backend: str, *, now: datetime | None = None) -> None:
    """Manually clear a backend's pause and start a fresh window (admin/testing)."""
    now = now or datetime.now(IST)
    with conn() as c:
        _load(c, backend)
        c.execute(
            "UPDATE backend_quota_state "
            "SET window_start = %s, calls_used = 0, paused_until = NULL WHERE backend = %s",
            (now, backend),
        )
    insert_audit("competition_quota", "reset", {"backend": backend})


def quota_status(*, now: datetime | None = None) -> list[dict]:
    """All backend quota rows enriched with config + live paused flag.

    Read-only — for the dashboard. Does not roll windows or mutate state.
    """
    now = now or datetime.now(IST)
    with conn() as c:
        rows = list(c.execute(
            "SELECT backend, window_start, calls_used, paused_until "
            "FROM backend_quota_state ORDER BY backend"
        ))
    out: list[dict] = []
    for r in rows:
        cfg = backend_quota(r["backend"])
        paused = r["paused_until"] is not None and now < r["paused_until"]
        out.append({
            "backend": r["backend"],
            "calls_used": int(r["calls_used"] or 0),
            "max_calls": cfg.max_calls,
            "window_minutes": cfg.window_minutes,
            "window_start": r["window_start"],
            "paused_until": r["paused_until"],
            "paused": paused,
        })
    return out
