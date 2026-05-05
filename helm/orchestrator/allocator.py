"""
Sleeve-aware allocator.

Computes the unified target weight for each instrument across both sleeves
and both brokers. For the passive sleeve this means cross-broker SIP splits
honouring currency and cash availability. For the intraday sleeve this means
per-session arming with strategy budgets.

PRD reference: §11 (Orchestrator Design), §10 (Sleeve Definitions).
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Sleeve(str, Enum):
    PASSIVE = "passive"
    INTRADAY = "intraday"


class Broker(str, Enum):
    ZERODHA = "zerodha"
    IBKR = "ibkr"


@dataclass
class TargetAllocation:
    sleeve: Sleeve
    broker: Broker
    symbol: str
    target_weight: Decimal           # 0..1 of total portfolio
    target_quantity: int
    target_notional_inr: Decimal


def compute_passive_targets(
    total_nav_inr: Decimal,
    sleeve_a_cap: Decimal,
    target_weights: dict[str, Decimal],
    instruments: dict[str, dict],     # symbol → {broker, ltp, currency}
    fx_usd_inr: Decimal,
) -> list[TargetAllocation]:
    """
    Compute target allocations for the passive sleeve across both brokers.

    TODO: implement once data layer + holdings sync are in place. Skeleton:
      1. Cap sleeve A allocation at sleeve_a_cap × total_nav_inr.
      2. For each (symbol, weight) in target_weights:
           - Determine broker from instruments.
           - Convert target notional → quantity using ltp + currency.
           - Append TargetAllocation.
      3. Return list. Caller diffs against current holdings to derive orders.
    """
    raise NotImplementedError("allocator.compute_passive_targets — Phase 3")


def arm_intraday_session(
    total_nav_inr: Decimal,
    sleeve_b_cap: Decimal,
    strategies: list[str],
) -> dict[str, Decimal]:
    """
    Pre-market: split sleeve B capital across active intraday strategies.

    TODO: implement once intraday_gate.py is built (Phase 7).
    """
    raise NotImplementedError("allocator.arm_intraday_session — Phase 7")
