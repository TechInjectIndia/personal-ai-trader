"""
Claude decider — two entry points.

  1. `decide_signal_inline(signal_id)` — public per-signal API. Called
     synchronously by scripts/scan_signals.py the moment a new signal is
     emitted, so the strategy → decision → paper-trade pipeline runs in a
     few seconds with no cron round-trip.

  2. main() — cron catch-up. Runs every couple of minutes and decides any
     unconsumed signals that the inline path missed (e.g. it crashed, or the
     LLM was temporarily down). The two paths are coordinated by the
     `signals.consumed` flag and a SELECT … FOR UPDATE SKIP LOCKED guard
     inside decide_signal_inline.

For every unconsumed signal, builds a context blob (signal details, recent
1-min candles, current risk state, today's open positions) and asks the LLM
for a verdict: TAKE or SKIP plus short reasoning. The verdict is recorded
to the `decisions` table; on TAKE we book a paper trade via
scripts.paper_execute.execute_signal (which also runs the risk gate as a
final guard).

Stale-signal short-circuit: any signal older than STALE_THRESHOLD_MINUTES
is auto-SKIPped without an LLM call. This is the lesson from the HDFCBANK
retro where a signal sat in the queue 3.5 h before being decided against
totally different price action.

Backend: `helm.llm.decide()` hides whether we're on the Anthropic SDK
(LLM_MODE=api) or shelling out to the Claude Code CLI on subscription auth
(LLM_MODE=cli, the POC default). See WORKAROUNDS.md.

Usage:
  python scripts/decide_signals.py             # processes all unconsumed today
  python scripts/decide_signals.py --signal-id 42   # one specific signal
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# When invoked as `python scripts/decide_signals.py` (cron path), sys.path[0]
# is `scripts/`, so the project root needs to be added explicitly for
# `from scripts.paper_execute import ...` to resolve.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from helm.config import (
    DECIDER_MAX_TOKENS,
    DECIDER_MODEL_DEFAULT,
    DECIDER_RECENT_BARS,
    DECIDER_TEMPERATURE,
    TRADING_END,
    TRADING_START,
    live_risk_limits,
)
from helm.context_client import get_context
from helm.data.store import conn, insert_audit, todays_candles
from helm.llm import LLMError, decide as llm_decide
from helm.orchestrator import risk
from scripts.paper_execute import execute_signal, record_skip

IST = ZoneInfo("Asia/Kolkata")
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

# Signals older than this are auto-SKIPped without an LLM call. Lesson from
# the HDFCBANK 10:29 BAD_CALL retro: a stale signal decided against fresh
# price action just wastes an LLM call and risks a bad entry. 20 minutes
# covers the worst-case cron stall (2-min cycle × a few retries) while
# protecting against multi-hour queue blowouts like today's.
STALE_THRESHOLD_MINUTES = 20

SYSTEM_PROMPT = """You are the decision officer for a personal intraday trading bot
that trades a curated watchlist of liquid NSE-listed instruments — large-cap equities
and broad-index / commodity ETFs (e.g. NIFTYBEES, GOLDBEES, SILVERBEES) — in
paper-mode MIS (intraday-only). The actual symbol and instrument type (stock vs ETF)
are shown in the per-signal context. Strategies feed you mechanical signals; your job
is to TAKE or SKIP each one based on the data shown.

Note on ETFs: they trade just like equities on NSE in intraday MIS, but tend to be
slower-moving and tighter-ranged than single stocks. Treat a clean breakout / VWAP
reclaim on an ETF as a valid setup; just calibrate expectations on magnitude.

Decision principles, in priority order:

1. Respect the risk envelope. The bot already enforces hard caps (max open
   positions, daily-loss kill, per-symbol cooldown). Do NOT TAKE if the
   surrounding context implies the trade will worsen risk: e.g. clear adverse
   intraday trend, two prior losses today on this symbol, signal arriving
   right before square-off, etc.

When fewer than 90 minutes remain to square-off, compute required_pace = target_distance_inr / minutes_remaining and observed_pace = (price change over the last 30 minutes) / 30. If required_pace exceeds 2x observed_pace, explicitly state that the target is unlikely to be reached at the instrument's current pace and weight this as a reason to SKIP, or propose a halved target before approving.

2. Confirm the signal with the recent-candles tape. ORB BUY deserves a TAKE
   when there is a decisive breakout (close above the range with follow-through
   visible) OR a marginal breakout (close ≤ 0.1% above range high) paired with
   adequate room to the stop. Don't auto-reject marginal breakouts: judge by
   the reward-to-risk ratio shown in the signal. R:R ≥ 1.5 with a stop more
   than 0.3% below entry is a fine setup even if the breakout is tight.
   VWAP reclaim only if the dip and reclaim are visible. Gap-fade only if
   price already showed momentum back toward yesterday's close.

3. A few-paise dip on the bar AFTER the trigger is noise, not a failure
   signal. Compare the dip's magnitude to the stop distance: if the dip is a
   small fraction of the distance to stop, ignore it. Only treat post-trigger
   weakness as disqualifying if the next 1–2 bars erase a meaningful chunk
   (>30%) of the move toward target.

4. Check whether the entry price is still actually tradeable on the tape
   before rejecting on "staleness". Look at the recent-candles lows: if any
   bar since the signal has traded down to the entry price, the entry is
   still on offer. Don't reject for "price has moved past entry" if the
   data shows it was available within the last few bars.

5. Take more trades, not fewer, when setups meet the criteria above. Skipping
   a marginal-but-clean breakout costs us the trade; taking a clean one costs
   ~₹15 in charges. The asymmetry favors action when reward-to-risk is good.
   Still default to SKIP if the signal is obviously broken (failed breakout,
   price already at target, late in the day, etc.).

6. The bot is long-only in v1. Never propose a SHORT.

When the closing price exceeds the opening-range boundary by less than 0.10%, treat the breakout as unconfirmed. Only TAKE if there is strong secondary evidence such as decisive acceleration in pace over the final two bars of the signal window, or a clearly rising broader index on the same timeframe. Otherwise SKIP and wait for a more convincing move.

OUTPUT FORMAT — strict JSON, no other text, no markdown fences:

{"verdict": "TAKE" | "SKIP",
 "confidence": 0.0 to 1.0,
 "reasoning": "one or two sentences explaining the decision"}
"""


def _within_window(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    # Allow decisions slightly past TRADING_END to clear backlog.
    return TRADING_START <= now.time() <= TRADING_END


def _summarize_candles(candles: list[dict], n: int = DECIDER_RECENT_BARS) -> list[dict]:
    """Trim to last n bars, drop tick_count, format for the prompt."""
    out = []
    for c in candles[-n:]:
        out.append({
            "t": c["bar_ts"].astimezone(IST).strftime("%H:%M"),
            "o": float(c["open"]),
            "h": float(c["high"]),
            "l": float(c["low"]),
            "c": float(c["close"]),
        })
    return out


def _risk_snapshot(symbol: str) -> dict:
    limits = live_risk_limits()
    return {
        "open_positions": risk.open_paper_positions(),
        "max_open_positions": limits.max_open_positions,
        "todays_realized_pnl_inr": float(risk.todays_realized_pnl()),
        "kill_engaged": risk.kill_engaged_today(),
        "already_open_in_symbol": risk.has_open_position(symbol),
        "symbol_trades_today": risk.signals_for_symbol_today(symbol),
        "max_signals_per_symbol_per_day": limits.max_signals_per_symbol_per_day,
    }


def _todays_decisions_summary() -> list[dict]:
    """Compact per-symbol record of what's been taken/skipped today."""
    with conn() as c:
        rows = list(c.execute(
            """
            SELECT d.actor, d.verdict, s.symbol, s.strategy, s.side, d.qty, d.reasoning
            FROM decisions d
            JOIN signals s ON s.id = d.signal_id
            WHERE d.ts >= date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata'
            ORDER BY d.ts ASC
            """
        ))
    return [
        {"actor": r["actor"], "verdict": r["verdict"], "symbol": r["symbol"],
         "strategy": r["strategy"], "side": r["side"], "qty": r["qty"],
         "reasoning": (r["reasoning"] or "")[:120]}
        for r in rows
    ]


def _fetch_unconsumed(signal_id: int | None) -> list[dict]:
    sql = (
        "SELECT * FROM signals WHERE consumed = FALSE "
        "AND ts >= date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata' "
    )
    args: tuple = ()
    if signal_id is not None:
        sql += "AND id = %s "
        args = (signal_id,)
    sql += "ORDER BY ts ASC"
    with conn() as c:
        return list(c.execute(sql, args))


def _build_user_prompt(sig: dict, candles: list[dict], snapshot: dict, history: list[dict],
                       *, context: dict | None = None) -> str:
    payload = {
        "signal": {
            "id": sig["id"],
            "ts_ist": sig["ts"].astimezone(IST).isoformat(timespec="seconds"),
            "strategy": sig["strategy"],
            "symbol": sig["symbol"],
            "side": sig["side"],
            "entry_price": float(sig["entry_price"]),
            "stop_loss": float(sig["stop_loss"]),
            "target": float(sig["target"]) if sig["target"] is not None else None,
            "rationale": sig["rationale"],
            "payload": sig["payload"],
        },
        "recent_candles_1m": _summarize_candles(candles),
        "risk_state": snapshot,
        "todays_decisions": history,
        "now_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
    }
    # When context is None (flag off / stale / fetch failed) the payload dict is
    # byte-identical to today → prompt cache + model behaviour unchanged. Only a
    # fresh non-stale hit adds the compact CONTEXT block.
    if context is not None:
        payload["external_context"] = {
            "score": context["score"],
            "rationale": context["rationale"],
        }
    return (
        "Decide whether to TAKE or SKIP this paper trade.\n\n"
        "```json\n" + json.dumps(payload, indent=2, default=str) + "\n```"
    )


def _decide_one(model: str, sig: dict) -> tuple[str, str, float]:
    """Returns (verdict, reasoning, confidence). Raises LLMError on failure."""
    candles = todays_candles(sig["symbol"])
    snapshot = _risk_snapshot(sig["symbol"])
    history = _todays_decisions_summary()
    # Fail-open, flag-gated: None when CONTEXT_ENGINE_URL unset / service down /
    # stale, in which case _build_user_prompt is byte-identical to today.
    context = get_context(sig["symbol"], signal_id=sig["id"])
    user = _build_user_prompt(sig, candles, snapshot, history, context=context)

    result = llm_decide(
        SYSTEM_PROMPT,
        user,
        model=model,
        max_tokens=DECIDER_MAX_TOKENS,
        temperature=DECIDER_TEMPERATURE,
    )
    return result["verdict"], result["reasoning"], result["confidence"]


def _signal_age_minutes(sig: dict) -> float:
    return (datetime.now(IST) - sig["ts"].astimezone(IST)).total_seconds() / 60.0


def decide_signal_inline(
    signal_id: int,
    *,
    model: str | None = None,
    mode: str | None = None,
    source: str = "decide_signals",
) -> dict:
    """Decide a single signal end-to-end. Idempotent w.r.t. signals.consumed.

    Used by scan_signals.py for instantaneous decision-making the moment a
    signal fires, and by main() for the cron catch-up loop.

    Returns a dict {verdict, ok, message, signal_id} where verdict is one of:
      TAKE | SKIP | STALE | ALREADY_CONSUMED | LLM_ERROR.
    Never raises.
    """
    model = model or os.environ.get("DECIDER_MODEL", DECIDER_MODEL_DEFAULT)
    mode = (mode or os.environ.get("LLM_MODE", "cli")).strip().lower()

    with conn() as c:
        sig = c.execute("SELECT * FROM signals WHERE id = %s", (signal_id,)).fetchone()
    if not sig:
        return {"verdict": "ALREADY_CONSUMED", "ok": False,
                "message": f"no signal id {signal_id}", "signal_id": signal_id}
    if sig["consumed"]:
        return {"verdict": "ALREADY_CONSUMED", "ok": False,
                "message": "signal already consumed", "signal_id": signal_id}

    age_min = _signal_age_minutes(sig)
    if age_min > STALE_THRESHOLD_MINUTES:
        reason = (f"[{source}/stale] signal age {age_min:.1f}m > "
                  f"{STALE_THRESHOLD_MINUTES}m threshold")
        decision_id = record_skip(sig["id"], actor=source, reasoning=reason)
        insert_audit(source, "decision",
                     {"signal_id": sig["id"], "verdict": "STALE_SKIP",
                      "decision_id": decision_id, "age_min": round(age_min, 1)})
        return {"verdict": "STALE", "ok": True,
                "message": f"stale ({age_min:.1f}m)", "signal_id": signal_id}

    try:
        verdict, reasoning, confidence = _decide_one(model, sig)
    except LLMError as exc:
        insert_audit(source, "llm_error",
                     {"signal_id": sig["id"], "mode": mode, "error": str(exc)[:500]})
        return {"verdict": "LLM_ERROR", "ok": False,
                "message": str(exc)[:200], "signal_id": signal_id}

    tag = f"[{source} {mode}/{model} conf={confidence:.2f}] {reasoning}"
    if verdict == "TAKE":
        res = execute_signal(sig["id"], actor=source, qty=None, reasoning=tag)
        insert_audit(source, "decision",
                     {"signal_id": sig["id"], "verdict": "TAKE", "ok": res.ok,
                      "message": res.message, "confidence": confidence})
        return {"verdict": "TAKE", "ok": res.ok, "message": res.message,
                "signal_id": signal_id}

    decision_id = record_skip(sig["id"], actor=source, reasoning=tag)
    insert_audit(source, "decision",
                 {"signal_id": sig["id"], "verdict": "SKIP",
                  "decision_id": decision_id, "confidence": confidence,
                  "reasoning": reasoning[:200]})
    return {"verdict": "SKIP", "ok": True, "message": reasoning[:200],
            "signal_id": signal_id}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--signal-id", type=int, default=None,
                   help="Decide just one signal; default is all unconsumed today")
    p.add_argument("--force-window", action="store_true",
                   help="Bypass the trading-window check (for manual runs)")
    args = p.parse_args()

    load_dotenv(ENV_PATH, override=False)
    mode = os.environ.get("LLM_MODE", "cli").strip().lower()
    model = os.environ.get("DECIDER_MODEL", DECIDER_MODEL_DEFAULT)
    now = datetime.now(IST)

    # Manual runs (--force-window) always narrate; cron stays quiet on no-op
    # paths so logs/decide.log doesn't fill with "nothing to do" lines.
    def _say(msg: str) -> None:
        if args.force_window:
            print(f"[decide] {msg}", flush=True)

    _say(f"start · mode={mode} model={model} signal_id={args.signal_id} now_ist={now.strftime('%H:%M:%S')}")

    if mode == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
        if not args.force_window:
            return 0
        print("[decide] LLM_MODE=api but ANTHROPIC_API_KEY missing in .env", file=sys.stderr)
        return 2

    if not args.force_window and not _within_window(now):
        return 0
    if not _within_window(now):
        _say("outside trading window — proceeding anyway because --force-window")

    pending = _fetch_unconsumed(args.signal_id)
    _say(f"fetched {len(pending)} unconsumed signal(s) from today")
    if not pending:
        if args.signal_id is not None:
            _say(f"signal id={args.signal_id} not in today's unconsumed set "
                 "(already decided? from a different day? does not exist?)")
        else:
            _say("no unconsumed signals to decide — cron may have already processed them")
        return 0

    taken = skipped = errored = stale = 0
    for sig in pending:
        res = decide_signal_inline(sig["id"], model=model, mode=mode,
                                   source="decide_signals")
        v = res["verdict"]
        if v == "TAKE":
            taken += 1
            print(f"signal {sig['id']} {sig['symbol']} → TAKE ({res['message']})")
        elif v == "SKIP":
            skipped += 1
            print(f"signal {sig['id']} {sig['symbol']} → SKIP ({res['message'][:80]})")
        elif v == "STALE":
            stale += 1
            print(f"signal {sig['id']} {sig['symbol']} → STALE ({res['message']})")
        elif v == "LLM_ERROR":
            errored += 1
        # ALREADY_CONSUMED falls through silently — race with inline path.

    insert_audit(
        "decide_signals",
        "run_summary",
        {"taken": taken, "skipped": skipped, "stale": stale, "errored": errored,
         "mode": mode, "model": model, "considered": len(pending)},
    )
    _say(f"done · taken={taken} skipped={skipped} stale={stale} "
         f"errored={errored} considered={len(pending)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
