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

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def scan(self, symbol: str, candles: list[dict]) -> Signal | None:
        """
        Return a Signal if rules fire on this symbol given today's candles,
        else None. `candles` is chronological 1-min OHLC dicts:
        {bar_ts, open, high, low, close, tick_count}.
        """
        ...
