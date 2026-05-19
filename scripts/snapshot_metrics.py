"""
Daily metrics snapshot — runs at 15:35 IST (10:05 UTC), Mon-Fri.

Captures the equity, win rate, expectancy, drawdown, take rate, signal/
decision counts, and queue depths into `metrics_snapshots`. The dashboard's
"Self-Improvement Loop" page uses this for the goal-progress curve.

One snapshot per trading day is the design. Re-runs the same day UPSERT on
the snapshot_ts date — we keep only the latest snapshot per IST day.

Failure modes:
  - Bad wallet state computation → log to audit, exit 1 (cron will retry next day).
  - DB write failure → propagate; cron logs the traceback.

Manual:
  python scripts/snapshot_metrics.py
  python scripts/snapshot_metrics.py --backfill 10   # snapshot last 10 days
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helm.data.store import conn, insert_audit
from helm.wallet import wallet_state

IST = ZoneInfo("Asia/Kolkata")


def _running_max_drawdown() -> Decimal:
    """Worst peak-to-trough drawdown across all closed trades, in rupees.

    Walks closed trades in exit-time order, tracks the running equity peak,
    returns the maximum (positive) drop below it.
    """
    with conn() as c:
        rows = list(c.execute(
            """
            SELECT COALESCE(net_pnl_inr, pnl_inr) AS pnl
            FROM paper_trades
            WHERE status='CLOSED'
            ORDER BY exit_ts ASC NULLS LAST, id ASC
            """
        ))
    if not rows:
        return Decimal("0")
    equity = Decimal("0")
    peak = Decimal("0")
    max_dd = Decimal("0")
    for r in rows:
        equity += Decimal(r["pnl"] or 0)
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return max_dd


def take_snapshot(*, note: str | None = None) -> dict:
    w = wallet_state()
    now = datetime.now(IST)

    with conn() as c:
        # Trade aggregates (all-time)
        tt = c.execute(
            """
            SELECT
              COUNT(*) AS n,
              COUNT(*) FILTER (WHERE COALESCE(net_pnl_inr, pnl_inr) > 0) AS wins,
              COUNT(*) FILTER (WHERE COALESCE(net_pnl_inr, pnl_inr) <= 0) AS losses,
              COALESCE(AVG(COALESCE(net_pnl_inr, pnl_inr))
                FILTER (WHERE COALESCE(net_pnl_inr, pnl_inr) > 0), 0) AS avg_win,
              COALESCE(AVG(COALESCE(net_pnl_inr, pnl_inr))
                FILTER (WHERE COALESCE(net_pnl_inr, pnl_inr) <= 0), 0) AS avg_loss
            FROM paper_trades WHERE status='CLOSED'
            """
        ).fetchone()
        # Today's signals / decisions
        st = c.execute(
            """
            SELECT COUNT(*) AS n FROM signals
            WHERE ts >= date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata')
                       AT TIME ZONE 'Asia/Kolkata'
            """
        ).fetchone()
        dt = c.execute(
            """
            SELECT
              COUNT(*) AS n,
              COUNT(*) FILTER (WHERE verdict='TAKE') AS takes
            FROM decisions
            WHERE ts >= date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata')
                       AT TIME ZONE 'Asia/Kolkata'
            """
        ).fetchone()
        # Queue depths
        po = c.execute(
            "SELECT COUNT(*) AS n FROM improvement_proposals WHERE status='open'"
        ).fetchone()
        to = c.execute(
            "SELECT COUNT(*) AS n FROM agent_tasks "
            "WHERE status IN ('open','in_progress','needs_human')"
        ).fetchone()
        ru = c.execute(
            "SELECT COUNT(*) AS n FROM releases WHERE status='deployed'"
        ).fetchone()

    total = int(tt["n"])
    wins = int(tt["wins"])
    losses = int(tt["losses"])
    win_rate = (Decimal(wins) / Decimal(total) * 100) if total else None
    avg_win = Decimal(tt["avg_win"])
    avg_loss = Decimal(tt["avg_loss"])
    expectancy: Decimal | None
    if total:
        expectancy = (Decimal(wins) * avg_win + Decimal(losses) * avg_loss) / Decimal(total)
    else:
        expectancy = None

    decisions_n = int(dt["n"])
    take_rate = (Decimal(int(dt["takes"])) / Decimal(decisions_n) * 100) if decisions_n else None

    max_dd = _running_max_drawdown()

    payload = {
        "snapshot_ts": now,
        "equity_inr": w.equity,
        "initial_capital_inr": w.initial,
        "goal_capital_inr": w.goal,
        "progress_pct": Decimal(str(round(w.progress_pct, 2))),
        "realised_net_pnl_inr": w.realised_net_pnl,
        "open_positions": 0,            # filled below
        "locked_inr": w.locked_in_open,
        "trades_total": total,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate,
        "avg_win_inr": avg_win if total else None,
        "avg_loss_inr": avg_loss if total else None,
        "expectancy_inr": expectancy,
        "max_drawdown_inr": max_dd,
        "signals_today": int(st["n"]),
        "decisions_today": decisions_n,
        "take_rate_pct": take_rate,
        "proposals_open": int(po["n"]),
        "tasks_open": int(to["n"]),
        "releases_unverified": int(ru["n"]),
        "notes": note,
    }

    with conn() as c:
        # open positions
        op = c.execute(
            "SELECT COUNT(*) AS n FROM paper_trades WHERE status='OPEN'"
        ).fetchone()
        payload["open_positions"] = int(op["n"])

        c.execute(
            """
            INSERT INTO metrics_snapshots (
                snapshot_ts, equity_inr, initial_capital_inr, goal_capital_inr,
                progress_pct, realised_net_pnl_inr, open_positions, locked_inr,
                trades_total, wins, losses, win_rate_pct, avg_win_inr,
                avg_loss_inr, expectancy_inr, max_drawdown_inr,
                signals_today, decisions_today, take_rate_pct,
                proposals_open, tasks_open, releases_unverified, notes)
            VALUES (%(snapshot_ts)s, %(equity_inr)s, %(initial_capital_inr)s,
                    %(goal_capital_inr)s, %(progress_pct)s,
                    %(realised_net_pnl_inr)s, %(open_positions)s,
                    %(locked_inr)s, %(trades_total)s, %(wins)s, %(losses)s,
                    %(win_rate_pct)s, %(avg_win_inr)s, %(avg_loss_inr)s,
                    %(expectancy_inr)s, %(max_drawdown_inr)s,
                    %(signals_today)s, %(decisions_today)s,
                    %(take_rate_pct)s, %(proposals_open)s, %(tasks_open)s,
                    %(releases_unverified)s, %(notes)s)
            ON CONFLICT (snapshot_ts) DO NOTHING
            """,
            payload,
        )

    insert_audit("snapshot_metrics", "snapshot_written",
                 {"equity_inr": float(w.equity),
                  "progress_pct": round(w.progress_pct, 2),
                  "tasks_open": payload["tasks_open"],
                  "releases_unverified": payload["releases_unverified"]})
    return payload


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--note", default=None, help="Free-text note attached to the snapshot")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    snap = take_snapshot(note=args.note)
    if not args.quiet:
        print(f"snapshot @ {snap['snapshot_ts'].strftime('%Y-%m-%d %H:%M IST')}: "
              f"equity ₹{snap['equity_inr']} ({snap['progress_pct']}% to goal) · "
              f"{snap['trades_total']} trades, "
              f"win {snap['win_rate_pct'] or 0:.1f}% · "
              f"tasks_open={snap['tasks_open']} releases_unverified={snap['releases_unverified']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
