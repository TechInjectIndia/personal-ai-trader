"""Persistent, append-only instrumentation for features we want to watch live.

The pino-style "alternative" for a Python/Postgres bot: structured JSON lines,
one file per feature under ``logs/instrumentation/<feature>.jsonl``. Append-only,
dependency-free, greppable, and survives restarts — so a future session can read
back exactly how a freshly-shipped feature behaved in production.

Design rules:
- NEVER raise into the caller. Instrumentation must not be able to break a live
  trade or a cron run. All failures are swallowed.
- Decimals/datetimes are stringified so the line is always valid JSON.
- Pair with the durable signals already in Postgres (``audit``, ``decisions``,
  ``agent_runs``, ``paper_trades``); ``scripts/review_digest.py`` reads both.

Usage:
    from helm.instrument import log_event
    log_event("f2_min_edge_gate", "blocked", signal_id=42, symbol="INFY",
              e2c="1.84", min_required="3.0")
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
INSTRUMENT_DIR = Path(__file__).resolve().parent.parent / "logs" / "instrumentation"


def _coerce(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)  # Decimal, datetime, etc.


def log_event(feature: str, event: str, **fields: Any) -> None:
    """Append one JSON line to logs/instrumentation/<feature>.jsonl. Never raises."""
    try:
        rec = {"ts": datetime.now(IST).isoformat(), "feature": feature, "event": event}
        rec.update({k: _coerce(v) for k, v in fields.items()})
        INSTRUMENT_DIR.mkdir(parents=True, exist_ok=True)
        with (INSTRUMENT_DIR / f"{feature}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # instrumentation is best-effort; never break the caller


def read_events(feature: str, *, since: datetime | None = None,
                limit: int | None = None) -> list[dict]:
    """Read back a feature's events (oldest→newest). Tolerant of bad lines."""
    path = INSTRUMENT_DIR / f"{feature}.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None:
                try:
                    if datetime.fromisoformat(rec.get("ts", "")) < since:
                        continue
                except ValueError:
                    pass
            out.append(rec)
    except Exception:
        return out
    return out[-limit:] if limit else out
