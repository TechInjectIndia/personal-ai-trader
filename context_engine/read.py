"""Time-decay read path — server-side so the bot stays dumb.

``current_context(symbol)`` reads the latest context_scores row and applies
exponential decay:  current = score * 0.5 ** (age_min / half_life_min).
It returns the contract dict the HTTP layer serializes verbatim. Staleness
(no row / beyond CONTEXT_STALENESS_MIN / decayed magnitude below
CONTEXT_FRESHNESS_FLOOR) is decided here, so a stale=true response is
functionally identical (to the bot) to the flag being off.

The pure decay/staleness math (``decay_score`` / ``evaluate``) takes plain
inputs so it can be unit-tested without a DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from helm.config import CONTEXT_FRESHNESS_FLOOR, CONTEXT_STALENESS_MIN

from context_engine.db import latest_score

IST = ZoneInfo("Asia/Kolkata")


def decay_score(score: float, age_min: float, half_life_min: float) -> float:
    """Exponential decay: score * 0.5 ** (age_min / half_life_min).

    age_min < 0 (clock skew) is treated as 0 (no decay). A non-positive
    half-life is treated as already fully decayed.
    """
    if half_life_min <= 0:
        return 0.0
    age = max(0.0, age_min)
    return score * (0.5 ** (age / half_life_min))


@dataclass
class ContextRead:
    score: float          # DECAYED, rounded to 3 dp; 0.0 when stale/absent
    rationale: str        # "" when stale/absent
    as_of: str | None     # scored_ts ISO (IST) of the row used; None when absent
    half_life_min: int | None
    stale: bool


def evaluate(
    raw_score: float,
    half_life_min: int,
    age_min: float,
    *,
    staleness_min: float = CONTEXT_STALENESS_MIN,
    freshness_floor: float = CONTEXT_FRESHNESS_FLOOR,
) -> tuple[float, bool]:
    """Pure core: decay a raw score and decide staleness.

    Returns (decayed_score_rounded, stale). stale=True when age exceeds the
    staleness window OR the decayed magnitude is below the freshness floor.
    When stale, the returned score is forced to 0.0.
    """
    decayed = decay_score(raw_score, age_min, half_life_min)
    stale = (age_min > staleness_min) or (abs(decayed) < freshness_floor)
    if stale:
        return 0.0, True
    rounded = float(
        Decimal(str(decayed)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    )
    return rounded, False


def _absent() -> ContextRead:
    return ContextRead(score=0.0, rationale="", as_of=None,
                       half_life_min=None, stale=True)


def current_context(symbol: str) -> ContextRead:
    """Latest decayed context for a symbol. Never raises on a missing row."""
    row = latest_score(symbol)
    if not row:
        return _absent()

    scored_ts: datetime = row["scored_ts"]
    if scored_ts.tzinfo is None:
        scored_ts = scored_ts.replace(tzinfo=timezone.utc)
    age_min = (datetime.now(timezone.utc) - scored_ts).total_seconds() / 60.0

    raw_score = float(row["score"])
    half_life = int(row["half_life_min"])
    decayed, stale = evaluate(raw_score, half_life, age_min)

    if stale:
        return ContextRead(
            score=0.0, rationale="",
            as_of=scored_ts.astimezone(IST).isoformat(timespec="seconds"),
            half_life_min=half_life, stale=True,
        )
    return ContextRead(
        score=decayed,
        rationale=row["rationale"] or "",
        as_of=scored_ts.astimezone(IST).isoformat(timespec="seconds"),
        half_life_min=half_life,
        stale=False,
    )


def to_response(symbol: str, read: ContextRead) -> dict[str, Any]:
    """Shape a ContextRead into the GET /context/{symbol} response body."""
    return {
        "symbol": symbol,
        "score": read.score,
        "rationale": read.rationale,
        "as_of": read.as_of,
        "half_life_min": read.half_life_min,
        "stale": read.stale,
    }
