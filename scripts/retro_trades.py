"""
Post-trade retrospective cron — runs every 10 minutes during the trading
window (and once after square-off to catch SKIP counterfactuals that need
the day's full price action).

For each CLOSED paper trade without a retro, asks Claude for a plain-English
review and persists it to `trade_retrospectives`. For SKIP decisions, the
same — but only after SQUARE_OFF_AT IST, since the counterfactual replay
needs the day's price path through square-off to judge fairly.

Failure modes:
  - LLM transport error → audit row, leave the item pending (next run retries).
  - Per-item exception → audit row, skip that item, continue with the rest.
  - LLM_MODE=api + no ANTHROPIC_API_KEY → cron exits silently; manual runs
    with --force-window print the reason.

Usage:
  python scripts/retro_trades.py                    # process pending, window-gated
  python scripts/retro_trades.py --force-window     # ignore the trading-window check
  python scripts/retro_trades.py --trade-id 42      # just this one trade
  python scripts/retro_trades.py --decision-id 99   # just this one SKIP
  python scripts/retro_trades.py --kind trade       # only TRADE retros
  python scripts/retro_trades.py --kind skip        # only SKIP retros
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from helm.config import MARKET_OPEN, MARKET_CLOSE, SQUARE_OFF_AT
from helm.data.store import conn, insert_audit
from helm.llm import LLMError
from helm.retro import (
    pending_skip_decision_ids,
    pending_trade_ids,
    run_for_skip,
    run_for_trade,
)

# Arbitrary fixed int — pg_try_advisory_lock uses it as the mutex key.
# Picked outside the range any other helm script uses (none today).
RETRO_LOCK_KEY = 8472001

IST = ZoneInfo("Asia/Kolkata")
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

# We let the cron run from market open through ~1h after close: catches
# trades that closed late and the post-square-off SKIP pass. Outside that
# the cron silently no-ops.
RETRO_END = (
    datetime.combine(datetime.today(), MARKET_CLOSE) + timedelta(minutes=60)
).time()


def _now_ist() -> datetime:
    return datetime.now(IST)


def _within_window(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= RETRO_END


def _say(force: bool, msg: str) -> None:
    if force:
        print(f"[retro] {msg}", flush=True)


def _process_trades(*, only_id: int | None, force: bool) -> tuple[int, int]:
    ids = [only_id] if only_id is not None else pending_trade_ids()
    done = errored = 0
    for tid in ids:
        try:
            res = run_for_trade(tid, force=False)
        except LLMError as exc:
            insert_audit("retro_trades", "llm_error",
                         {"kind": "TRADE", "trade_id": tid, "error": str(exc)[:500]})
            errored += 1
            continue
        except Exception as exc:  # noqa: BLE001 — cron must not crash
            insert_audit("retro_trades", "error",
                         {"kind": "TRADE", "trade_id": tid, "error": repr(exc)[:500]})
            errored += 1
            continue
        if res is None:
            _say(force, f"trade {tid}: skipped (already has retro or not closed)")
            continue
        done += 1
        insert_audit(
            "retro_trades",
            "retro_written",
            {"kind": "TRADE", "trade_id": tid, "retro_id": res.retro_id,
             "verdict": res.verdict_label, "proposals": res.proposals_count},
        )
        _say(force, f"trade {tid} → {res.verdict_label} ({res.proposals_count} proposal(s))")
    return done, errored


def _process_skips(*, only_id: int | None, force: bool,
                   on_or_after: datetime | None = None,
                   strictly_before: datetime | None = None) -> tuple[int, int]:
    if only_id is not None:
        ids = [only_id]
    else:
        ids = pending_skip_decision_ids(
            on_or_after=on_or_after,
            strictly_before=strictly_before,
        )
    done = errored = 0
    for did in ids:
        try:
            res = run_for_skip(did, force=False)
        except LLMError as exc:
            insert_audit("retro_trades", "llm_error",
                         {"kind": "SKIP", "decision_id": did, "error": str(exc)[:500]})
            errored += 1
            continue
        except Exception as exc:  # noqa: BLE001
            insert_audit("retro_trades", "error",
                         {"kind": "SKIP", "decision_id": did, "error": repr(exc)[:500]})
            errored += 1
            continue
        if res is None:
            _say(force, f"skip {did}: skipped (already has retro or no candles)")
            continue
        done += 1
        insert_audit(
            "retro_trades",
            "retro_written",
            {"kind": "SKIP", "decision_id": did, "retro_id": res.retro_id,
             "verdict": res.verdict_label, "proposals": res.proposals_count},
        )
        _say(force, f"skip {did} → {res.verdict_label} ({res.proposals_count} proposal(s))")
    return done, errored


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--trade-id", type=int, default=None,
                   help="Run a TRADE retro for one specific paper_trades.id")
    p.add_argument("--decision-id", type=int, default=None,
                   help="Run a SKIP retro for one specific decisions.id")
    p.add_argument("--kind", choices=("trade", "skip", "both"), default="both",
                   help="Restrict to one retro kind; default both.")
    p.add_argument("--force-window", action="store_true",
                   help="Bypass the trading-window check (manual runs).")
    p.add_argument("--skip-days-back", type=int, default=7,
                   help="How many days of SKIP decisions to consider; "
                        "ignored if --decision-id is set.")
    args = p.parse_args()

    load_dotenv(ENV_PATH, override=False)
    mode = os.environ.get("LLM_MODE", "cli").strip().lower()
    now = _now_ist()
    force = args.force_window

    _say(force, f"start · mode={mode} now_ist={now.strftime('%H:%M:%S')} kind={args.kind}")

    if mode == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
        if not force:
            return 0
        print("[retro] LLM_MODE=api but ANTHROPIC_API_KEY missing in .env",
              file=sys.stderr)
        return 2

    if not force and not _within_window(now):
        return 0

    # Per-process Postgres advisory lock. Retros take ~30s per item × up to 10
    # items; the cron fires every 10 min. Without a lock, an in-flight backfill
    # (or a slow run that overshoots its 10-min slot) doubles up with the next
    # firing — both processes race for the same pending list and burn
    # subscription quota on items the other is already doing. The unique
    # indexes on trade_retrospectives stop duplicate rows, but the wasted LLM
    # calls aren't free. Lock auto-releases when this connection closes.
    with conn() as lock_conn:
        got = lock_conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS ok", (RETRO_LOCK_KEY,)
        ).fetchone()["ok"]
        if not got:
            _say(force, "another retro run is already in progress — exiting cleanly")
            insert_audit("retro_trades", "skipped_locked", {"reason": "advisory_lock_held"})
            return 0

        # Targeted single-item runs ignore the kind filter logic.
        if args.trade_id is not None:
            done, err = _process_trades(only_id=args.trade_id, force=force)
            insert_audit("retro_trades", "run_summary",
                         {"trades_done": done, "trades_errored": err, "scope": "single-trade"})
            _say(force, f"done · trades={done} errored={err}")
            return 0 if err == 0 else 1

        if args.decision_id is not None:
            done, err = _process_skips(only_id=args.decision_id, force=force)
            insert_audit("retro_trades", "run_summary",
                         {"skips_done": done, "skips_errored": err, "scope": "single-skip"})
            _say(force, f"done · skips={done} errored={err}")
            return 0 if err == 0 else 1

        # Batch path.
        t_done = t_err = s_done = s_err = 0
        if args.kind in ("trade", "both"):
            t_done, t_err = _process_trades(only_id=None, force=force)

        if args.kind in ("skip", "both"):
            # Counterfactual replay needs the day's full price path. So today's
            # SKIPs are only safe to judge after square-off (15:15 IST); before
            # that, we restrict the batch to strictly-before-today.
            floor_ist = (now - timedelta(days=args.skip_days_back)).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            today_start_ist = now.replace(hour=0, minute=0, second=0, microsecond=0)
            upper: datetime | None = None
            if not force and now.time() < SQUARE_OFF_AT:
                upper = today_start_ist
                _say(force, "before square-off — restricting SKIPs to prior days only")
            d, e = _process_skips(
                only_id=None, force=force,
                on_or_after=floor_ist, strictly_before=upper,
            )
            s_done += d
            s_err += e

        insert_audit(
            "retro_trades",
            "run_summary",
            {
                "trades_done": t_done, "trades_errored": t_err,
                "skips_done": s_done, "skips_errored": s_err,
                "mode": mode,
            },
        )
        _say(force,
             f"done · trades={t_done}/{t_err}err skips={s_done}/{s_err}err")
        return 0 if (t_err + s_err) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
