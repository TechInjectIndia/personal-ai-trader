"""
Print the fund-only-winners leaderboard (S3): rank agent×market×strategy combos
by net expectancy, and flag the fund candidates.

    python scripts/leaderboard.py [--days 30] [--min-trades 30]
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from helm.eval.leaderboard import combo_scores, fund_candidates


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--min-trades", type=int, default=1)
    p.add_argument("--fund-min-trades", type=int, default=30)
    args = p.parse_args()

    rows = combo_scores(days=args.days, min_trades=args.min_trades)
    cands = {c.label() for c in fund_candidates(days=args.days,
                                                min_trades=args.fund_min_trades,
                                                min_expectancy=Decimal("0"))}
    print(f"{'combo (market:agent:strategy)':45} {'n':>4} {'net':>10} "
          f"{'exp/trade':>10} {'win%':>6} {'cost_drag':>9}  fund?")
    print("-" * 95)
    for c in rows:
        flag = "✅" if c.label() in cands else ""
        print(f"{c.label():45} {c.n:>4} {float(c.net):>10.2f} "
              f"{float(c.expectancy):>10.2f} {float(c.win_pct):>6.1f} "
              f"{float(c.cost_drag):>9.2f}  {flag}")
    if not rows:
        print("(no closed trades in window)")
    print(f"\nFund candidates (≥{args.fund_min_trades} trades, net expectancy > 0): "
          f"{len(cands)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
