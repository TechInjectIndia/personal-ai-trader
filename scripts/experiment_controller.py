"""
Autonomous experiment controller (#4) — the agent owns the live experiment flags.

For each `flag_experiments` row with status='watching', run an ON arm then an OFF
arm. Each arm ends when it has accrued `min_trades` closed house trades OR
`max_days` have passed (whichever first), so it never stalls on low volume. When
both arms are measured, compare net expectancy (helm.experiments.decide_ab) and
LOCK the winner — flipping the flag in `settings` itself. No human in the loop.

Run on cron (daily post-close is enough; trade counts only move in-session).

    python scripts/experiment_controller.py            # one tick
    python scripts/experiment_controller.py --status   # show experiment state
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helm.config import HOUSE_TRADE_FILTER
from helm.data.store import conn, insert_audit, set_setting
from helm.experiments import ArmStats, decide_ab

IST = ZoneInfo("Asia/Kolkata")


def _arm_stats(since: datetime) -> tuple[int, Decimal]:
    """Closed IN house trades + net P&L since `since` (the current arm's window)."""
    with conn() as c:
        r = c.execute(
            f"SELECT count(*) AS n, COALESCE(SUM(COALESCE(net_pnl_inr, pnl_inr)), 0) AS net "
            f"FROM paper_trades WHERE status='CLOSED' AND market='IN' "
            f"AND exit_ts > %s AND {HOUSE_TRADE_FILTER}",
            (since,),
        ).fetchone()
    return int(r["n"]), Decimal(r["net"])


def run_once(now: datetime | None = None) -> list[dict]:
    """Advance every watching experiment one tick. Returns a list of actions."""
    now = now or datetime.now(IST)
    actions: list[dict] = []
    with conn() as c:
        watching = list(c.execute("SELECT * FROM flag_experiments WHERE status='watching'"))

    for e in watching:
        flag, phase, started = e["flag"], e["phase"], e["phase_started_ts"]
        n, net = _arm_stats(started)
        elapsed_days = (now - started).total_seconds() / 86400.0
        arm_done = n >= e["min_trades"] or elapsed_days >= e["max_days"]
        if not arm_done:
            actions.append({"flag": flag, "action": "accruing", "phase": phase, "n": n})
            continue

        # Record this arm's result.
        col_n, col_net = (("on_n", "on_net") if phase == "on" else ("off_n", "off_net"))
        with conn() as c:
            c.execute(f"UPDATE flag_experiments SET {col_n}=%s, {col_net}=%s, updated_ts=now() "
                      f"WHERE flag=%s", (n, net, flag))
            row = c.execute("SELECT on_n, on_net, off_n, off_net FROM flag_experiments "
                            "WHERE flag=%s", (flag,)).fetchone()

        if row["on_n"] is not None and row["off_n"] is not None:
            # Both arms measured → decide + lock + flip the flag autonomously.
            on = ArmStats(int(row["on_n"]), Decimal(row["on_net"]))
            off = ArmStats(int(row["off_n"]), Decimal(row["off_net"]))
            verdict = decide_ab(on, off)
            keep_on = verdict == "keep_on"
            note = (f"ON exp/trade={on.expectancy:.2f} (n={on.n}) vs "
                    f"OFF exp/trade={off.expectancy:.2f} (n={off.n})")
            set_setting(flag, keep_on, actor="experiment-controller")
            with conn() as c:
                c.execute("UPDATE flag_experiments SET status=%s, verdict=%s, decided_ts=now(), "
                          "notes=%s, updated_ts=now() WHERE flag=%s",
                          ("locked_on" if keep_on else "locked_off", verdict, note, flag))
            insert_audit("experiment-controller", "flag_decided",
                         {"flag": flag, "verdict": verdict, "detail": note})
            actions.append({"flag": flag, "action": "decided", "verdict": verdict, "note": note})
        else:
            # First arm done → flip the flag to measure the other arm.
            other = "off" if phase == "on" else "on"
            set_setting(flag, other == "on", actor="experiment-controller")
            with conn() as c:
                c.execute("UPDATE flag_experiments SET phase=%s, phase_started_ts=now(), "
                          "updated_ts=now() WHERE flag=%s", (other, flag))
            insert_audit("experiment-controller", "flag_phase_flip",
                         {"flag": flag, "from": phase, "to": other, "arm_n": n, "arm_net": str(net)})
            actions.append({"flag": flag, "action": "flipped", "to": other, "arm_n": n})
    return actions


def _status() -> None:
    with conn() as c:
        rows = list(c.execute("SELECT * FROM flag_experiments ORDER BY flag"))
    for r in rows:
        print(f"{r['flag']:28} status={r['status']:11} phase={r['phase']:3} "
              f"on(n={r['on_n']},net={r['on_net']}) off(n={r['off_n']},net={r['off_net']}) "
              f"verdict={r['verdict']}")
    if not rows:
        print("(no experiments registered)")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--status", action="store_true", help="print experiment state and exit")
    args = p.parse_args()
    if args.status:
        _status()
        return 0
    actions = run_once()
    for a in actions:
        print(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
