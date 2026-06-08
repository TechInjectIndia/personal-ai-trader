"""
Gap-down fade.

Rule:
  1. Fetch yesterday's close (yfinance, daily bar).
  2. If today's first 1-min bar's open is at least GAP_THRESHOLD below
     yesterday's close, the stock gapped down.
  3. While we are still in the first 30 minutes of the session, fire BUY when
     a bar closes back ABOVE yesterday's close — the gap has been reclaimed.
  4. Stop = today's low so far (intraday low). Target = entry + 1.5 × stop
     distance.

Why this is interesting:
  - Gap-fade has decades of evidence in equity microstructure literature; gaps
     fill more often than not on liquid large-caps within the same session.
  - Long-only fade of a gap-DOWN is symmetric with the watchlist (all 5 names
     are long-bias, blue-chip).

Caveats:
  - Cache yesterday's close per symbol so we don't slam yfinance on every scan.
  - Skip if yesterday's close lookup fails.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from functools import lru_cache

import yfinance as yf

from helm.config import MIN_TARGET_PCT
from helm.strategies._moves import enforce_min_move
from helm.strategies.base import Signal, Strategy

GAP_THRESHOLD_PCT = Decimal("0.50")     # 0.5% gap-down to qualify
ENTRY_CUTOFF = time(9, 45)              # only fade in first 30 min of session
RR_MULTIPLIER = Decimal("1.5")


@lru_cache(maxsize=32)
def _yesterday_close(symbol: str) -> Decimal | None:
    """Cached for the lifetime of the process. Cron re-spawns each run, so
    cache resets implicitly between scans."""
    try:
        hist = yf.Ticker(f"{symbol}.NS").history(period="5d", interval="1d")
        if hist.empty or len(hist) < 2:
            return None
        # Last row is today's session in progress; -2 is yesterday's close.
        return Decimal(str(hist["Close"].iloc[-2]))
    except Exception:
        return None


class GapFade(Strategy):
    name = "gap_fade"

    def scan(self, symbol: str, candles: list[dict]) -> Signal | None:
        if len(candles) < 2:
            return None

        latest = candles[-1]
        latest_ts = latest["bar_ts"]
        if hasattr(latest_ts, "time") and latest_ts.time() > ENTRY_CUTOFF:
            return None

        prev_close = _yesterday_close(symbol)
        if prev_close is None or prev_close <= 0:
            return None

        today_open = Decimal(str(candles[0]["open"]))
        gap_pct = (prev_close - today_open) / prev_close * 100
        if gap_pct < GAP_THRESHOLD_PCT:
            return None  # not a gap-down (or too small)

        latest_close = Decimal(str(latest["close"]))
        prior_close = Decimal(str(candles[-2]["close"]))

        # Reclaim: prior bar still under prev_close, this bar closes >= prev_close.
        if not (prior_close < prev_close and latest_close >= prev_close):
            return None

        intraday_low = min(Decimal(str(c["low"])) for c in candles)
        stop = intraday_low
        if stop >= latest_close:
            return None

        risk = latest_close - stop
        # Natural 1.5x-risk target, floored at MIN_TARGET_PCT (F4). Stop unchanged,
        # so any widening only raises realized RR above the 1.5 floor.
        target = enforce_min_move(
            latest_close, latest_close + RR_MULTIPLIER * risk, "BUY", MIN_TARGET_PCT
        )

        return Signal(
            strategy=self.name,
            symbol=symbol,
            asof=latest_ts,
            side="BUY",
            entry_price=latest_close,
            stop_loss=stop,
            target=target,
            rationale=(
                f"Gap-fade: yesterday's close {prev_close}, today opened {today_open} "
                f"({gap_pct:.2f}% gap-down). Bar at {latest_ts} reclaimed {prev_close} "
                f"closing {latest_close}. Stop {stop} (intraday low). "
                f"Target {target:.2f} ({RR_MULTIPLIER}× risk)."
            ),
            payload={
                "yesterday_close": str(prev_close),
                "today_open": str(today_open),
                "gap_pct": f"{gap_pct:.2f}",
                "rr_multiplier": str(RR_MULTIPLIER),
            },
        )
