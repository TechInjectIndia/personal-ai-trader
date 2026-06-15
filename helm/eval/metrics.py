"""
Eval-gate metrics + pass@k comparison (FRD G3).

`book_metrics` rolls a list of SimOutcomes into the same economic terms the
digest/REVIEW use (net, expectancy, E2C, win%, max drawdown, n). `fold_verdict`
and `pass_at_k` turn a baseline-vs-candidate comparison across k time-folds into
a ship/hold decision — the deterministic gate that replaces the Tester's blind
7:1 "it compiles" revert lottery.

Guards built in (a change must not game one metric):
  * a candidate that improves expectancy by trading almost nothing fails the
    `min_trades` floor;
  * a candidate that improves net but deepens drawdown fails the non-regression
    check (`drawdown_tolerance`).

All pure — no DB, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import ceil

from helm.eval.replay import SimOutcome


@dataclass(frozen=True)
class BookMetrics:
    n: int
    closed: int
    gross: Decimal
    charges: Decimal
    net: Decimal
    expectancy: Decimal          # net / closed
    e2c: Decimal                 # gross / charges (edge-to-cost)
    win_pct: float               # % of closed trades with net > 0
    max_drawdown: Decimal        # worst peak-to-trough on cumulative net

    def as_dict(self) -> dict:
        return {
            "n": self.n, "closed": self.closed,
            "gross": float(self.gross), "charges": float(self.charges),
            "net": float(self.net), "expectancy": float(self.expectancy),
            "e2c": float(self.e2c), "win_pct": round(self.win_pct, 1),
            "max_drawdown": float(self.max_drawdown),
        }


def _max_drawdown(nets_in_order: list[Decimal]) -> Decimal:
    """Worst peak-to-trough dip on the running cumulative-net curve (≥ 0)."""
    peak = Decimal("0")
    cum = Decimal("0")
    worst = Decimal("0")
    for x in nets_in_order:
        cum += x
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def book_metrics(outcomes: list[SimOutcome]) -> BookMetrics:
    """Aggregate replayed outcomes. Only CLOSED trades count toward net/win/DD;
    UNCLOSED (window ran out mid-trade) are reported in `n` but excluded from the
    economics so a truncated window can't flatter or punish a change."""
    closed = [o for o in outcomes if o.closed]
    n_closed = len(closed)
    gross = sum((o.gross for o in closed), Decimal("0"))
    charges = sum((o.charges for o in closed), Decimal("0"))
    net = sum((o.net for o in closed), Decimal("0"))
    wins = sum(1 for o in closed if o.net > 0)
    expectancy = (net / n_closed).quantize(Decimal("0.01")) if n_closed else Decimal("0")
    e2c = (gross / charges).quantize(Decimal("0.01")) if charges > 0 else Decimal("0")
    win_pct = (100.0 * wins / n_closed) if n_closed else 0.0
    # Drawdown over the realised sequence, ordered by exit time (None last).
    ordered = sorted(closed, key=lambda o: (o.exit_ts is None, o.exit_ts))
    return BookMetrics(
        n=len(outcomes), closed=n_closed, gross=gross, charges=charges, net=net,
        expectancy=expectancy, e2c=e2c, win_pct=win_pct,
        max_drawdown=_max_drawdown([o.net for o in ordered]),
    )


@dataclass(frozen=True)
class FoldVerdict:
    improved: bool               # candidate strictly better on net this fold
    non_regressive: bool         # drawdown not materially worse
    enough_trades: bool          # candidate cleared the min-trades floor
    passed: bool                 # all three


def fold_verdict(baseline: BookMetrics, candidate: BookMetrics, *,
                 min_trades: int = 5,
                 drawdown_tolerance: Decimal = Decimal("1.10")) -> FoldVerdict:
    """Did the candidate beat the baseline on ONE fold?

    Pass requires: candidate net > baseline net, candidate drawdown ≤ baseline ×
    tolerance (no worse-than-10% deeper drawdown by default), and candidate
    cleared `min_trades` closed trades (no win-by-not-trading)."""
    improved = candidate.net > baseline.net
    cap = baseline.max_drawdown * drawdown_tolerance
    non_regressive = candidate.max_drawdown <= cap
    enough_trades = candidate.closed >= min_trades
    return FoldVerdict(improved, non_regressive, enough_trades,
                       improved and non_regressive and enough_trades)


@dataclass(frozen=True)
class GateResult:
    passed: bool
    folds: int
    folds_passed: int
    required: int
    verdicts: list[FoldVerdict]
    detail: str


def pass_at_k(fold_pairs: list[tuple[BookMetrics, BookMetrics]], *,
              threshold_frac: float = 0.6,
              min_trades: int = 5,
              drawdown_tolerance: Decimal = Decimal("1.10")) -> GateResult:
    """ECC-style pass@k: the candidate ships only if it wins on at least
    ⌈k·threshold_frac⌉ of the k folds. `fold_pairs` is [(baseline, candidate)]
    per fold. A change that helps in one cherry-picked window but not robustly
    across folds does NOT pass."""
    k = len(fold_pairs)
    if k == 0:
        return GateResult(False, 0, 0, 0, [], "no folds to evaluate")
    verdicts = [fold_verdict(b, c, min_trades=min_trades,
                             drawdown_tolerance=drawdown_tolerance)
                for b, c in fold_pairs]
    folds_passed = sum(1 for v in verdicts if v.passed)
    required = ceil(k * threshold_frac)
    passed = folds_passed >= required
    detail = (f"passed {folds_passed}/{k} folds (need {required}); "
              f"{'SHIP' if passed else 'HOLD'}")
    return GateResult(passed, k, folds_passed, required, verdicts, detail)


__all__ = ["BookMetrics", "book_metrics", "FoldVerdict", "fold_verdict",
           "GateResult", "pass_at_k"]
