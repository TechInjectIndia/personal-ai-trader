"""
Risk module — portfolio-wide and sleeve-aware hard limits.

Every proposed order passes through this module pre-trade. After every fill,
post-trade checks may trigger sleeve halt or manual hold.

PRD reference: §12 Risk Management Framework. NFR-9 requires ≥80% test coverage
on risk paths and ≥90% on intraday risk paths.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from helm.orchestrator.allocator import Broker, Sleeve


class RiskAction(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    SPLIT = "split"
    THROTTLE = "throttle"


@dataclass(frozen=True)
class RiskLimits:
    # Portfolio-wide
    max_daily_loss_pct: Decimal = Decimal("0.02")          # 2% of total
    max_weekly_drawdown_pct: Decimal = Decimal("0.05")     # 5%
    max_monthly_drawdown_pct: Decimal = Decimal("0.10")    # 10%
    max_gross_exposure_pct: Decimal = Decimal("1.00")      # 100% no overnight leverage
    min_cash_buffer_pct: Decimal = Decimal("0.05")         # 5%
    max_daily_orders_total: int = 60
    max_usd_exposure_pct: Decimal = Decimal("0.60")        # 60%

    # Passive sleeve
    sleeve_a_floor_pct: Decimal = Decimal("0.85")
    max_single_name_pct_passive: Decimal = Decimal("0.07")
    max_sector_pct_passive: Decimal = Decimal("0.30")
    max_order_notional_pct_passive: Decimal = Decimal("0.20")

    # Intraday sleeve (tighter)
    sleeve_b_cap_pct: Decimal = Decimal("0.15")
    intraday_daily_loss_cap_pct: Decimal = Decimal("0.01")  # of TOTAL (≈8% of sleeve)
    max_single_name_pct_intraday: Decimal = Decimal("0.02")
    max_order_notional_pct_intraday: Decimal = Decimal("0.05")
    max_daily_orders_intraday: int = 40
    intraday_stop_loss_pct: Decimal = Decimal("0.005")     # 0.5% of total per trade
    intraday_no_new_entries_after: str = "14:55:00"        # IST
    intraday_consecutive_losing_days: int = 3


@dataclass
class ProposedOrder:
    sleeve: Sleeve
    broker: Broker
    symbol: str
    side: str                           # "buy" | "sell"
    quantity: int
    estimated_price: Decimal
    product_type: str                   # "CNC" | "MIS"
    strategy: str                       # which strategy proposed it


@dataclass
class RiskDecision:
    action: RiskAction
    reason: str | None = None
    suggested_quantity: int | None = None    # for SPLIT


def pre_trade_check(
    order: ProposedOrder,
    portfolio_state: dict,             # holdings + cash + drawdown snapshot
    limits: RiskLimits = RiskLimits(),
) -> RiskDecision:
    """
    Pre-trade gate. TODO: implement Phase 3.

    Order of checks (fail fast on first violation):
      1. Sleeve-product consistency (passive=CNC, intraday=MIS).
      2. Manual-hold / kill-switch state.
      3. Daily loss cap (portfolio + sleeve).
      4. Single-name cap.
      5. Sector cap.
      6. Per-order notional cap.
      7. Sleeve capital cap.
      8. Cash buffer.
      9. Daily order count.
      10. Intraday-only: stop-loss attached, square-off buffer (no new after 14:55).
    """
    raise NotImplementedError("risk.pre_trade_check — Phase 3")


def post_trade_check(
    fill: dict,
    portfolio_state: dict,
    limits: RiskLimits = RiskLimits(),
) -> RiskDecision:
    """
    Post-trade evaluation. TODO: implement Phase 3.

    Triggers sleeve halt or manual hold if any drawdown limit breached
    by the new exposure. Returns the RiskDecision describing the action
    the orchestrator should execute next.
    """
    raise NotImplementedError("risk.post_trade_check — Phase 3")
