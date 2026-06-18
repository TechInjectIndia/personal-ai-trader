"""Competitor execution path — the freestyle league's chokepoint.

The incumbent house bot books trades from strategy-emitted `signals` rows via
scripts.paper_execute.execute_signal. Freestyle competitors don't have strategy
signals — they emit OPEN/CLOSE intents directly. This module is their analogue:

  execute_competitor_open()  — synthesises a `signals` row (strategy='freestyle',
      stamped competitor_id, immediately consumed), runs the per-competitor risk
      gate, writes a `decisions` row, and on pass opens a `paper_trades` row.
      Every row carries competitor_id so wallets/leaderboards stay isolated.

  close_competitor_position() — discretionary early exit of one of a competitor's
      OPEN trades, using the same Zerodha-MIS charge model as manage_positions.

manage_positions.py already walks *all* OPEN paper_trades (it doesn't filter by
competitor), so competitor positions get stop/target/EOD square-off for free —
this module only adds the open + discretionary-close behaviours.

Money is Decimal throughout; state changes are audited via insert_audit.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import NamedTuple
from zoneinfo import ZoneInfo

from helm.config import dynamic_position_cap, live_risk_limits_for
from helm.competition.wallet import competitor_wallet_state, sync_wallet_cache
from helm.data.store import conn, insert_audit
from helm.markets import get_market
from helm.orchestrator import risk

IST = ZoneInfo("Asia/Kolkata")

FREESTYLE_STRATEGY = "freestyle"


class OpenResult(NamedTuple):
    ok: bool
    decision_id: int | None
    trade_id: int | None
    message: str


class CloseResult(NamedTuple):
    ok: bool
    trade_id: int | None
    net_pnl_inr: Decimal | None
    message: str


def _size_qty(
    competitor_id: str, entry: Decimal, requested_qty: int | float | None,
    market: str = "IN",
) -> int | Decimal:
    """Quantity to buy: explicit request CLAMPED to budget, else auto-size to it.

    Budget = min(dynamic per-trade cap for this competitor+market, wallet cash
    free). A freestyle agent that asks for more than the cap/wallet can fund used
    to be hard-rejected by risk.evaluate, discarding the whole order (the dominant
    cause of cap-blocked SKIPs). Instead, size DOWN to what fits. Fractional venues
    (crypto) size to 8 dp; lot venues (IN/US equities) to whole shares. The risk
    gate re-checks both ceilings, so a clamped qty always passes them.
    """
    wallet = competitor_wallet_state(competitor_id, market)
    base_cap = live_risk_limits_for(market).max_position_inr
    effective_cap = dynamic_position_cap(wallet.realised_net_pnl, base_cap)
    budget = min(effective_cap, wallet.available)
    if get_market(market).fractional:
        affordable = ((budget / entry).quantize(Decimal("0.00000001"))
                      if entry > 0 and budget > 0 else Decimal("0"))
        if requested_qty is not None:
            return max(Decimal("0"), min(Decimal(str(requested_qty)), affordable))
        return affordable
    affordable = int(budget // entry) if budget >= entry else 0
    if requested_qty is not None:
        return min(max(0, int(requested_qty)), affordable)
    return affordable


def execute_competitor_open(
    competitor_id: str,
    symbol: str,
    side: str,
    entry: Decimal,
    stop_loss: Decimal,
    target: Decimal | None,
    *,
    qty: int | None = None,
    actor: str,
    rationale: str = "",
    market: str = "IN",
) -> OpenResult:
    """Open a paper trade for a competitor through the risk gate.

    Always records a `decisions` row (TAKE on pass, SKIP on block) tied to a
    freshly synthesised, already-consumed `signals` row, all stamped with
    `market` so the per-(competitor, market) wallet/leaderboard stay isolated.
    Returns OpenResult. Long-only for v1 (matches the house bot): non-BUY
    sides are rejected.
    """
    entry = Decimal(entry)
    stop_loss = Decimal(stop_loss)
    target = Decimal(target) if target is not None else None

    if side != "BUY":
        return OpenResult(False, None, None, f"v1 is long-only; refused side={side!r}")

    sized_qty = _size_qty(competitor_id, entry, qty, market)

    with conn() as c:
        sig = c.execute(
            """
            INSERT INTO signals
                (strategy, symbol, side, entry_price, stop_loss, target,
                 rationale, consumed, competitor_id, market)
            VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s)
            RETURNING id
            """,
            (FREESTYLE_STRATEGY, symbol, side, entry, stop_loss, target,
             rationale, competitor_id, market),
        ).fetchone()
        signal_id = sig["id"]

        if sized_qty <= 0:
            allowed, reason = False, "wallet cannot afford one share / qty<=0"
        else:
            allowed, reason = risk.evaluate(
                symbol, side, sized_qty, entry, competitor_id=competitor_id,
                market=market,
            )

        decision = c.execute(
            """
            INSERT INTO decisions
                (signal_id, actor, verdict, qty, final_entry, final_stop,
                 final_target, reasoning, competitor_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                signal_id, actor, "TAKE" if allowed else "SKIP", sized_qty,
                entry, stop_loss, target,
                rationale + (f" [BLOCKED: {reason}]" if not allowed else ""),
                competitor_id,
            ),
        ).fetchone()
        decision_id = decision["id"]

        if not allowed:
            insert_audit(
                "competition_execute", "blocked",
                {"competitor_id": competitor_id, "signal_id": signal_id,
                 "symbol": symbol, "reason": reason, "decision_id": decision_id},
            )
            return OpenResult(False, decision_id, None, f"BLOCKED: {reason}")

        trade = c.execute(
            """
            INSERT INTO paper_trades
                (decision_id, symbol, side, qty, entry_price, entry_ts,
                 stop_loss, target, status, competitor_id, market)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', %s, %s)
            RETURNING id
            """,
            (decision_id, symbol, side, sized_qty, entry, datetime.now(IST),
             stop_loss, target, competitor_id, market),
        ).fetchone()
        trade_id = trade["id"]

    sync_wallet_cache(competitor_id, market)
    insert_audit(
        "competition_execute", "opened",
        {"competitor_id": competitor_id, "decision_id": decision_id,
         "trade_id": trade_id, "symbol": symbol, "side": side, "market": market,
         "qty": float(sized_qty), "entry": str(entry)},
    )
    return OpenResult(
        True, decision_id, trade_id,
        f"OPENED {symbol} {side} qty={sized_qty} @ {entry} for {competitor_id}",
    )


def close_competitor_position(
    competitor_id: str,
    symbol: str,
    exit_price: Decimal,
    *,
    reason: str = "MANUAL",
    actor: str = "competition",
    market: str = "IN",
) -> CloseResult:
    """Close the competitor's OPEN trade in `symbol` (in `market`) at `exit_price`.

    Mirrors manage_positions._close_trade's charge + net-P&L accounting, using
    that market's own cost model (IN → ZerodhaCosts, byte-identical). No-op
    (ok=False) if the competitor holds no open position in that symbol+market.
    """
    exit_price = Decimal(exit_price)
    with conn() as c:
        trade = c.execute(
            """
            SELECT * FROM paper_trades
            WHERE status = 'OPEN' AND symbol = %s AND competitor_id = %s AND market = %s
            ORDER BY entry_ts ASC LIMIT 1
            """,
            (symbol, competitor_id, market),
        ).fetchone()
        if not trade:
            return CloseResult(False, None, None,
                               f"{competitor_id} has no open position in {symbol}")

        qty = trade["qty"]
        entry = Decimal(trade["entry_price"])
        side = trade["side"]
        pnl = (exit_price - entry) * qty if side == "BUY" else (entry - exit_price) * qty
        breakdown = get_market(market).costs.round_trip_breakdown(side, qty, entry, exit_price)
        net_pnl = pnl - breakdown.total

        c.execute(
            """
            UPDATE paper_trades
            SET status = 'CLOSED', exit_price = %s, exit_ts = %s, exit_reason = %s,
                pnl_inr = %s, charges_inr = %s, net_pnl_inr = %s
            WHERE id = %s
            """,
            (exit_price, datetime.now(IST), reason, pnl, breakdown.total, net_pnl,
             trade["id"]),
        )

    sync_wallet_cache(competitor_id, market)
    insert_audit(
        "competition_execute", "closed",
        {"competitor_id": competitor_id, "trade_id": trade["id"], "symbol": symbol,
         "side": side, "qty": float(qty), "market": market, "entry": str(entry),
         "exit": str(exit_price), "pnl_inr": str(pnl), "charges_inr": str(breakdown.total),
         "net_pnl_inr": str(net_pnl), "reason": reason, "actor": actor},
    )
    return CloseResult(
        True, trade["id"], net_pnl,
        f"CLOSED {symbol} for {competitor_id} @ {exit_price} (net ₹{net_pnl})",
    )
