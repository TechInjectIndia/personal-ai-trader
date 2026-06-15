"""
Product-Manager agent — weekly improvement-loop triage.

Wakes once a week (or on `--force`), looks at:
  * the goal brief (where we are vs `goal_capital_inr`),
  * `improvement_proposals` open in the last 30 days (clustered by title so
    recurring themes surface),
  * recent retros of the last 7 days,
  * what the engineer agent has shipped lately (so we don't re-suggest the
    same change),

and asks the LLM for 0–3 concrete next-tasks to queue. The LLM speaks in a
constrained schema; this module never invents tasks itself — it only validates
the LLM's suggestions and persists the ones that fit the engineer's surface.

Skip rules (no-op exits):
  * unverified release in flight (one ship at a time — Tester gates the next
    PM run),
  * no new closed trades AND no new proposals since the last successful PM
    run (nothing to react to),
  * either of the above is overridden by `force=True`.

Every PM invocation lands one `agent_runs` row (via `record_run`), zero or
more `agent_tasks` rows, and zero or more proposal status updates.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from helm.agents.base import (
    GoalBrief,
    build_goal_brief,
    create_task,
    record_run,
    unverified_config_versions,
    unverified_releases,
)
from helm.analytics.economics import book_economics
from helm.config import AGENT_MODEL_DEFAULT, HOUSE_COMPETITOR_ID, HOUSE_TRADE_FILTER
from helm.data.store import conn, insert_audit
from helm.llm import LLMError, complete_json

IST = ZoneInfo("Asia/Kolkata")

PM_MAX_TOKENS = 2000
PM_TEMPERATURE = 0.2

# Hard cap on how many open proposals the weekly review feeds the LLM. The
# prompt serialises every open proposal (title + rationale + proposed_change +
# evidence) to JSON; an unbounded backlog can blow past the kernel's per-argv
# size limit and crash the claude CLI call (E2BIG). 60 is comfortably more than
# any single week produces yet keeps the prompt well inside the CLI's reach.
# Recurrence counts are computed over this same capped set, so a very large
# backlog slightly undercounts recurrence — acceptable, and the backlog-drain
# path exists to chew through anything that overflows.
PM_MAX_OPEN_PROPOSALS = 60

# Closed set the engineer knows how to act on. Anything else lands as
# `needs_human` and surfaces for manual handling. The first group are
# house/code task types (commit + pytest); the last two are freestyle-only
# config edits (a competitor's persona / strategy_config).
HOUSE_ENGINEER_TASK_TYPES = (
    "prompt_tweak",
    "param_change",
    "add_filter",
    "setting_override",
    "add_strategy_variant",
)
FREESTYLE_ENGINEER_TASK_TYPES = (
    "persona_edit",
    "strategy_config_edit",
)
# Superset for the LLM output schema enum; per-agent applicability is enforced
# in _validate_suggestion using the run's competitor_id.
VALID_ENGINEER_TASK_TYPES = (
    *HOUSE_ENGINEER_TASK_TYPES,
    *FREESTYLE_ENGINEER_TASK_TYPES,
    "needs_human",
)

VALID_PROPOSAL_STATUS_CHANGES = ("accepted", "rejected")

PM_SUGGESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create_task", "reject_proposal"],
                    },
                    "proposal_id": {"type": ["integer", "null"]},
                    "task_type": {
                        "type": "string",
                        "enum": list(VALID_ENGINEER_TASK_TYPES),
                    },
                    "title": {"type": "string"},
                    "rationale": {"type": "string"},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                    "spec": {"type": "object"},
                    "proposal_status_change": {
                        "type": ["string", "null"],
                        "enum": ["accepted", "rejected", None],
                    },
                    "status_note": {"type": "string"},
                },
                "required": ["action", "task_type", "title", "rationale",
                             "priority", "spec"],
            },
        },
        "deferred_reason": {"type": "string"},
    },
    "required": ["suggestions"],
}


SYSTEM_PROMPT = """You are the Product Manager for an autonomous intraday paper-trading
bot. Your job once a week: pick 0–3 concrete next changes to ship and reject
proposals that are noise. You DO NOT write code; you queue tasks for the Engineer
agent and update proposal statuses.

Principles (in priority order):

1. RECURRENCE BEATS NOVELTY. The strongest single signal is the `recurrence`
   field on each proposal cluster — how many separate retros surfaced the same
   theme. A 3× cluster of "widen ORB stop on choppy mornings" outranks a single
   5/5-confidence one-off. Quote the recurrence number in your rationale.

2. PREFER CHEAP, REVERSIBLE CHANGES. Order of preference:
       prompt_tweak  >  param_change  ≈  setting_override  >  add_filter
       >>  add_strategy_variant
   A `prompt_tweak` is one PR; an `add_strategy_variant` is a week of data and
   a regression risk. Ship the cheap thing first; revisit the expensive one
   only after the cheap one has been tested.

3. ONE CHANGE PER TASK. Even if two proposals overlap, ship them as separate
   tasks so the Tester can attribute regressions correctly.

4. RESPECT THE ENGINEER'S CLOSED SURFACE. Valid task_types depend on WHICH
   agent you are reviewing (see `agent` in the input):

   HOUSE agent (`agent.id` = "house-claude") — edits the shared codebase:
       prompt_tweak       — wording in the decider system prompt
       param_change       — numeric knob (cap, threshold, lookback)
       add_filter         — new gate in risk/strategy (entry guard)
       setting_override   — write a row to the `settings` table
       add_strategy_variant — new strategy class (heaviest)
       needs_human        — out-of-surface; PM punts to a human

   FREESTYLE agent (any other `agent.id`) — has NO code of its own. Its only
   levers are DATA in Postgres: its persona and its weekly strategy_config.
   Valid task_types are ONLY:
       persona_edit          — rewrite this competitor's trading persona/edge
       strategy_config_edit  — nudge its weekly numeric strategy params
       needs_human           — out-of-surface; PM punts to a human
   A code task_type (prompt_tweak/param_change/add_filter/setting_override/
   add_strategy_variant) is INVALID for a freestyle agent and will be
   rejected — never queue one for a freestyle competitor.

   If the only fix is out-of-surface, file `needs_human` and stop — never
   water down into a fake task of the wrong type.

5. EVIDENCE OR DON'T SHIP. Every task's rationale must cite the retros,
   metrics, or recurrence count that motivated it. No fix without evidence.

6. HONEST UNCERTAINTY. If the data is thin (few trades, no recurring theme),
   queue zero tasks and explain in `deferred_reason`. Forcing a task for the
   sake of activity is worse than silence.

7. RESPECT WHAT WAS JUST TRIED. The input lists the last few Engineer runs.
   If a similar change shipped in the last 7 days, do NOT re-queue it — wait
   for the Tester's verdict.

PROPOSAL-STATUS MOVES — every suggestion may update the source proposal:
  * `accepted` — PM agrees this is the next ship; pair with action=create_task.
  * `rejected` — PM judges this is noise / already addressed / too costly;
                 pair with action=reject_proposal (no task created).
  * null       — leave the proposal in `open` for next week's review.

PRIORITY CONVENTION: 5 = highest, 1 = lowest. The Engineer claims tasks
ORDER BY priority DESC, so a "ship-this-first" task is priority 5.

SPEC SHAPES — the Engineer applies these as deterministic mutators. The
keys below are MANDATORY and exact. Any extra/missing key fails validation
and the task gets rejected unshipped. No paraphrasing — use these literal
field names. Concrete examples are non-negotiable.

  * prompt_tweak — edit a system prompt block.
    spec = {
      "file": "scripts/decide_signals.py" | "helm/retro.py",
      "anchor": "<a substring copied VERBATIM from that file that occurs EXACTLY
                 ONCE — do NOT invent, paraphrase, or reuse the example below.
                 The Engineer checks it against the real file and fails the task
                 if it is absent or non-unique>",
      "action": "append" | "prepend" | "replace",
      "text":   "<the new wording — append/prepend add a paragraph; replace swaps the anchor>"
    }
    Example (ILLUSTRATIVE shape only — never emit this literal anchor; copy a
    real phrase out of the target file instead):
      {"file": "scripts/decide_signals.py",
       "anchor": "<verbatim phrase lifted from the decider prompt>",
       "action": "append",
       "text": "Auto-skip any signal whose age exceeds 15 minutes..."}

  * param_change — rewrite one module-level constant.
    spec = {
      "file": "helm/config.py" | "helm/strategies/intraday/<name>.py",
      "symbol": "<EXACT module-level name, e.g. STALE_THRESHOLD_MINUTES>",
      "new_value": <literal — int/float/string>,
      "expected_type": "int" | "decimal" | "str" | "time"
    }
    Example: {"file": "scripts/decide_signals.py",
              "symbol": "STALE_THRESHOLD_MINUTES", "new_value": 15,
              "expected_type": "int"}

  * add_filter — insert a guarded early-return inside a strategy's scan().
    spec = {
      "strategy": "orb_15m" | "orb_5m" | "vwap_reclaim" | "gap_fade",
      "filter_id": "<unique-kebab-case-marker>",
      "predicate_code": "<single-expression bool using locals already in scope>",
      "where": "pre" | "post"
    }
    The mutator inserts: `if {predicate_code}: return None  # filter: {id}`.
    Locals available in scan(): symbol, candles, latest, prior, latest_close,
    or_high, or_low (ORB), vwaps (VWAP), and any helper defined above.
    Example: {"strategy": "gap_fade", "filter_id": "no-afternoon-entries",
              "predicate_code": "candles[-1]['bar_ts'].astimezone(IST).time() >= time(12, 30)",
              "where": "pre"}

  * setting_override — UPSERT one row in the live `settings` table. No code edit.
    spec = {"key": "<settings key>", "value": <literal>, "note": "<why>"}
    Example: {"key": "max_position_inr", "value": 17500,
              "note": "step up cap after two profitable weeks"}

  * add_strategy_variant — register a new instance of an existing class.
    spec = {
      "base_strategy": "OpeningRangeBreakout" | "VWAPReclaim" | "GapFade",
      "variant_name": "<unique str produced by the new instance's .name>",
      "param_overrides": {<__init__ kwargs>}
    }
    Example: {"base_strategy": "OpeningRangeBreakout",
              "variant_name": "orb_30m",
              "param_overrides": {"or_minutes": 30}}

  * bug_fix — exact-anchor string replacement (any file).
    spec = {"target_file": "<path>", "anchor": "<unique substring>",
            "replacement": "<exact new string>", "reason": "<why>"}

  * persona_edit — FREESTYLE ONLY. Rewrite a competitor's persona/edge.
    spec = {
      "new_persona": "<the full replacement persona text, 50–2000 chars,
                      describing the trading style/edge in plain prose>",
      "diff_summary": "<one line on what changed and why>"
    }
    Example: {"new_persona": "Momentum trader. Buys the strongest large-cap "
              "that has cleanly cleared its morning range on rising volume; "
              "cuts losers fast, lets winners run to a 1.5x-risk target. "
              "Avoids the lunch lull and never holds into the last 20 minutes.",
              "diff_summary": "add lunch-lull avoidance after midday churn losses"}

  * strategy_config_edit — FREESTYLE ONLY. Nudge this week's numeric params.
    spec = {
      "param_overrides": {"<short_key>": <number>, ...},
      "diff_summary": "<one line on what changed and why>"
    }
    Values must be sane scalars: integer counts in [1, 50], multipliers /
    ratios / thresholds in [0, 10]. The override is MERGED into the existing
    weekly strategy_config (so omit keys you don't want to change).
    Example: {"param_overrides": {"max_positions": 2, "stop_atr_mult": 1.5},
              "diff_summary": "tighten stops and concurrency after choppy week"}

  * needs_human — out-of-surface; PM punts to a human.
    spec = {"reason": "<what & why>", "suggested_owner": "<role>"}

If you cannot map a proposal to ONE of the shapes above with all required
keys filled exactly, choose `needs_human`. Never invent extra fields.

OUTPUT FORMAT — strict JSON, no markdown, no prose outside the object:

{"suggestions": [
   {"action": "create_task" | "reject_proposal",
    "proposal_id": <int|null>,
    "task_type": "<one of the closed set>",
    "title": "...",
    "rationale": "Cites recurrence × N or retro evidence.",
    "priority": 1-5,
    "spec": {...},
    "proposal_status_change": "accepted" | "rejected" | null,
    "status_note": "..."}
 ],
 "deferred_reason": "Empty if any suggestions; otherwise a one-line why."}
"""


# ─── inputs ──────────────────────────────────────────────────────────


def _trade_filter(competitor_id: str) -> tuple[str, tuple]:
    """SQL fragment + args to scope paper_trades/signals/decisions to one agent.

    House (competitor_id='house-claude') uses the (NULL OR ='house-claude')
    filter since the live cron path inserts NULL; freestyle uses an exact
    competitor_id match."""
    if competitor_id == HOUSE_COMPETITOR_ID:
        return HOUSE_TRADE_FILTER, ()
    return "competitor_id = %s", (competitor_id,)


def _last_successful_pm_run(competitor_id: str) -> dict | None:
    """Most recent PM run for this agent that finished with outcome='ok'.
    Drives the 'anything new since then' skip check. agent_runs.competitor_id
    is canonicalised to the loop id (house = 'house-claude')."""
    with conn() as c:
        return c.execute(
            "SELECT id, started_ts, finished_ts FROM agent_runs "
            "WHERE agent = 'pm' AND outcome = 'ok' AND competitor_id = %s "
            "ORDER BY started_ts DESC LIMIT 1",
            (competitor_id,),
        ).fetchone()


def _new_trades_since(since: datetime | None, competitor_id: str) -> int:
    trade_filter, fargs = _trade_filter(competitor_id)
    sql = (f"SELECT COUNT(*) AS n FROM paper_trades "
           f"WHERE status = 'CLOSED' AND {trade_filter}")  # noqa: S608
    args: tuple = fargs
    if since is not None:
        sql += " AND exit_ts > %s"
        args = (*fargs, since)
    with conn() as c:
        return int(c.execute(sql, args).fetchone()["n"])


def _new_proposals_since(since: datetime | None, competitor_id: str) -> int:
    sql = "SELECT COUNT(*) AS n FROM improvement_proposals WHERE competitor_id = %s"
    args: tuple = (competitor_id,)
    if since is not None:
        sql += " AND created_ts > %s"
        args = (competitor_id, since)
    with conn() as c:
        return int(c.execute(sql, args).fetchone()["n"])


def _cluster_key(title: str) -> str:
    """Same loose dedupe the digest uses — lowercase, strip light punctuation,
    collapse whitespace. Stable across retros so a recurring idea clusters."""
    t = title.lower()
    for ch in "().,:;-—":
        t = t.replace(ch, " ")
    return " ".join(t.split())


def _open_proposals_30d(competitor_id: str) -> list[dict]:
    """Open proposals + parent-retro context for one agent, clustered by title.

    Each row is a single proposal but carries `recurrence` = how many open
    proposals in the window share the same cluster key. That's the headline
    signal the LLM should weight.
    """
    with conn() as c:
        rows = list(c.execute(
            """
            SELECT
                p.id, p.category, p.title, p.rationale, p.proposed_change,
                p.evidence, p.confidence, p.status, p.created_ts,
                r.kind AS retro_kind, r.verdict_label,
                r.trade_id, r.decision_id, r.tags
            FROM improvement_proposals p
            JOIN trade_retrospectives r ON r.id = p.retro_id
            WHERE p.status = 'open'
              AND p.created_ts >= now() - interval '30 days'
              AND p.competitor_id = %s
            ORDER BY p.created_ts DESC
            LIMIT %s
            """,
            (competitor_id, PM_MAX_OPEN_PROPOSALS),
        ))

    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[_cluster_key(r["title"])] += 1

    out: list[dict] = []
    for r in rows:
        key = _cluster_key(r["title"])
        out.append({
            "id": int(r["id"]),
            "category": r["category"],
            "title": r["title"],
            "rationale": r["rationale"],
            "proposed_change": r["proposed_change"],
            "evidence": r["evidence"],
            "confidence": int(r["confidence"]) if r["confidence"] is not None else None,
            "retro_kind": r["retro_kind"],
            "verdict_label": r["verdict_label"],
            "trade_id": r["trade_id"],
            "decision_id": r["decision_id"],
            "tags": r["tags"],
            "created_ts": r["created_ts"].astimezone(IST).strftime("%Y-%m-%d %H:%M"),
            "recurrence": counts[key],
        })
    return out


def _recent_retros_7d(competitor_id: str, limit: int = 10) -> list[dict]:
    with conn() as c:
        rows = list(c.execute(
            """
            SELECT id, kind, verdict_label, signal_quality_score,
                   decision_quality_score, summary_layman, created_ts
            FROM trade_retrospectives
            WHERE created_ts >= now() - interval '7 days'
              AND competitor_id = %s
            ORDER BY created_ts DESC
            LIMIT %s
            """,
            (competitor_id, limit),
        ))
    return [
        {
            "id": int(r["id"]),
            "kind": r["kind"],
            "verdict_label": r["verdict_label"],
            "signal_quality_score": r["signal_quality_score"],
            "decision_quality_score": r["decision_quality_score"],
            "summary_layman": (r["summary_layman"] or "")[:400],
            "created_ts": r["created_ts"].astimezone(IST).strftime("%Y-%m-%d %H:%M"),
        }
        for r in rows
    ]


def _recent_engineer_runs(competitor_id: str, limit: int = 5) -> list[dict]:
    """What the engineer has tried recently for this agent — keeps the PM from
    re-suggesting the same change while a change is in flight or just landed.

    House engineer runs on code tasks aren't always tagged with a competitor_id
    (the historic code path inserts NULL), so the house view uses the
    (NULL OR ='house-claude') filter; freestyle uses an exact match.
    """
    fargs: tuple
    if competitor_id == HOUSE_COMPETITOR_ID:
        run_filter, fargs = HOUSE_TRADE_FILTER, ()
    else:
        run_filter, fargs = "competitor_id = %s", (competitor_id,)
    with conn() as c:
        rows = list(c.execute(
            f"""
            SELECT id, started_ts, finished_ts, task_id, release_id,
                   outcome, summary
            FROM agent_runs
            WHERE agent = 'engineer' AND {run_filter}
            ORDER BY started_ts DESC
            LIMIT %s
            """,  # noqa: S608 — filter is a hardcoded literal
            (*fargs, limit),
        ))
    return [
        {
            "id": int(r["id"]),
            "task_id": r["task_id"],
            "release_id": r["release_id"],
            "outcome": r["outcome"],
            "summary": (r["summary"] or "")[:300],
            "started_ts": r["started_ts"].astimezone(IST).strftime("%Y-%m-%d %H:%M"),
        }
        for r in rows
    ]


# ─── prompt builder ──────────────────────────────────────────────────


def _build_user_prompt(agent: dict, goal: GoalBrief, proposals: list[dict],
                       retros: list[dict], engineer_runs: list[dict]) -> str:
    is_house = agent["id"] == HOUSE_COMPETITOR_ID
    valid_types = list(
        HOUSE_ENGINEER_TASK_TYPES if is_house else FREESTYLE_ENGINEER_TASK_TYPES
    ) + ["needs_human"]
    # F1: this agent's own unit economics so the PM targets the real lever
    # (costs, not just net). Read-only; never break the run if analytics fail.
    try:
        econ = book_economics(competitor_filter=agent["id"], last_n=30).as_dict()
    except Exception:
        econ = {}
    payload = {
        "now_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
        "agent": agent,
        "valid_task_types_for_this_agent": valid_types,
        "goal_brief": goal.to_prompt_dict(),
        "open_proposals_last_30d": proposals,
        "recent_retros_last_7d": retros,
        "recent_engineer_runs": engineer_runs,
        "unit_economics_last_30d": econ,
        "notes": (
            "You are reviewing ONE agent (see `agent`). "
            + ("This is the HOUSE agent — it edits the shared codebase; use "
               "code task_types." if is_house else
               "This is a FREESTYLE competitor — it has no code; use ONLY "
               "persona_edit / strategy_config_edit / needs_human.")
            + " recurrence = how many open proposals share the same loose "
            "title cluster. That is the single strongest signal — quote it in "
            "your rationale. Queue 0–3 tasks. Empty is fine if nothing recurs."
            + " `unit_economics_last_30d` shows this agent's gross vs cost vs "
            "net and its edge-to-cost ratio (E2C): if E2C < ~3 the real problem "
            "is that moves are too small relative to ~₹13/trade cost — prefer "
            "changes that capture bigger moves / trade less over win-rate tweaks."
        ),
    }
    return (
        "Review the bot's state and pick 0–3 next changes to queue.\n\n"
        "```json\n" + json.dumps(payload, indent=2, default=str) + "\n```"
    )


# ─── apply ───────────────────────────────────────────────────────────


def _validate_suggestion(s: dict, *, is_house: bool) -> tuple[bool, str]:
    """Sanity-check one LLM suggestion before we act on it. Returns
    (ok, reason). Reasons surface in the run trace so misbehaving LLMs are
    debuggable.

    `is_house` gates which task_types are valid for the agent under review:
    code types are house-only, config types are freestyle-only; `needs_human`
    is always valid.
    """
    action = s.get("action")
    if action not in ("create_task", "reject_proposal"):
        return False, f"invalid action {action!r}"

    task_type = s.get("task_type")
    if task_type not in VALID_ENGINEER_TASK_TYPES:
        return False, f"invalid task_type {task_type!r}"
    if is_house and task_type in FREESTYLE_ENGINEER_TASK_TYPES:
        return False, f"task_type {task_type!r} is freestyle-only, not valid for house"
    if not is_house and task_type in HOUSE_ENGINEER_TASK_TYPES:
        return False, f"task_type {task_type!r} is house-only, not valid for freestyle"

    title = str(s.get("title") or "").strip()
    rationale = str(s.get("rationale") or "").strip()
    if not title or not rationale:
        return False, "title and rationale must be non-empty"

    spec = s.get("spec")
    if not isinstance(spec, dict):
        return False, "spec must be an object"

    try:
        priority = int(s.get("priority", 3))
    except (TypeError, ValueError):
        return False, "priority not an int"
    if not 1 <= priority <= 5:
        return False, f"priority out of range: {priority}"

    proposal_id = s.get("proposal_id")
    if proposal_id is not None and not isinstance(proposal_id, int):
        return False, "proposal_id must be int or null"

    if action == "reject_proposal" and proposal_id is None:
        return False, "reject_proposal requires proposal_id"

    status_change = s.get("proposal_status_change")
    if status_change not in (None, "accepted", "rejected"):
        return False, f"invalid proposal_status_change {status_change!r}"

    return True, ""


def _apply_proposal_status(proposal_id: int, new_status: str, note: str) -> bool:
    """Update one proposal's status; return True on success."""
    with conn() as c:
        row = c.execute(
            "UPDATE improvement_proposals "
            "SET status = %s, status_note = %s, status_ts = now() "
            "WHERE id = %s AND status = 'open' "
            "RETURNING id",
            (new_status, note[:1000] if note else None, proposal_id),
        ).fetchone()
    return row is not None


# ─── per-agent review body ───────────────────────────────────────────


def _agent_descriptor(competitor_id: str) -> dict:
    """Compact agent record for the prompt (id + persona + freestyle flag)."""
    is_house = competitor_id == HOUSE_COMPETITOR_ID
    persona = None
    with conn() as c:
        row = c.execute(
            "SELECT name, persona FROM competitors WHERE id = %s",
            (competitor_id,),
        ).fetchone()
    name = row["name"] if row else competitor_id
    persona = row["persona"] if row else None
    return {
        "id": competitor_id,
        "name": name,
        "kind": "house" if is_house else "freestyle",
        "persona": persona,
    }


def _unverified_for_agent(competitor_id: str) -> list[dict]:
    """The agent's in-flight change(s) — house releases, freestyle config
    versions — gating the next ship (one change at a time per agent)."""
    if competitor_id == HOUSE_COMPETITOR_ID:
        return unverified_releases()
    return unverified_config_versions(competitor_id)


def _review_one_agent(competitor_id: str, *, model: str, mode: str,
                      force: bool) -> dict:
    """Run one PM review for a single agent (house or freestyle).

    Returns a summary dict with a stable key set (plus `competitor_id`) so the
    CLI can render a uniform per-agent summary regardless of whether work
    happened. Each agent's run lands its own `agent_runs` row tagged with the
    competitor.
    """
    is_house = competitor_id == HOUSE_COMPETITOR_ID

    def _deferred(run, reason: str, **trace) -> dict:
        run.outcome = "noop"
        run.summary = f"deferred — {reason}"
        run.add_trace(deferred=True, reason=reason, competitor_id=competitor_id,
                      **trace)
        return {
            "run_id": run.run_id,
            "competitor_id": competitor_id,
            "tasks_created": [],
            "proposals_accepted": [],
            "proposals_rejected": [],
            "deferred": True,
            "reason": reason,
        }

    with record_run("pm", "weekly_review", model=model, llm_mode=mode,
                    competitor_id=competitor_id) as run:
        # ── skip rules ────────────────────────────────────────────────
        unverified = _unverified_for_agent(competitor_id)
        if unverified and not force:
            change_word = "release" if is_house else "config version"
            reason = f"{len(unverified)} unverified {change_word}(s) in flight"
            insert_audit("agents", "pm_deferred",
                         {"reason": reason, "competitor_id": competitor_id,
                          "unverified": [r["id"] for r in unverified]})
            return _deferred(run, reason,
                             unverified_ids=[r["id"] for r in unverified])

        last_run = _last_successful_pm_run(competitor_id)
        since = last_run["finished_ts"] if last_run else None
        new_trades = _new_trades_since(since, competitor_id)
        new_proposals = _new_proposals_since(since, competitor_id)
        if not force and new_trades == 0 and new_proposals == 0:
            reason = "no new closed trades and no new proposals since last PM run"
            return _deferred(run, reason,
                             last_run_id=last_run["id"] if last_run else None,
                             new_trades=new_trades, new_proposals=new_proposals)

        # ── gather inputs (all scoped to this agent) ──────────────────
        agent = _agent_descriptor(competitor_id)
        goal = build_goal_brief(competitor_id=competitor_id)
        proposals = _open_proposals_30d(competitor_id)
        retros = _recent_retros_7d(competitor_id)
        eng_runs = _recent_engineer_runs(competitor_id)

        run.add_trace(
            competitor_id=competitor_id,
            inputs={
                "open_proposals_count": len(proposals),
                "recent_retros_count": len(retros),
                "recent_engineer_runs_count": len(eng_runs),
                "new_trades_since_last_run": new_trades,
                "new_proposals_since_last_run": new_proposals,
            },
        )

        # ── ask the LLM ───────────────────────────────────────────────
        user = _build_user_prompt(agent, goal, proposals, retros, eng_runs)
        try:
            parsed = complete_json(
                SYSTEM_PROMPT, user,
                schema=PM_SUGGESTION_SCHEMA,
                model=model, mode=mode,
                max_tokens=PM_MAX_TOKENS,
                temperature=PM_TEMPERATURE,
            )
        except LLMError as exc:
            run.summary = f"LLM error: {str(exc)[:200]}"
            run.add_trace(llm_error=str(exc)[:500])
            raise

        suggestions_raw = parsed.get("suggestions") or []
        if not isinstance(suggestions_raw, list):
            suggestions_raw = []
        deferred_reason = str(parsed.get("deferred_reason") or "").strip()

        # ── apply ─────────────────────────────────────────────────────
        tasks_created: list[int] = []
        proposals_accepted: list[int] = []
        proposals_rejected: list[int] = []
        rejected_for_invalid: list[dict] = []

        for s in suggestions_raw[:3]:
            if not isinstance(s, dict):
                continue
            ok, why = _validate_suggestion(s, is_house=is_house)
            if not ok:
                rejected_for_invalid.append({"suggestion": s, "reason": why})
                continue

            action = s["action"]
            proposal_id = s.get("proposal_id")
            status_change = s.get("proposal_status_change")
            status_note = str(s.get("status_note") or "")

            if action == "reject_proposal":
                assert isinstance(proposal_id, int)
                applied = _apply_proposal_status(
                    proposal_id, "rejected", status_note or "rejected by pm")
                if applied:
                    proposals_rejected.append(proposal_id)
                continue

            # action == 'create_task'
            try:
                task_id = create_task(
                    created_by="pm",
                    title=str(s["title"]).strip()[:200],
                    rationale=str(s["rationale"]).strip()[:2000],
                    task_type=s["task_type"],
                    spec=s["spec"],
                    proposal_id=proposal_id,
                    priority=int(s.get("priority", 3)),
                    competitor_id=competitor_id,
                )
            except ValueError as exc:
                rejected_for_invalid.append(
                    {"suggestion": s, "reason": f"create_task: {exc}"})
                continue

            tasks_created.append(task_id)

            if proposal_id is not None and status_change in ("accepted", "rejected"):
                applied = _apply_proposal_status(
                    proposal_id, status_change,
                    status_note or f"{status_change} by pm via task {task_id}")
                if applied and status_change == "accepted":
                    proposals_accepted.append(proposal_id)
                elif applied and status_change == "rejected":
                    proposals_rejected.append(proposal_id)

        # ── finalise run trace ────────────────────────────────────────
        run.summary = (
            f"[{competitor_id}] tasks={len(tasks_created)} "
            f"accepted={len(proposals_accepted)} "
            f"rejected={len(proposals_rejected)}"
        )
        run.add_trace(
            tasks_created=tasks_created,
            proposals_accepted=proposals_accepted,
            proposals_rejected=proposals_rejected,
            invalid_suggestions=rejected_for_invalid,
            deferred_reason=deferred_reason,
            goal_brief=goal.to_prompt_dict(),
        )
        insert_audit("agents", "pm_review_done",
                     {"run_id": run.run_id,
                      "competitor_id": competitor_id,
                      "tasks_created": tasks_created,
                      "proposals_accepted": proposals_accepted,
                      "proposals_rejected": proposals_rejected})

        return {
            "run_id": run.run_id,
            "competitor_id": competitor_id,
            "tasks_created": tasks_created,
            "proposals_accepted": proposals_accepted,
            "proposals_rejected": proposals_rejected,
            "deferred": False,
            "reason": deferred_reason or "",
        }


# ─── public entry point ──────────────────────────────────────────────


def run_weekly_review(*, model: str | None = None, mode: str | None = None,
                      force: bool = False,
                      competitor_id: str | None = None) -> dict | list[dict]:
    """Run the PM weekly review.

    `competitor_id` selects which agent to review:
      * a specific id → review just that agent; returns a single summary dict
        (same shape as before, plus `competitor_id`).
      * None (cron default) → review the HOUSE agent plus every active
        freestyle competitor, one independent review each; returns a LIST of
        per-agent summary dicts.

    Each per-agent review is fully isolated: its own skip rules, inputs, LLM
    call, and `agent_runs` row. A failure in one agent's LLM call propagates
    (the cron log surfaces it) — but the per-agent rows already written stand.
    """
    model = model or os.environ.get("AGENT_MODEL", AGENT_MODEL_DEFAULT)
    mode = (mode or os.environ.get("LLM_MODE", "cli")).strip().lower()

    if competitor_id is not None:
        return _review_one_agent(competitor_id, model=model, mode=mode,
                                 force=force)

    # All-agents pass (cron): house first, then active freestyle competitors.
    from helm.competition.competitors import freestyle_competitors

    agent_ids = [HOUSE_COMPETITOR_ID] + [c.id for c in freestyle_competitors()]
    results: list[dict] = []
    for aid in agent_ids:
        results.append(_review_one_agent(aid, model=model, mode=mode,
                                         force=force))
    return results


# ─── backlog-drain mode (additive, flag-gated) ───────────────────────
# A bounded, autonomous pass that clears an agent's EXISTING status='open'
# proposal backlog. Unlike run_weekly_review it:
#   * ignores the "nothing new since last run" + "unverified in flight" skip
#     gates (a human explicitly nudged it),
#   * feeds the PM a RANKED BATCH of RAW proposals (helm.agents.backlog's
#     batch_open_proposals) and asks it to do the dedup SEMANTICALLY — the
#     lexical Jaccard clusterer is NOT in the authoritative path (it produced
#     ~zero dedup against the real backlog; restatements share no tokens).
#   * the PM returns a per-proposal action list (accept / supersede / reject):
#       - accept    → create ONE task + mark the proposal 'accepted',
#       - supersede → mark a restatement 'superseded', noting the accepted id,
#       - reject    → mark a low-value proposal 'rejected'.
#   * NEW tasks created per pass are capped at the existing 0–3 shape so the
#     Engineer isn't flooded; supersede/reject are UNBOUNDED per pass so a
#     single batch can collapse dozens of restatements at once.
# Designed to be called repeatedly (scripts/drain_backlog.py) until the backlog
# is empty — far fewer passes than the old one-cluster-per-pass shape.

BACKLOG_MAX_CLUSTERS_DEFAULT = 3

# How many NEW tasks (action='accept') a single batch pass may create. Keeps
# the Engineer's queue to the same 0–3 shape as the weekly review. supersede /
# reject are deliberately NOT capped — a pass should be free to collapse the
# whole long tail of restatements in one shot.
BACKLOG_MAX_ACCEPTS_PER_PASS = 3

# How many raw proposals to hand the PM in one batch. Big enough that genuine
# restatements land in the same batch (so the PM can see and supersede them),
# small enough to stay inside the model's context + the PM_BATCH token budget
# AND to finish one constrained-JSON `claude` CLI call inside CLI_TIMEOUT_S
# (300s). Measured: batch=8 ≈ 112s; batch=20 overran 300s and timed out. Keep
# this conservative — the drain just runs more (reliable) passes.
BACKLOG_BATCH_SIZE_DEFAULT = 8

# Larger token ceiling than the weekly review: the batch response carries one
# entry PER proposal (potentially 20+), not just 0–3 suggestions.
PM_BATCH_MAX_TOKENS = 4000

# Per-proposal action verbs the PM returns in a batch pass.
BACKLOG_BATCH_ACTIONS = ("accept", "supersede", "reject")

# Batch response schema — one entry per proposal the PM acted on. `accept`
# carries the same task fields as a weekly create_task suggestion; `supersede`
# carries the id of the accepted proposal it duplicates; `reject` is bare.
PM_BACKLOG_BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "proposal_id": {"type": "integer"},
                    "action": {
                        "type": "string",
                        "enum": list(BACKLOG_BATCH_ACTIONS),
                    },
                    # accept-only task fields (mandatory when action=accept)
                    "task_type": {
                        "type": "string",
                        "enum": list(VALID_ENGINEER_TASK_TYPES),
                    },
                    "title": {"type": "string"},
                    "rationale": {"type": "string"},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                    "spec": {"type": "object"},
                    # supersede-only field (mandatory when action=supersede)
                    "supersedes_id": {"type": ["integer", "null"]},
                    "status_note": {"type": "string"},
                },
                "required": ["proposal_id", "action"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["actions"],
}


# ─── batch action validation (semantic-dedup path) ────────────────────


def _validate_batch_action(a: dict, *, is_house: bool,
                           batch_ids: set[int]) -> tuple[bool, str]:
    """Sanity-check one per-proposal batch action before acting on it.

    Returns (ok, reason). Validates structurally only — semantic dedup quality
    is the PM's job. `batch_ids` is the set of proposal ids actually present in
    this batch; a `proposal_id` outside it is rejected (the PM may only act on
    proposals it was shown). For action='accept' the same task-shape checks as
    `_validate_suggestion` apply (valid task_type for the agent kind, non-empty
    title/rationale, object spec, in-range priority).
    """
    if not isinstance(a, dict):
        return False, "action entry must be an object"

    pid = a.get("proposal_id")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False, "proposal_id must be an integer"
    if pid not in batch_ids:
        return False, f"proposal_id {pid} not in this batch"

    action = a.get("action")
    if action not in BACKLOG_BATCH_ACTIONS:
        return False, f"invalid action {action!r}"

    if action == "supersede":
        sup = a.get("supersedes_id")
        if not isinstance(sup, int) or isinstance(sup, bool):
            return False, "supersede requires integer supersedes_id"
        if sup == pid:
            return False, "a proposal cannot supersede itself"
        return True, ""

    if action == "reject":
        return True, ""

    # action == "accept" — must carry a valid, in-surface task.
    task_type = a.get("task_type")
    if task_type not in VALID_ENGINEER_TASK_TYPES:
        return False, f"invalid task_type {task_type!r}"
    if is_house and task_type in FREESTYLE_ENGINEER_TASK_TYPES:
        return False, f"task_type {task_type!r} is freestyle-only, not valid for house"
    if not is_house and task_type in HOUSE_ENGINEER_TASK_TYPES:
        return False, f"task_type {task_type!r} is house-only, not valid for freestyle"

    title = str(a.get("title") or "").strip()
    rationale = str(a.get("rationale") or "").strip()
    if not title or not rationale:
        return False, "accept requires non-empty title and rationale"

    if not isinstance(a.get("spec"), dict):
        return False, "accept requires an object spec"

    try:
        priority = int(a.get("priority", 3))
    except (TypeError, ValueError):
        return False, "priority not an int"
    if not 1 <= priority <= 5:
        return False, f"priority out of range: {priority}"

    return True, ""


def _backlog_proposal_view(p: dict) -> dict:
    """Compact per-proposal view fed to the PM batch prompt. The PM dedups on
    the words, so title + proposed_change + rationale are the load-bearing
    fields; category and confidence give it ranking context."""
    return {
        "id": int(p["id"]),
        "category": p.get("category"),
        "confidence": p.get("confidence"),
        "title": p.get("title"),
        "proposed_change": p.get("proposed_change"),
        "rationale": p.get("rationale"),
    }


def _build_backlog_prompt(agent: dict, goal: GoalBrief,
                          proposals: list[dict], *,
                          max_accepts: int = BACKLOG_MAX_ACCEPTS_PER_PASS) -> str:
    """User prompt for the SEMANTIC batch-consolidation pass.

    The input is a RANKED BATCH of RAW open proposals (NOT lexical clusters):
    the PM reads them and dedups by MEANING — restatements of the same idea
    must merge even when they share no tokens (e.g. "R:R floor" vs
    "reward-to-risk floor" vs "reward-vs-risk floor"). For each proposal in the
    batch the PM returns exactly one action:

      * accept    — the BEST distinct idea in a group of restatements. Carries
                    task fields (task_type/title/rationale/priority/spec). This
                    is what gets built. AT MOST `max_accepts` accepts per batch.
      * supersede — a restatement of an accepted idea. Carries `supersedes_id`
                    = the proposal_id it duplicates. UNBOUNDED per batch.
      * reject    — genuinely low-value / already-addressed / out-of-surface.
                    UNBOUNDED per batch.

    Proposals the PM omits stay 'open' for the next batch.
    """
    is_house = agent["id"] == HOUSE_COMPETITOR_ID
    valid_types = list(
        HOUSE_ENGINEER_TASK_TYPES if is_house else FREESTYLE_ENGINEER_TASK_TYPES
    ) + ["needs_human"]
    proposal_payload = [_backlog_proposal_view(p) for p in proposals]
    payload = {
        "now_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
        "mode": "backlog_drain_batch",
        "agent": agent,
        "valid_task_types_for_this_agent": valid_types,
        "goal_brief": goal.to_prompt_dict(),
        "open_proposals": proposal_payload,
        "max_accepts_this_batch": max_accepts,
        "notes": (
            "BACKLOG-DRAIN (SEMANTIC CONSOLIDATION). The list below is a batch "
            "of this agent's EXISTING open proposals, ranked by category then "
            "confidence. They are NOT pre-deduplicated — many are the SAME idea "
            "restated with different wording or abbreviations (e.g. 'R:R floor' "
            "== 'reward-to-risk floor' == 'reward-vs-risk floor'). Your job is "
            "to consolidate them by MEANING, not by shared words.\n\n"
            "For EVERY proposal in the batch, emit ONE entry in `actions`:\n"
            "  - 'accept'    : this is the single BEST, distinct idea. Provide "
            "task_type, title, rationale, priority (1-5), spec. The Engineer "
            "will build exactly this. Accept the strongest phrasing of each "
            "distinct idea — and AT MOST "
            f"{max_accepts} accepts in this whole batch.\n"
            "  - 'supersede' : this restates an idea you accepted (or already "
            "shipped). Set `supersedes_id` to the accepted proposal_id it "
            "duplicates. Use this LIBERALLY — every redundant restatement of an "
            "accepted idea should be superseded. No limit on supersedes.\n"
            "  - 'reject'    : genuinely low-value, already-addressed, or "
            "out-of-surface noise. No limit on rejects.\n\n"
            "Prefer accept+supersede over reject when proposals are real but "
            "duplicative: one accept, the rest superseded to it. Only reject "
            "what has no value at all. You may omit a proposal to leave it open "
            "for a later batch, but prefer to resolve everything you can. "
            "Cite how many restatements you collapsed in each accept's "
            "rationale.\n\n"
            + ("This is the HOUSE agent — use code task_types." if is_house else
               "This is a FREESTYLE competitor — use ONLY persona_edit / "
               "strategy_config_edit / needs_human for accepts.")
        ),
    }
    return (
        "Consolidate this agent's open-proposal backlog SEMANTICALLY. Return a "
        "per-proposal action list (accept / supersede / reject).\n\n"
        "```json\n" + json.dumps(payload, indent=2, default=str) + "\n```"
    )


def run_backlog_drain(competitor_id: str, *, model: str | None = None,
                      mode: str | None = None,
                      max_clusters: int = BACKLOG_MAX_CLUSTERS_DEFAULT,
                      batch_size: int = BACKLOG_BATCH_SIZE_DEFAULT) -> dict:
    """One bounded backlog-drain pass for a single agent — SEMANTIC dedup.

    Loads a RANKED BATCH of the agent's raw status='open' proposals (NOT lexical
    clusters; the Jaccard clusterer demonstrably under-dedups) and asks the PM
    to consolidate them by MEANING. The PM returns a per-proposal action list:

      * accept    → create ONE task + mark the proposal 'accepted'. Capped at
                    `max_clusters` (kept ≤3) accepts per pass so the Engineer's
                    queue stays the same 0–3 shape.
      * supersede → mark the restatement 'superseded' with a status_note
                    referencing the accepted proposal id. UNBOUNDED per pass.
      * reject    → mark the proposal 'rejected'. UNBOUNDED per pass.

    `batch_size` is how many raw proposals are shown to the PM in this pass (the
    highest-ranked slice). Because one pass can supersede/reject the whole long
    tail, the backlog drains in far fewer passes than one-cluster-per-pass.

    Records an `agent_runs` row exactly like the weekly path. Returns a summary
    dict with `tasks_created`, `proposals_accepted`, `proposals_superseded`,
    `proposals_rejected`, and `clusters_seen` (= proposals shown this batch, so
    the drain loop's progress check keeps working). Designed to be called
    repeatedly (scripts/drain_backlog.py) until no open proposals remain.
    """
    from helm.agents.backlog import batch_open_proposals

    model = model or os.environ.get("AGENT_MODEL", AGENT_MODEL_DEFAULT)
    mode = (mode or os.environ.get("LLM_MODE", "cli")).strip().lower()
    # `max_clusters` doubles as the accept cap here (kept to the 0–3 shape so the
    # Engineer isn't flooded). supersede / reject are NOT capped.
    max_accepts = max(0, min(int(max_clusters), BACKLOG_MAX_ACCEPTS_PER_PASS))
    batch_size = max(1, int(batch_size))

    is_house = competitor_id == HOUSE_COMPETITOR_ID

    with record_run("pm", "backlog_drain", model=model, llm_mode=mode,
                    competitor_id=competitor_id) as run:
        batch = batch_open_proposals(competitor_id, limit=batch_size)

        if not batch:
            run.outcome = "noop"
            run.summary = f"[{competitor_id}] backlog empty"
            run.add_trace(competitor_id=competitor_id,
                          mode="backlog_drain_batch", clusters_seen=0)
            return {
                "run_id": run.run_id,
                "competitor_id": competitor_id,
                "tasks_created": [],
                "proposals_accepted": [],
                "proposals_superseded": [],
                "proposals_rejected": [],
                "clusters_seen": 0,
                "proposals_seen": 0,
                "deferred": False,
                "reason": "backlog empty",
            }

        agent = _agent_descriptor(competitor_id)
        goal = build_goal_brief(competitor_id=competitor_id)
        batch_ids = {int(p["id"]) for p in batch}

        run.add_trace(
            competitor_id=competitor_id,
            mode="backlog_drain_batch",
            inputs={
                "batch_size": len(batch),
                "max_accepts": max_accepts,
                "batch_proposal_ids": sorted(batch_ids),
            },
        )

        user = _build_backlog_prompt(agent, goal, batch, max_accepts=max_accepts)
        try:
            parsed = complete_json(
                SYSTEM_PROMPT, user,
                schema=PM_BACKLOG_BATCH_SCHEMA,
                model=model, mode=mode,
                max_tokens=PM_BATCH_MAX_TOKENS,
                temperature=PM_TEMPERATURE,
            )
        except LLMError as exc:
            run.summary = f"LLM error: {str(exc)[:200]}"
            run.add_trace(llm_error=str(exc)[:500])
            raise

        actions_raw = parsed.get("actions") or []
        if not isinstance(actions_raw, list):
            actions_raw = []
        batch_summary = str(parsed.get("summary") or "").strip()

        tasks_created: list[int] = []
        proposals_accepted: list[int] = []
        proposals_superseded: list[int] = []
        proposals_rejected: list[int] = []
        rejected_for_invalid: list[dict] = []
        handled_ids: set[int] = set()
        accept_task_by_pid: dict[int, int] = {}

        # PASS 1 — accepts first, so supersedes can reference a task/accept that
        # has already landed. Accepts are capped; supersede/reject are not.
        for a in actions_raw:
            if not isinstance(a, dict) or a.get("action") != "accept":
                continue
            ok, why = _validate_batch_action(a, is_house=is_house,
                                             batch_ids=batch_ids)
            if not ok:
                rejected_for_invalid.append({"action": a, "reason": why})
                continue
            pid = int(a["proposal_id"])
            if pid in handled_ids:
                rejected_for_invalid.append(
                    {"action": a, "reason": f"proposal {pid} already handled"})
                continue
            if len(tasks_created) >= max_accepts:
                # Over the accept cap — leave this proposal 'open' for a later
                # batch rather than flooding the Engineer.
                rejected_for_invalid.append(
                    {"action": a,
                     "reason": f"accept cap {max_accepts} reached this pass"})
                continue

            status_note = str(a.get("status_note") or "")
            try:
                task_id = create_task(
                    created_by="pm",
                    title=str(a["title"]).strip()[:200],
                    rationale=str(a["rationale"]).strip()[:2000],
                    task_type=a["task_type"],
                    spec=a["spec"],
                    proposal_id=pid,
                    priority=int(a.get("priority", 3)),
                    competitor_id=competitor_id,
                )
            except ValueError as exc:
                rejected_for_invalid.append(
                    {"action": a, "reason": f"create_task: {exc}"})
                continue

            tasks_created.append(task_id)
            accept_task_by_pid[pid] = task_id
            handled_ids.add(pid)
            if _apply_proposal_status(
                    pid, "accepted",
                    status_note or f"accepted by pm via task {task_id} "
                                   "(backlog drain)"):
                proposals_accepted.append(pid)

        accepted_set = set(proposals_accepted) | set(accept_task_by_pid)

        # PASS 2 — supersedes + rejects. Unbounded.
        for a in actions_raw:
            if not isinstance(a, dict):
                continue
            action = a.get("action")
            if action == "accept":
                continue  # already handled in pass 1
            ok, why = _validate_batch_action(a, is_house=is_house,
                                             batch_ids=batch_ids)
            if not ok:
                rejected_for_invalid.append({"action": a, "reason": why})
                continue
            pid = int(a["proposal_id"])
            if pid in handled_ids:
                rejected_for_invalid.append(
                    {"action": a, "reason": f"proposal {pid} already handled"})
                continue

            status_note = str(a.get("status_note") or "")

            if action == "reject":
                note = status_note or "rejected by pm (backlog drain)"
                if _apply_proposal_status(pid, "rejected", note):
                    proposals_rejected.append(pid)
                handled_ids.add(pid)
                continue

            # action == "supersede"
            sup_id = a.get("supersedes_id")
            # Reference is informational; if it points at an accept this pass we
            # note the task too. We do NOT require the target to be accepted in
            # the same batch (it may have shipped earlier).
            ref = f"superseded by proposal {sup_id} (backlog drain)"
            if isinstance(sup_id, int) and sup_id in accept_task_by_pid:
                ref = (f"superseded by accepted proposal {sup_id} "
                       f"(task {accept_task_by_pid[sup_id]}, backlog drain)")
            elif isinstance(sup_id, int) and sup_id in accepted_set:
                ref = f"superseded by accepted proposal {sup_id} (backlog drain)"
            note = status_note or ref
            if _apply_proposal_status(pid, "superseded", note):
                proposals_superseded.append(pid)
            handled_ids.add(pid)

        run.summary = (
            f"[{competitor_id}] backlog batch={len(batch)} "
            f"tasks={len(tasks_created)} accepted={len(proposals_accepted)} "
            f"superseded={len(proposals_superseded)} "
            f"rejected={len(proposals_rejected)}"
        )
        run.add_trace(
            tasks_created=tasks_created,
            proposals_accepted=proposals_accepted,
            proposals_superseded=proposals_superseded,
            proposals_rejected=proposals_rejected,
            invalid_actions=rejected_for_invalid,
            batch_summary=batch_summary,
        )
        insert_audit("agents", "pm_backlog_drain_done",
                     {"run_id": run.run_id,
                      "competitor_id": competitor_id,
                      "proposals_seen": len(batch),
                      "tasks_created": tasks_created,
                      "proposals_accepted": proposals_accepted,
                      "proposals_superseded": proposals_superseded,
                      "proposals_rejected": proposals_rejected})

        return {
            "run_id": run.run_id,
            "competitor_id": competitor_id,
            "tasks_created": tasks_created,
            "proposals_accepted": proposals_accepted,
            "proposals_superseded": proposals_superseded,
            "proposals_rejected": proposals_rejected,
            # `clusters_seen` retained for the drain loop's progress check; now
            # it means "raw proposals shown to the PM this batch".
            "clusters_seen": len(batch),
            "proposals_seen": len(batch),
            "deferred": False,
            "reason": batch_summary or "",
        }


# ─── cluster promotion (Self-Improvement Loop v2, FRD G2 runtime) ────────
# The claim that closes the loop: turn the top-ranked OPEN proposal_cluster for
# a surface into ONE typed Engineer task, then mark the cluster in_flight. The
# existing Engineer/Tester crons build + verify it; reconcile_cluster_statuses
# (helm.agents.clustering) later flips the cluster to verified or reopens it.
#
# This is the automated form of the manual cap-clamp fix: an escalated cluster
# (a freestyle-surfaced shared-code bug) lands on the 'house' surface and is
# promoted to a house code task — insight that previously had no path to ship.

PM_CLUSTER_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_type": {"type": "string", "enum": list(VALID_ENGINEER_TASK_TYPES)},
        "title": {"type": "string"},
        "rationale": {"type": "string"},
        "priority": {"type": "integer", "minimum": 1, "maximum": 5},
        "spec": {"type": "object"},
        "deferred_reason": {"type": "string"},
    },
    "required": ["task_type", "title", "rationale", "priority", "spec"],
}


def _cluster_member_views(cluster_id: int, limit: int = 8) -> list[dict]:
    """A few member proposals of a cluster — the recurring evidence the PM uses
    to synthesise one durable fix instead of a one-trade patch."""
    with conn() as c:
        rows = list(c.execute(
            "SELECT id, title, rationale, proposed_change, confidence "
            "FROM improvement_proposals WHERE cluster_id = %s "
            "ORDER BY confidence DESC NULLS LAST, created_ts DESC LIMIT %s",
            (cluster_id, limit),
        ))
    return [{"id": int(r["id"]), "title": r["title"], "rationale": r["rationale"],
             "proposed_change": r["proposed_change"], "confidence": r["confidence"]}
            for r in rows]


def _build_cluster_prompt(agent: dict, goal: GoalBrief, cluster: dict,
                          members: list[dict]) -> str:
    is_house = agent["id"] == HOUSE_COMPETITOR_ID
    valid_types = list(
        HOUSE_ENGINEER_TASK_TYPES if is_house else FREESTYLE_ENGINEER_TASK_TYPES
    ) + ["needs_human"]
    payload = {
        "now_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
        "mode": "cluster_promotion",
        "agent": agent,
        "valid_task_types_for_this_agent": valid_types,
        "goal_brief": goal.to_prompt_dict(),
        "cluster": {
            "id": cluster["id"], "theme": cluster["theme"], "layer": cluster["layer"],
            "recurrence": cluster["recurrence"], "escalated": cluster["escalated"],
            "origin_competitor_id": cluster.get("origin_competitor_id"),
            "representative": {
                "title": cluster.get("rep_title"),
                "rationale": cluster.get("rep_rationale"),
                "proposed_change": cluster.get("rep_proposed_change"),
                "category": cluster.get("rep_category"),
            },
        },
        "member_proposals": members,
        "notes": (
            "CLUSTER PROMOTION. The cluster below is the highest-priority "
            f"DISTINCT idea for this agent — {cluster['recurrence']} separate "
            "proposals restated it. Synthesise ONE typed Engineer task that "
            "ships the durable fix for the WHOLE cluster (not a single trade's "
            "patch). Use the exact spec shape for the task_type (see the system "
            "prompt). Quote the recurrence count in the rationale. "
            + ("This cluster is on the HOUSE surface — use a code task_type."
               + (" It was ESCALATED from a freestyle agent that can't touch "
                  "shared house code; you ARE the house owner — ship it."
                  if cluster["escalated"] else "")
               if is_house else
               "This is a FREESTYLE competitor — use ONLY persona_edit / "
               "strategy_config_edit / needs_human.")
            + " If you genuinely cannot map it to a valid task_type, return "
            "task_type='needs_human' with a reason in spec."
        ),
    }
    return ("Synthesise ONE typed task that ships the fix for this recurring "
            "cluster.\n\n```json\n" + json.dumps(payload, indent=2, default=str)
            + "\n```")


def run_cluster_promotion(surface: str = HOUSE_COMPETITOR_ID, *,
                          model: str | None = None, mode: str | None = None,
                          force: bool = False) -> dict:
    """Promote one cluster on `surface` to a typed Engineer task.

    `surface` is 'house' / HOUSE_COMPETITOR_ID for the house book, else a
    competitor_id. Respects the same one-change-in-flight backpressure as the
    weekly review (skippable with force). No-ops cleanly when the surface has no
    open clusters. Records an `agent_runs` row; on success creates one task and
    marks the cluster in_flight.
    """
    from helm.agents.clustering import next_cluster_for_surface, set_cluster_status

    model = model or os.environ.get("AGENT_MODEL", AGENT_MODEL_DEFAULT)
    mode = (mode or os.environ.get("LLM_MODE", "cli")).strip().lower()
    # Normalise: clusters store the literal 'house' for the house surface; the
    # task/competitor + agent descriptor use HOUSE_COMPETITOR_ID.
    cluster_surface = "house" if surface in ("house", HOUSE_COMPETITOR_ID) else surface
    agent_id = HOUSE_COMPETITOR_ID if cluster_surface == "house" else surface
    is_house = agent_id == HOUSE_COMPETITOR_ID

    def _noop(run, reason: str, **trace) -> dict:
        run.outcome = "noop"
        run.summary = f"[{agent_id}] cluster promotion — {reason}"
        run.add_trace(deferred=True, reason=reason, surface=cluster_surface, **trace)
        return {"run_id": run.run_id, "competitor_id": agent_id,
                "task_created": None, "cluster_id": None, "deferred": True,
                "reason": reason}

    with record_run("pm", "cluster_promotion", model=model, llm_mode=mode,
                    competitor_id=agent_id) as run:
        unverified = _unverified_for_agent(agent_id)
        if unverified and not force:
            return _noop(run, f"{len(unverified)} unverified change(s) in flight",
                         unverified_ids=[r["id"] for r in unverified])

        cluster = next_cluster_for_surface(cluster_surface)
        if cluster is None:
            return _noop(run, "no open clusters on this surface")

        agent = _agent_descriptor(agent_id)
        goal = build_goal_brief(competitor_id=agent_id)
        members = _cluster_member_views(int(cluster["id"]))
        run.add_trace(surface=cluster_surface, cluster_id=int(cluster["id"]),
                      theme=cluster["theme"], recurrence=cluster["recurrence"],
                      escalated=cluster["escalated"])

        user = _build_cluster_prompt(agent, goal, cluster, members)
        try:
            parsed = complete_json(SYSTEM_PROMPT, user,
                                   schema=PM_CLUSTER_TASK_SCHEMA,
                                   model=model, mode=mode,
                                   max_tokens=PM_MAX_TOKENS, temperature=PM_TEMPERATURE)
        except LLMError as exc:
            run.summary = f"LLM error: {str(exc)[:200]}"
            run.add_trace(llm_error=str(exc)[:500])
            raise

        suggestion = {"action": "create_task", **parsed}
        ok, why = _validate_suggestion(suggestion, is_house=is_house)
        if not ok:
            return _noop(run, f"invalid task synthesis: {why}",
                         cluster_id=int(cluster["id"]), raw=parsed)

        try:
            task_id = create_task(
                created_by="pm",
                title=str(parsed["title"]).strip()[:200],
                rationale=str(parsed["rationale"]).strip()[:2000],
                task_type=parsed["task_type"],
                spec=parsed["spec"],
                proposal_id=cluster.get("representative_proposal_id"),
                priority=int(parsed.get("priority", 3)),
                competitor_id=agent_id,
            )
        except ValueError as exc:
            return _noop(run, f"create_task rejected: {exc}",
                         cluster_id=int(cluster["id"]))

        set_cluster_status(int(cluster["id"]), "in_flight",
                           note=f"promoted to task {task_id}")
        run.summary = (f"[{agent_id}] promoted cluster {cluster['id']} "
                       f"(×{cluster['recurrence']}) → task {task_id}")
        run.add_trace(task_created=task_id, cluster_id=int(cluster["id"]))
        insert_audit("agents", "cluster_promoted",
                     {"run_id": run.run_id, "cluster_id": int(cluster["id"]),
                      "task_id": task_id, "surface": cluster_surface,
                      "escalated": cluster["escalated"]})
        return {"run_id": run.run_id, "competitor_id": agent_id,
                "task_created": task_id, "cluster_id": int(cluster["id"]),
                "deferred": False, "reason": ""}


__all__ = ["run_weekly_review", "run_backlog_drain", "run_cluster_promotion"]
