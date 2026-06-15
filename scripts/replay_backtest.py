"""
Replay backtest (FRD G3, eval-gate foundation) — re-price ACTUAL closed trades
over recorded candles_1m and report the book's economics deterministically.

This is the reusable engine the Tester's eval-gate will call (G3.2): it proves a
change's P&L impact on real history instead of trusting "it compiles". We replay
real `paper_trades` (which had sane, gate-passed sizing/targets) rather than raw
signals (the strategy backlog includes degenerate 10%-target/bad-data signals
that make a raw-signal backtest meaningless).

  python scripts/replay_backtest.py --days 14            # house book
  python scripts/replay_backtest.py --days 30 --validate # sim vs recorded P&L
  python scripts/replay_backtest.py --days 30 --compare-ratchet --folds 3

UNCLOSED trades (window ran out mid-trade) are reported but excluded from the
economics.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helm.eval.backtest import (  # noqa: E402
    _candles_after,
    closed_trades,
    replay_trades as _replay,
)
from helm.eval.metrics import book_metrics, pass_at_k  # noqa: E402
from helm.eval.replay import SimOutcome  # noqa: E402


def _trades(days: int, symbol: str | None, all_books: bool) -> list[dict]:
    return closed_trades(days, symbol, all_books)


def _validate(trades: list[dict]) -> None:
    """Fidelity check: how close is the simulator's net to the recorded net?
    (They won't match exactly — live exits used yfinance LTP, the replay uses
    candle closes — but a tight median delta means the engine is faithful.)"""
    sim = _replay(trades, apply_ratchet=True)
    recorded = [Decimal(t["net_pnl_inr"]) for t in trades
                if int(t["qty"]) > 0 and _candles_after(t["symbol"], t["entry_ts"])]
    paired = [(s.net, r) for s, r in zip(sim, recorded) if s.closed]
    if not paired:
        print("no closed trades with candles to validate against.")
        return
    deltas = sorted(abs(s - r) for s, r in paired)
    median = deltas[len(deltas) // 2]
    same_dir = sum(1 for s, r in paired if (s >= 0) == (r >= 0))
    print(f"\nfidelity vs recorded P&L (n={len(paired)}):")
    print(f"  median |sim_net − recorded_net| = ₹{median}")
    print(f"  same-sign (win/loss agreement)   = {100*same_dir/len(paired):.0f}%")


def _print_metrics(label: str, outcomes: list[SimOutcome]) -> None:
    m = book_metrics(outcomes)
    print(f"\n{label}")
    print(f"  trades={m.n} (closed {m.closed})  net=₹{m.net}  gross=₹{m.gross}  "
          f"cost=₹{m.charges}")
    print(f"  expectancy=₹{m.expectancy}/trade  E2C={m.e2c}  win%={m.win_pct:.1f}  "
          f"maxDD=₹{m.max_drawdown}")


def _fold(trades: list[dict], k: int) -> list[list[dict]]:
    """Contiguous time-folds (trades are already entry_ts-ordered)."""
    if k <= 1:
        return [trades]
    size = max(1, len(trades) // k)
    return [trades[i:i + size] for i in range(0, len(trades), size)][:k] or [trades]


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay backtest over candles_1m (G3).")
    ap.add_argument("--days", type=int, default=14, help="look-back window (default 14)")
    ap.add_argument("--symbol", help="restrict to one symbol")
    ap.add_argument("--all-books", action="store_true",
                    help="span every competitor (default: house book only)")
    ap.add_argument("--validate", action="store_true",
                    help="compare simulated net vs recorded net (fidelity check)")
    ap.add_argument("--compare-ratchet", action="store_true",
                    help="A/B the #681 ratchet (baseline ON vs candidate OFF)")
    ap.add_argument("--folds", type=int, default=1, help="time-folds for pass@k")
    args = ap.parse_args()

    trades = _trades(args.days, args.symbol, args.all_books)
    if not trades:
        print("no closed trades with a target in the window.")
        return 0
    book = "all books" if args.all_books else "house book"
    print(f"replaying {len(trades)} closed trade(s) over the last {args.days}d"
          + (f" [{args.symbol}]" if args.symbol else "") + f" — {book} …")

    if args.validate:
        _validate(trades)
        return 0

    if not args.compare_ratchet:
        _print_metrics("book (live rules, ratchet ON)", _replay(trades, apply_ratchet=True))
        return 0

    # A/B: baseline = ratchet ON (live), candidate = ratchet OFF.
    _print_metrics("BASELINE — ratchet ON (live)", _replay(trades, apply_ratchet=True))
    _print_metrics("CANDIDATE — ratchet OFF", _replay(trades, apply_ratchet=False))

    folds = _fold(trades, args.folds)
    pairs = [(book_metrics(_replay(f, apply_ratchet=True)),
              book_metrics(_replay(f, apply_ratchet=False))) for f in folds]
    gate = pass_at_k(pairs, min_trades=1)
    print(f"\npass@k (candidate=ratchet-OFF vs baseline=ratchet-ON): {gate.detail}")
    print("  (a real eval-gate HOLDs a change unless it passes; here it shows "
          "whether turning the ratchet OFF would help — HOLD means the ratchet "
          "earns its keep.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
