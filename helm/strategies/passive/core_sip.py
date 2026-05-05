"""
Core SIP — monthly fixed-amount investment into the agreed ETF allocation.

PRD reference: §10 sleeve A initial strategies.

This is the canonical example of how a Strategy is structured. Other
strategies in passive/ and intraday/ follow the same shape.
"""

from datetime import datetime
from decimal import Decimal

from helm.orchestrator.allocator import Sleeve, Broker
from helm.strategies.base import Signal, Strategy, StrategyMetadata


# Target weights for the Indian passive allocation.
# (PRD strategy: 70% Nifty 50 ETF, 20% Nifty Next 50 ETF, 10% liquid fund.)
INDIA_TARGETS: dict[str, Decimal] = {
    "NIFTYBEES": Decimal("0.70"),
    "JUNIORBEES": Decimal("0.20"),
    "LIQUIDBEES": Decimal("0.10"),
}


class CoreSIPIndia(Strategy):
    """Monthly SIP into Indian ETF allocation."""

    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            name="core_sip_in",
            sleeve=Sleeve.PASSIVE,
            market="NSE",
            broker=Broker.ZERODHA,
            asset_class="etf",
            capacity_inr=Decimal("10_00_00_000"),  # 10 Cr — far above personal scale
            cooldown_hours=24 * 30,
            expected_turnover=Decimal("0.05"),
            min_history_days=30,
        )

    def generate_signals(self, asof: datetime, market_data: dict) -> list[Signal]:
        """Emit a 'sip-buy' signal on the 1st business day of each month."""
        # TODO Phase 2: implement first-business-day check via market calendar.
        raise NotImplementedError("CoreSIPIndia.generate_signals — Phase 2")

    def proposed_orders(self, portfolio_state: dict, signals: list[Signal]) -> list[dict]:
        """Translate this month's SIP cash into per-symbol buy orders honouring INDIA_TARGETS."""
        # TODO Phase 2.
        raise NotImplementedError("CoreSIPIndia.proposed_orders — Phase 2")
