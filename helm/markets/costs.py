"""
Per-venue cost models (FRD M1 wires Zerodha; M4 adds US + crypto).

The CostModel interface mirrors `helm.charges` exactly, so the same model that
prices the books also drives the cost-aware gates (F2 min-edge, M5 backtest).
ZerodhaCosts is a thin adapter over the canonical `helm.charges` module — there
is ONE NSE charge schedule, and this keeps it the single source of truth.
"""

from __future__ import annotations

from decimal import Decimal

from helm import charges
from helm.charges import ChargeBreakdown


class ZerodhaCosts:
    """India NSE intraday equity (STT/stamp/GST/SEBI/exchange) — delegates to
    the canonical helm.charges model so books and gate never drift."""

    def round_trip_breakdown(
        self, side: str, qty: Decimal, entry: Decimal, exit_price: Decimal
    ) -> ChargeBreakdown:
        return charges.round_trip_breakdown(side, qty, entry, exit_price)

    def round_trip_charges(
        self, side: str, qty: Decimal, entry: Decimal, exit_price: Decimal
    ) -> Decimal:
        return charges.round_trip_charges(side, qty, entry, exit_price)
