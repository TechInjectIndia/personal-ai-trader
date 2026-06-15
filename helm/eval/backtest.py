"""
Backtest data layer (FRD G3) — pull real closed trades + the candles that
followed them, and replay them through the deterministic simulator. Shared by
the eval-gate (helm.eval.gate) and the CLI (scripts/replay_backtest.py).

We replay ACTUAL `paper_trades` (sane, gate-passed sizing/targets) rather than
raw signals (the strategy backlog contains degenerate 10%-target / bad-data
signals that make a raw-signal backtest meaningless). The ORIGINAL planned stop
is pulled from the source signal — `paper_trades.stop_loss` is the already-
ratcheted high-water value, so using it would double-count the ratchet and make
an A/B of the exit policy a no-op.
"""

from __future__ import annotations

from decimal import Decimal

from helm.config import HOUSE_TRADE_FILTER
from helm.data.store import conn
from helm.eval.replay import SimOutcome, simulate_trade


def closed_trades(days: int, symbol: str | None = None,
                  all_books: bool = False) -> list[dict]:
    """Closed trades in the window with a target, oldest-first. House book by
    default; `all_books=True` spans every competitor. Carries `orig_stop` from
    the source signal (falls back to the stored stop if the link is missing)."""
    sql = ("SELECT pt.id, pt.entry_ts, pt.symbol, pt.side, pt.entry_price, "
           "COALESCE(s.stop_loss, pt.stop_loss) AS orig_stop, pt.stop_loss, "
           "pt.target, pt.qty, pt.pnl_inr, pt.net_pnl_inr "
           "FROM paper_trades pt "
           "LEFT JOIN decisions d ON d.id = pt.decision_id "
           "LEFT JOIN signals s ON s.id = d.signal_id "
           "WHERE pt.status = 'CLOSED' AND pt.target IS NOT NULL "
           "AND pt.entry_ts >= now() - make_interval(days => %s)")
    args: list = [days]
    if not all_books:
        sql += " AND " + HOUSE_TRADE_FILTER.replace("competitor_id", "pt.competitor_id")
    if symbol:
        sql += " AND pt.symbol = %s"
        args.append(symbol)
    sql += " ORDER BY pt.entry_ts ASC"
    with conn() as c:
        return list(c.execute(sql, tuple(args)))


def _candles_after(symbol: str, ts) -> list[dict]:
    """1-min closes from just after entry through end of that session."""
    with conn() as c:
        return list(c.execute(
            "SELECT bar_ts, close FROM candles_1m "
            "WHERE symbol = %s AND bar_ts > %s AND bar_ts < %s + interval '7 hours' "
            "ORDER BY bar_ts ASC",
            (symbol, ts, ts),
        ))


def replay_trades(trades: list[dict], *, apply_ratchet: bool = True) -> list[SimOutcome]:
    """Replay each trade from its ORIGINAL stop over the candles that followed."""
    out: list[SimOutcome] = []
    for t in trades:
        qty = int(t["qty"])
        if qty <= 0:
            continue
        candles = _candles_after(t["symbol"], t["entry_ts"])
        if not candles:
            continue
        out.append(simulate_trade(
            t["side"], Decimal(t["entry_price"]), Decimal(t["orig_stop"]),
            Decimal(t["target"]), qty, candles, apply_ratchet=apply_ratchet))
    return out


def replay_house_window(days: int = 30) -> list[SimOutcome]:
    """The eval-gate's candidate book: replay the recent house window under the
    current code (so exit-policy config changes show up in the metrics)."""
    return replay_trades(closed_trades(days), apply_ratchet=True)


__all__ = ["closed_trades", "replay_trades", "replay_house_window"]
