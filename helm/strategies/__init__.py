"""Strategy registry."""

from helm.strategies.base import Signal, Strategy
from helm.strategies.intraday.context_momentum import ContextMomentum
from helm.strategies.intraday.gap_fade import GapFade
from helm.strategies.intraday.mean_reversion import MeanReversion
from helm.strategies.intraday.orb import OpeningRangeBreakout
from helm.strategies.intraday.vwap import VWAPReclaim


# --- F6: 5-min timeframe variants -------------------------------------------
# These consume 5-min aggregated bars (scan_signals feeds resample_candles(.,5))
# and carry a _5m-suffixed name so F1 attributes per timeframe and the
# dedupe/trigger-once guards run independently from the 1-min instances.
# NOTE: ORB is deliberately NOT given a 5-min-bar variant — orb_5m/orb_15m are
# opening-RANGE minutes computed off 1-min bars, a different concept.
class VWAPReclaim5m(VWAPReclaim):
    name = "vwap_reclaim_5m"
    bar_minutes = 5


class GapFade5m(GapFade):
    name = "gap_fade_5m"
    bar_minutes = 5


ACTIVE: list[Strategy] = [
    OpeningRangeBreakout(or_minutes=5),    # fast variant, fires from 09:20 IST
    OpeningRangeBreakout(or_minutes=15),   # canonical, fires from 09:30 IST
    VWAPReclaim(),
    GapFade(),
    MeanReversion(),                       # bbands_zscore_20, ORB A/B counterpart
    # F6 5-min A/B counterparts (distinct names → independent F1 + dedupe).
    MeanReversion(bar_minutes=5),          # bbands_zscore_20_5m
    VWAPReclaim5m(),                       # vwap_reclaim_5m
    GapFade5m(),                           # gap_fade_5m
    # F7-P3c: context-driven signals. requires_context=True → scan_signals SKIPS
    # it unless CONTEXT_SIGNALS_ENABLED (default OFF → inert).
    ContextMomentum(),                     # context_momentum
]

__all__ = ["Signal", "Strategy", "ACTIVE"]
