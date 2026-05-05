"""
Strategy interface — every strategy in passive/ or intraday/ inherits from
this and implements three methods.

PRD reference: §11 strategies/base.py.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from helm.orchestrator.allocator import Sleeve, Broker


@dataclass
class Signal:
    strategy: str
    symbol: str
    asof: datetime
    score: Decimal             # confidence in [0, 1]
    rationale: str
    payload: dict              # strategy-specific


@dataclass
class StrategyMetadata:
    name: str
    sleeve: Sleeve
    market: str                # 'NSE' | 'NYSE' | 'NASDAQ' | ...
    broker: Broker
    asset_class: str           # 'equity' | 'etf' | 'liquid_fund'
    capacity_inr: Decimal      # estimated capacity
    cooldown_hours: int
    expected_turnover: Decimal # annualised, fraction of NAV
    min_history_days: int


class Strategy(ABC):
    """Base class. All strategies inherit from this."""

    @abstractmethod
    def metadata(self) -> StrategyMetadata: ...

    @abstractmethod
    def generate_signals(self, asof: datetime, market_data: dict) -> list[Signal]: ...

    @abstractmethod
    def proposed_orders(
        self,
        portfolio_state: dict,
        signals: list[Signal],
    ) -> list[dict]:                     # list of ProposedOrder-shaped dicts
        ...
