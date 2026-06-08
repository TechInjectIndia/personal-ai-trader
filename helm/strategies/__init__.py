"""Strategy registry."""

from helm.strategies.base import Signal, Strategy
from helm.strategies.intraday.gap_fade import GapFade
from helm.strategies.intraday.mean_reversion import MeanReversion
from helm.strategies.intraday.orb import OpeningRangeBreakout
from helm.strategies.intraday.vwap import VWAPReclaim

ACTIVE: list[Strategy] = [
    OpeningRangeBreakout(or_minutes=5),    # fast variant, fires from 09:20 IST
    OpeningRangeBreakout(or_minutes=15),   # canonical, fires from 09:30 IST
    VWAPReclaim(),
    GapFade(),
    MeanReversion(),                       # bbands_zscore_20, ORB A/B counterpart
]

__all__ = ["Signal", "Strategy", "ACTIVE"]
