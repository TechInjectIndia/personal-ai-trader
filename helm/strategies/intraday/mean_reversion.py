"""
Bollinger-band / z-score intraday mean reversion (long-only fade).

Rule:
  1. Over a trailing window of N=20 closed 1-min bars, compute the close-price
     mean and SAMPLE stdev (N-1). The z-score of a bar's close is
     (close - mean) / sd.
  2. Fire BUY on the single bar that PIERCES the lower band: z_latest <= -K
     (oversold now) while z_prior > -K (the immediately prior bar was not yet
     oversold). This edge-trigger mirrors orb.py / vwap.py so re-scanning every
     minute never re-emits.
  3. Time gate: only between 09:45 and 14:45 IST — skip the opening-25min sigma
     instability and leave runway before the 15:15 EOD square-off.
  4. Volatility floor: band half-width (sd/mean) must be >= 0.10% of price, so a
     "2-sigma" move on a near-flat tape (a few paise of noise) is ignored.
  5. Drift guard: don't fade a clean falling knife — reject when the first-half
     vs second-half window-mean divergence is >= 1.5 × sd.
  6. Stop = mean - (K+1)·sd (one further sigma below entry; entry - 1·sd).
     Target = the 20-bar mean it reverts toward, widened to honour the 1.5
     reward-to-risk floor if the natural ~2:1 ever falls short.

Why this works: the top-5 NSE mega-caps are the most index-arbitraged,
MM-delta-hedged, ETF-anchored names on the exchange, so 2-sigma intraday
down-stretches are overwhelmingly transient liquidity air-pockets that snap
back toward the 20-min mean. This harvests exactly the reversion that bleeds
ORB. Long-only for paper v1.

Uses ONLY close prices — no dependence on (absent) traded volume or the noisy
tick_count proxy. Pure Decimal arithmetic throughout (statistics.mean/stdev
return float and would TypeError when mixed with Decimal).
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from zoneinfo import ZoneInfo

from helm.strategies.base import Signal, Strategy

IST = ZoneInfo("Asia/Kolkata")

N = 20
K = Decimal("2.0")
MIN_BARS = 22
VOL_FLOOR = Decimal("0.0010")
DRIFT_MULT = Decimal("1.5")
RR_MULTIPLIER = Decimal("1.5")
ENTRY_START = time(9, 45)
ENTRY_CUTOFF = time(14, 45)


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


def _stdev(values: list[Decimal], mean: Decimal) -> Decimal:
    """Sample (N-1) stdev in pure Decimal."""
    n = len(values)
    if n < 2:
        return Decimal("0")
    var = sum(((v - mean) ** 2 for v in values), Decimal("0")) / Decimal(n - 1)
    return var.sqrt()


def _z(close: Decimal, mean: Decimal, sd: Decimal) -> Decimal | None:
    if sd <= 0:
        return None
    return (close - mean) / sd


class MeanReversion(Strategy):
    """Z-score lower-band fade. Parametrizable (N, K) for later fast variants."""

    def __init__(self, window: int = N, k: Decimal = K) -> None:
        self.window = window
        self.k = k
        self._name = f"bbands_zscore_{window}"

    @property
    def name(self) -> str:
        return self._name

    def scan(self, symbol: str, candles: list[dict]) -> Signal | None:
        if len(candles) < MIN_BARS:
            return None

        # Trailing-N window ending on the latest closed bar.
        latest = candles[-1]
        latest_close = Decimal(str(latest["close"]))
        window_latest = [Decimal(str(c["close"])) for c in candles[-self.window:]]
        mean_latest = _mean(window_latest)
        sd_latest = _stdev(window_latest, mean_latest)
        if sd_latest <= 0:
            return None

        z_latest = _z(latest_close, mean_latest, sd_latest)
        if z_latest is None:
            return None

        # Trailing-N window ending on the PRIOR closed bar (its own mean/sd).
        prior = candles[-2]
        prior_close = Decimal(str(prior["close"]))
        window_prior = [Decimal(str(c["close"])) for c in candles[-self.window - 1:-1]]
        mean_prior = _mean(window_prior)
        sd_prior = _stdev(window_prior, mean_prior)
        z_prior = _z(prior_close, mean_prior, sd_prior)
        if z_prior is None:
            return None

        # Time gate (IST). bar_ts is tz-aware.
        bar_time = latest["bar_ts"].astimezone(IST).time()
        if not (ENTRY_START <= bar_time <= ENTRY_CUTOFF):
            return None

        # Oversold now AND prior bar not yet oversold → edge trigger (fire-once).
        if not (z_latest <= -self.k and z_prior > -self.k):
            return None

        # Volatility floor: band half-width must be a real move, not noise.
        if mean_latest <= 0 or (sd_latest / mean_latest) < VOL_FLOOR:
            return None

        # Drift guard: don't fade a clean directional sell-off.
        half = self.window // 2
        first_half = window_latest[:half]
        second_half = window_latest[half:]
        drift = abs(_mean(first_half) - _mean(second_half))
        if drift >= DRIFT_MULT * sd_latest:
            return None

        entry = latest_close
        stop = mean_latest - (self.k + Decimal("1")) * sd_latest
        if stop >= entry:
            return None
        target = mean_latest
        if target <= entry:
            return None

        # RR floor: widen target if the natural reward-to-risk falls short.
        risk = entry - stop
        if risk <= 0:
            return None
        if (target - entry) / risk < RR_MULTIPLIER:
            target = entry + RR_MULTIPLIER * risk

        lower_band = mean_latest - self.k * sd_latest

        return Signal(
            strategy=self.name,
            symbol=symbol,
            asof=latest["bar_ts"],
            side="BUY",
            entry_price=entry,
            stop_loss=stop,
            target=target,
            rationale=(
                f"bbands_zscore_{self.window}: bar at {latest['bar_ts']} closed "
                f"{entry} at z={z_latest:.2f} (<= -{self.k}), piercing the lower "
                f"band {lower_band:.2f} (mean {mean_latest:.2f}, sd {sd_latest:.4f}). "
                f"Prior bar z={z_prior:.2f} not yet oversold. Fading toward the "
                f"{self.window}-bar mean. Stop {stop:.2f} (mean-{self.k + 1}sigma). "
                f"Target {target:.2f}."
            ),
            payload={
                "window": str(self.window),
                "k": str(self.k),
                "mean": str(mean_latest),
                "sd": str(sd_latest),
                "z": str(z_latest),
                "lower_band": str(lower_band),
                "rr_multiplier": str(RR_MULTIPLIER),
                "stop_basis": "mean-3sigma",
                "target_basis": "trailing-20-mean",
                "max_hold_min": "30",
            },
        )
