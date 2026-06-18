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
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from helm.competition.backend import BackendCall, call_backend
from helm.competition.competitors import (
    Competitor,
    freestyle_competitors,
    get_competitor,
    week_start,
)
from helm.competition.execute import close_competitor_position, execute_competitor_open
from helm.competition.wallet import competitor_wallet_state
from helm.config import market_default_watchlist, market_framing
from helm.data.store import conn, insert_audit, todays_candles
from helm.markets import get_market

# Re-exported for backwards-compatible imports (scripts/run_competitors.py).
__all__ = [
    "Competitor", "freestyle_competitors", "get_competitor",
    "CycleResult", "run_competitor_cycle", "competitor_universe", "build_snapshot",
]

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
live paper-trading league against other AI agents. You trade {venue_clause}. You \
have your own isolated cash wallet — winning means growing YOUR equity faster \
than the other agents.

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
nothing is a valid, often correct, move. Every trade pays round-trip charges \
({currency_symbol}-denominated), so only act on edges you actually see in the tape.

You decide every ~5 minutes. Judge entries off the recent 1-minute candles in \
the snapshot. Be decisive but disciplined.

OUTPUT — strict JSON only, no prose, no markdown fences:
{{"actions": [{{"action": "OPEN"|"CLOSE"|"HOLD", "symbol": "SYM", \
"side": "BUY", "qty": <int, optional>, "stop_loss": <number, required for OPEN>, \
"target": <number, optional>, "reason": "short why"}}], \
"commentary": "one-line overall read (optional)"}}"""


@dataclass
class CycleResult:
    competitor_id: str
    ok: bool
    opened: int = 0
    closed: int = 0
    held: int = 0
    blocked: int = 0
    errors: int = 0
    paused: bool = False        # backend was quota-paused; cycle skipped, not failed
    message: str = ""


def competitor_universe(competitor_id: str, market: str = "IN") -> list[str]:
    """This week's mandated universe for (competitor, market), else a default
    slice of that market's watchlist."""
    with conn() as c:
        row = c.execute(
            """
            SELECT universe FROM competitor_mandates
            WHERE competitor_id = %s AND market = %s AND week_start = %s
            """,
            (competitor_id, market, week_start()),
        ).fetchone()
    if row and row["universe"]:
        syms = [str(s).upper() for s in row["universe"] if s]
        if syms:
            return syms
    return list(market_default_watchlist(market)[:DEFAULT_UNIVERSE_SIZE])


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


def _competitor_open_positions(competitor_id: str, market: str = "IN") -> list[dict]:
    with conn() as c:
        rows = list(c.execute(
            """
            SELECT symbol, side, qty, entry_price, stop_loss, target, entry_ts
            FROM paper_trades
            WHERE status = 'OPEN' AND competitor_id = %s AND market = %s
            ORDER BY entry_ts ASC
            """,
            (competitor_id, market),
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


def build_snapshot(competitor: Competitor, universe: list[str],
                   market: str = "IN") -> dict:
    """Market + portfolio snapshot the competitor reasons over (scoped to one
    market: that market's wallet, candles, and open positions)."""
    wallet = competitor_wallet_state(competitor.id, market)
    quotes: dict[str, Any] = {}
    for sym in universe:
        bars = _summarize_candles(todays_candles(sym, market))
        last_close = bars[-1]["c"] if bars else None
        quotes[sym] = {"last_close": last_close, "recent_candles_1m": bars}
    return {
        "now_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
        "market_key": market,
        "currency": market_framing(market)["currency"],
        "wallet": {
            "initial_inr": float(wallet.initial),
            "equity_inr": float(wallet.equity),
            "available_inr": float(wallet.available),
            "realised_pnl_inr": float(wallet.realised_net_pnl),
            "open_positions": wallet.locked_in_open and float(wallet.locked_in_open) or 0.0,
        },
        "open_positions": _competitor_open_positions(competitor.id, market),
        "universe": universe,
        "market": quotes,
    }


def _build_user_prompt(snapshot: dict) -> str:
    return (
        "Here is your current market + portfolio snapshot. Decide your actions "
        "for this cycle.\n\n```json\n"
        + json.dumps(snapshot, indent=2, default=str)
        + "\n```"
    )


def invoke_competitor(competitor: Competitor, snapshot: dict,
                      market: str = "IN") -> BackendCall:
    """Call the competitor's backend for actions through the quota-gated path.

    Returns a BackendCall: `.quota_blocked` ⇒ benched (no call made), `.ok`
    ⇒ `.parsed` holds the actions dict, else a transport/parse failure (already
    traced to agent_invocations). Quota skips and errors are also audited here.
    """
    framing = market_framing(market)
    system = SYSTEM_PROMPT_TEMPLATE.format(
        persona=competitor.persona or "Balanced discretionary intraday trader.",
        venue_clause=framing["venue_clause"],
        currency_symbol=framing["currency_symbol"],
    )
    user = _build_user_prompt(snapshot)
    call = call_backend(
        competitor_id=competitor.id, backend=competitor.backend,
        model=competitor.model, system=system, user=user, schema=ACTIONS_SCHEMA,
    )
    if call.quota_blocked:
        insert_audit("competition_runner", "quota_skip",
                     {"competitor_id": competitor.id, "backend": competitor.backend,
                      "reason": call.reason})
    elif not call.ok:
        insert_audit("competition_runner", "llm_error",
                     {"competitor_id": competitor.id, "backend": competitor.backend,
                      "error": (call.error or "")[:500]})
    return call


def _ltp(symbol: str, market: str = "IN") -> Decimal | None:
    """Live last price via the market's own data adapter (IN → YFinanceNS, i.e.
    the same `.NS` yfinance call as before; US → bare yfinance; crypto → ccxt)."""
    try:
        return get_market(market).data.last_price(symbol)
    except Exception:
        return None


def _to_decimal(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _resolve_price(symbol: str, snapshot: dict, market: str = "IN") -> Decimal | None:
    """Execution price: live LTP (via market adapter), else snapshot last close."""
    px = _ltp(symbol, market)
    if px is not None:
        return px
    last_close = snapshot.get("market", {}).get(symbol, {}).get("last_close")
    return _to_decimal(last_close)


def _apply_action(
    competitor: Competitor, action: dict, universe: list[str],
    snapshot: dict, *, dry_run: bool, result: CycleResult, market: str = "IN",
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

    price = _resolve_price(symbol, snapshot, market)
    if price is None:
        result.errors += 1
        return f"REJECT {kind} {symbol}: no price available"

    if kind == "CLOSE":
        if dry_run:
            return f"[dry-run] CLOSE {symbol} @ {price}"
        close = close_competitor_position(competitor.id, symbol, price,
                                          reason="DISCRETIONARY", actor=competitor.id,
                                          market=market)
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
            qty=qty, actor=competitor.id, rationale=reason, market=market,
        )
        if opened.ok:
            result.opened += 1
        else:
            result.blocked += 1
        return opened.message

    result.blocked += 1
    return f"REJECT unknown action {kind!r}"


def run_competitor_cycle(competitor: Competitor, *, dry_run: bool = False,
                         market: str = "IN") -> CycleResult:
    """One full decision cycle for a single freestyle competitor in one market."""
    result = CycleResult(competitor_id=competitor.id, ok=False)
    try:
        universe = competitor_universe(competitor.id, market)
        snapshot = build_snapshot(competitor, universe, market)
    except ValueError as exc:  # e.g. no wallet seeded
        result.message = f"setup error: {exc}"
        return result

    call = invoke_competitor(competitor, snapshot, market)
    if call.quota_blocked:
        result.ok = True
        result.paused = True
        result.message = f"skipped (quota): {call.reason}"
        return result
    if not call.ok or call.parsed is None:
        result.errors += 1
        result.message = "backend call failed (see agent_invocations)"
        return result

    parsed = call.parsed
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
                                      dry_run=dry_run, result=result, market=market))

    result.ok = True
    result.message = "; ".join(outcomes)[:1000] or "no actions"
    insert_audit(
        "competition_runner", "cycle",
        {"competitor_id": competitor.id, "backend": competitor.backend,
         "market": market, "dry_run": dry_run, "opened": result.opened,
         "closed": result.closed, "held": result.held, "blocked": result.blocked,
         "errors": result.errors, "commentary": str(parsed.get("commentary", ""))[:300]},
    )
    return result
