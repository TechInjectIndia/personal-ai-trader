"""
League status — print the competition leaderboard, mandates and quota state.

Read-only, $0 (no backend calls). A terminal-friendly view of the same data the
dashboard's Competition League page shows, handy for cron-free spot checks.

Usage:
  python scripts/league_status.py            # leaderboard + mandates + quotas
  python scripts/league_status.py --quotas   # just backend quota state
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helm.competition.leaderboard import leaderboard
from helm.competition.mandate import current_mandate, current_week_start
from helm.competition.quota import quota_status


def _print_leaderboard() -> None:
    rows = leaderboard()
    print(f"\n=== Leaderboard (week of {current_week_start():%Y-%m-%d}) ===")
    print(f"{'#':>2}  {'competitor':<18} {'backend':<9} {'equity':>10} "
          f"{'P&L':>9} {'prog%':>7} {'open':>4} {'trades':>6} {'win%':>5} {'E2C':>5}")
    for r in rows:
        win = f"{r.win_rate_pct:.0f}" if r.win_rate_pct is not None else "-"
        e2c = f"{r.e2c}" if r.e2c is not None else "-"
        print(f"{r.rank:>2}  {r.competitor_id:<18} {r.backend:<9} "
              f"{float(r.equity):>10,.0f} {float(r.realised_net_pnl):>+9,.0f} "
              f"{r.progress_pct:>+7.1f} {r.open_positions:>4} {r.trades:>6} {win:>5} {e2c:>5}")


def _print_mandates() -> None:
    print("\n=== This week's mandates ===")
    rows = leaderboard()
    any_mandate = False
    for r in rows:
        if r.autonomy_level != "freestyle":
            continue
        m = current_mandate(r.competitor_id)
        if m and m.get("universe"):
            any_mandate = True
            print(f"  {r.competitor_id:<18} {list(m['universe'])}")
    if not any_mandate:
        print("  (none yet — run scripts/plan_mandates.py)")


def _print_quotas() -> None:
    print("\n=== Backend quota state ===")
    rows = quota_status()
    if not rows:
        print("  (no calls recorded yet)")
        return
    for q in rows:
        flag = "  PAUSED" if q["paused"] else ""
        print(f"  {q['backend']:<9} {q['calls_used']:>4}/{q['max_calls']:<5} "
              f"(window {q['window_minutes']}m){flag}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--quotas", action="store_true", help="show only quota state")
    args = p.parse_args()

    if args.quotas:
        _print_quotas()
        return 0

    _print_leaderboard()
    _print_mandates()
    _print_quotas()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
