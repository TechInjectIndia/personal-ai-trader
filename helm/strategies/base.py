"""Strategy interface for intraday signal generators."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class Signal:
    """A candidate trade. Strategies emit these; the decider acts on them."""
    strategy: str
    symbol: str
    asof: datetime
    side: str                  # 'BUY' | 'SELL'
    entry_price: Decimal
    stop_loss: Decimal
    target: Decimal | None
    rationale: str
    payload: dict


class Strategy(ABC):
    """Pure-function-style: in candles, out signals. No DB access here."""

    # Candle timeframe (in minutes) this strategy consumes. Default 1 so every
    # existing strategy inherits the 1-min path with zero edits. Variants set a
    # larger value (e.g. 5) and scan_signals feeds them resampled N-min bars.
    bar_minutes: int = 1

    # F7-P3c: a strategy that needs the external context score. scan_signals sets
    # `self.context` (Decimal in [-1,1], or None) before calling scan(), and SKIPS
    # such strategies entirely unless CONTEXT_SIGNALS_ENABLED. Default False keeps
    # every other strategy a pure function of candles only (no DB/HTTP).
    requires_context: bool = False

    # M6: a strategy that depends on a session OPEN (opening range, gap-from-
    # prior-close). scan_signals SKIPS these on 24/7 venues (crypto), where there
    # is no session open to anchor to. Session-agnostic strategies leave it False.
    session_required: bool = False

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def scan(self, symbol: str, candles: list[dict]) -> Signal | None:
        """
        Return a Signal if rules fire on this symbol given today's candles,
        else None. `candles` is chronological OHLC dicts at this strategy's
        `bar_minutes` timeframe (1-min by default):
        {bar_ts, open, high, low, close, tick_count}.
        """
        ...
