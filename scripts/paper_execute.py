"""
Paper-trade executor — books a paper trade against an unconsumed signal.

Called by the Claude Code decision routine after it has reviewed a signal and
chosen to TAKE it. Writes a `decisions` row, applies the risk gate, then on
pass writes a `paper_trades` row (status OPEN). Manages positions
(stop/target/EOD exit) is a separate script.

Usage (CLI):
  python scripts/paper_execute.py --signal-id 42 --actor claude-code [--qty N]
                                  [--reasoning "..."]

If --qty is omitted, sized automatically as
  floor(min(max_position_inr, wallet.available) / entry)
where max_position_inr is the live per-trade cap from
helm.config.live_risk_limits() and wallet.available is whatever cash the
single pool currently has free (initial + realised net P&L − locked in OPEN).
"""

from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
from typing import NamedTuple
from zoneinfo import ZoneInfo

from helm.config import dynamic_position_cap, live_risk_limits
from helm.data.store import conn, insert_audit
from helm.orchestrator import risk
from helm.wallet import wallet_state

IST = ZoneInfo("Asia/Kolkata")


class ExecutionResult(NamedTuple):
    ok: bool
    decision_id: int | None
    message: str


def execute_signal(
    signal_id: int,
    actor: str,
    qty: int | None = None,
    reasoning: str = "",
) -> ExecutionResult:
    """Apply the risk gate and book a paper trade if allowed.

    Always writes a `decisions` row (TAKE on pass, SKIP on block) and marks
    the signal consumed so it isn't re-processed. Returns (ok, decision_id,
    human-readable message).
    """
    with conn() as c:
        sig = c.execute(
            "SELECT * FROM signals WHERE id = %s",
            (signal_id,),
        ).fetchone()
        if not sig:
            return ExecutionResult(False, None, f"no signal id {signal_id}")
        if sig["consumed"]:
            return ExecutionResult(False, None, f"signal {signal_id} already consumed")

        entry = Decimal(sig["entry_price"])
        # Sizing budget = min(dynamic per-trade cap, wallet cash available).
        # The dynamic cap starts at live_risk_limits().max_position_inr and
        # grows with realised pnl (see helm.config.dynamic_position_cap), so
        # winners compound. Wallet may still be smaller than the cap (drawdown
        # or many open positions), in which case it dominates.
        # risk.evaluate() rechecks both ceilings as a final guard.
        wallet = wallet_state()
        base_cap = live_risk_limits().max_position_inr
        effective_cap = dynamic_position_cap(wallet.realised_net_pnl, base_cap)
        budget = min(effective_cap, wallet.available)
        if qty is None:
            sized_qty = int(budget // entry) if budget >= entry else 0
        else:
            sized_qty = qty

        if sized_qty <= 0:
            # Wallet can't afford even a single share. Skip cleanly so the
            # signal still gets a decision row and is marked consumed.
            allowed, reason = False, (
                f"wallet has ₹{wallet.available} available, one share of "
                f"{sig['symbol']} costs ₹{entry}"
            )
        else:
            allowed, reason = risk.evaluate(sig["symbol"], sig["side"], sized_qty, entry)

        decision_row = c.execute(
            """
            INSERT INTO decisions
                (signal_id, actor, verdict, qty, final_entry, final_stop, final_target, reasoning)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                sig["id"],
                actor,
                "TAKE" if allowed else "SKIP",
                sized_qty,
                entry,
                sig["stop_loss"],
                sig["target"],
                reasoning + (f" [BLOCKED: {reason}]" if not allowed else ""),
            ),
        ).fetchone()
        decision_id = decision_row["id"]

        c.execute("UPDATE signals SET consumed = TRUE WHERE id = %s", (sig["id"],))

        if not allowed:
            insert_audit(
                "paper_execute",
                "blocked",
                {"signal_id": sig["id"], "reason": reason, "decision_id": decision_id},
            )
            return ExecutionResult(False, decision_id, f"BLOCKED: {reason}")

        c.execute(
            """
            INSERT INTO paper_trades
                (decision_id, symbol, side, qty, entry_price, entry_ts, stop_loss, target, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'OPEN')
            """,
            (
                decision_id,
                sig["symbol"],
                sig["side"],
                sized_qty,
                entry,
                datetime.now(IST),
                sig["stop_loss"],
                sig["target"],
            ),
        )

    insert_audit(
        "paper_execute",
        "opened",
        {
            "signal_id": sig["id"],
            "decision_id": decision_id,
            "symbol": sig["symbol"],
            "side": sig["side"],
            "qty": sized_qty,
            "entry": str(entry),
        },
    )
    return ExecutionResult(
        True,
        decision_id,
        f"OPENED paper trade: {sig['symbol']} {sig['side']} qty={sized_qty} @ {entry}",
    )


def record_skip(signal_id: int, actor: str, reasoning: str) -> int | None:
    """Mark a signal SKIP without booking a trade. Returns decision_id."""
    with conn() as c:
        sig = c.execute(
            "SELECT id, entry_price, stop_loss, target, consumed FROM signals WHERE id = %s",
            (signal_id,),
        ).fetchone()
        if not sig or sig["consumed"]:
            return None

        decision_row = c.execute(
            """
            INSERT INTO decisions
                (signal_id, actor, verdict, qty, final_entry, final_stop, final_target, reasoning)
            VALUES (%s, %s, 'SKIP', NULL, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                sig["id"],
                actor,
                sig["entry_price"],
                sig["stop_loss"],
                sig["target"],
                reasoning,
            ),
        ).fetchone()
        c.execute("UPDATE signals SET consumed = TRUE WHERE id = %s", (sig["id"],))

    insert_audit(
        "paper_execute",
        "skipped",
        {"signal_id": signal_id, "decision_id": decision_row["id"], "actor": actor},
    )
    return decision_row["id"]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--signal-id", type=int, required=True)
    p.add_argument("--actor", default="claude-code")
    p.add_argument("--qty", type=int, default=None)
    p.add_argument("--reasoning", default="")
    args = p.parse_args()

    res = execute_signal(args.signal_id, args.actor, args.qty, args.reasoning)
    print(res.message)
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
