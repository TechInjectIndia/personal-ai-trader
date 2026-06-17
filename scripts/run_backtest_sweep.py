"""
Run a cross-market backtest evidence sweep (S6) → persist to backtest_runs.

    python scripts/run_backtest_sweep.py --markets CRYPTO --days 180 --persist
    python scripts/run_backtest_sweep.py            # plan over all registered markets

Decider-OFF; the only cost is data fetches. Use --plan to print the combo plan
without running anything.
"""

from __future__ import annotations

import argparse

from helm.eval.sweep import run_sweep, sweep_plan


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--markets", nargs="*", default=None,
                   help="market keys (default: all registered)")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--persist", action="store_true", help="write backtest_runs rows")
    p.add_argument("--plan", action="store_true", help="print the combo plan and exit")
    args = p.parse_args()

    if args.plan:
        plan = sweep_plan(args.markets)
        for mkt, strat, sym in plan:
            print(f"{mkt:8} {strat:24} {sym}")
        print(f"\n{len(plan)} combos")
        return 0

    results = run_sweep(args.markets, days=args.days, persist=args.persist)
    for r in results:
        print(f"{r['market']:8} {r['strategy']:24} {r['symbol']:8} "
              f"signals={r['n_signals']:>4} trades={r['n_trades']:>4} net={r['net']:>10.2f}")
    print(f"\n{len(results)} combos with trades"
          + ("  (persisted)" if args.persist else "  (dry run — pass --persist to save)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
