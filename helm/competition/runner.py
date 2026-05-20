"""Freestyle competition runner — one decision cycle per competitor.

For a freestyle competitor the runner:
  1. builds a compact market snapshot (recent 1-min candles per symbol in the
     competitor's universe + its own open positions + its wallet),
  2. asks that competitor's backend (claude/gemini/qwen/codex/opencode, via the
     helm.llm registry) for a list of OPEN/CLOSE/HOLD actions — its own picks,
     its own logic, framed by its persona,
  3. records the full call to `agent_invocations` (the "how it thinks" + quota
     ledger), and
  4. routes each action through the per-competitor risk gate + execution path
     (helm.competition.execute) into the competitor's isolated wallet.

The universe comes from the competitor's current-week `competitor_mandates` row
when present (J3), else falls back to a default slice of helm.config.WATCHLIST —
so wiring up mandates later is a drop-in. Long-only for v1.

Nothing here touches the incumbent house pipeline.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from helm.competition.execute import close_competitor_position, execute_competitor_open
from helm.competition.wallet import competitor_wallet_state
from helm.config import WATCHLIST
from helm.data.store import conn, insert_audit, todays_candles
from helm.llm import LLMError, complete_json

IST = ZoneInfo("Asia/Kolkata")

# Default universe size when a competitor has no mandate yet (J3 supplies the
# real per-competitor universe). Kept modest so the snapshot prompt stays small.
DEFAULT_UNIVERSE_SIZE = 8
SNAPSHOT_BARS = 20          # recent 1-min bars per symbol shown to the agent

ACTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["OPEN", "CLOSE", "HOLD"]},
                    "symbol": {"type": "string"},
                    "side": {"type": "string", "enum": ["BUY"]},
                    "qty": {"type": "integer"},
                    "stop_loss": {"type": "number"},
                    "target": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["action", "symbol", "reason"],
            },
        },
        "commentary": {"type": "string"},
    },
    "required": ["actions"],
}

SYSTEM_PROMPT_TEMPLATE = """You are an autonomous intraday trader competing in a \
live paper-trading league against other AI agents. You trade NSE equities and \
ETFs in intraday MIS (square off same day). You have your own isolated cash \
wallet — winning means growing YOUR equity faster than the other agents.

YOUR PERSONA / EDGE:
{persona}

HARD RULES:
- Long-only (BUY to open) in v1. Never propose a SHORT/SELL-to-open.
- Trade ONLY symbols listed in the snapshot's universe.
- Every OPEN must include a stop_loss strictly below your entry, and should \
include a target. Size positions sanely — the risk gate caps per-trade notional \
and you cannot spend more cash than your wallet's `available_inr`.
- You may CLOSE any position you currently hold (early discretionary exit). \
Stops, targets and end-of-day square-off are also enforced automatically.
- If nothing is worth doing this cycle, return a single HOLD action. Doing \
nothing is a valid, often correct, move. Every trade pays ~₹15-40 round-trip \
charges, so only act on edges you actually see in the tape.

You decide every ~5 minutes. Judge entries off the recent 1-minute candles in \
the snapshot. Be decisive but disciplined.

OUTPUT — strict JSON only, no prose, no markdown fences:
{{"actions": [{{"action": "OPEN"|"CLOSE"|"HOLD", "symbol": "SYM", \
"side": "BUY", "qty": <int, optional>, "stop_loss": <number, required for OPEN>, \
"target": <number, optional>, "reason": "short why"}}], \
"commentary": "one-line overall read (optional)"}}"""


@dataclass(frozen=True)
class Competitor:
    id: str
    name: str
    backend: str
    model: str | None
    persona: str
    autonomy_level: str
    status: str


@dataclass
class CycleResult:
    competitor_id: str
    ok: bool
    opened: int = 0
    closed: int = 0
    held: int = 0
    blocked: int = 0
    errors: int = 0
    message: str = ""


def freestyle_competitors() -> list[Competitor]:
    """Active freestyle competitors, in stable id order."""
    with conn() as c:
        rows = list(c.execute(
            """
            SELECT id, name, backend, model, persona, autonomy_level, status
            FROM competitors
            WHERE status = 'active' AND autonomy_level = 'freestyle'
            ORDER BY id ASC
            """
        ))
    return [Competitor(**r) for r in rows]


def get_competitor(competitor_id: str) -> Competitor | None:
    with conn() as c:
        row = c.execute(
            """
            SELECT id, name, backend, model, persona, autonomy_level, status
            FROM competitors WHERE id = %s
            """,
            (competitor_id,),
        ).fetchone()
    return Competitor(**row) if row else None


def _week_start(d: date) -> date:
    """Monday of the week containing d (mandates are keyed by week_start)."""
    return d - timedelta(days=d.weekday())


def competitor_universe(competitor_id: str) -> list[str]:
    """This week's mandated universe, else a default slice of WATCHLIST."""
    with conn() as c:
        row = c.execute(
            """
            SELECT universe FROM competitor_mandates
            WHERE competitor_id = %s AND week_start = %s
            """,
            (competitor_id, _week_start(datetime.now(IST).date())),
        ).fetchone()
    if row and row["universe"]:
        syms = [str(s).upper() for s in row["universe"] if s]
        if syms:
            return syms
    return list(WATCHLIST[:DEFAULT_UNIVERSE_SIZE])


def _summarize_candles(candles: list[dict], n: int = SNAPSHOT_BARS) -> list[dict]:
    return [
        {
            "t": c["bar_ts"].astimezone(IST).strftime("%H:%M"),
            "o": float(c["open"]),
            "h": float(c["high"]),
            "l": float(c["low"]),
            "c": float(c["close"]),
        }
        for c in candles[-n:]
    ]


def _competitor_open_positions(competitor_id: str) -> list[dict]:
    with conn() as c:
        rows = list(c.execute(
            """
            SELECT symbol, side, qty, entry_price, stop_loss, target, entry_ts
            FROM paper_trades
            WHERE status = 'OPEN' AND competitor_id = %s
            ORDER BY entry_ts ASC
            """,
            (competitor_id,),
        ))
    return [
        {
            "symbol": r["symbol"], "side": r["side"], "qty": r["qty"],
            "entry": float(r["entry_price"]),
            "stop_loss": float(r["stop_loss"]),
            "target": float(r["target"]) if r["target"] is not None else None,
            "entry_ist": r["entry_ts"].astimezone(IST).strftime("%H:%M"),
        }
        for r in rows
    ]


def build_snapshot(competitor: Competitor, universe: list[str]) -> dict:
    """Market + portfolio snapshot the competitor reasons over."""
    wallet = competitor_wallet_state(competitor.id)
    market: dict[str, Any] = {}
    for sym in universe:
        bars = _summarize_candles(todays_candles(sym))
        last_close = bars[-1]["c"] if bars else None
        market[sym] = {"last_close": last_close, "recent_candles_1m": bars}
    return {
        "now_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
        "wallet": {
            "initial_inr": float(wallet.initial),
            "equity_inr": float(wallet.equity),
            "available_inr": float(wallet.available),
            "realised_pnl_inr": float(wallet.realised_net_pnl),
            "open_positions": wallet.locked_in_open and float(wallet.locked_in_open) or 0.0,
        },
        "open_positions": _competitor_open_positions(competitor.id),
        "universe": universe,
        "market": market,
    }


def _build_user_prompt(snapshot: dict) -> str:
    return (
        "Here is your current market + portfolio snapshot. Decide your actions "
        "for this cycle.\n\n```json\n"
        + json.dumps(snapshot, indent=2, default=str)
        + "\n```"
    )


def _log_invocation(
    competitor: Competitor, prompt: str, raw_output: str | None,
    latency_ms: int, ok: bool, error: str | None,
) -> None:
    with conn() as c:
        c.execute(
            """
            INSERT INTO agent_invocations
                (competitor_id, backend, model, prompt, raw_output, latency_ms, ok, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (competitor.id, competitor.backend, competitor.model,
             prompt[:20000], (raw_output or "")[:20000], latency_ms, ok,
             (error or None) and error[:1000]),
        )


def invoke_competitor(competitor: Competitor, snapshot: dict) -> dict | None:
    """Call the competitor's backend for actions; log it; return parsed dict.

    Returns None on transport/parse failure (already logged + audited).
    """
    system = SYSTEM_PROMPT_TEMPLATE.format(persona=competitor.persona or "Balanced discretionary intraday trader.")
    user = _build_user_prompt(snapshot)
    started = time.monotonic()
    try:
        result = complete_json(
            system, user, schema=ACTIONS_SCHEMA,
            model=competitor.model, backend=competitor.backend,
        )
    except LLMError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        _log_invocation(competitor, user, None, latency_ms, ok=False, error=str(exc))
        insert_audit("competition_runner", "llm_error",
                     {"competitor_id": competitor.id, "backend": competitor.backend,
                      "error": str(exc)[:500]})
        return None
    latency_ms = int((time.monotonic() - started) * 1000)
    _log_invocation(competitor, user, json.dumps(result), latency_ms, ok=True, error=None)
    return result


def _ltp(symbol: str) -> Decimal | None:
    """Live last price via yfinance (Kite quotes 403 on this account)."""
    try:
        import yfinance as yf

        return Decimal(str(yf.Ticker(f"{symbol}.NS").fast_info.last_price))
    except Exception:
        return None


def _to_decimal(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _resolve_price(symbol: str, snapshot: dict) -> Decimal | None:
    """Execution price: live LTP, else the snapshot's last candle close."""
    px = _ltp(symbol)
    if px is not None:
        return px
    last_close = snapshot.get("market", {}).get(symbol, {}).get("last_close")
    return _to_decimal(last_close)


def _apply_action(
    competitor: Competitor, action: dict, universe: list[str],
    snapshot: dict, *, dry_run: bool, result: CycleResult,
) -> str:
    """Validate + execute one action. Returns a short human-readable outcome."""
    kind = str(action.get("action", "")).upper()
    symbol = str(action.get("symbol", "")).upper()
    reason = str(action.get("reason", ""))[:500]

    if kind == "HOLD":
        result.held += 1
        return f"HOLD {symbol or '-'}"

    if symbol not in universe:
        result.blocked += 1
        return f"REJECT {kind} {symbol}: not in universe"

    price = _resolve_price(symbol, snapshot)
    if price is None:
        result.errors += 1
        return f"REJECT {kind} {symbol}: no price available"

    if kind == "CLOSE":
        if dry_run:
            return f"[dry-run] CLOSE {symbol} @ {price}"
        close = close_competitor_position(competitor.id, symbol, price,
                                          reason="DISCRETIONARY", actor=competitor.id)
        if close.ok:
            result.closed += 1
        else:
            result.blocked += 1
        return close.message

    if kind == "OPEN":
        stop = _to_decimal(action.get("stop_loss"))
        target = _to_decimal(action.get("target"))
        raw_qty = action.get("qty")
        qty = int(raw_qty) if isinstance(raw_qty, (int, float)) and raw_qty else None
        if stop is None or stop >= price:
            result.blocked += 1
            return f"REJECT OPEN {symbol}: stop_loss must be below entry {price}"
        if dry_run:
            return f"[dry-run] OPEN {symbol} BUY @ {price} stop={stop} target={target}"
        opened = execute_competitor_open(
            competitor.id, symbol, "BUY", price, stop, target,
            qty=qty, actor=competitor.id, rationale=reason,
        )
        if opened.ok:
            result.opened += 1
        else:
            result.blocked += 1
        return opened.message

    result.blocked += 1
    return f"REJECT unknown action {kind!r}"


def run_competitor_cycle(competitor: Competitor, *, dry_run: bool = False) -> CycleResult:
    """One full decision cycle for a single freestyle competitor."""
    result = CycleResult(competitor_id=competitor.id, ok=False)
    try:
        universe = competitor_universe(competitor.id)
        snapshot = build_snapshot(competitor, universe)
    except ValueError as exc:  # e.g. no wallet seeded
        result.message = f"setup error: {exc}"
        return result

    parsed = invoke_competitor(competitor, snapshot)
    if parsed is None:
        result.errors += 1
        result.message = "backend call failed (see agent_invocations)"
        return result

    actions = parsed.get("actions") or []
    if not isinstance(actions, list):
        result.message = "backend returned no actions list"
        return result

    outcomes: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            result.errors += 1
            continue
        outcomes.append(_apply_action(competitor, action, universe, snapshot,
                                      dry_run=dry_run, result=result))

    result.ok = True
    result.message = "; ".join(outcomes)[:1000] or "no actions"
    insert_audit(
        "competition_runner", "cycle",
        {"competitor_id": competitor.id, "backend": competitor.backend,
         "dry_run": dry_run, "opened": result.opened, "closed": result.closed,
         "held": result.held, "blocked": result.blocked, "errors": result.errors,
         "commentary": str(parsed.get("commentary", ""))[:300]},
    )
    return result
