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
    unverified_releases,
)
from helm.config import DECIDER_MODEL_DEFAULT
from helm.data.store import conn, insert_audit
from helm.llm import LLMError, complete_json

IST = ZoneInfo("Asia/Kolkata")

PM_MAX_TOKENS = 2000
PM_TEMPERATURE = 0.2

# Closed set the engineer knows how to act on. Anything else lands as
# `needs_human` and surfaces for manual handling.
VALID_ENGINEER_TASK_TYPES = (
    "prompt_tweak",
    "param_change",
    "add_filter",
    "setting_override",
    "add_strategy_variant",
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

4. RESPECT THE ENGINEER'S CLOSED SURFACE. Valid task_types:
       prompt_tweak       — wording in the decider system prompt
       param_change       — numeric knob (cap, threshold, lookback)
       add_filter         — new gate in risk/strategy (entry guard)
       setting_override   — write a row to the `settings` table
       add_strategy_variant — new strategy class (heaviest)
       needs_human        — out-of-surface; PM punts to a human
   If the only fix is out-of-surface, file `needs_human` and stop — never
   water down into a fake `prompt_tweak`.

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
      "anchor": "<unique-substring inside the prompt to locate edit>",
      "action": "append" | "prepend" | "replace",
      "text":   "<the new wording — append/prepend add a paragraph; replace swaps the anchor>"
    }
    Example: {"file": "scripts/decide_signals.py",
              "anchor": "Default to SKIP when ambiguous",
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


def _last_successful_pm_run() -> dict | None:
    """Most recent PM run that finished with outcome='ok'. Drives the
    'anything new since then' skip check."""
    with conn() as c:
        return c.execute(
            "SELECT id, started_ts, finished_ts FROM agent_runs "
            "WHERE agent = 'pm' AND outcome = 'ok' "
            "ORDER BY started_ts DESC LIMIT 1"
        ).fetchone()


def _new_trades_since(since: datetime | None) -> int:
    sql = "SELECT COUNT(*) AS n FROM paper_trades WHERE status = 'CLOSED'"
    args: tuple = ()
    if since is not None:
        sql += " AND exit_ts > %s"
        args = (since,)
    with conn() as c:
        return int(c.execute(sql, args).fetchone()["n"])


def _new_proposals_since(since: datetime | None) -> int:
    sql = "SELECT COUNT(*) AS n FROM improvement_proposals"
    args: tuple = ()
    if since is not None:
        sql += " WHERE created_ts > %s"
        args = (since,)
    with conn() as c:
        return int(c.execute(sql, args).fetchone()["n"])


def _cluster_key(title: str) -> str:
    """Same loose dedupe the digest uses — lowercase, strip light punctuation,
    collapse whitespace. Stable across retros so a recurring idea clusters."""
    t = title.lower()
    for ch in "().,:;-—":
        t = t.replace(ch, " ")
    return " ".join(t.split())


def _open_proposals_30d() -> list[dict]:
    """Open proposals + parent-retro context, clustered by title key.

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
            ORDER BY p.created_ts DESC
            """
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


def _recent_retros_7d(limit: int = 10) -> list[dict]:
    with conn() as c:
        rows = list(c.execute(
            """
            SELECT id, kind, verdict_label, signal_quality_score,
                   decision_quality_score, summary_layman, created_ts
            FROM trade_retrospectives
            WHERE created_ts >= now() - interval '7 days'
            ORDER BY created_ts DESC
            LIMIT %s
            """,
            (limit,),
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


def _recent_engineer_runs(limit: int = 5) -> list[dict]:
    """What the engineer has tried recently — keeps the PM from re-suggesting
    the same change while a release is still in flight or just landed."""
    with conn() as c:
        rows = list(c.execute(
            """
            SELECT id, started_ts, finished_ts, task_id, release_id,
                   outcome, summary
            FROM agent_runs
            WHERE agent = 'engineer'
            ORDER BY started_ts DESC
            LIMIT %s
            """,
            (limit,),
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


def _build_user_prompt(goal: GoalBrief, proposals: list[dict],
                       retros: list[dict], engineer_runs: list[dict]) -> str:
    payload = {
        "now_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
        "goal_brief": goal.to_prompt_dict(),
        "open_proposals_last_30d": proposals,
        "recent_retros_last_7d": retros,
        "recent_engineer_runs": engineer_runs,
        "notes": (
            "recurrence = how many open proposals share the same loose title "
            "cluster. That is the single strongest signal — quote it in your "
            "rationale. Queue 0–3 tasks. Empty is fine if nothing recurs."
        ),
    }
    return (
        "Review the bot's state and pick 0–3 next changes to queue.\n\n"
        "```json\n" + json.dumps(payload, indent=2, default=str) + "\n```"
    )


# ─── apply ───────────────────────────────────────────────────────────


def _validate_suggestion(s: dict) -> tuple[bool, str]:
    """Sanity-check one LLM suggestion before we act on it. Returns
    (ok, reason). Reasons surface in the run trace so misbehaving LLMs are
    debuggable."""
    action = s.get("action")
    if action not in ("create_task", "reject_proposal"):
        return False, f"invalid action {action!r}"

    task_type = s.get("task_type")
    if task_type not in VALID_ENGINEER_TASK_TYPES:
        return False, f"invalid task_type {task_type!r}"

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


# ─── public entry point ──────────────────────────────────────────────


def run_weekly_review(*, model: str | None = None, mode: str | None = None,
                      force: bool = False) -> dict:
    """Run one PM review. Returns a summary dict; raises on internal errors.

    The returned dict always has the same keys so the CLI can render a
    uniform summary regardless of whether work happened.
    """
    model = model or os.environ.get("DECIDER_MODEL", DECIDER_MODEL_DEFAULT)
    mode = (mode or os.environ.get("LLM_MODE", "cli")).strip().lower()

    with record_run("pm", "weekly_review", model=model, llm_mode=mode) as run:
        # ── skip rules ────────────────────────────────────────────────
        unverified = unverified_releases()
        if unverified and not force:
            reason = f"{len(unverified)} unverified release(s) in flight"
            run.outcome = "noop"
            run.summary = f"deferred — {reason}"
            run.add_trace(deferred=True, reason=reason,
                          unverified_release_ids=[r["id"] for r in unverified])
            insert_audit("agents", "pm_deferred",
                         {"reason": reason,
                          "unverified": [r["id"] for r in unverified]})
            return {
                "run_id": run.run_id,
                "tasks_created": [],
                "proposals_accepted": [],
                "proposals_rejected": [],
                "deferred": True,
                "reason": reason,
            }

        last_run = _last_successful_pm_run()
        since = last_run["finished_ts"] if last_run else None
        new_trades = _new_trades_since(since)
        new_proposals = _new_proposals_since(since)
        if not force and new_trades == 0 and new_proposals == 0:
            reason = "no new closed trades and no new proposals since last PM run"
            run.outcome = "noop"
            run.summary = f"deferred — {reason}"
            run.add_trace(deferred=True, reason=reason,
                          last_run_id=last_run["id"] if last_run else None,
                          new_trades=new_trades, new_proposals=new_proposals)
            return {
                "run_id": run.run_id,
                "tasks_created": [],
                "proposals_accepted": [],
                "proposals_rejected": [],
                "deferred": True,
                "reason": reason,
            }

        # ── gather inputs ─────────────────────────────────────────────
        goal = build_goal_brief()
        proposals = _open_proposals_30d()
        retros = _recent_retros_7d()
        eng_runs = _recent_engineer_runs()

        run.add_trace(
            inputs={
                "open_proposals_count": len(proposals),
                "recent_retros_count": len(retros),
                "recent_engineer_runs_count": len(eng_runs),
                "new_trades_since_last_run": new_trades,
                "new_proposals_since_last_run": new_proposals,
            },
        )

        # ── ask the LLM ───────────────────────────────────────────────
        user = _build_user_prompt(goal, proposals, retros, eng_runs)
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
            ok, why = _validate_suggestion(s)
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
            f"tasks={len(tasks_created)} accepted={len(proposals_accepted)} "
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
                      "tasks_created": tasks_created,
                      "proposals_accepted": proposals_accepted,
                      "proposals_rejected": proposals_rejected})

        return {
            "run_id": run.run_id,
            "tasks_created": tasks_created,
            "proposals_accepted": proposals_accepted,
            "proposals_rejected": proposals_rejected,
            "deferred": False,
            "reason": deferred_reason or "",
        }


__all__ = ["run_weekly_review"]
