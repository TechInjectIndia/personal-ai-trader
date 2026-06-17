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

from helm.config import (
    EXIT_BREAKEVEN_CUSHION_R,
    EXIT_BREAKEVEN_TRIGGER_R,
    EXIT_TIME_DECAY_MAX_LOCK,
    EXIT_TIME_DECAY_START_MIN,
    HOUSE_COMPETITOR_ID,
)
from helm.data.store import conn, insert_audit
from helm.markets import get_market

IST = ZoneInfo("Asia/Kolkata")


def _now_ist() -> datetime:
    return datetime.now(IST)


def _ltp(market, symbol: str) -> Decimal | None:
    return market.data.last_price(symbol)


def _is_house(trade: dict) -> bool:
    """True for house-owned rows (competitor_id NULL or the house id). Mirrors
    HOUSE_TRADE_FILTER. The #681 stop lock-in is a house risk policy; freestyle
    competitors author and own their own stops, so we must not ratchet theirs."""
    return trade["competitor_id"] in (None, HOUSE_COMPETITOR_ID)


def _close_trade(c, trade: dict, exit_price: Decimal, reason: str) -> None:
    qty = trade["qty"]
    entry = Decimal(trade["entry_price"])
    side = trade["side"]
    pnl = (exit_price - entry) * qty if side == "BUY" else (entry - exit_price) * qty
    # Cost model is the trade's own market (IN → ZerodhaCosts, byte-identical).
    try:
        mkt = get_market(trade.get("market") or "IN")
    except KeyError:
        insert_audit("manage_positions", "unknown_market_cost_fallback",
                     {"trade_id": trade["id"], "market": trade.get("market")})
        mkt = get_market("IN")
    breakdown = mkt.costs.round_trip_breakdown(side, qty, entry, exit_price)
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
            "qty": float(qty),
            "entry": str(entry),
            "exit": str(exit_price),
            "pnl_inr": str(pnl),
            "charges_inr": str(charges),
            "net_pnl_inr": str(net_pnl),
            "charges_breakdown": breakdown.as_dict(),
            "reason": reason,
        },
    )


def _tighten_stop(
    c,
    t: dict,
    ltp: Decimal,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    side: str,
    t_rem_min: Decimal,
) -> Decimal:
    """Give-back protection: ratchet stop_loss toward the favorable side in place.

    Stateless — recomputed every minute from (entry, stop, target, side, ltp,
    t_rem_min); the stored stop_loss IS the high-water state, so the every-minute
    cron is idempotent and crash-safe. Two stacked levers (task #681):

      (A) BREAKEVEN LOCK: once price reaches EXIT_BREAKEVEN_TRIGGER_R of the
          entry->target distance, lock the stop to entry + a thin cushion.
      (B) EOD-APPROACH TIGHTEN: in the final EXIT_TIME_DECAY_START_MIN minutes
          before SQUARE_OFF_AT, while in profit, ratchet the stop from breakeven
          toward LTP (up to EXIT_TIME_DECAY_MAX_LOCK of the open profit).

    Returns the (possibly tightened) stop. When it tightens, issues the in-place
    UPDATE + a 'stop_tightened' audit row as a side effect. Monotone: only ever
    moves the stop in the favorable direction, never loosens. Never pushes the
    stop across LTP (would self-fill at better-than-market). The actual exit
    still fires through the unchanged STOP branch + _close_trade.
    """
    span = (target - entry) if side == "BUY" else (entry - target)
    if span <= 0:  # malformed / target on the wrong side -> no-op
        return stop

    prog = ((ltp - entry) if side == "BUY" else (entry - ltp)) / span

    floor: Decimal | None = None

    # (A) Breakeven lock on progress.
    if prog >= EXIT_BREAKEVEN_TRIGGER_R:
        cushion = EXIT_BREAKEVEN_CUSHION_R * span
        floor = entry + cushion if side == "BUY" else entry - cushion

    # (B) EOD-approach tighten (time decay), only when in profit.
    in_profit = (ltp > entry) if side == "BUY" else (ltp < entry)
    if t_rem_min <= EXIT_TIME_DECAY_START_MIN and in_profit:
        raw = (EXIT_TIME_DECAY_START_MIN - t_rem_min) / EXIT_TIME_DECAY_START_MIN
        lock_frac = max(Decimal("0"), min(raw, EXIT_TIME_DECAY_MAX_LOCK))
        td_floor = entry + lock_frac * (ltp - entry)  # BUY; SELL mirrors via signs
        if floor is None:
            floor = td_floor
        else:
            floor = max(floor, td_floor) if side == "BUY" else min(floor, td_floor)

    if floor is None:
        return stop

    floor = floor.quantize(Decimal("0.01"))
    tighter = (floor > stop) if side == "BUY" else (floor < stop)
    if not tighter:
        return stop

    # Never push the stop across LTP — that would manufacture an instant fill at
    # a better-than-market price. A genuine reversal through the stop on a later
    # minute closes normally via the STOP branch.
    if (side == "BUY" and floor >= ltp) or (side == "SELL" and floor <= ltp):
        return stop

    c.execute(
        "UPDATE paper_trades SET stop_loss = %s WHERE id = %s AND status = 'OPEN'",
        (floor, t["id"]),
    )
    insert_audit(
        "manage_positions",
        "stop_tightened",
        {
            "trade_id": t["id"],
            "symbol": t["symbol"],
            "side": side,
            "from": str(stop),
            "to": str(floor),
            "ltp": str(ltp),
            "progress": str(prog.quantize(Decimal("0.01"))),
            "t_rem_min": str(round(float(t_rem_min), 1)),
        },
    )
    return floor


def main() -> int:
    with conn() as c:
        opens = list(c.execute("SELECT * FROM paper_trades WHERE status = 'OPEN'"))
    if not opens:
        return 0

    # Group open positions by market so each book is managed on its OWN clock,
    # calendar, data adapter and cost model. IN-only books reproduce the prior
    # behaviour exactly (one IST-windowed, 15:15-square-off pass).
    by_market: dict[str, list[dict]] = {}
    for t in opens:
        by_market.setdefault(t.get("market") or "IN", []).append(t)

    snapshots: list[dict] = []
    total_closed = 0
    managed: list[str] = []

    for mkey, trades in by_market.items():
        try:
            market = get_market(mkey)
        except KeyError:
            # A rogue/stale market value must not abort managing OTHER markets'
            # open positions (incl. live IN stops/targets). Skip + audit loudly.
            insert_audit("manage_positions", "unknown_market",
                         {"market": mkey, "open_trades": len(trades)})
            continue
        now = datetime.now(market.calendar.tz)
        if not market.calendar.is_market_open(now):
            continue  # venue closed → no LTP, nothing to manage (silent no-op)
        managed.append(mkey)
        square_off = market.calendar.square_off_at()
        eod = square_off is not None and now.time() >= square_off
        max_hold = market.max_hold_min

        with conn() as c:
            for t in trades:
                ltp = _ltp(market, t["symbol"])
                entry = Decimal(t["entry_price"])
                stop = Decimal(t["stop_loss"])
                target = Decimal(t["target"]) if t["target"] is not None else None
                side = t["side"]
                qty = Decimal(t["qty"])
                outcome = "held"
                reason = None

                # Give-back ratchet (#681): house + SESSIONED venues only (it is
                # keyed to the session close). Crypto (square_off None) skips it.
                if (_is_house(t) and ltp is not None and not eod and target is not None
                        and square_off is not None):
                    cutoff = datetime.combine(now.date(), square_off, tzinfo=market.calendar.tz)
                    t_rem = Decimal(str((cutoff - now).total_seconds())) / Decimal("60")
                    stop = _tighten_stop(c, t, ltp, entry, stop, target, side, t_rem)

                # 24/7 time-stop (crypto): force-close a position held past
                # max_hold_min — the EOD-flat analog for a venue with no close.
                time_stopped = (
                    square_off is None and max_hold is not None and t["entry_ts"] is not None
                    and (now - t["entry_ts"]).total_seconds() / 60.0 >= max_hold
                )

                if ltp is None:
                    outcome = "no_ltp"
                elif eod:
                    _close_trade(c, t, ltp, "EOD")
                    outcome, reason, total_closed = "closed", "EOD", total_closed + 1
                elif time_stopped:
                    _close_trade(c, t, ltp, "TIME")
                    outcome, reason, total_closed = "closed", "TIME", total_closed + 1
                elif side == "BUY":
                    if ltp <= stop:
                        _close_trade(c, t, ltp, "STOP")
                        outcome, reason, total_closed = "closed", "STOP", total_closed + 1
                    elif target and ltp >= target:
                        _close_trade(c, t, ltp, "TARGET")
                        outcome, reason, total_closed = "closed", "TARGET", total_closed + 1
                else:  # SELL/SHORT
                    if ltp >= stop:
                        _close_trade(c, t, ltp, "STOP")
                        outcome, reason, total_closed = "closed", "STOP", total_closed + 1
                    elif target and ltp <= target:
                        _close_trade(c, t, ltp, "TARGET")
                        outcome, reason, total_closed = "closed", "TARGET", total_closed + 1

                snapshots.append({
                    "market": mkey,
                    "trade_id": t["id"],
                    "symbol": t["symbol"],
                    "side": side,
                    "qty": float(qty),
                    "entry": float(entry),
                    "stop": float(stop),
                    "target": float(target) if target is not None else None,
                    "ltp": float(ltp) if ltp is not None else None,
                    "unrealized": (
                        float((ltp - entry) * qty) if (ltp is not None and side == "BUY")
                        else float((entry - ltp) * qty) if ltp is not None
                        else None
                    ),
                    "outcome": outcome,
                    "reason": reason,
                })

    if not managed:
        return 0

    insert_audit(
        "manage_positions",
        "position_check",
        {"watched": len(opens), "closed": total_closed, "markets": managed,
         "snapshots": snapshots},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
