"""
Signal scanner — runs every minute via cron during market hours.

For each ENABLED market (helm.markets.enabled_markets) currently in its trading
window, runs each applicable strategy × the market's watchlist against that
market's candles. A fired signal that isn't a same-day duplicate is written to
`signals` (stamped with its market) AND immediately decided inline (synchronous
TAKE/SKIP) so signal → decision → paper-trade is one ~5-10 s pipeline.

Only the IN market is enabled by default, so this is byte-identical to the
single-market scanner; US/CRYPTO plug in once enabled. Session strategies (ORB,
gap-fade) are skipped on 24/7 venues; context strategies run on IN only.

The cron-driven decide_signals.py still runs every 2 min as a catch-up net.

Idempotency: strategies fire only on the bar OF the breakout (see ORB.scan), so
re-running every minute won't re-emit; we also dedupe at insert time.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

# scripts/ is the cron working dir, so add the repo root to sys.path so the
# `scripts.decide_signals` import below resolves the same way decide_signals does.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helm.config import live_flag
from helm.context_client import get_context
from helm.data.store import conn, insert_audit, resample_candles
from helm.markets import enabled_markets
from helm.strategies import ACTIVE


def _already_emitted_today(strategy: str, symbol: str, side: str, market: str,
                           tz: str = "Asia/Kolkata") -> bool:
    with conn() as c:
        row = c.execute(
            """
            SELECT 1 FROM signals
            WHERE strategy = %s AND symbol = %s AND side = %s AND market = %s
              AND ts >= date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s
            LIMIT 1
            """,
            (strategy, symbol, side, market, tz, tz),
        ).fetchone()
        return row is not None


def _strategy_allowed(strat, market) -> bool:
    """A session strategy (opening-range/gap) can't run on a 24/7 venue (no
    session open to anchor to)."""
    if market.calendar.square_off_at() is None and getattr(strat, "session_required", False):
        return False
    return True


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--no-decide", action="store_true",
                   help="Just emit signals; skip the inline decider call")
    p.add_argument("--force-window", action="store_true",
                   help="Bypass the per-market trading-window check")
    args = p.parse_args()

    # Markets in their trading window now (silent no-op + no log spam if none —
    # preserves the off-hours behaviour for the IN-only default).
    markets = [m for m in enabled_markets()
               if args.force_window or m.calendar.is_trading_window()]
    if not markets:
        return 0

    context_signals_on = live_flag("CONTEXT_SIGNALS_ENABLED")
    emitted = 0
    skipped_dup = 0
    new_signal_ids: list[int] = []
    checks: list[dict] = []
    fired_summaries: list[dict] = []

    with conn() as c:
        for market in markets:
            for strat in ACTIVE:
                requires_ctx = getattr(strat, "requires_context", False)
                # Context strategies run on IN only (the context engine is NSE),
                # and only when the flag is on → provably inert otherwise.
                if requires_ctx and (not context_signals_on or market.key != "IN"):
                    continue
                if not _strategy_allowed(strat, market):
                    continue
                for symbol in market.watchlist:
                    candles = resample_candles(
                        symbol, getattr(strat, "bar_minutes", 1),
                        market=market.key, tz=market.calendar.tz.key,
                        anchor_minutes=market.calendar.resample_anchor_minutes())
                    if requires_ctx:
                        ctx = get_context(symbol)
                        strat.context = Decimal(str(ctx["score"])) if ctx else None
                    signal = strat.scan(symbol, candles)
                    check = {
                        "market": market.key,
                        "strategy": strat.name,
                        "symbol": symbol,
                        "candles_seen": len(candles),
                        "fired": signal is not None,
                    }
                    if not signal:
                        checks.append(check)
                        continue

                    if _already_emitted_today(signal.strategy, signal.symbol,
                                              signal.side, market.key,
                                              market.calendar.tz.key):
                        check["dedup_skip"] = True
                        checks.append(check)
                        skipped_dup += 1
                        continue

                    row = c.execute(
                        """
                        INSERT INTO signals
                            (ts, strategy, symbol, market, side, entry_price, stop_loss,
                             target, rationale, payload)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        RETURNING id
                        """,
                        (
                            signal.asof, signal.strategy, signal.symbol, market.key,
                            signal.side, signal.entry_price, signal.stop_loss,
                            signal.target, signal.rationale, json.dumps(signal.payload),
                        ),
                    ).fetchone()
                    new_signal_ids.append(row["id"])
                    emitted += 1
                    fired_summaries.append({
                        "id": row["id"], "market": market.key,
                        "strategy": signal.strategy, "symbol": signal.symbol,
                        "side": signal.side, "entry": float(signal.entry_price),
                        "stop": float(signal.stop_loss),
                        "target": float(signal.target) if signal.target is not None else None,
                        "rationale": signal.rationale,
                    })
                    checks.append(check)

    insert_audit(
        actor="scan_signals",
        event="scan_complete",
        detail={
            "emitted": emitted,
            "skipped_dup": skipped_dup,
            "markets": [m.key for m in markets],
            "strategies": [s.name for s in ACTIVE],
            "checks": checks,
            "fired": fired_summaries,
        },
    )

    # Inline decision — only after the signals are committed (so the decider can
    # see them, and an LLM crash here doesn't roll back the insert).
    if not args.no_decide and new_signal_ids:
        from scripts.decide_signals import decide_signal_inline

        for sid in new_signal_ids:
            try:
                res = decide_signal_inline(sid, source="scan_signals")
                print(f"inline-decide signal {sid} → {res['verdict']} "
                      f"({res['message'][:120]})", flush=True)
            except Exception as exc:  # noqa: BLE001 — catch-all by design
                insert_audit("scan_signals", "inline_decide_exception",
                             {"signal_id": sid, "error": str(exc)[:500]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
