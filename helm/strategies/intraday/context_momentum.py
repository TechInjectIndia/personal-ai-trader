"""Context-driven momentum (F7-P3c) — long on a strong bullish external catalyst
confirmed by price.

The Context Engine microservice scores per-symbol news/event conviction in
[-1, 1]. This strategy fires a BUY when that score is strongly bullish AND the
latest bar confirms upward (close > prior close) — i.e. the catalyst is real and
price is already moving with it, not fading.

Decoupling: the strategy stays a pure function of (candles, self.context). It
NEVER touches the DB or HTTP — scan_signals resolves the context (via the
fail-open bot client) and sets `self.context` before calling scan(). The whole
strategy is inert unless CONTEXT_SIGNALS_ENABLED (it's SKIPPED in scan_signals
otherwise) and unless the engine is actually returning a fresh score.

Cost discipline is delegated downstream exactly like every other strategy: the
F2 E2C gate and F4 target floor apply at execution. Long-only for paper v1.
"""
from __future__ import annotations

from decimal import Decimal

from helm.config import CONTEXT_SIGNAL_THRESHOLD
from helm.strategies.base import Signal, Strategy

STOP_PCT = Decimal("0.005")    # 0.5% protective stop
TARGET_PCT = Decimal("0.008")  # 0.8% target → clears the F4 0.6% floor, RR ~1.6


class ContextMomentum(Strategy):
    requires_context = True

    def __init__(self) -> None:
        # Set by scan_signals each scan (Decimal score in [-1,1], or None).
        self.context: Decimal | None = None

    @property
    def name(self) -> str:
        return "context_momentum"

    def scan(self, symbol: str, candles: list[dict]) -> Signal | None:
        ctx = self.context
        if ctx is None or ctx < CONTEXT_SIGNAL_THRESHOLD:
            return None
        if len(candles) < 2:
            return None

        latest = candles[-1]
        prior = candles[-2]
        latest_close = Decimal(str(latest["close"]))
        prior_close = Decimal(str(prior["close"]))
        # Price must confirm the bullish catalyst (moving up, not fading).
        if latest_close <= prior_close:
            return None

        entry = latest_close
        stop = (entry * (Decimal("1") - STOP_PCT)).quantize(Decimal("0.01"))
        target = (entry * (Decimal("1") + TARGET_PCT)).quantize(Decimal("0.01"))
        if stop >= entry or target <= entry:
            return None

        return Signal(
            strategy=self.name,
            symbol=symbol,
            asof=latest["bar_ts"],
            side="BUY",
            entry_price=entry,
            stop_loss=stop,
            target=target,
            rationale=(
                f"context_momentum: external context score {ctx:.2f} "
                f">= {CONTEXT_SIGNAL_THRESHOLD} (bullish catalyst) and price "
                f"confirms up ({prior_close} -> {latest_close}). Entry {entry}, "
                f"stop {stop} (-{STOP_PCT:%}), target {target} (+{TARGET_PCT:%})."
            ),
            payload={
                "context_score": str(ctx),
                "threshold": str(CONTEXT_SIGNAL_THRESHOLD),
                "stop_pct": str(STOP_PCT),
                "target_pct": str(TARGET_PCT),
            },
        )
