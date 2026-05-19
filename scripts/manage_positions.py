"""
Manage open paper positions — runs every minute via cron.

For each OPEN paper_trades row: pull current LTP (yfinance), check if
stop_loss or target is hit. If so, close the trade. Also handles EOD square-off
at SQUARE_OFF_AT (15:15 IST).

Long-only for v1 (matches ORB strategy — short signals disabled).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import yfinance as yf

from helm.charges import round_trip_breakdown
from helm.config import MARKET_CLOSE, MARKET_OPEN, SQUARE_OFF_AT
from helm.data.store import conn, insert_audit

IST = ZoneInfo("Asia/Kolkata")


def _now_ist() -> datetime:
    return datetime.now(IST)


def _ltp(symbol: str) -> Decimal | None:
    try:
        return Decimal(str(yf.Ticker(f"{symbol}.NS").fast_info.last_price))
    except Exception:
        return None


def _close_trade(c, trade: dict, exit_price: Decimal, reason: str) -> None:
    qty = trade["qty"]
    entry = Decimal(trade["entry_price"])
    side = trade["side"]
    pnl = (exit_price - entry) * qty if side == "BUY" else (entry - exit_price) * qty
    breakdown = round_trip_breakdown(side, qty, entry, exit_price)
    charges = breakdown.total
    net_pnl = pnl - charges
    c.execute(
        """
        UPDATE paper_trades
        SET status = 'CLOSED',
            exit_price = %s,
            exit_ts = %s,
            exit_reason = %s,
            pnl_inr = %s,
            charges_inr = %s,
            net_pnl_inr = %s
        WHERE id = %s
        """,
        (exit_price, _now_ist(), reason, pnl, charges, net_pnl, trade["id"]),
    )
    insert_audit(
        "manage_positions",
        "closed",
        {
            "trade_id": trade["id"],
            "symbol": trade["symbol"],
            "side": side,
            "qty": qty,
            "entry": str(entry),
            "exit": str(exit_price),
            "pnl_inr": str(pnl),
            "charges_inr": str(charges),
            "net_pnl_inr": str(net_pnl),
            "charges_breakdown": breakdown.as_dict(),
            "reason": reason,
        },
    )


def main() -> int:
    now = _now_ist()
    if now.weekday() >= 5:
        return 0
    if not (MARKET_OPEN <= now.time() <= MARKET_CLOSE):
        return 0

    eod = now.time() >= SQUARE_OFF_AT

    with conn() as c:
        opens = list(c.execute("SELECT * FROM paper_trades WHERE status = 'OPEN'"))
        if not opens:
            return 0

        # Per-position snapshot for the activity log: shows what was watched,
        # the live LTP, distance to stop/target, and whether the run closed
        # anything. Only writes audit when there were positions to manage.
        snapshots: list[dict] = []
        closed = 0
        for t in opens:
            ltp = _ltp(t["symbol"])
            entry = Decimal(t["entry_price"])
            stop = Decimal(t["stop_loss"])
            target = Decimal(t["target"]) if t["target"] is not None else None
            side = t["side"]
            outcome = "held"
            reason = None

            if ltp is None:
                outcome = "no_ltp"
            elif eod:
                _close_trade(c, t, ltp, "EOD")
                outcome, reason, closed = "closed", "EOD", closed + 1
            elif side == "BUY":
                if ltp <= stop:
                    _close_trade(c, t, ltp, "STOP")
                    outcome, reason, closed = "closed", "STOP", closed + 1
                elif target and ltp >= target:
                    _close_trade(c, t, ltp, "TARGET")
                    outcome, reason, closed = "closed", "TARGET", closed + 1
            else:  # SELL/SHORT
                if ltp >= stop:
                    _close_trade(c, t, ltp, "STOP")
                    outcome, reason, closed = "closed", "STOP", closed + 1
                elif target and ltp <= target:
                    _close_trade(c, t, ltp, "TARGET")
                    outcome, reason, closed = "closed", "TARGET", closed + 1

            snapshots.append({
                "trade_id": t["id"],
                "symbol": t["symbol"],
                "side": side,
                "qty": t["qty"],
                "entry": float(entry),
                "stop": float(stop),
                "target": float(target) if target is not None else None,
                "ltp": float(ltp) if ltp is not None else None,
                "unrealized_inr": (
                    float((ltp - entry) * t["qty"]) if (ltp is not None and side == "BUY")
                    else float((entry - ltp) * t["qty"]) if ltp is not None
                    else None
                ),
                "outcome": outcome,
                "reason": reason,
            })

        insert_audit(
            "manage_positions",
            "position_check",
            {"watched": len(opens), "closed": closed, "eod": eod, "snapshots": snapshots},
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
