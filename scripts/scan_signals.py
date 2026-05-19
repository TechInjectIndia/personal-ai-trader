"""
Signal scanner — runs every minute via cron during market hours.

For each strategy × symbol, runs the rule against today's 1-min candles. If a
signal fires AND we haven't already emitted the same {strategy, symbol, side}
today, write it to `signals` AND immediately ask the decider for a TAKE/SKIP
verdict (inline, synchronous). This collapses signal → decision → paper-trade
into one ~5-10 s pipeline instead of waiting up to ~7 min for the next cron
decider cycle.

The cron-driven decide_signals.py still runs every 2 min as a catch-up safety
net for any signals where the inline decision failed (LLM down, etc.).

Idempotency note: strategies are designed to fire only on the bar OF the
breakout (see ORB.scan), so re-running this script every minute won't keep
re-emitting. We additionally dedupe at insert time as a belt-and-braces.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# scripts/ is the cron working dir, so add the repo root to sys.path so the
# `scripts.decide_signals` import below resolves the same way decide_signals
# itself does.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helm.config import TRADING_END, TRADING_START, WATCHLIST
from helm.data.store import conn, insert_audit, todays_candles
from helm.strategies import ACTIVE

IST = ZoneInfo("Asia/Kolkata")


def _within_trading_window() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return TRADING_START <= now.time() <= TRADING_END


def _already_emitted_today(strategy: str, symbol: str, side: str) -> bool:
    with conn() as c:
        row = c.execute(
            """
            SELECT 1 FROM signals
            WHERE strategy = %s AND symbol = %s AND side = %s
              AND ts >= date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata'
            LIMIT 1
            """,
            (strategy, symbol, side),
        ).fetchone()
        return row is not None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--no-decide", action="store_true",
                   help="Just emit signals; skip the inline decider call "
                        "(useful for tests / manual scans)")
    p.add_argument("--force-window", action="store_true",
                   help="Bypass the trading-window check")
    args = p.parse_args()

    if not args.force_window and not _within_trading_window():
        return 0

    emitted = 0
    skipped_dup = 0
    new_signal_ids: list[int] = []
    # Per-(strategy,symbol) outcome so the activity log can show what was
    # actually checked and what fired vs didn't.
    checks: list[dict] = []
    fired_summaries: list[dict] = []
    with conn() as c:
        for strat in ACTIVE:
            for symbol in WATCHLIST:
                candles = todays_candles(symbol)
                signal = strat.scan(symbol, candles)
                check = {
                    "strategy": strat.name,
                    "symbol": symbol,
                    "candles_seen": len(candles),
                    "fired": signal is not None,
                }
                if not signal:
                    checks.append(check)
                    continue

                if _already_emitted_today(signal.strategy, signal.symbol, signal.side):
                    check["dedup_skip"] = True
                    checks.append(check)
                    skipped_dup += 1
                    continue

                row = c.execute(
                    """
                    INSERT INTO signals
                        (ts, strategy, symbol, side, entry_price, stop_loss, target, rationale, payload)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    RETURNING id
                    """,
                    (
                        signal.asof,
                        signal.strategy,
                        signal.symbol,
                        signal.side,
                        signal.entry_price,
                        signal.stop_loss,
                        signal.target,
                        signal.rationale,
                        json.dumps(signal.payload),
                    ),
                ).fetchone()
                new_signal_ids.append(row["id"])
                emitted += 1
                fired_summaries.append({
                    "id": row["id"],
                    "strategy": signal.strategy,
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "entry": float(signal.entry_price),
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
            "strategies": [s.name for s in ACTIVE],
            "checks": checks,
            "fired": fired_summaries,
        },
    )

    # Inline decision step — only after the signals are committed, so the
    # decider can see them (and so any LLM crash here doesn't roll back the
    # insert). The cron-driven decide_signals.py is the catch-up net.
    if not args.no_decide and new_signal_ids:
        # Local import: decide_signals imports from scripts.paper_execute
        # which imports helm.* — keeping the import lazy avoids paying the
        # anthropic-SDK import cost on no-fire scans (most of them).
        from scripts.decide_signals import decide_signal_inline

        for sid in new_signal_ids:
            try:
                res = decide_signal_inline(sid, source="scan_signals")
                print(f"inline-decide signal {sid} → {res['verdict']} "
                      f"({res['message'][:120]})", flush=True)
            except Exception as exc:  # noqa: BLE001 — catch-all by design
                # An unhandled exception inside decide_signal_inline must NOT
                # poison the scan loop. The cron decider will retry.
                insert_audit("scan_signals", "inline_decide_exception",
                             {"signal_id": sid, "error": str(exc)[:500]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
