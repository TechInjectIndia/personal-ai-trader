"""
VWAP reclaim.

Rule:
  1. Compute anchored VWAP from the day's open using each 1-min bar's typical
     price (HLC/3) weighted by tick_count. (We don't capture true volume —
     yfinance fast_info only gives last_price — so tick_count is our liquidity
     proxy. For the top-5 large-caps in the watchlist, busier bars do
     correspond to busier tape, so the proxy is informative even if not equal
     to traded-volume VWAP.)
  2. Wait until at least one bar has closed BELOW VWAP (price has spent time
     under the line). This filters out trends that never tested down.
  3. Fire BUY on the first bar that closes ABOVE VWAP after that dip, with the
     bar itself green (close > open) — confirmation that buyers showed up.
  4. Stop = the lowest low across the dip + 1 bar; target = entry + 1.5 × stop
     distance.

Long-only for paper v1.
"""

from __future__ import annotations

from decimal import Decimal

from helm.strategies.base import Signal, Strategy

MIN_BARS = 10
RR_MULTIPLIER = Decimal("1.5")


def _anchored_vwap(candles: list[dict]) -> list[Decimal]:
    """Cumulative tick-count-weighted typical price, one entry per bar."""
    out: list[Decimal] = []
    cum_pv = Decimal("0")
    cum_v = Decimal("0")
    for c in candles:
        tp = (Decimal(str(c["high"])) + Decimal(str(c["low"])) + Decimal(str(c["close"]))) / 3
        v = Decimal(int(c["tick_count"]) or 1)
        cum_pv += tp * v
        cum_v += v
        out.append(cum_pv / cum_v)
    return out


class VWAPReclaim(Strategy):
    name = "vwap_reclaim"

    def scan(self, symbol: str, candles: list[dict]) -> Signal | None:
        if len(candles) < MIN_BARS:
            return None

        vwaps = _anchored_vwap(candles)

        latest = candles[-1]
        prior = candles[-2]
        latest_close = Decimal(str(latest["close"]))
        latest_open = Decimal(str(latest["open"]))
        prior_close = Decimal(str(prior["close"]))

        # Reclaim: prior bar closed at/under VWAP, this bar closed above.
        if not (prior_close <= vwaps[-2] and latest_close > vwaps[-1]):
            return None
        # Confirmation: this bar must be green.
        if latest_close <= latest_open:
            return None

        # Need at least one earlier bar that actually traded BELOW VWAP — not
        # just the immediately prior bar — so we know there was a dip to fade.
        had_dip = any(
            Decimal(str(c["close"])) < v
            for c, v in zip(candles[:-1], vwaps[:-1], strict=True)
        )
        if not had_dip:
            return None

        # Stop: lowest low among recent bars (last 5 closed bars before this).
        lookback = candles[-6:-1] if len(candles) >= 6 else candles[:-1]
        stop = min(Decimal(str(c["low"])) for c in lookback)
        if stop >= latest_close:
            return None  # malformed; skip

        risk = latest_close - stop
        target = latest_close + RR_MULTIPLIER * risk

        return Signal(
            strategy=self.name,
            symbol=symbol,
            asof=latest["bar_ts"],
            side="BUY",
            entry_price=latest_close,
            stop_loss=stop,
            target=target,
            rationale=(
                f"VWAP reclaim: bar at {latest['bar_ts']} closed {latest_close} "
                f"above anchored VWAP {vwaps[-1]:.2f} after dip below. "
                f"Stop {stop} (5-bar low). Target {target:.2f} ({RR_MULTIPLIER}× risk)."
            ),
            payload={
                "vwap_now": str(vwaps[-1]),
                "vwap_prior": str(vwaps[-2]),
                "rr_multiplier": str(RR_MULTIPLIER),
                "stop_basis": "5-bar low",
            },
        )
