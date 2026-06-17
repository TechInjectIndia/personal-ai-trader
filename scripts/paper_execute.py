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

from helm.config import (
    SAFETY_MAX_CONSECUTIVE_LOSSES,
    SAFETY_NOTIONAL_CEILING_MULT,
    dynamic_position_cap,
    live_flag,
    live_risk_limits_for,
    live_tunable,
)
from helm.data.store import conn, first_candle_open_at_or_after, insert_audit
from helm.instrument import log_event
from helm.markets import get_market
from helm.orchestrator import risk
from helm.wallet import wallet_state

IST = ZoneInfo("Asia/Kolkata")


class ExecutionResult(NamedTuple):
    ok: bool
    decision_id: int | None
    message: str


def _edge_to_cost(cost_model, side: str, qty, entry: Decimal, target: Decimal) -> Decimal:
    """Gross reward to target as a multiple of the expected round-trip cost.

    Uses the trade's MARKET cost model (the same one the realized P&L uses), so
    the gate and the books agree per venue. Returns Decimal('0') when cost is
    zero so the caller treats a degenerate position as un-tradeable rather than
    dividing by 0.
    """
    exp_cost = cost_model.round_trip_breakdown(side, qty, entry, target).total
    gross_reward = abs(target - entry) * qty
    return (gross_reward / exp_cost) if exp_cost > 0 else Decimal("0")


def _conviction_mult(conviction: Decimal, floor: Decimal, min_mult: Decimal) -> Decimal:
    """F5: linear cap multiplier ramping from `min_mult` at `floor` to 1.0 at
    conviction=1.0, clamped. Only used when sizing is enabled."""
    span = Decimal("1") - floor
    if span <= 0:
        return Decimal("1")
    m = min_mult + (Decimal("1") - min_mult) * (conviction - floor) / span
    return max(min_mult, min(Decimal("1"), m))


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

        # Which market this signal belongs to (default IN → every legacy row).
        # Drives the cost model, fractional sizing, and per-market risk/wallet
        # scoping. get_market raises only for an unknown key (IN always exists).
        market_key = sig.get("market") or "IN"
        mkt = get_market(market_key)
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
        realistic = first_candle_open_at_or_after(sig["symbol"], fill_at, market=market_key)
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
        wallet = wallet_state(market_key)
        base_cap = live_risk_limits_for(market_key).max_position_inr
        effective_cap = dynamic_position_cap(wallet.realised_net_pnl, base_cap)
        # F5 (flag-gated, default OFF): conviction-weighted sizing on the
        # auto-sized path. When enabled with a supplied confidence, scale the cap
        # by conviction and skip sub-floor convictions. OFF / no conviction /
        # explicit qty => byte-identical to today.
        conviction_block = False
        conviction_floor = live_tunable("CONVICTION_FLOOR")
        if live_flag("CONVICTION_SIZING_ENABLED") and conviction is not None and qty is None:
            if conviction < conviction_floor:
                conviction_block = True
            else:
                effective_cap = effective_cap * _conviction_mult(
                    conviction, conviction_floor, live_tunable("CONVICTION_SIZE_MIN_MULT"))
        budget = min(effective_cap, wallet.available)
        if qty is None:
            if mkt.fractional:
                # Fractional venues (crypto): size to 8 dp rather than whole lots.
                sized_qty = ((budget / entry).quantize(Decimal("0.00000001"))
                             if entry > 0 and budget > 0 else Decimal("0"))
            else:
                sized_qty = int(budget // entry) if budget >= entry else 0
        else:
            sized_qty = qty

        target = sig["target"]
        # S4 trading-safety backstop (flag-gated; OFF → byte-identical). An
        # INDEPENDENT pre-trade guard on top of risk.evaluate: hard notional
        # ceiling, worst-case loss bound, and stop-side sanity. A trade must pass
        # both this and the risk gate.
        safety_ok, safety_reason = True, "ok"
        if live_flag("SAFETY_GUARD_ENABLED") and sized_qty and sized_qty > 0:
            from helm.safety import pre_trade_check, should_halt
            _limits = live_risk_limits_for(market_key)
            safety_ok, safety_reason = pre_trade_check(
                sig["side"], sized_qty, entry, Decimal(sig["stop_loss"]), market_key,
                hard_ceiling=SAFETY_NOTIONAL_CEILING_MULT * effective_cap,
                max_loss=_limits.daily_loss_kill_inr,
            )
            # Circuit breaker: halt the day after a hard daily loss OR a
            # consecutive-loss streak (the streak arm is new vs the risk gate).
            if safety_ok and should_halt(
                risk.todays_realized_pnl(None, market_key),
                risk.consecutive_losses(None, market_key),
                max_daily_loss=_limits.daily_loss_kill_inr,
                max_consecutive=SAFETY_MAX_CONSECUTIVE_LOSSES,
            ):
                safety_ok, safety_reason = False, "circuit breaker tripped"
        if conviction_block:
            allowed, reason = False, (
                f"low_conviction (conf={conviction:.2f} < floor {conviction_floor})"
            )
        elif sized_qty <= 0:
            # Wallet can't afford even a single share. Skip cleanly so the
            # signal still gets a decision row and is marked consumed.
            allowed, reason = False, (
                f"wallet has ₹{wallet.available} available, one share of "
                f"{sig['symbol']} costs ₹{entry}"
            )
        elif not safety_ok:
            allowed, reason = False, f"safety_guard: {safety_reason}"
        elif target is not None and (
            e2c := _edge_to_cost(mkt.costs, sig["side"], sized_qty, entry, Decimal(target))
        ) < (min_e2c := live_tunable("MIN_EDGE_TO_COST")):
            # F2: target move too small relative to round-trip cost — a
            # guaranteed net loser even if right. Skip via the shared SKIP tail.
            # (target=None signals can't be E2C-evaluated → fall through.)
            allowed, reason = False, f"below_min_edge_to_cost (E2C={e2c:.2f})"
            log_event("f2_min_edge_gate", "blocked", signal_id=sig["id"],
                      symbol=sig["symbol"], qty=sized_qty, entry=entry,
                      target=target, e2c=e2c, min_required=min_e2c)
        else:
            allowed, reason = risk.evaluate(sig["symbol"], sig["side"], sized_qty, entry,
                                            strategy=sig.get("strategy"), market=market_key)

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
                (decision_id, symbol, market, side, qty, entry_price, entry_ts, stop_loss, target, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN')
            """,
            (
                decision_id,
                sig["symbol"],
                market_key,
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
            # int for integer-lot venues (IN: unchanged); str for fractional
            # (crypto) qty so the Decimal stays JSON-serializable + precise.
            "qty": sized_qty if isinstance(sized_qty, int) else str(sized_qty),
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
