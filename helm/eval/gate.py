"""
Eval-gate (FRD G3.2) — the deterministic P&L regression guard the Tester runs
before signing off a release.

The blind Tester verifies "compiles + smoke-passes," which is why good and bad
changes are reverted ~7:1 by luck. This gate re-prices the recent house book
over recorded candles (helm.eval.replay) under the CURRENT code and pass@k's it
against the metrics baseline captured at the last verified release:

  * no baseline yet               → ABSTAIN, establish the baseline (first run).
  * candidate regresses economics → HOLD (the Tester fails the stage → revert).
  * candidate holds/improves       → PASS, advance the baseline.

What it can and cannot see (honest scope): the replay re-prices fixed historical
trades, so it surfaces changes to the modeled economics — the exit/ratchet
policy (read live from config) and the trailing window of trades. A change that
touches none of those replays flat → no regression → PASS (a safe no-op). It is
NOT a per-change counterfactual of arbitrary code (that needs the pre-change
tree); it is a regression floor on the levers the engine models. Everything is
pure given its injected replay/baseline I/O, so it unit-tests without a DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from helm.eval.metrics import BookMetrics, book_metrics, fold_verdict
from helm.eval.replay import SimOutcome

BASELINE_KEY = "eval_gate_baseline"


@dataclass(frozen=True)
class _Baseline:
    """Just the fields fold_verdict reads, rehydrated from the stored snapshot."""
    net: Decimal
    max_drawdown: Decimal
    closed: int


@dataclass(frozen=True)
class GateOutcome:
    verdict: str                 # PASS | HOLD | ABSTAIN
    detail: str
    candidate: BookMetrics
    baseline: _Baseline | None

    @property
    def ok(self) -> bool:
        """A non-blocking verdict (PASS or first-run ABSTAIN)."""
        return self.verdict in ("PASS", "ABSTAIN")


def _snapshot(m: BookMetrics) -> dict:
    return {"net": str(m.net), "max_drawdown": str(m.max_drawdown), "closed": m.closed}


def _rehydrate(raw: object) -> _Baseline | None:
    if not isinstance(raw, dict):
        return None
    try:
        return _Baseline(Decimal(str(raw["net"])),
                         Decimal(str(raw["max_drawdown"])), int(raw["closed"]))
    except (KeyError, TypeError, ValueError):
        return None


def evaluate(
    *,
    min_trades: int = 5,
    drawdown_tolerance: Decimal = Decimal("1.10"),
    replay_fn: Callable[[], list[SimOutcome]] | None = None,
    load_baseline: Callable[[], object] | None = None,
    save_baseline: Callable[[dict], None] | None = None,
) -> GateOutcome:
    """Run the gate. I/O is injectable for tests; the defaults hit the live
    replay (recent house window) and the `settings` baseline row."""
    if replay_fn is None:
        from helm.eval.backtest import replay_house_window  # lazy: DB-touching
        replay_fn = replay_house_window
    if load_baseline is None or save_baseline is None:
        from helm.data.store import get_setting, set_setting

        def _load() -> object:
            return get_setting(BASELINE_KEY)

        def _save(snap: dict) -> None:
            set_setting(BASELINE_KEY, snap, actor="eval_gate")

        load_baseline = load_baseline or _load
        save_baseline = save_baseline or _save

    candidate = book_metrics(replay_fn())
    baseline = _rehydrate(load_baseline())

    if baseline is None:
        save_baseline(_snapshot(candidate))
        return GateOutcome("ABSTAIN",
                           f"baseline established (net=₹{candidate.net}, "
                           f"closed={candidate.closed})", candidate, None)

    if candidate.closed < min_trades:
        # Too few replayable trades to judge — don't block on noise.
        return GateOutcome("ABSTAIN",
                           f"only {candidate.closed} replayable trades "
                           f"(< {min_trades}) — not enough to gate", candidate, baseline)

    v = fold_verdict(baseline, candidate, min_trades=min_trades,  # type: ignore[arg-type]
                     drawdown_tolerance=drawdown_tolerance)
    # A regression = candidate net materially below baseline OR drawdown worse.
    regressed = (candidate.net < baseline.net) or (not v.non_regressive)
    if regressed:
        return GateOutcome(
            "HOLD",
            f"regression: net ₹{candidate.net} vs baseline ₹{baseline.net}, "
            f"maxDD ₹{candidate.max_drawdown} vs ₹{baseline.max_drawdown}",
            candidate, baseline)

    save_baseline(_snapshot(candidate))   # advance the baseline on a clean pass
    return GateOutcome(
        "PASS",
        f"no regression: net ₹{candidate.net} ≥ baseline ₹{baseline.net}, "
        f"drawdown within tolerance", candidate, baseline)


__all__ = ["evaluate", "GateOutcome", "BASELINE_KEY"]
