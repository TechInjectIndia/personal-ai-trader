"""
Forward historical backtester (FRD M5) — decider-OFF, deterministic.

Unlike helm.eval.backtest (which re-prices ALREADY-BOOKED paper_trades for the
eval-gate), this runs a strategy FORWARD over historical candles it never
traded: scan bar-by-bar → gate (F2 edge-to-cost + per-trade cap, no LLM) →
simulate the exit with the market's cost model → roll up metrics. It measures a
strategy's RAW edge net of a venue's costs — the evidence the M8 funding gate
consumes.

Determinism + honesty:
  * No look-ahead: `strategy.scan` only ever sees `candles[:i+1]`; the fill is
    the NEXT bar's open and exits use bars strictly after it.
  * No LLM: the decider is a pluggable predicate (default TakeAll). Running the
    real Claude decider over months of history is expensive + non-deterministic,
    so it is intentionally excluded — the LLM is validated forward, in paper.
  * No clock/random: same (strategy, candles, params) → identical metrics.
  * One position at a time per symbol (sequential), so the equity curve is well
    defined without a DB.

Fidelity note: exits are checked at the strategy's bar granularity (not 1-min),
the same series the scan sees — a deliberate simplification for a POC backtester.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from helm.config import live_risk_limits, live_tunable
from helm.eval.metrics import book_metrics
from helm.eval.replay import SimOutcome, simulate_trade

_TWO = Decimal("0.01")


class TakeAll:
    """Default backtest decider: take every gated signal. Measures the raw
    strategy + risk-gate + cost edge with no model in the loop."""

    name = "take_all"

    def __call__(self, signal, candles_so_far: list[dict]) -> bool:
        return True


@dataclass(frozen=True)
class BacktestReport:
    strategy: str
    market: str
    symbol: str
    bar_minutes: int
    start: datetime
    end: datetime
    decider: str
    n_signals: int
    n_trades: int
    base_cap: Decimal
    metrics: dict          # BookMetrics.as_dict()

    def as_row(self) -> dict:
        return {
            "strategy": self.strategy, "market": self.market, "symbol": self.symbol,
            "bar_minutes": self.bar_minutes, "decider": self.decider,
            "n_signals": self.n_signals, "n_trades": self.n_trades,
            "base_cap": float(self.base_cap), "metrics": self.metrics,
        }


def _size(entry: Decimal, base_cap: Decimal, fractional: bool) -> Decimal:
    """Per-trade quantity from the notional cap. Fractional venues size to 8 dp;
    integer venues floor to whole lots. Returns 0 if a unit is unaffordable."""
    if entry <= 0 or base_cap <= 0:
        return Decimal("0")
    if fractional:
        return (base_cap / entry).quantize(Decimal("0.00000001"))
    return Decimal(int(base_cap // entry))


def _e2c(cost_model, side: str, qty: Decimal, entry: Decimal, target: Decimal) -> Decimal:
    exp = cost_model.round_trip_breakdown(side, qty, entry, target).total
    return (abs(target - entry) * qty / exp) if exp > 0 else Decimal("0")


def _exit_window(after: list[dict], market, fill_bar_ts: datetime, bar_minutes: int) -> list[dict]:
    """The candles an open position is evaluated over. Sessioned venues (IN/US)
    cap to the fill's trading day; 24/7 venues cap to max_hold_min worth of bars
    (the crypto time-stop analog)."""
    if market.calendar.square_off_at() is not None:
        day = market.calendar.trading_day_key(fill_bar_ts)
        return [b for b in after if market.calendar.trading_day_key(b["bar_ts"]) == day]
    max_hold = market.max_hold_min or 1440
    return after[: max(1, max_hold // max(1, bar_minutes))]


def backtest(
    strategy,
    market,
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    bar_minutes: int | None = None,
    decider=None,
    base_cap: Decimal | None = None,
    candles: list[dict] | None = None,
) -> BacktestReport:
    """Run `strategy` over `symbol` history on `market` and return a report.

    `candles` may be injected (deterministic tests); otherwise they are fetched
    from the market's data adapter at the strategy's bar timeframe.
    """
    decider = decider or TakeAll()
    bar_minutes = bar_minutes or getattr(strategy, "bar_minutes", 1)
    base_cap = base_cap if base_cap is not None else live_risk_limits().max_position_inr
    min_e2c = live_tunable("MIN_EDGE_TO_COST")
    square_off = market.calendar.square_off_at()

    series = candles if candles is not None else market.data.historical(
        symbol, bar_minutes, start, end)

    outcomes: list[SimOutcome] = []
    n_signals = 0
    busy_until = -1   # index through which a position is held (no overlap)

    for i in range(len(series)):
        if i <= busy_until or i + 1 >= len(series):
            continue
        sig = strategy.scan(symbol, series[: i + 1])   # NO look-ahead
        if sig is None:
            continue
        n_signals += 1
        if not decider(sig, series[: i + 1]):
            continue

        entry = Decimal(series[i + 1]["open"])         # realistic next-bar-open fill
        qty = _size(entry, base_cap, market.fractional)
        if qty <= 0:
            continue
        target = Decimal(sig.target) if sig.target is not None else None
        if target is not None and _e2c(market.costs, sig.side, qty, entry, target) < min_e2c:
            continue   # F2: guaranteed net loser after costs

        after = _exit_window(series[i + 2:], market, series[i + 1]["bar_ts"], bar_minutes)
        if not after:
            continue
        outcome = simulate_trade(
            sig.side, entry, Decimal(sig.stop_loss), target, qty, after,
            apply_ratchet=square_off is not None, square_off_at=square_off,
            cost_model=market.costs,
        )
        if not outcome.closed:
            outcome = _time_stop(sig.side, entry, qty, after, market.costs)
        outcomes.append(outcome)
        busy_until = (i + 1) + outcome.bars_held

    return BacktestReport(
        strategy=getattr(strategy, "name", strategy.__class__.__name__),
        market=market.key, symbol=symbol, bar_minutes=bar_minutes,
        start=start, end=end, decider=getattr(decider, "name", "custom"),
        n_signals=n_signals, n_trades=len(outcomes), base_cap=base_cap,
        metrics=book_metrics(outcomes).as_dict(),
    )


def _time_stop(side: str, entry: Decimal, qty: Decimal,
               after: list[dict], cost_model) -> SimOutcome:
    """Force-close an unclosed 24/7 position at the window's last bar (the crypto
    max-hold time-stop), so it counts as a real exit instead of being dropped."""
    last = Decimal(after[-1]["close"])
    gross = ((last - entry) if side == "BUY" else (entry - last)) * qty
    charges = cost_model.round_trip_charges(side, qty, entry, last)
    return SimOutcome(
        side=side, entry=entry, exit_price=last, qty=int(qty) if qty == int(qty) else qty,
        exit_reason="TIME", exit_ts=after[-1]["bar_ts"], bars_held=len(after),
        gross=gross.quantize(_TWO), charges=charges.quantize(_TWO),
        net=(gross - charges).quantize(_TWO), closed=True,
    )


__all__ = ["BacktestReport", "TakeAll", "backtest"]
