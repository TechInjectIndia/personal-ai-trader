"""
Shared plumbing for the PM / Engineer / Tester agents.

Every agent invocation wraps its work in `record_run(...)` so we capture
inputs, outputs, latency, and outcome to `agent_runs`. Task lifecycle moves
through `claim_task(...)` and `complete_task(...)` which both audit-write.
Release lifecycle (Engineer ships, Tester verifies/reverts) uses
`record_release(...)`, `mark_release_verified(...)`, `mark_release_reverted(...)`.

Why a shared layer:
  - One place for the lifecycle invariants (task statuses, release statuses).
  - Audit + agent_runs writes stay symmetric across agents.
  - Tests can mock the few entry points here instead of every agent.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from helm.data.store import conn, insert_audit

IST = ZoneInfo("Asia/Kolkata")
REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASES_MD = REPO_ROOT / "RELEASES.md"


# ─── agent_runs ───────────────────────────────────────────────────────

@dataclass
class RunHandle:
    """Mutable trace handed to an agent so it can record what it did."""
    run_id: int
    agent: str
    started_at: float
    trace: dict
    outcome: str = "ok"
    summary: str = ""
    task_id: int | None = None
    release_id: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    competitor_id: str | None = None

    def set(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)

    def add_trace(self, **kwargs: Any) -> None:
        self.trace.update(kwargs)


@contextlib.contextmanager
def record_run(agent: str, invocation: str, *, model: str | None = None,
               llm_mode: str | None = None,
               competitor_id: str | None = None) -> Iterator[RunHandle]:
    """Wrap an agent invocation. Inserts agent_runs row at entry and updates
    on exit. Exceptions mark outcome='error' but are re-raised so the cron
    log still shows the traceback.

    `competitor_id` scopes the run to one agent (NULL = house); defaults to
    None so existing callers stay unchanged. The handle's `competitor_id` is
    also writable mid-run via `handle.set(...)`."""
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO agent_runs (agent, invocation, model, llm_mode,
                                    competitor_id, outcome)
            VALUES (%s, %s, %s, %s, %s, 'in_progress')
            RETURNING id
            """,
            (agent, invocation, model, llm_mode, competitor_id),
        ).fetchone()
    handle = RunHandle(
        run_id=row["id"], agent=agent, started_at=time.time(), trace={},
        competitor_id=competitor_id,
    )
    try:
        yield handle
    except Exception:
        handle.outcome = "error"
        raise
    finally:
        latency_ms = int((time.time() - handle.started_at) * 1000)
        with conn() as c:
            c.execute(
                """
                UPDATE agent_runs SET
                    finished_ts = now(),
                    task_id = %s,
                    release_id = %s,
                    input_tokens = %s,
                    output_tokens = %s,
                    latency_ms = %s,
                    outcome = %s,
                    summary = %s,
                    competitor_id = %s,
                    trace = %s::jsonb
                WHERE id = %s
                """,
                (
                    handle.task_id, handle.release_id,
                    handle.input_tokens, handle.output_tokens,
                    latency_ms, handle.outcome, handle.summary[:500],
                    handle.competitor_id,
                    json.dumps(handle.trace, default=str),
                    handle.run_id,
                ),
            )


# ─── agent_tasks ──────────────────────────────────────────────────────

VALID_TASK_TYPES = {
    "prompt_tweak", "param_change", "add_filter",
    "setting_override", "add_strategy_variant",
    "bug_fix", "needs_human",
    # Freestyle-competitor config edits (data, not code — see Engineer/Tester).
    "persona_edit", "strategy_config_edit",
}


def create_task(*, created_by: str, title: str, rationale: str,
                task_type: str, spec: dict, proposal_id: int | None = None,
                parent_task_id: int | None = None,
                priority: int = 3, competitor_id: str | None = None) -> int:
    """Insert an agent_tasks row. Returns id.

    `competitor_id` scopes the task to one agent (NULL = house). Defaults to
    None so existing callers stay unchanged.
    """
    if task_type not in VALID_TASK_TYPES:
        raise ValueError(f"invalid task_type {task_type!r}")
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO agent_tasks (created_by, title, rationale, task_type,
                                     spec, proposal_id, parent_task_id, priority,
                                     competitor_id, status)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s,
                    CASE WHEN %s = 'needs_human' THEN 'needs_human' ELSE 'open' END)
            RETURNING id
            """,
            (created_by, title, rationale, task_type,
             json.dumps(spec), proposal_id, parent_task_id, priority,
             competitor_id, task_type),
        ).fetchone()
    insert_audit("agents", "task_created",
                 {"task_id": row["id"], "task_type": task_type,
                  "created_by": created_by, "title": title,
                  "competitor_id": competitor_id})
    return row["id"]


def claim_next_task(*, claimed_by: str, allow_types: set[str] | None = None,
                    ) -> dict | None:
    """Atomically pick the highest-priority open task and move it to in_progress.

    Optional `allow_types` restricts to a subset (e.g. Engineer skips
    `needs_human`). Returns None if nothing to do.
    """
    type_filter = ""
    args: list = []
    if allow_types:
        type_filter = " AND task_type = ANY(%s)"
        args.append(list(allow_types))
    sql = (
        "WITH next AS ("
        "  SELECT id FROM agent_tasks "
        "  WHERE status = 'open'" + type_filter + " "
        "  ORDER BY priority DESC, created_ts ASC "
        "  FOR UPDATE SKIP LOCKED "
        "  LIMIT 1"
        ") "
        "UPDATE agent_tasks t "
        "SET status='in_progress', claimed_by=%s, claimed_ts=now() "
        "FROM next "
        "WHERE t.id = next.id "
        "RETURNING t.*"
    )
    args.append(claimed_by)
    with conn() as c:
        row = c.execute(sql, tuple(args)).fetchone()
    if row:
        insert_audit("agents", "task_claimed",
                     {"task_id": row["id"], "claimed_by": claimed_by})
    return row


def complete_task(task_id: int, *, status: str, release_id: int | None = None,
                  error: str | None = None) -> None:
    """Move a claimed task to a terminal state."""
    if status not in {"done", "failed", "cancelled", "needs_human"}:
        raise ValueError(f"invalid terminal status {status!r}")
    with conn() as c:
        c.execute(
            """
            UPDATE agent_tasks SET status=%s, completed_ts=now(),
                                   release_id=%s, error=%s
            WHERE id=%s
            """,
            (status, release_id, error, task_id),
        )
    insert_audit("agents", "task_completed",
                 {"task_id": task_id, "status": status, "release_id": release_id})


# ─── autonomy kill-switch ─────────────────────────────────────────────

def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def is_autonomy_paused(competitor_id: str | None = None) -> bool:
    """True if the self-improvement loop is paused and agents must no-op.

    Two scopes, either of which pauses:
      * global  — settings key ``autonomy_paused`` (the documented human
        kill-switch, also tripped by the Tester after two reverts in a row).
      * per-agent — settings key ``autonomy_paused:{competitor_id}`` (tripped by
        the Tester after two freestyle config reverts for that competitor).

    Every agent entrypoint (PM / Engineer / Tester) checks this before doing
    work, so the switch — and the Tester's circuit-breaker — actually halt the
    loop. Clear with ``set_setting('autonomy_paused', 'false')`` (or delete the
    row) to resume.
    """
    from helm.data.store import get_setting
    if _truthy(get_setting("autonomy_paused")):
        return True
    if competitor_id and _truthy(get_setting(f"autonomy_paused:{competitor_id}")):
        return True
    return False


# ─── releases ─────────────────────────────────────────────────────────

def record_release(*, task_id: int, commit_sha: str, summary: str,
                   diff_stat: str | None = None, branch: str = "main") -> int:
    """Insert a deployed release row + append to RELEASES.md."""
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO releases (task_id, commit_sha, branch, summary,
                                  diff_stat, status)
            VALUES (%s, %s, %s, %s, %s, 'deployed')
            RETURNING id
            """,
            (task_id, commit_sha, branch, summary, diff_stat),
        ).fetchone()
    _append_releases_md(row["id"], commit_sha, summary, diff_stat or "")
    insert_audit("agents", "release_deployed",
                 {"release_id": row["id"], "task_id": task_id,
                  "commit_sha": commit_sha})
    return row["id"]


def mark_release_verified(release_id: int, notes: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE releases SET status='verified', verified_ts=now(), "
            "tester_notes=%s WHERE id=%s",
            (notes[:2000], release_id),
        )
    insert_audit("agents", "release_verified",
                 {"release_id": release_id, "notes": notes[:200]})


def mark_release_reverted(release_id: int, *, revert_sha: str,
                          notes: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE releases SET status='reverted', reverted_ts=now(), "
            "revert_sha=%s, tester_notes=%s WHERE id=%s",
            (revert_sha, notes[:2000], release_id),
        )
    insert_audit("agents", "release_reverted",
                 {"release_id": release_id, "revert_sha": revert_sha,
                  "notes": notes[:200]})


def unverified_releases() -> list[dict]:
    with conn() as c:
        return list(c.execute(
            "SELECT * FROM releases WHERE status='deployed' "
            "ORDER BY created_ts ASC"
        ))


def _append_releases_md(release_id: int, sha: str, summary: str,
                        diff_stat: str) -> None:
    """Append a markdown block. Creates the file on first run."""
    ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    block = (
        f"\n## release {release_id} · {sha[:10]} · {ts}\n\n"
        f"{summary}\n\n"
        + (f"```\n{diff_stat}\n```\n" if diff_stat.strip() else "")
    )
    if not RELEASES_MD.exists():
        RELEASES_MD.write_text("# Releases\n\nAutonomous improvement log.\n")
    with RELEASES_MD.open("a", encoding="utf-8") as f:
        f.write(block)


# ─── competitor config versions (freestyle "release analogue") ─────────
# Persona / strategy_config edits for freestyle competitors are DATA changes
# in Postgres, not code commits — so they get their own version table instead
# of `releases`. Same lifecycle (deployed → verified | reverted | failed), but
# rollback writes the captured old_value back rather than `git revert`. None
# of these helpers touch RELEASES.md or git.

def record_config_version(*, competitor_id: str, task_id: int | None,
                          field: str, mandate_week: Any | None,
                          old_value: Any, new_value: Any) -> int:
    """Insert a deployed competitor_config_versions row. Returns id.

    `old_value` / `new_value` are JSON-serialised as-is (a string for persona,
    an object for strategy_config). `mandate_week` is the target
    competitor_mandates.week_start for strategy_config edits, else None.
    """
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO competitor_config_versions
                (competitor_id, task_id, field, mandate_week,
                 old_value, new_value, status)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, 'deployed')
            RETURNING id
            """,
            (competitor_id, task_id, field, mandate_week,
             json.dumps(old_value), json.dumps(new_value)),
        ).fetchone()
    insert_audit("agents", "config_version_deployed",
                 {"version_id": row["id"], "competitor_id": competitor_id,
                  "field": field, "task_id": task_id})
    return row["id"]


def mark_config_version_verified(version_id: int, notes: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE competitor_config_versions SET status='verified', "
            "verified_ts=now(), tester_notes=%s WHERE id=%s",
            (notes[:2000], version_id),
        )
    insert_audit("agents", "config_version_verified",
                 {"version_id": version_id, "notes": notes[:200]})


def mark_config_version_reverted(version_id: int, notes: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE competitor_config_versions SET status='reverted', "
            "reverted_ts=now(), tester_notes=%s WHERE id=%s",
            (notes[:2000], version_id),
        )
    insert_audit("agents", "config_version_reverted",
                 {"version_id": version_id, "notes": notes[:200]})


def mark_config_version_failed(version_id: int, notes: str) -> None:
    """Used when applying the rollback itself fails — neither verified nor
    cleanly reverted."""
    with conn() as c:
        c.execute(
            "UPDATE competitor_config_versions SET status='failed', "
            "tester_notes=%s WHERE id=%s",
            (notes[:2000], version_id),
        )
    insert_audit("agents", "config_version_failed",
                 {"version_id": version_id, "notes": notes[:200]})


def unverified_config_versions(competitor_id: str | None = None) -> list[dict]:
    """Deployed-but-unverified config versions, oldest first.

    Optional `competitor_id` narrows to one agent (used by the PM ship-gate);
    default returns every agent's pending versions (used by the Tester loop).
    """
    sql = "SELECT * FROM competitor_config_versions WHERE status='deployed'"
    args: tuple = ()
    if competitor_id is not None:
        sql += " AND competitor_id = %s"
        args = (competitor_id,)
    sql += " ORDER BY created_ts ASC"
    with conn() as c:
        return list(c.execute(sql, args))


def apply_config_revert(version_id: int) -> None:
    """Write a config version's `old_value` back to the live record.

    persona          → UPDATE competitors SET persona = old_value
    strategy_config  → UPDATE competitor_mandates SET strategy_config = old_value
                       WHERE competitor_id = … AND week_start = mandate_week

    A NULL `old_value` (first-ever edit) means there's nothing to restore for a
    strategy_config edit — the row is left as-is; persona is never NULL since a
    persona always pre-exists. Raises ValueError on an unknown field.
    """
    with conn() as c:
        ver = c.execute(
            "SELECT competitor_id, field, mandate_week, old_value "
            "FROM competitor_config_versions WHERE id=%s",
            (version_id,),
        ).fetchone()
        if ver is None:
            raise ValueError(f"no config version id={version_id}")
        field = ver["field"]
        old_value = ver["old_value"]
        if field == "persona":
            c.execute(
                "UPDATE competitors SET persona=%s WHERE id=%s",
                (old_value, ver["competitor_id"]),
            )
        elif field == "strategy_config":
            if old_value is None:
                # No prior strategy_config to restore; leave the live row.
                return
            c.execute(
                "UPDATE competitor_mandates SET strategy_config=%s::jsonb "
                "WHERE competitor_id=%s AND week_start=%s",
                (json.dumps(old_value), ver["competitor_id"], ver["mandate_week"]),
            )
        else:  # pragma: no cover — field is CHECK-constrained
            raise ValueError(f"unknown config field {field!r}")


# ─── git + PM2 helpers ────────────────────────────────────────────────

def run_cmd(args: list[str], *, cwd: Path | None = None,
            timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    """Thin subprocess wrapper used by Engineer + Tester."""
    return subprocess.run(
        args, cwd=str(cwd or REPO_ROOT), capture_output=True, text=True,
        timeout=timeout, check=check,
    )


def git_head_sha() -> str:
    return run_cmd(["git", "rev-parse", "HEAD"]).stdout.strip()


def git_diff_stat(rev: str = "HEAD~1") -> str:
    try:
        return run_cmd(["git", "diff", "--stat", f"{rev}..HEAD"]).stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def git_commit_all(message: str, *, author_name: str = "helm-engineer-agent",
                   author_email: str = "agents@helm.local") -> str:
    """Stage all tracked changes and commit. Returns the new HEAD sha."""
    run_cmd(["git", "add", "-A"])
    env_args = [
        "-c", f"user.name={author_name}",
        "-c", f"user.email={author_email}",
    ]
    run_cmd(["git", *env_args, "commit", "-m", message])
    return git_head_sha()


def git_revert(sha: str, *, author_name: str = "helm-tester-agent",
               author_email: str = "agents@helm.local") -> str:
    """Revert a single commit non-interactively. Returns the revert sha."""
    env_args = [
        "-c", f"user.name={author_name}",
        "-c", f"user.email={author_email}",
    ]
    run_cmd(["git", *env_args, "revert", "--no-edit", sha])
    return git_head_sha()


def pm2_reload(app: str = "helm-dashboard") -> None:
    """Restart PM2 app — used by Engineer when config or dashboard changed."""
    run_cmd(["pm2", "restart", app], check=False)


# ─── metrics for goal-alignment briefs ────────────────────────────────

@dataclass
class GoalBrief:
    equity: Decimal
    initial: Decimal
    goal: Decimal
    progress_pct: float
    days_to_goal: int | None
    realised_pnl: Decimal
    realised_pnl_7d: Decimal
    win_rate_pct: float
    expectancy_inr: Decimal
    trades_total: int
    trades_7d: int
    signals_7d: int
    take_rate_pct_7d: float
    open_proposals: int
    open_tasks: int
    unverified_releases: int

    def to_prompt_dict(self) -> dict:
        return {
            "equity_inr": float(self.equity),
            "initial_capital_inr": float(self.initial),
            "goal_capital_inr": float(self.goal),
            "progress_pct": round(self.progress_pct, 2),
            "days_to_goal": self.days_to_goal,
            "realised_pnl_total_inr": float(self.realised_pnl),
            "realised_pnl_last_7d_inr": float(self.realised_pnl_7d),
            "win_rate_pct": round(self.win_rate_pct, 2),
            "expectancy_inr": float(self.expectancy_inr),
            "trades_total": self.trades_total,
            "trades_last_7d": self.trades_7d,
            "signals_last_7d": self.signals_7d,
            "take_rate_pct_7d": round(self.take_rate_pct_7d, 2),
            "open_improvement_proposals": self.open_proposals,
            "open_agent_tasks": self.open_tasks,
            "unverified_releases": self.unverified_releases,
        }


def build_goal_brief(*, goal_deadline_date: datetime | None = None,
                     competitor_id: str | None = None) -> GoalBrief:
    """Aggregate the snapshot the PM agent needs to make decisions.

    `competitor_id` scopes the brief to one agent's book. None or the house id
    builds the incumbent house brief (the single-pool wallet + house-filtered
    trade/decision/signal stats, same as before). A freestyle competitor id
    scopes the wallet to that competitor's isolated wallet and every
    trade/decision/signal/proposal/task/version count to that competitor's rows.
    """
    from helm.config import HOUSE_COMPETITOR_ID, HOUSE_TRADE_FILTER

    is_house = competitor_id is None or competitor_id == HOUSE_COMPETITOR_ID

    if is_house:
        from helm.wallet import wallet_state  # local: avoid circular at load
        w = wallet_state()
        # House rows are (competitor_id IS NULL OR ='house-claude'). The same
        # filter applies to signals/decisions; agent-loop tables canonicalise
        # NULL→house at write time, so house = ='house-claude' there.
        trade_filter = HOUSE_TRADE_FILTER
        signal_filter = HOUSE_TRADE_FILTER
        decision_filter = HOUSE_TRADE_FILTER
        loop_id = HOUSE_COMPETITOR_ID
        scope_args: tuple = ()
    else:
        assert competitor_id is not None  # narrowed by is_house above
        from helm.competition.wallet import competitor_wallet_state
        w = competitor_wallet_state(competitor_id)
        trade_filter = "competitor_id = %s"
        signal_filter = "competitor_id = %s"
        decision_filter = "competitor_id = %s"
        loop_id = competitor_id
        scope_args = (competitor_id,)

    now_ist = datetime.now(IST)
    seven_days_ago = now_ist - timedelta(days=7)

    with conn() as c:
        # Win/loss totals
        ts = c.execute(
            f"""
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE COALESCE(net_pnl_inr, pnl_inr) > 0) AS wins,
              COUNT(*) FILTER (WHERE COALESCE(net_pnl_inr, pnl_inr) <= 0) AS losses,
              COALESCE(AVG(COALESCE(net_pnl_inr, pnl_inr))
                       FILTER (WHERE COALESCE(net_pnl_inr, pnl_inr) > 0), 0) AS avg_win,
              COALESCE(AVG(COALESCE(net_pnl_inr, pnl_inr))
                       FILTER (WHERE COALESCE(net_pnl_inr, pnl_inr) <= 0), 0) AS avg_loss,
              COALESCE(SUM(COALESCE(net_pnl_inr, pnl_inr))
                       FILTER (WHERE exit_ts >= %s), 0) AS pnl_7d,
              COUNT(*) FILTER (WHERE exit_ts >= %s) AS trades_7d
            FROM paper_trades WHERE status='CLOSED' AND {trade_filter}
            """,  # noqa: S608 — filters are hardcoded literals, not user input
            (seven_days_ago, seven_days_ago, *scope_args),
        ).fetchone()
        s7 = c.execute(
            f"SELECT COUNT(*) AS n FROM signals "
            f"WHERE ts >= %s AND {signal_filter}",  # noqa: S608
            (seven_days_ago, *scope_args),
        ).fetchone()
        d7 = c.execute(
            f"""
            SELECT
              COUNT(*) AS n,
              COUNT(*) FILTER (WHERE verdict='TAKE') AS takes
            FROM decisions WHERE ts >= %s AND {decision_filter}
            """,  # noqa: S608
            (seven_days_ago, *scope_args),
        ).fetchone()
        op = c.execute(
            "SELECT COUNT(*) AS n FROM improvement_proposals "
            "WHERE status='open' AND competitor_id = %s",
            (loop_id,),
        ).fetchone()
        ot = c.execute(
            "SELECT COUNT(*) AS n FROM agent_tasks "
            "WHERE status IN ('open','in_progress','needs_human') "
            "AND competitor_id = %s",
            (loop_id,),
        ).fetchone()
        if is_house:
            ur = c.execute(
                "SELECT COUNT(*) AS n FROM releases WHERE status='deployed'"
            ).fetchone()
        else:
            ur = c.execute(
                "SELECT COUNT(*) AS n FROM competitor_config_versions "
                "WHERE status='deployed' AND competitor_id = %s",
                (loop_id,),
            ).fetchone()

    total = int(ts["total"])
    wins = int(ts["wins"])
    losses = int(ts["losses"])
    win_rate = (wins / total * 100) if total else 0.0
    avg_win = Decimal(ts["avg_win"])
    avg_loss = Decimal(ts["avg_loss"])
    expectancy = (Decimal(wins) * avg_win + Decimal(losses) * avg_loss) / (
        Decimal(total) if total else Decimal(1)
    )

    decisions_n = int(d7["n"])
    take_rate = (int(d7["takes"]) / decisions_n * 100) if decisions_n else 0.0

    days_to_goal: int | None = None
    if goal_deadline_date is not None:
        days_to_goal = max(0, (goal_deadline_date.date() - now_ist.date()).days)

    return GoalBrief(
        equity=w.equity, initial=w.initial, goal=w.goal,
        progress_pct=w.progress_pct, days_to_goal=days_to_goal,
        realised_pnl=w.realised_net_pnl,
        realised_pnl_7d=Decimal(ts["pnl_7d"]),
        win_rate_pct=win_rate,
        expectancy_inr=expectancy,
        trades_total=total, trades_7d=int(ts["trades_7d"]),
        signals_7d=int(s7["n"]),
        take_rate_pct_7d=take_rate,
        open_proposals=int(op["n"]),
        open_tasks=int(ot["n"]),
        unverified_releases=int(ur["n"]),
    )
