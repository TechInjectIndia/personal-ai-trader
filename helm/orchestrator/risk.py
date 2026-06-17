"""
Pre-trade risk gate.

Single source of truth for whether a proposed paper trade is allowed. Called
from paper_execute.py before any signal is acted on. Returns (allowed, reason).

Competition note
----------------
Every public function takes an optional `competitor_id`. When it is None
(the default) behaviour is byte-for-byte the legacy single-pool house path:
the counts/sums span *all* paper_trades regardless of competitor, and sizing
uses helm.wallet.wallet_state(). When a competitor_id is given, every check is
scoped to that competitor's own rows and its isolated competitor wallet, giving
each league agent independent position/loss/cooldown limits. The incumbent
house cron path always calls these with no competitor_id, so it is unchanged.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from helm.config import (
    HOUSE_TRADE_FILTER,
    MAX_OPEN_POSITIONS_PER_SYMBOL,
    dynamic_position_cap,
    live_flag,
    live_risk_limits_for,
)
from helm.data.store import conn
from helm.wallet import WalletState, wallet_state


def _market_tz(market: str) -> str:
    """IANA tz name for a market's calendar (IN → 'Asia/Kolkata')."""
    from helm.markets import get_market  # local: avoid import cycle on load

    return get_market(market).calendar.tz.key


def _market_today(market: str) -> date:
    """Current trading-day boundary in the market's own timezone (so a US daily
    kill resets at ET-midnight, crypto at UTC-midnight; IN stays IST)."""
    from helm.markets import get_market

    cal = get_market(market).calendar
    return cal.trading_day_key(datetime.now(cal.tz))


def _scope(competitor_id: str | None) -> tuple[str, tuple]:
    """Return a SQL fragment + params that scope a query to one book.

    competitor_id None ⇒ the incumbent house book (rows with NULL or
    'house-claude'); with no competitors this matches the same rows the
    previous unfiltered query did, but it keeps league trades out of the house
    counts. A concrete competitor_id ⇒ that competitor's own rows only.
    """
    if competitor_id is None:
        return f" AND {HOUSE_TRADE_FILTER}", ()
    return " AND competitor_id = %s", (competitor_id,)


def _wallet(competitor_id: str | None, market: str = "IN") -> WalletState:
    """House wallet (scoped to `market`) when unscoped, else the competitor's
    isolated wallet (competitor books are IN-only today)."""
    if competitor_id is None:
        return wallet_state(market)
    # Local import: helm.competition.wallet imports from helm.wallet, and risk
    # is imported widely — keep the dependency lazy to avoid import cycles.
    from helm.competition.wallet import competitor_wallet_state

    return competitor_wallet_state(competitor_id)


def open_paper_positions(competitor_id: str | None = None, market: str = "IN") -> int:
    frag, params = _scope(competitor_id)
    with conn() as c:
        row = c.execute(
            f"SELECT COUNT(*) AS n FROM paper_trades "
            f"WHERE status = 'OPEN' AND market = %s{frag}",
            (market, *params),
        ).fetchone()
    return int(row["n"])


def todays_realized_pnl(competitor_id: str | None = None, market: str = "IN") -> Decimal:
    """Sum of pnl_inr for trades closed today (IST)."""
    frag, params = _scope(competitor_id)
    with conn() as c:
        row = c.execute(
            f"""
            SELECT COALESCE(SUM(pnl_inr), 0) AS total
            FROM paper_trades
            WHERE status = 'CLOSED' AND market = %s
              AND exit_ts >= date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s
              {frag}
            """,
            (market, _market_tz(market), _market_tz(market), *params),
        ).fetchone()
    return Decimal(row["total"])


def kill_engaged_today(competitor_id: str | None = None, market: str = "IN") -> bool:
    """Manual kill switch for the day, per (trade_date, market).

    `daily_state` is keyed by (trade_date, market) (M2), so the kill switch is
    per-market but league-wide within a market (not per-competitor). Competitors
    additionally get their own daily-loss kill via the per-competitor
    `todays_realized_pnl` threshold checked in `evaluate`.
    """
    with conn() as c:
        row = c.execute(
            "SELECT kill_engaged FROM daily_state WHERE trade_date = %s AND market = %s",
            (_market_today(market), market),
        ).fetchone()
    return bool(row and row["kill_engaged"])


def has_open_position(symbol: str, competitor_id: str | None = None,
                      strategy: str | None = None, market: str = "IN") -> bool:
    frag, params = _scope(competitor_id)
    with conn() as c:
        if strategy is not None:
            # Key the slot on (symbol, strategy): join through decisions→signals.
            # Qualify competitor_id with pt. (signals/decisions also carry it).
            row = c.execute(
                f"SELECT 1 FROM paper_trades pt "
                f"JOIN decisions d ON d.id = pt.decision_id "
                f"JOIN signals s ON s.id = d.signal_id "
                f"WHERE pt.status = 'OPEN' AND pt.market = %s AND pt.symbol = %s"
                f"{frag.replace('competitor_id', 'pt.competitor_id')} "
                f"AND s.strategy = %s LIMIT 1",
                (market, symbol, *params, strategy),
            ).fetchone()
        else:
            row = c.execute(
                f"SELECT 1 FROM paper_trades "
                f"WHERE status = 'OPEN' AND market = %s AND symbol = %s{frag} LIMIT 1",
                (market, symbol, *params),
            ).fetchone()
    return row is not None


def signals_for_symbol_today(symbol: str, competitor_id: str | None = None,
                             market: str = "IN") -> int:
    frag, params = _scope(competitor_id)
    with conn() as c:
        row = c.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM paper_trades
            WHERE symbol = %s AND market = %s
              AND entry_ts >= date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s
              {frag}
            """,
            (symbol, market, _market_tz(market), _market_tz(market), *params),
        ).fetchone()
    return int(row["n"])


def open_positions_for_symbol(symbol: str, competitor_id: str | None = None,
                              market: str = "IN") -> int:
    """Count OPEN positions in `symbol` for this book (across strategies)."""
    frag, params = _scope(competitor_id)
    with conn() as c:
        row = c.execute(
            f"SELECT count(*) AS n FROM paper_trades "
            f"WHERE status = 'OPEN' AND market = %s AND symbol = %s{frag}",
            (market, symbol, *params),
        ).fetchone()
    return int(row["n"])


def minutes_since_last_exit(symbol: str, competitor_id: str | None = None,
                            market: str = "IN") -> Decimal | None:
    """Minutes since this book last CLOSED a trade in `symbol`, or None if it
    never has. Powers the per-symbol re-entry cooldown (rate-limits churn that
    racks up round-trip costs)."""
    frag, params = _scope(competitor_id)
    with conn() as c:
        row = c.execute(
            f"""
            SELECT EXTRACT(EPOCH FROM (now() - MAX(exit_ts))) / 60.0 AS mins
            FROM paper_trades
            WHERE status = 'CLOSED' AND market = %s AND symbol = %s AND exit_ts IS NOT NULL{frag}
            """,
            (market, symbol, *params),
        ).fetchone()
    return Decimal(str(row["mins"])) if row and row["mins"] is not None else None


def evaluate(
    symbol: str,
    side: str,
    qty: int | Decimal,
    entry_price: Decimal,
    competitor_id: str | None = None,
    strategy: str | None = None,
    market: str = "IN",
) -> tuple[bool, str]:
    """Return (allowed, reason). Reason is human-readable, used for audit.

    Every limit is scoped to `market` (so INR/USD/crypto books are independent)
    and, when competitor_id is set, to that competitor and its isolated wallet.
    With competitor_id None + market 'IN' (every legacy row is 'IN'), the checks
    span the incumbent house pool exactly as before.
    """
    limits = live_risk_limits_for(market)

    if kill_engaged_today(competitor_id, market):
        return False, "kill switch engaged for today"

    realized = todays_realized_pnl(competitor_id, market)
    if realized <= -limits.daily_loss_kill_inr:
        return False, f"daily loss kill hit ({realized} ≤ -{limits.daily_loss_kill_inr})"

    if open_paper_positions(competitor_id, market) >= limits.max_open_positions:
        return False, f"max_open_positions={limits.max_open_positions} reached"

    # F6 A/B unblock (flag-gated, house-only): when enabled, distinct house
    # strategies may hold concurrent positions in the same symbol so 1-min and
    # 5-min variants don't block each other. Default OFF => one slot per symbol.
    slot_strategy = (strategy if (live_flag("HOUSE_STRATEGY_KEYED_SLOTS")
                                  and competitor_id is None) else None)
    if has_open_position(symbol, competitor_id, strategy=slot_strategy, market=market):
        in_sym = f"{symbol}" + (f" [{strategy}]" if slot_strategy else "")
        return False, f"already have an open position in {in_sym}"

    # With keyed slots on, multiple strategies may hold the same symbol — cap the
    # per-symbol concurrency so correlated 1m/5m pairs can't pile the whole book
    # into one name. (Inert when slot_strategy is None: the check above already
    # enforces 1/symbol.)
    if slot_strategy is not None:
        if open_positions_for_symbol(symbol, competitor_id, market) >= MAX_OPEN_POSITIONS_PER_SYMBOL:
            return False, (f"max {MAX_OPEN_POSITIONS_PER_SYMBOL} concurrent "
                           f"positions in {symbol} reached")

    if signals_for_symbol_today(symbol, competitor_id, market) >= limits.max_signals_per_symbol_per_day:
        return False, f"already traded {symbol} max times today"

    # Per-symbol re-entry cooldown: after a trade closes, wait before re-entering
    # the same name. Rate-limits churn that compounds round-trip costs (the cost
    # thesis). 0 disables. (Previously defined but unenforced — wired 2026-06-08.)
    cooldown = limits.per_symbol_cooldown_min
    if cooldown > 0:
        since = minutes_since_last_exit(symbol, competitor_id, market)
        if since is not None and since < cooldown:
            return False, (f"{symbol} in cooldown "
                           f"({since:.0f}/{cooldown} min since last exit)")

    # The per-trade cap scales with realised pnl (see dynamic_position_cap),
    # so we recompute the effective ceiling here rather than using the raw
    # limits.max_position_inr — otherwise winners couldn't be sized up.
    wallet = _wallet(competitor_id, market)
    effective_cap = dynamic_position_cap(wallet.realised_net_pnl, limits.max_position_inr)
    notional = entry_price * qty
    if notional > effective_cap:
        return False, (f"notional ₹{notional} exceeds dynamic per-trade cap "
                       f"₹{effective_cap} (base ₹{limits.max_position_inr})")

    # Wallet is the hard ceiling: a trade can never be opened for more cash
    # than the pool currently has free. This is what makes the bot
    # self-bounded — losses shrink the pool, profits grow it, and no trade
    # can go beyond what's actually there.
    if notional > wallet.available:
        return False, (
            f"notional ₹{notional} exceeds available wallet cash ₹{wallet.available} "
            f"(equity ₹{wallet.equity} − locked ₹{wallet.locked_in_open})"
        )

    return True, "ok"
