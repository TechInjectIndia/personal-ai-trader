"""Ingest + score pipeline, shared by POST /refresh and the cron script.

For each requested symbol:
  1. source.fetch(symbol, since) -> ContextItems (yfinance .news, free)
  2. upsert into context_items (dedupe on UNIQUE(symbol, url))
  3. for symbols with NEW items: score_symbol(...) via the LLM -> context_scores

This is the only LLM cost path: scoring runs ONLY for symbols that got new
items (unless force=True), so most cadences score 0. Window/weekday no-op
mirrors the cron convention (skipped unless forced).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from helm.config import TRADING_END, TRADING_START, WATCHLIST
from helm.llm import LLMError

from context_engine.db import insert_score, upsert_item
from context_engine.score import score_symbol
from context_engine.sources.base import ContextSource
from context_engine.sources.yfinance_news import YFinanceNewsSource

IST = ZoneInfo("Asia/Kolkata")


def within_window(now: datetime) -> bool:
    """Weekday + IST trading window (same convention as the bot's cron)."""
    if now.weekday() >= 5:
        return False
    return TRADING_START <= now.time() <= TRADING_END


@dataclass
class IngestResult:
    ran_at: str
    symbols: list[str]
    items_fetched: int = 0
    items_new: int = 0
    scored: int = 0
    skipped_window: bool = False
    errors: list[str] = field(default_factory=list)


def run_ingest(
    symbols: list[str] | None = None,
    *,
    force: bool = False,
    source: ContextSource | None = None,
) -> IngestResult:
    """Fetch -> upsert -> score for the given symbols (default: full WATCHLIST).

    Outside the market window/weekend it no-ops with skipped_window=True unless
    ``force`` is set. ``source`` is injectable for tests/alternate feeds.
    """
    now = datetime.now(IST)
    syms = symbols or list(WATCHLIST)
    result = IngestResult(ran_at=now.isoformat(timespec="seconds"), symbols=syms)

    if not force and not within_window(now):
        result.skipped_window = True
        return result

    src = source or YFinanceNewsSource()

    for sym in syms:
        try:
            items = src.fetch(sym)
        except Exception as exc:  # source contract is no-raise, belt-and-suspenders
            result.errors.append(f"{sym}: fetch {exc}"[:200])
            continue
        result.items_fetched += len(items)

        new_ids: list[int] = []
        for it in items:
            new_id = upsert_item(
                it.symbol, it.source, it.url, it.headline, it.body, it.published_ts
            )
            if new_id is not None:
                new_ids.append(new_id)
        result.items_new += len(new_ids)

        if not new_ids and not force:
            continue  # nothing new -> no LLM spend for this symbol
        score_items = items if new_ids else items
        if not score_items:
            continue
        try:
            cs = score_symbol(sym, score_items)
        except LLMError as exc:
            result.errors.append(f"{sym}: score {exc}"[:200])
            continue
        insert_score(sym, cs.score, cs.half_life_min, cs.rationale, new_ids)
        result.scored += 1

    return result
