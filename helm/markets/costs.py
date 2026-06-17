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

_TWO = Decimal("0.01")
_FOUR = Decimal("0.0001")


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


class AlpacaEquityCosts:
    """US equities via Alpaca: $0 commission. The only round-trip costs are the
    SEC fee + FINRA TAF, both charged on the SELL leg. Rates as published 2026
    (revisit if SEC/FINRA republish). Components map onto ChargeBreakdown's
    nearest fields (sebi=SEC regulator fee, exchange_txn=FINRA TAF); `total` is
    exact, which is what the books and the cost gates consume."""

    _SEC_RATE = Decimal("0.0000278")      # SEC fee per $ of sell notional
    _TAF_PER_SHARE = Decimal("0.000166")  # FINRA TAF per share sold
    _TAF_CAP = Decimal("8.30")            # per-trade FINRA TAF cap

    def round_trip_breakdown(
        self, side: str, qty: Decimal, entry: Decimal, exit_price: Decimal
    ) -> ChargeBreakdown:
        q = Decimal(qty)
        sell_value = (exit_price if side == "BUY" else entry) * q
        sec = (sell_value * self._SEC_RATE).quantize(_FOUR)
        taf = min(self._TAF_CAP, q * self._TAF_PER_SHARE).quantize(_FOUR)
        total = (sec + taf).quantize(_TWO)
        return ChargeBreakdown(
            brokerage=Decimal("0.00"), stt=Decimal("0.00"),
            exchange_txn=taf, sebi=sec, stamp=Decimal("0.00"),
            gst=Decimal("0.00"), total=total,
        )

    def round_trip_charges(
        self, side: str, qty: Decimal, entry: Decimal, exit_price: Decimal
    ) -> Decimal:
        return self.round_trip_breakdown(side, qty, entry, exit_price).total


class CryptoBpsCosts:
    """Crypto spot: a flat taker fee (in fractional bps) on BOTH legs, no
    statutory taxes. Default 0.10% (Binance spot taker); set per venue. The fee
    lands in `brokerage`; `total` is exact."""

    def __init__(self, taker_bps: Decimal = Decimal("0.0010")) -> None:
        self.taker_bps = Decimal(taker_bps)

    def round_trip_breakdown(
        self, side: str, qty: Decimal, entry: Decimal, exit_price: Decimal
    ) -> ChargeBreakdown:
        q = Decimal(qty)
        buy_value = (entry if side == "BUY" else exit_price) * q
        sell_value = (exit_price if side == "BUY" else entry) * q
        fee = ((buy_value + sell_value) * self.taker_bps).quantize(_TWO)
        return ChargeBreakdown(
            brokerage=fee, stt=Decimal("0.00"), exchange_txn=Decimal("0.0000"),
            sebi=Decimal("0.0000"), stamp=Decimal("0.00"), gst=Decimal("0.00"),
            total=fee,
        )

    def round_trip_charges(
        self, side: str, qty: Decimal, entry: Decimal, exit_price: Decimal
    ) -> Decimal:
        return self.round_trip_breakdown(side, qty, entry, exit_price).total
