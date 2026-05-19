"""
Zerodha MIS equity-intraday charge model.

Computes the realistic round-trip cost of a paper trade so that net P&L on the
dashboard mirrors what would have been booked live. Rates as published on
zerodha.com/charges as of 2026; revisit if Zerodha republishes.

Components, per round trip (BUY + SELL legs):
  - Brokerage:     min(0.03% × turnover, ₹20) per executed order
  - STT (sell):    0.025% of sell-side turnover
  - Exchange txn:  0.00297% of (buy + sell) turnover (NSE equity)
  - SEBI charges:  0.0001% of (buy + sell) turnover (₹10 per crore)
  - Stamp duty:    0.003% of buy-side turnover (intraday)
  - GST:           18% on (brokerage + exchange txn + SEBI)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

_TWO_PLACES = Decimal("0.01")
_FOUR_PLACES = Decimal("0.0001")

# Rate constants (fractions, not percent).
_BROKERAGE_RATE = Decimal("0.0003")     # 0.03%
_BROKERAGE_CAP = Decimal("20")          # ₹20 per executed order
_STT_RATE = Decimal("0.00025")          # 0.025% sell-side
_TXN_RATE = Decimal("0.0000297")        # 0.00297% both sides (NSE equity)
_SEBI_RATE = Decimal("0.000001")        # 0.0001% both sides (₹10/crore)
_STAMP_RATE = Decimal("0.00003")        # 0.003% buy-side
_GST_RATE = Decimal("0.18")


@dataclass(frozen=True)
class ChargeBreakdown:
    brokerage: Decimal
    stt: Decimal
    exchange_txn: Decimal
    sebi: Decimal
    stamp: Decimal
    gst: Decimal
    total: Decimal

    def as_dict(self) -> dict[str, float]:
        return {
            "brokerage": float(self.brokerage),
            "stt": float(self.stt),
            "exchange_txn": float(self.exchange_txn),
            "sebi": float(self.sebi),
            "stamp": float(self.stamp),
            "gst": float(self.gst),
            "total": float(self.total),
        }


def round_trip_charges(
    side: str,
    qty: int,
    entry_price: Decimal,
    exit_price: Decimal,
) -> Decimal:
    """Total round-trip charges (₹) for a closed intraday trade."""
    return _breakdown(side, qty, entry_price, exit_price).total


def round_trip_breakdown(
    side: str,
    qty: int,
    entry_price: Decimal,
    exit_price: Decimal,
) -> ChargeBreakdown:
    """Same as round_trip_charges but with per-component values for audit/UI."""
    return _breakdown(side, qty, entry_price, exit_price)


def _breakdown(
    side: str,
    qty: int,
    entry_price: Decimal,
    exit_price: Decimal,
) -> ChargeBreakdown:
    entry = Decimal(entry_price)
    exit_ = Decimal(exit_price)
    q = Decimal(qty)

    if side == "BUY":
        buy_value, sell_value = entry * q, exit_ * q
    else:
        buy_value, sell_value = exit_ * q, entry * q

    brokerage = (
        min(_BROKERAGE_CAP, buy_value * _BROKERAGE_RATE)
        + min(_BROKERAGE_CAP, sell_value * _BROKERAGE_RATE)
    )
    stt = sell_value * _STT_RATE
    txn = (buy_value + sell_value) * _TXN_RATE
    sebi = (buy_value + sell_value) * _SEBI_RATE
    stamp = buy_value * _STAMP_RATE
    gst = (brokerage + txn + sebi) * _GST_RATE
    total = brokerage + stt + txn + sebi + stamp + gst

    return ChargeBreakdown(
        brokerage=brokerage.quantize(_TWO_PLACES),
        stt=stt.quantize(_TWO_PLACES),
        exchange_txn=txn.quantize(_FOUR_PLACES),
        sebi=sebi.quantize(_FOUR_PLACES),
        stamp=stamp.quantize(_TWO_PLACES),
        gst=gst.quantize(_TWO_PLACES),
        total=total.quantize(_TWO_PLACES),
    )
