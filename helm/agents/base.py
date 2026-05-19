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

    def set(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)

    def add_trace(self, **kwargs: Any) -> None:
        self.trace.update(kwargs)


@contextlib.contextmanager
def record_run(agent: str, invocation: str, *, model: str | None = None,
               llm_mode: str | None = None) -> Iterator[RunHandle]:
    """Wrap an agent invocation. Inserts agent_runs row at entry and updates
    on exit. Exceptions mark outcome='error' but are re-raised so the cron
    log still shows the traceback."""
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO agent_runs (agent, invocation, model, llm_mode, outcome)
            VALUES (%s, %s, %s, %s, 'in_progress')
            RETURNING id
            """,
            (agent, invocation, model, llm_mode),
        ).fetchone()
    handle = RunHandle(
        run_id=row["id"], agent=agent, started_at=time.time(), trace={},
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
                    trace = %s::jsonb
                WHERE id = %s
                """,
                (
                    handle.task_id, handle.release_id,
                    handle.input_tokens, handle.output_tokens,
                    latency_ms, handle.outcome, handle.summary[:500],
                    json.dumps(handle.trace, default=str),
                    handle.run_id,
                ),
            )


# ─── agent_tasks ──────────────────────────────────────────────────────

VALID_TASK_TYPES = {
    "prompt_tweak", "param_change", "add_filter",
    "setting_override", "add_strategy_variant",
    "bug_fix", "needs_human",
}


def create_task(*, created_by: str, title: str, rationale: str,
                task_type: str, spec: dict, proposal_id: int | None = None,
                parent_task_id: int | None = None,
                priority: int = 3) -> int:
    """Insert an agent_tasks row. Returns id."""
    if task_type not in VALID_TASK_TYPES:
        raise ValueError(f"invalid task_type {task_type!r}")
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO agent_tasks (created_by, title, rationale, task_type,
                                     spec, proposal_id, parent_task_id, priority,
                                     status)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s,
                    CASE WHEN %s = 'needs_human' THEN 'needs_human' ELSE 'open' END)
            RETURNING id
            """,
            (created_by, title, rationale, task_type,
             json.dumps(spec), proposal_id, parent_task_id, priority,
             task_type),
        ).fetchone()
    insert_audit("agents", "task_created",
                 {"task_id": row["id"], "task_type": task_type,
                  "created_by": created_by, "title": title})
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
                     ) -> GoalBrief:
    """Aggregate the snapshot the PM agent needs to make decisions."""
    from helm.wallet import wallet_state  # local: avoid circular at module load
    w = wallet_state()
    now_ist = datetime.now(IST)
    seven_days_ago = now_ist - timedelta(days=7)

    with conn() as c:
        # Win/loss totals
        ts = c.execute(
            """
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
            FROM paper_trades WHERE status='CLOSED'
            """,
            (seven_days_ago, seven_days_ago),
        ).fetchone()
        s7 = c.execute(
            "SELECT COUNT(*) AS n FROM signals WHERE ts >= %s",
            (seven_days_ago,),
        ).fetchone()
        d7 = c.execute(
            """
            SELECT
              COUNT(*) AS n,
              COUNT(*) FILTER (WHERE verdict='TAKE') AS takes
            FROM decisions WHERE ts >= %s
            """,
            (seven_days_ago,),
        ).fetchone()
        op = c.execute(
            "SELECT COUNT(*) AS n FROM improvement_proposals "
            "WHERE status='open'"
        ).fetchone()
        ot = c.execute(
            "SELECT COUNT(*) AS n FROM agent_tasks "
            "WHERE status IN ('open','in_progress','needs_human')"
        ).fetchone()
        ur = c.execute(
            "SELECT COUNT(*) AS n FROM releases WHERE status='deployed'"
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
