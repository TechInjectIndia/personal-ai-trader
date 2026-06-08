"""ContextSource ABC + ContextItem dataclass.

A ContextSource is a pure-ish adapter: given a symbol (and an optional
``since`` cutoff) it returns a list of ContextItem records. The service owns
all the flakiness/rate-limit risk of the underlying feed; the bot never sees a
source directly. Swap the implementation (yfinance -> RSS -> paid API) without
touching the bot or the rest of the service.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class ContextItem:
    """One unit of external context (a news headline + optional body).

    ``url`` is the dedupe key (UNIQUE(symbol, url) in storage). When the feed
    lacks a canonical link the source synthesizes a stable key (e.g. a hash of
    source+headline) so de-duplication still works; an item with no usable key
    is dropped by the source rather than stored with a NULL url (NULLs don't
    dedupe in Postgres).
    """

    symbol: str
    source: str
    url: str
    headline: str
    body: str = ""
    published_ts: datetime | None = None


class ContextSource(ABC):
    """Pluggable context feed. Implementations must never raise on a transport
    error — return an empty list so ingestion degrades gracefully."""

    #: Stable identifier stored in context_items.source.
    name: str = "base"

    @abstractmethod
    def fetch(self, symbol: str, since: datetime | None = None) -> list[ContextItem]:
        """Return recent ContextItems for ``symbol`` (optionally newer than
        ``since``). Best-effort: on any feed error, return ``[]``."""
        raise NotImplementedError
