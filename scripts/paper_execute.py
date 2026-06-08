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
from datetime import datetime, timedelta
from decimal import Decimal
from typing import NamedTuple
from zoneinfo import ZoneInfo

from helm.charges import round_trip_breakdown
from helm.config import (
    CONVICTION_FLOOR,
    CONVICTION_SIZE_MIN_MULT,
    CONVICTION_SIZING_ENABLED,
    MIN_EDGE_TO_COST,
    dynamic_position_cap,
    live_risk_limits,
)
from helm.data.store import conn, first_candle_open_at_or_after, insert_audit
from helm.instrument import log_event
from helm.orchestrator import risk
from helm.wallet import wallet_state

IST = ZoneInfo("Asia/Kolkata")


class ExecutionResult(NamedTuple):
    ok: bool
    decision_id: int | None
    message: str


def _edge_to_cost(side: str, qty: int, entry: Decimal, target: Decimal) -> Decimal:
    """Gross reward to target as a multiple of the expected round-trip cost.

    Uses the SAME charge model the realized P&L uses (round_trip_breakdown), so
    the gate and the books agree. Returns Decimal('0') when cost is zero so the
    caller treats a degenerate position as un-tradeable rather than dividing by 0.
    """
    exp_cost = round_trip_breakdown(side, qty, entry, target).total
    gross_reward = abs(target - entry) * qty
    return (gross_reward / exp_cost) if exp_cost > 0 else Decimal("0")


def _conviction_mult(conviction: Decimal) -> Decimal:
    """F5: linear cap multiplier ramping from CONVICTION_SIZE_MIN_MULT at the
    floor to 1.0 at conviction=1.0, clamped. Only used when sizing is enabled."""
    span = Decimal("1") - CONVICTION_FLOOR
    if span <= 0:
        return Decimal("1")
    m = (CONVICTION_SIZE_MIN_MULT
         + (Decimal("1") - CONVICTION_SIZE_MIN_MULT) * (conviction - CONVICTION_FLOOR) / span)
    return max(CONVICTION_SIZE_MIN_MULT, min(Decimal("1"), m))


def execute_signal(
    signal_id: int,
    actor: str,
    qty: int | None = None,
    reasoning: str = "",
    conviction: Decimal | None = None,
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

        signal_entry = Decimal(sig["entry_price"])
        # BUG #682: book the realistic fill — the OPEN of the first 1-min bar
        # AFTER the signal/decision, not the breakout bar's close. The strategy
        # fires on the breakout bar and signals.ts is that bar's open minute, so
        # we look one minute past it to avoid picking the breakout bar itself
        # (whose open is the pre-breakout price).
        # Truncate to the minute first: candles_1m.bar_ts is always minute-
        # aligned (date_trunc('minute', ...) in roll_minute_candles), so a
        # sub-minute signals.ts would otherwise make `bar_ts >= fill_at` skip
        # the intended next bar.
        fill_at = (sig["ts"].astimezone(IST).replace(second=0, microsecond=0)
                   + timedelta(minutes=1))
        realistic = first_candle_open_at_or_after(sig["symbol"], fill_at)
        if realistic is not None:
            entry = realistic
            fill_source = "next_bar_open"
        else:
            # Live-intraday: the next bar hasn't formed yet (the inline path
            # runs seconds after the breakout bar closes). Don't block the
            # trade — fall back to the signal's entry (the prior behavior).
            entry = signal_entry
            fill_source = "signal_entry_fallback"
        # Sizing budget = min(dynamic per-trade cap, wallet cash available).
        # The dynamic cap starts at live_risk_limits().max_position_inr and
        # grows with realised pnl (see helm.config.dynamic_position_cap), so
        # winners compound. Wallet may still be smaller than the cap (drawdown
        # or many open positions), in which case it dominates.
        # risk.evaluate() rechecks both ceilings as a final guard.
        wallet = wallet_state()
        base_cap = live_risk_limits().max_position_inr
        effective_cap = dynamic_position_cap(wallet.realised_net_pnl, base_cap)
        # F5 (flag-gated, default OFF): conviction-weighted sizing on the
        # auto-sized path. When enabled with a supplied confidence, scale the cap
        # by conviction and skip sub-floor convictions. OFF / no conviction /
        # explicit qty => byte-identical to today.
        conviction_block = False
        if CONVICTION_SIZING_ENABLED and conviction is not None and qty is None:
            if conviction < CONVICTION_FLOOR:
                conviction_block = True
            else:
                effective_cap = effective_cap * _conviction_mult(conviction)
        budget = min(effective_cap, wallet.available)
        if qty is None:
            sized_qty = int(budget // entry) if budget >= entry else 0
        else:
            sized_qty = qty

        target = sig["target"]
        if conviction_block:
            allowed, reason = False, (
                f"low_conviction (conf={conviction:.2f} < floor {CONVICTION_FLOOR})"
            )
        elif sized_qty <= 0:
            # Wallet can't afford even a single share. Skip cleanly so the
            # signal still gets a decision row and is marked consumed.
            allowed, reason = False, (
                f"wallet has ₹{wallet.available} available, one share of "
                f"{sig['symbol']} costs ₹{entry}"
            )
        elif target is not None and (
            e2c := _edge_to_cost(sig["side"], sized_qty, entry, Decimal(target))
        ) < MIN_EDGE_TO_COST:
            # F2: target move too small relative to round-trip cost — a
            # guaranteed net loser even if right. Skip via the shared SKIP tail.
            # (target=None signals can't be E2C-evaluated → fall through.)
            allowed, reason = False, f"below_min_edge_to_cost (E2C={e2c:.2f})"
            log_event("f2_min_edge_gate", "blocked", signal_id=sig["id"],
                      symbol=sig["symbol"], qty=sized_qty, entry=entry,
                      target=target, e2c=e2c, min_required=MIN_EDGE_TO_COST)
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
            "fill_source": fill_source,
            "signal_entry": str(signal_entry),
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
