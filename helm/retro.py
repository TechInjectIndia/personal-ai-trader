"""
Post-trade retrospective layer.

After a paper trade closes (TRADE retro) or a SKIP decision can be judged
against the day's price path (SKIP retro), this module asks Claude to write
a plain-English review answering four questions:

  1. Why did the bot enter (or pass on) this trade?
  2. What did the price actually do after?
  3. Was the decision a good call, a bad call, lucky, or unlucky?
  4. What can a non-technical reader learn from this for next time?

Why a separate retro layer:
  The decider sees the signal at one instant. The retro layer has the
  benefit of hindsight — the full price path, the actual exit, and the
  charges paid. We use the verdicts and learnings later to review the
  strategies and the decider itself.

Design:
  - Pure module: builds context, calls helm.llm.complete_json, parses,
    persists. No cron concerns (those live in scripts/retro_trades.py).
  - Idempotent: insertions are guarded by unique indexes on
    (trade_id) for kind='TRADE' and (decision_id) for kind='SKIP'.
  - Layman-language is enforced in the system prompt with an explicit
    banned-words list. Output is strict JSON.

The retro is NOT in the decision loop — it never blocks a trade. It runs
after the fact, on its own cron, and persists for the dashboard to read.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from helm.config import DECIDER_MODEL_DEFAULT, HOUSE_COMPETITOR_ID, SQUARE_OFF_AT
from helm.data.store import conn
from helm.llm import complete_json

IST = ZoneInfo("Asia/Kolkata")

# Token budget for the retro completion. Layman prose + 1-3 learnings +
# tags fit comfortably in ~600 tokens; 1500 leaves headroom.
RETRO_MAX_TOKENS = 1500
RETRO_TEMPERATURE = 0.3   # a touch of variance helps the prose feel less robotic
# How many 1-min bars of context before the entry to send to the model. Gives
# Claude a sense of the lead-up tape ("price was drifting down before the buy").
PRE_ENTRY_BARS = 15
# Truncate candle lists so a long trade doesn't blow past max_tokens.
MAX_CANDLES_IN_PROMPT = 80


PROPOSAL_CATEGORIES = (
    "strategy", "decider_prompt", "risk", "sizing",
    "execution", "data", "meta",
)

RETRO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict_label": {
            "type": "string",
            "enum": ["GOOD_CALL", "BAD_CALL", "LUCKY", "UNLUCKY", "MIXED"],
        },
        "summary_layman": {"type": "string"},
        "why_we_acted": {"type": "string"},
        "what_happened": {"type": "string"},
        "verdict_reasoning": {"type": "string"},
        "learnings": {"type": "array", "items": {"type": "string"}},
        "signal_quality_score": {"type": "integer", "minimum": 1, "maximum": 5},
        "decision_quality_score": {"type": "integer", "minimum": 1, "maximum": 5},
        "execution_quality_score": {"type": "integer", "minimum": 1, "maximum": 5},
        "tags": {"type": "array", "items": {"type": "string"}},
        "improvement_proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": list(PROPOSAL_CATEGORIES)},
                    "title": {"type": "string"},
                    "rationale": {"type": "string"},
                    "proposed_change": {"type": "string"},
                    "evidence": {"type": "object"},
                    "confidence": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "required": ["category", "title", "rationale", "proposed_change",
                             "confidence"],
            },
        },
    },
    "required": [
        "verdict_label", "summary_layman", "why_we_acted",
        "what_happened", "verdict_reasoning", "learnings",
        "signal_quality_score", "decision_quality_score",
        "execution_quality_score", "tags",
        "improvement_proposals",
    ],
}


SYSTEM_PROMPT = """You are the post-trade reviewer for a personal intraday trading bot
that paper-trades a curated watchlist of liquid NSE-listed instruments — large-cap
equities and broad-index / commodity ETFs (e.g. NIFTYBEES, GOLDBEES, SILVERBEES).
The bot uses strategies to spot ideas, an AI decision-maker to TAKE or SKIP each
idea, and the price action of the rest of the day to deliver an outcome. Your job
is to look back with hindsight and write a plain-English review of that decision
and its outcome.

Two flavours of review:
  - TRADE — the bot took the trade and it has now closed. You see entry,
    exit, P&L (after broker charges), and the full minute-by-minute price
    path while the trade was open.
  - SKIP — the bot decided NOT to take the trade. You see what the price
    would have done if it had been taken (a counterfactual replay), so you
    can judge whether the skip saved money or missed a clean winner.

LANGUAGE — HARD CONSTRAINTS. Failure to follow these makes the review useless:
  - Write for a smart friend with zero trading or finance background.
  - NO jargon. Banned words/phrases: "VWAP", "ORB", "breakout", "reclaim",
    "doji", "stop hunt", "R:R", "1R", "ATR", "EMA", "MA", "support",
    "resistance", "retrace", "fade", "long", "short", "tape", "wick",
    "candle pattern", "indicator", "trend line".
  - Translate instead. Examples:
      "price crossed above the morning's opening range" (NOT "ORB long")
      "price came back to its average for the day and bounced" (NOT "VWAP reclaim")
      "a tiny up-and-down bar that shows the market couldn't decide" (NOT "doji")
      "the trade lost ₹14 against the ₹500 risked" (NOT "0.03R")
  - Money in rupees, rounded to the rupee. Times like "10:32 AM" (no IST tag).
  - Short sentences. One idea per sentence.

VERDICT — pick exactly one label:
  - GOOD_CALL  — the bot's reasoning was sound AND the outcome confirmed it
                 (a take that profited; a skip that avoided a loser).
  - UNLUCKY    — the bot's reasoning was sound but the outcome went against it
                 (a sensible take that lost; a sensible skip that would have won).
  - LUCKY      — the bot's reasoning was weak but the outcome favoured it anyway
                 (a sloppy take that happened to profit; a skip of a clear winner
                 that happened to reverse).
  - BAD_CALL   — the bot's reasoning was weak AND the outcome confirmed that
                 (a sloppy take that lost; a skip of a clean winner that would
                 have profited noticeably).
  - MIXED      — genuine ambiguity; only when no other label fits.

The size of P&L matters LESS than the quality of the decision. A ₹2 profit on a
fundamentally bad setup is still LUCKY, not GOOD_CALL.

SCORES (1 = poor, 5 = excellent):
  - signal_quality_score: how good was the underlying strategy idea?
  - decision_quality_score: how sound was the AI's take/skip given that idea?
  - execution_quality_score: how well did the entry and exit play out
                              relative to the plan? (For SKIPs, set 3.)

LEARNINGS — 1 to 3 short, concrete sentences a non-trader can act on.
  - Specific, not platitudes. "Manage risk" is banned.
  - Tied to THIS trade's facts. "When the buy price was hit on a small bar
    right after a fast rise, it tended to reverse" beats "be careful of
    overbought conditions".

TAGS — 2 to 5 short kebab-case labels for grouping similar trades later.
Pick from this vocabulary where possible, invent only if nothing fits:
  late-entry, early-entry, tight-stop, wide-stop, trend-aligned,
  against-trend, low-volume, morning-spike, midday-chop, eod-squareoff,
  fast-reversal, slow-grind, missed-target, hit-target, hit-stop,
  no-fill, news-gap, choppy.

IMPROVEMENT_PROPOSALS — the "learning from mistakes" pipeline. This is the
engineer-facing companion to `learnings`. While `learnings` is plain English
for a human reader, `improvement_proposals` is concrete change suggestions
for the code, the prompt, the strategies, the risk gate, the sizing, or the
data we feed the model. Zero proposals is fine when nothing actionable
surfaces — do NOT pad. One to three sharp proposals is the sweet spot.

Each proposal has:
  - category — exactly one of:
      strategy        — a rule change to a strategy (e.g. raise OR width,
                        add an ATR filter, drop a symbol)
      decider_prompt  — wording to add/remove from the decider's system
                        prompt (e.g. "skip when within 30m of square-off")
      risk            — risk-gate change (caps, kill-switches, cooldowns)
      sizing          — position-sizing rule change
      execution       — paper_execute / manage_positions logic (entry
                        slippage assumption, trailing stop, partial exits)
      data            — what we feed the decider (more candles, news flag,
                        broader-market direction, sector context)
      meta            — process / monitoring / cadence change
  - title — short headline, kebab/sentence-case mix is fine
            ("widen orb stop on choppy mornings")
  - rationale — 1-3 sentences on WHY, grounded in THIS trade's facts
                ("the stop was 0.3% wide on a name that had been moving
                0.6% per 15-min window — random noise was enough to trip it")
  - proposed_change — the actual change in concrete terms. For prompt
                      tweaks, suggest the wording. For strategies, name
                      the parameter and the new value or the new filter.
                      Be specific enough that an engineer could implement
                      it without re-reading this retro.
  - evidence — optional small object of supporting facts pulled from this
               retro ({"loss_inr": -59, "trip_price": 556.5,
                       "stop": 556.4, "noise_avg": 0.6})
  - confidence — 1 (just a hypothesis) through 5 (very high — already
                 seen in multiple trades, clear root cause)

Bias toward proposals that are testable / reversible. A prompt tweak you
can A/B is better than a "rip out ORB" suggestion. Reuse titles across
retros when the same idea recurs — the digest script clusters by title.

OUTPUT FORMAT — strict JSON, no other text, no markdown fences:

{"verdict_label": "GOOD_CALL" | "BAD_CALL" | "LUCKY" | "UNLUCKY" | "MIXED",
 "summary_layman": "2-3 plain-English sentences summarising the whole review",
 "why_we_acted": "2-3 sentences on why the bot took (or passed on) the trade",
 "what_happened": "2-3 sentences on what the price did and the outcome",
 "verdict_reasoning": "2-3 sentences explaining the chosen verdict label",
 "learnings": ["...", "..."],
 "signal_quality_score": 1-5,
 "decision_quality_score": 1-5,
 "execution_quality_score": 1-5,
 "tags": ["...", "..."],
 "improvement_proposals": [
   {"category": "decider_prompt" | "strategy" | "risk" | "sizing" | "execution" | "data" | "meta",
    "title": "...",
    "rationale": "...",
    "proposed_change": "...",
    "evidence": {"key": "value"},
    "confidence": 1-5}
 ]}
"""


@dataclass
class RetroResult:
    retro_id: int
    kind: str
    verdict_label: str
    summary_layman: str
    proposals_count: int = 0


# ───────────────────────── DB helpers ─────────────────────────

def _existing_trade_retro(trade_id: int) -> int | None:
    with conn() as c:
        row = c.execute(
            "SELECT id FROM trade_retrospectives WHERE trade_id = %s",
            (trade_id,),
        ).fetchone()
    return row["id"] if row else None


def _existing_skip_retro(decision_id: int) -> int | None:
    with conn() as c:
        row = c.execute(
            "SELECT id FROM trade_retrospectives "
            "WHERE kind = 'SKIP' AND decision_id = %s",
            (decision_id,),
        ).fetchone()
    return row["id"] if row else None


def _fetch_trade_bundle(trade_id: int) -> dict | None:
    """Trade + its decision + the originating signal, in one shot."""
    with conn() as c:
        row = c.execute(
            """
            SELECT
                t.id AS trade_id, t.symbol, t.side, t.qty,
                t.entry_price, t.entry_ts, t.stop_loss, t.target,
                t.exit_price, t.exit_ts, t.exit_reason,
                t.pnl_inr, t.charges_inr, t.net_pnl_inr, t.status,
                t.competitor_id,
                d.id AS decision_id, d.actor, d.verdict, d.reasoning,
                s.id AS signal_id, s.strategy, s.rationale, s.payload, s.ts AS signal_ts
            FROM paper_trades t
            JOIN decisions d ON d.id = t.decision_id
            JOIN signals s   ON s.id = d.signal_id
            WHERE t.id = %s
            """,
            (trade_id,),
        ).fetchone()
    return row


def _fetch_skip_bundle(decision_id: int) -> dict | None:
    """SKIP decision + the originating signal."""
    with conn() as c:
        row = c.execute(
            """
            SELECT
                d.id AS decision_id, d.actor, d.verdict, d.reasoning,
                d.competitor_id,
                d.final_entry, d.final_stop, d.final_target, d.ts AS decision_ts,
                s.id AS signal_id, s.strategy, s.symbol, s.side,
                s.entry_price, s.stop_loss, s.target,
                s.rationale, s.payload, s.ts AS signal_ts
            FROM decisions d
            JOIN signals s ON s.id = d.signal_id
            WHERE d.id = %s AND d.verdict = 'SKIP'
            """,
            (decision_id,),
        ).fetchone()
    return row


def _candles_in_range(symbol: str, start_ts: datetime, end_ts: datetime) -> list[dict]:
    """1-min OHLC in chronological order, inclusive on both ends."""
    with conn() as c:
        return list(c.execute(
            """
            SELECT bar_ts, open, high, low, close
            FROM candles_1m
            WHERE symbol = %s AND bar_ts BETWEEN %s AND %s
            ORDER BY bar_ts ASC
            """,
            (symbol, start_ts, end_ts),
        ))


# ───────────────────────── counterfactual replay ─────────────────────────

def _replay_skip(signal_ts: datetime, side: str, entry: Decimal,
                 stop: Decimal, target: Decimal | None,
                 candles: list[dict]) -> dict:
    """Walk candles forward from signal_ts; report what the trade would have done.

    Treats the signal's entry_price as a touch-fill: the hypothetical trade
    is opened the first time a bar's range crosses entry. After fill, the
    first leg (stop or target) touched within a bar wins; ties favour stop
    (conservative). If neither hits, the trade is squared off at the last
    candle's close.
    """
    triggered = False
    bars_before_fill = 0
    bars_held = 0
    exit_price: Decimal | None = None
    exit_reason: str | None = None
    exit_ts: datetime | None = None

    for c in candles:
        if c["bar_ts"] < signal_ts:
            continue
        lo = Decimal(c["low"])
        hi = Decimal(c["high"])
        if not triggered:
            if lo <= entry <= hi:
                triggered = True
            else:
                bars_before_fill += 1
                continue
        bars_held += 1
        if side == "BUY":
            hit_stop = lo <= stop
            hit_target = target is not None and hi >= target
        else:
            hit_stop = hi >= stop
            hit_target = target is not None and lo <= target
        if hit_stop:
            exit_price, exit_reason, exit_ts = stop, "STOP", c["bar_ts"]
            break
        if hit_target:
            exit_price, exit_reason, exit_ts = target, "TARGET", c["bar_ts"]
            break

    if not triggered:
        return {
            "would_have_filled": False,
            "would_have_exit_price": None,
            "would_have_exit_reason": "NO_FILL",
            "would_have_exit_time": None,
            "would_have_pnl_per_share_inr": None,
            "bars_before_fill": bars_before_fill,
            "bars_held": 0,
        }

    if exit_price is None:
        last = candles[-1]
        exit_price = Decimal(last["close"])
        exit_reason = "EOD"
        exit_ts = last["bar_ts"]

    pnl_per_share = (exit_price - entry) if side == "BUY" else (entry - exit_price)
    return {
        "would_have_filled": True,
        "would_have_exit_price": float(exit_price),
        "would_have_exit_reason": exit_reason,
        "would_have_exit_time": exit_ts.astimezone(IST).strftime("%H:%M") if exit_ts else None,
        "would_have_pnl_per_share_inr": float(pnl_per_share),
        "bars_before_fill": bars_before_fill,
        "bars_held": bars_held,
    }


# ───────────────────────── prompt builders ─────────────────────────

def _trim_candles(candles: list[dict]) -> list[dict]:
    """Compact OHLC for the prompt; keep head + tail if too long."""
    if len(candles) <= MAX_CANDLES_IN_PROMPT:
        keep = candles
    else:
        half = MAX_CANDLES_IN_PROMPT // 2
        keep = candles[:half] + candles[-half:]
    return [
        {
            "t": c["bar_ts"].astimezone(IST).strftime("%H:%M"),
            "o": float(c["open"]),
            "h": float(c["high"]),
            "l": float(c["low"]),
            "c": float(c["close"]),
        }
        for c in keep
    ]


def _trade_user_prompt(bundle: dict, candles: list[dict]) -> str:
    payload = {
        "kind": "TRADE",
        "trade": {
            "symbol": bundle["symbol"],
            "side": bundle["side"],
            "qty": bundle["qty"],
            "entry_price": float(bundle["entry_price"]),
            "entry_time": bundle["entry_ts"].astimezone(IST).strftime("%H:%M"),
            "planned_stop": float(bundle["stop_loss"]),
            "planned_target": (
                float(bundle["target"]) if bundle["target"] is not None else None
            ),
            "exit_price": float(bundle["exit_price"]),
            "exit_time": bundle["exit_ts"].astimezone(IST).strftime("%H:%M"),
            "exit_reason": bundle["exit_reason"],
            "gross_pnl_inr": float(bundle["pnl_inr"]),
            "charges_inr": (
                float(bundle["charges_inr"]) if bundle["charges_inr"] is not None else 0.0
            ),
            "net_pnl_inr": (
                float(bundle["net_pnl_inr"])
                if bundle["net_pnl_inr"] is not None
                else float(bundle["pnl_inr"])
            ),
        },
        "strategy_signal": {
            "strategy": bundle["strategy"],
            "rationale": bundle["rationale"],
            "payload": bundle["payload"],
        },
        "ai_decision": {
            "actor": bundle["actor"],
            "verdict": bundle["verdict"],
            "reasoning": bundle["reasoning"],
        },
        "price_path_1m_ohlc": candles,
        "notes_for_reviewer": (
            "Decide if the entry made sense given the lead-up candles and the "
            "signal, then judge the exit. P&L sign alone is not enough — weak "
            "logic that profited is LUCKY, sound logic that lost is UNLUCKY."
        ),
    }
    return (
        "Write a plain-English retrospective for this completed paper trade.\n\n"
        "```json\n" + json.dumps(payload, indent=2, default=str) + "\n```"
    )


def _skip_user_prompt(bundle: dict, candles: list[dict], replay: dict) -> str:
    payload = {
        "kind": "SKIP",
        "skipped_idea": {
            "symbol": bundle["symbol"],
            "side": bundle["side"],
            "proposed_entry": float(bundle["entry_price"]),
            "proposed_stop": float(bundle["stop_loss"]),
            "proposed_target": (
                float(bundle["target"]) if bundle["target"] is not None else None
            ),
            "signal_time": bundle["signal_ts"].astimezone(IST).strftime("%H:%M"),
        },
        "strategy_signal": {
            "strategy": bundle["strategy"],
            "rationale": bundle["rationale"],
            "payload": bundle["payload"],
        },
        "ai_decision": {
            "actor": bundle["actor"],
            "verdict": bundle["verdict"],
            "reasoning": bundle["reasoning"],
        },
        "what_would_have_happened": replay,
        "price_path_1m_ohlc_from_signal_to_eod": candles,
        "notes_for_reviewer": (
            "The trade was NOT taken. Use 'what_would_have_happened' to judge "
            "whether passing was the right call. A skip of a setup that would "
            "have hit target meaningfully is a BAD_CALL; a skip of a setup that "
            "would have stopped out is a GOOD_CALL."
        ),
    }
    return (
        "Write a plain-English retrospective for this SKIPPED trade idea.\n\n"
        "```json\n" + json.dumps(payload, indent=2, default=str) + "\n```"
    )


# ───────────────────────── normalize + persist ─────────────────────────

def _normalize(parsed: dict) -> dict:
    """Clamp scores to 1-5, coerce strings, cap lengths."""
    def _score(x: Any) -> int:
        try:
            n = int(x)
        except (TypeError, ValueError):
            n = 3
        return max(1, min(5, n))

    def _str(x: Any, cap: int) -> str:
        return str(x or "")[:cap]

    label = str(parsed.get("verdict_label", "MIXED")).upper()
    if label not in {"GOOD_CALL", "BAD_CALL", "LUCKY", "UNLUCKY", "MIXED"}:
        label = "MIXED"

    learnings_raw = parsed.get("learnings") or []
    if isinstance(learnings_raw, str):
        learnings_raw = [learnings_raw]
    learnings = [str(x)[:400] for x in learnings_raw if str(x).strip()][:3]
    if not learnings:
        learnings = ["No specific lesson surfaced from this one."]

    tags_raw = parsed.get("tags") or []
    if isinstance(tags_raw, str):
        tags_raw = [tags_raw]
    tags = [str(x).strip().lower()[:40] for x in tags_raw if str(x).strip()][:8]

    proposals = _normalize_proposals(parsed.get("improvement_proposals"))

    return {
        "verdict_label": label,
        "summary_layman": _str(parsed.get("summary_layman"), 1000),
        "why_we_acted": _str(parsed.get("why_we_acted"), 1000),
        "what_happened": _str(parsed.get("what_happened"), 1000),
        "verdict_reasoning": _str(parsed.get("verdict_reasoning"), 1000),
        "learnings": learnings,
        "signal_quality_score": _score(parsed.get("signal_quality_score")),
        "decision_quality_score": _score(parsed.get("decision_quality_score")),
        "execution_quality_score": _score(parsed.get("execution_quality_score")),
        "tags": tags,
        "improvement_proposals": proposals,
    }


def _normalize_proposals(raw: Any) -> list[dict]:
    """Validate and clamp improvement-proposal entries.

    Drops any entry missing required fields or with an unknown category. Caps
    at 5 per retro (more than that is usually padding); truncates string
    lengths to keep rows from bloating.
    """
    if not isinstance(raw, list):
        return []

    out: list[dict] = []
    for item in raw[:5]:
        if not isinstance(item, dict):
            continue
        cat = str(item.get("category", "")).strip().lower()
        if cat not in PROPOSAL_CATEGORIES:
            continue
        title = str(item.get("title", "")).strip()[:160]
        rationale = str(item.get("rationale", "")).strip()[:800]
        proposed = str(item.get("proposed_change", "")).strip()[:1200]
        if not (title and rationale and proposed):
            continue
        try:
            confidence = int(item.get("confidence", 3))
        except (TypeError, ValueError):
            confidence = 3
        confidence = max(1, min(5, confidence))
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else None
        out.append({
            "category": cat,
            "title": title,
            "rationale": rationale,
            "proposed_change": proposed,
            "evidence": evidence,
            "confidence": confidence,
        })
    return out


def _persist(kind: str, *, trade_id: int | None, decision_id: int,
             model: str, llm_mode: str, norm: dict, raw: dict,
             competitor_id: str) -> int:
    """Insert a retro row + any improvement-proposal rows.

    Returns the new retro id. The retro row and its proposals are written
    inside the same connection so a crash mid-way can't leave orphaned
    proposals (the FK is ON DELETE CASCADE for safety anyway).

    `competitor_id` (already canonicalised NULL→'house-claude' by the caller)
    is stamped on BOTH the retro and every proposal so the per-agent PM review
    can scope cleanly.
    """
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO trade_retrospectives
                (kind, trade_id, decision_id, model, llm_mode,
                 verdict_label, signal_quality_score, decision_quality_score,
                 execution_quality_score, tags,
                 summary_layman, why_we_acted, what_happened, verdict_reasoning,
                 learnings, raw_response, competitor_id)
            VALUES (%s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s::jsonb,
                    %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s)
            RETURNING id
            """,
            (
                kind, trade_id, decision_id, model, llm_mode,
                norm["verdict_label"],
                norm["signal_quality_score"],
                norm["decision_quality_score"],
                norm["execution_quality_score"],
                json.dumps(norm["tags"]),
                norm["summary_layman"],
                norm["why_we_acted"],
                norm["what_happened"],
                norm["verdict_reasoning"],
                json.dumps(norm["learnings"]),
                json.dumps(raw),
                competitor_id,
            ),
        ).fetchone()
        retro_id = row["id"]

        for p in norm.get("improvement_proposals", []):
            c.execute(
                """
                INSERT INTO improvement_proposals
                    (retro_id, category, title, rationale, proposed_change,
                     evidence, confidence, competitor_id)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                (
                    retro_id, p["category"], p["title"], p["rationale"],
                    p["proposed_change"],
                    json.dumps(p["evidence"]) if p["evidence"] is not None else None,
                    p["confidence"], competitor_id,
                ),
            )
    return retro_id


# ───────────────────────── public entry points ─────────────────────────

def run_for_trade(trade_id: int, *, force: bool = False) -> RetroResult | None:
    """Generate and persist a retrospective for a CLOSED paper trade.

    Returns None if no retro is produced — either because one already exists
    (and force is False) or because the trade isn't closed yet. Raises
    LLMError if the model call fails (caller decides whether to retry).
    """
    if not force:
        existing = _existing_trade_retro(trade_id)
        if existing is not None:
            return None

    bundle = _fetch_trade_bundle(trade_id)
    if bundle is None or bundle["status"] != "CLOSED":
        return None

    # Candle window: 15 mins before entry through exit, so the reviewer sees
    # the lead-up and the in-trade tape.
    start = bundle["entry_ts"] - timedelta(minutes=PRE_ENTRY_BARS)
    end = bundle["exit_ts"]
    candles = _trim_candles(_candles_in_range(bundle["symbol"], start, end))

    model = os.environ.get("DECIDER_MODEL", DECIDER_MODEL_DEFAULT)
    mode = os.environ.get("LLM_MODE", "cli").strip().lower()

    raw = complete_json(
        SYSTEM_PROMPT,
        _trade_user_prompt(bundle, candles),
        schema=RETRO_SCHEMA,
        model=model,
        max_tokens=RETRO_MAX_TOKENS,
        temperature=RETRO_TEMPERATURE,
    )
    norm = _normalize(raw)
    retro_id = _persist(
        "TRADE",
        trade_id=trade_id,
        decision_id=bundle["decision_id"],
        model=model, llm_mode=mode,
        norm=norm, raw=raw,
        competitor_id=bundle["competitor_id"] or HOUSE_COMPETITOR_ID,
    )
    return RetroResult(
        retro_id=retro_id, kind="TRADE",
        verdict_label=norm["verdict_label"],
        summary_layman=norm["summary_layman"],
        proposals_count=len(norm.get("improvement_proposals", [])),
    )


def run_for_skip(decision_id: int, *, force: bool = False) -> RetroResult | None:
    """Generate and persist a retrospective for a SKIP decision.

    Needs the day's price action to replay the counterfactual, so callers
    should only invoke this after square-off (or when backfilling old days).
    """
    if not force:
        existing = _existing_skip_retro(decision_id)
        if existing is not None:
            return None

    bundle = _fetch_skip_bundle(decision_id)
    if bundle is None:
        return None

    # Pull candles from a bit before the signal through the end of that
    # trading day (square-off time). For old days that's strictly historical;
    # for today, the cron only runs this pass after SQUARE_OFF_AT IST anyway.
    sig_ts = bundle["signal_ts"]
    sig_day_ist = sig_ts.astimezone(IST).date()
    eod_ist = datetime.combine(sig_day_ist, SQUARE_OFF_AT, tzinfo=IST)
    start = sig_ts - timedelta(minutes=PRE_ENTRY_BARS)
    candles = _candles_in_range(bundle["symbol"], start, eod_ist)

    if not candles:
        # No price data — can't fairly judge. Caller will retry tomorrow if needed.
        return None

    replay = _replay_skip(
        signal_ts=sig_ts,
        side=bundle["side"],
        entry=Decimal(bundle["entry_price"]),
        stop=Decimal(bundle["stop_loss"]),
        target=Decimal(bundle["target"]) if bundle["target"] is not None else None,
        candles=candles,
    )

    model = os.environ.get("DECIDER_MODEL", DECIDER_MODEL_DEFAULT)
    mode = os.environ.get("LLM_MODE", "cli").strip().lower()

    raw = complete_json(
        SYSTEM_PROMPT,
        _skip_user_prompt(bundle, _trim_candles(candles), replay),
        schema=RETRO_SCHEMA,
        model=model,
        max_tokens=RETRO_MAX_TOKENS,
        temperature=RETRO_TEMPERATURE,
    )
    norm = _normalize(raw)
    retro_id = _persist(
        "SKIP",
        trade_id=None,
        decision_id=decision_id,
        model=model, llm_mode=mode,
        norm=norm, raw=raw,
        competitor_id=bundle["competitor_id"] or HOUSE_COMPETITOR_ID,
    )
    return RetroResult(
        retro_id=retro_id, kind="SKIP",
        verdict_label=norm["verdict_label"],
        summary_layman=norm["summary_layman"],
        proposals_count=len(norm.get("improvement_proposals", [])),
    )


def pending_trade_ids() -> list[int]:
    """CLOSED paper_trades that don't yet have a retro."""
    with conn() as c:
        return [r["id"] for r in c.execute(
            """
            SELECT t.id
            FROM paper_trades t
            LEFT JOIN trade_retrospectives r
                ON r.trade_id = t.id
            WHERE t.status = 'CLOSED' AND r.id IS NULL
            ORDER BY t.exit_ts ASC
            """
        )]


def pending_skip_decision_ids(*, on_or_after: datetime | None = None,
                              strictly_before: datetime | None = None) -> list[int]:
    """SKIP decisions without a retro. Optional inclusive lower / exclusive
    upper time bounds; used by the cron to skip today's decisions before
    square-off (when the counterfactual replay can't see the full day)."""
    clauses = ["d.verdict = 'SKIP'", "r.id IS NULL"]
    args: list = []
    if on_or_after is not None:
        clauses.append("d.ts >= %s")
        args.append(on_or_after)
    if strictly_before is not None:
        clauses.append("d.ts < %s")
        args.append(strictly_before)
    sql = (
        "SELECT d.id FROM decisions d "
        "LEFT JOIN trade_retrospectives r "
        "  ON r.decision_id = d.id AND r.kind = 'SKIP' "
        "WHERE " + " AND ".join(clauses) +
        " ORDER BY d.ts ASC"
    )
    with conn() as c:
        return [r["id"] for r in c.execute(sql, tuple(args))]


__all__ = [
    "RetroResult",
    "run_for_trade",
    "run_for_skip",
    "pending_trade_ids",
    "pending_skip_decision_ids",
]
