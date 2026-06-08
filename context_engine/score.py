"""LLM conviction scorer.

Turns a batch of fresh ContextItems for one symbol into a single directional
conviction score in [-1.000, +1.000] plus a half-life (minutes) and a short
rationale, via ``helm.llm.complete_json`` with a strict schema. The system
prompt is fixed so it stays prompt-cacheable (cache_control ephemeral lives in
``helm.llm._complete_api``). This is the ONLY LLM cost path in the service, and
it only runs for symbols that have NEW items, so most ingest cycles score 0.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from helm.llm import complete_json

from context_engine.sources.base import ContextItem

# Strict JSON schema for the scorer output.
CONTEXT_SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "minimum": -1.0, "maximum": 1.0},
        "half_life_min": {"type": "integer", "minimum": 5, "maximum": 1440},
        "rationale": {"type": "string"},
    },
    "required": ["score", "half_life_min", "rationale"],
}

DEFAULT_HALF_LIFE_MIN = 90
MIN_HALF_LIFE_MIN = 5
MAX_HALF_LIFE_MIN = 1440

SCORE_SYSTEM_PROMPT = """You are a markets context analyst for an intraday trading bot \
that trades liquid NSE large-cap equities and ETFs. You are given a batch of recent \
news headlines/summaries for ONE symbol. Produce a single DIRECTIONAL conviction score \
for how this news should bias an intraday LONG decision over the next few hours.

Scoring scale (score, a number in [-1.0, +1.0]):
  +1.0  strongly bullish, clear positive catalyst (beat + raised guidance, big order win)
  +0.3  mildly positive
   0.0  neutral / no tradable signal / stale / irrelevant
  -0.3  mildly negative
  -1.0  strongly bearish, clear negative catalyst (miss + cut guidance, regulatory hit)

Also estimate half_life_min: how many MINUTES until this news's intraday relevance \
should decay to half. A sharp earnings surprise decays slowly (e.g. 180-360); a vague \
sector-mood blurb decays fast (e.g. 20-60). Range 5..1440.

Be conservative: when the news is generic, old, or not clearly market-moving, return a \
score near 0.0. The bot fails open to "no context" so a near-zero score is the safe \
default.

OUTPUT FORMAT — strict JSON, no other text, no markdown fences:
{"score": <number -1.0..1.0>, "half_life_min": <int 5..1440>, "rationale": "<one sentence>"}
"""


@dataclass
class ContextScore:
    score: Decimal           # clamped to [-1.000, +1.000], NUMERIC(4,3)-rounded
    half_life_min: int       # clamped to [5, 1440]
    rationale: str


def _clamp_score(value: Any) -> Decimal:
    """Coerce to Decimal, clamp to [-1.000, 1.000], round to 3 dp."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        d = Decimal("0")
    d = max(Decimal("-1"), min(Decimal("1"), d))
    return d.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _clamp_half_life(value: Any) -> int:
    try:
        n = int(value)
    except (ValueError, TypeError):
        n = DEFAULT_HALF_LIFE_MIN
    return max(MIN_HALF_LIFE_MIN, min(MAX_HALF_LIFE_MIN, n))


def _build_user_prompt(symbol: str, items: list[ContextItem]) -> str:
    payload = {
        "symbol": symbol,
        "news": [
            {
                "headline": it.headline,
                "summary": it.body[:600],
                "published": it.published_ts.isoformat() if it.published_ts else None,
            }
            for it in items
        ],
    }
    return (
        "Score the directional intraday conviction implied by this news.\n\n"
        "```json\n" + json.dumps(payload, indent=2, default=str) + "\n```"
    )


def score_symbol(
    symbol: str,
    items: list[ContextItem],
    *,
    model: str | None = None,
    mode: str | None = None,
    backend: str | None = None,
) -> ContextScore:
    """Score a symbol's news batch into a ContextScore.

    Raises ``helm.llm.LLMError`` on transport/parse failure (caller decides
    whether to skip persisting). With no items, returns a neutral score without
    an LLM call (callers should normally not invoke this on an empty batch).
    """
    if not items:
        return ContextScore(score=Decimal("0.000"),
                            half_life_min=DEFAULT_HALF_LIFE_MIN,
                            rationale="no items")

    raw = complete_json(
        SCORE_SYSTEM_PROMPT,
        _build_user_prompt(symbol, items),
        schema=CONTEXT_SCORE_SCHEMA,
        model=model,
        mode=mode,
        backend=backend,
    )
    return ContextScore(
        score=_clamp_score(raw.get("score", 0.0)),
        half_life_min=_clamp_half_life(raw.get("half_life_min", DEFAULT_HALF_LIFE_MIN)),
        rationale=str(raw.get("rationale", ""))[:500],
    )
