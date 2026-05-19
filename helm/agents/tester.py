"""
Tester agent — verifies every release through a deterministic stage gauntlet.

See AGENTS/Tester-Agent.md for the role charter. This module is the
implementation. The CLI entry point is `scripts/tester_run.py`.

For each release in `releases` with status='deployed' (oldest first), this
module:

  1. Runs the test stages defined in the charter in order, fail-fast.
  2. On pass: marks the release `verified`, writes a short LLM-summarised
     `tester_notes`, returns.
  3. On fail: reverts the commit, marks the release `reverted`, files a
     `bug_fix` task for the Engineer. If two releases in a row reverted,
     pauses the autonomy loop via the `settings` table.

The LLM is only used to summarise the verdict for `tester_notes`. Everything
else is deterministic so two cron firings against the same release produce
the same answer.
"""

from __future__ import annotations

import json
import subprocess
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from helm.agents.base import (
    REPO_ROOT,
    create_task,
    git_revert,
    mark_release_reverted,
    mark_release_verified,
    pm2_reload,
    record_run,
    run_cmd,
    unverified_releases,
)
from helm.data.store import conn, insert_audit, set_setting

IST = ZoneInfo("Asia/Kolkata")

# Postgres advisory lock key — distinct from retro_trades' 8472001.
TESTER_LOCK_KEY = 8472002

# Stages that must pass. Order matters: a static-gate failure short-circuits
# before we spend time on the pipeline smoke or hit the dashboard.
STAGES_ORDER = (
    "static_gates",
    "pipeline_smoke",
    "dashboard_reachability",
    "pm2_health",
    "spot_check",
    "pnl_sanity",
)

# How long to give each subprocess. ruff + pytest can be slow on a cold cache.
RUFF_TIMEOUT_S = 60
PYTEST_TIMEOUT_S = 180
CURL_TIMEOUT_S = 10
PM2_TIMEOUT_S = 15


# ─── Synthetic candles for the strategy spot-check ────────────────────

def _synth_candles() -> list[dict]:
    """30 1-min bars defining a clean ORB-15m setup with a breakout bar.

    Used by the strategy spot-check stage to invoke every registered
    strategy's `scan(...)` without depending on live DB candles. Numbers
    are picked so:

      * The opening 15 bars range 99–101 (so or_high=101, or_low=99).
      * Bars 16–29 stay below the OR high.
      * The final bar breaks out at 101.8, which makes ORB-15m emit.

    Other strategies (ORB-5m, VWAP reclaim, gap_fade) may or may not fire;
    the spot-check only asserts they don't raise.
    """
    out: list[dict] = []
    base = datetime(2026, 5, 19, 9, 15, tzinfo=IST)
    # 15 opening-range bars hugging 99–101.
    for i in range(15):
        out.append({
            "bar_ts": base + timedelta(minutes=i),
            "open": Decimal("100.00"),
            "high": Decimal("101.00"),
            "low": Decimal("99.00"),
            "close": Decimal("100.50"),
            "tick_count": 5,
        })
    # 14 quiet bars staying inside the range (so no premature breakout).
    for i in range(15, 29):
        out.append({
            "bar_ts": base + timedelta(minutes=i),
            "open": Decimal("100.20"),
            "high": Decimal("100.80"),
            "low": Decimal("99.50"),
            "close": Decimal("100.40"),
            "tick_count": 5,
        })
    # Breakout bar.
    out.append({
        "bar_ts": base + timedelta(minutes=29),
        "open": Decimal("100.80"),
        "high": Decimal("102.00"),
        "low": Decimal("100.80"),
        "close": Decimal("101.80"),
        "tick_count": 8,
    })
    return out


SAMPLE_CANDLES: list[dict] = _synth_candles()


# ─── Stage runner helpers ─────────────────────────────────────────────

@dataclass
class StageResult:
    name: str
    ok: bool
    duration_ms: int
    output_excerpt: str
    failure_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "output_excerpt": self.output_excerpt,
            "failure_reason": self.failure_reason,
        }


def _excerpt(s: str, limit: int = 1200) -> str:
    """Trim noisy subprocess output for the trace blob."""
    if s is None:
        return ""
    s = s.strip()
    if len(s) <= limit:
        return s
    # Keep tail — pytest/ruff failures land at the bottom.
    return "…[truncated]…\n" + s[-limit:]


def _run_stage(name: str, fn) -> StageResult:
    """Time a stage callable and pack its return into a StageResult."""
    t0 = _time.time()
    try:
        ok, excerpt, reason = fn()
    except Exception as exc:  # noqa: BLE001 — any uncaught exception → stage failure
        return StageResult(
            name=name, ok=False,
            duration_ms=int((_time.time() - t0) * 1000),
            output_excerpt=_excerpt(repr(exc)),
            failure_reason=f"stage {name} raised: {type(exc).__name__}: {exc}",
        )
    return StageResult(
        name=name, ok=ok,
        duration_ms=int((_time.time() - t0) * 1000),
        output_excerpt=_excerpt(excerpt),
        failure_reason=reason,
    )


# ─── Individual stage implementations ─────────────────────────────────

def _stage_static_gates() -> tuple[bool, str, str | None]:
    """ruff check + pytest -q. Both must exit 0."""
    out_lines: list[str] = []
    try:
        ruff = run_cmd(
            ["ruff", "check", "helm", "scripts"],
            timeout=RUFF_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return False, f"ruff timed out: {exc}", "ruff timed out"
    out_lines.append("$ ruff check helm scripts")
    out_lines.append(ruff.stdout)
    out_lines.append(ruff.stderr)
    if ruff.returncode != 0:
        return False, "\n".join(out_lines), f"ruff exit {ruff.returncode}"

    try:
        py = run_cmd(
            ["pytest", "-q", "tests/"],
            timeout=PYTEST_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return False, "\n".join(out_lines + [f"pytest timed out: {exc}"]), "pytest timed out"
    out_lines.append("$ pytest -q tests/")
    out_lines.append(py.stdout)
    out_lines.append(py.stderr)
    if py.returncode != 0:
        return False, "\n".join(out_lines), f"pytest exit {py.returncode}"
    return True, "\n".join(out_lines), None


def _stage_pipeline_smoke() -> tuple[bool, str, str | None]:
    """Run the dedicated integration-smoke pytest file."""
    smoke = REPO_ROOT / "tests" / "test_pipeline_smoke.py"
    if not smoke.exists():
        return False, f"missing fixture file: {smoke}", "pipeline smoke file missing"
    try:
        proc = run_cmd(
            ["pytest", "-q", "tests/test_pipeline_smoke.py"],
            timeout=PYTEST_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return False, f"pipeline smoke timed out: {exc}", "pipeline smoke timed out"
    out = f"$ pytest -q tests/test_pipeline_smoke.py\n{proc.stdout}\n{proc.stderr}"
    if proc.returncode != 0:
        return False, out, f"pipeline smoke exit {proc.returncode}"
    return True, out, None


def _stage_dashboard_reachability() -> tuple[bool, str, str | None]:
    """Curl the local Streamlit. Treat any 2xx/3xx as ok (303 is normal here)."""
    try:
        proc = run_cmd(
            [
                "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                "http://127.0.0.1:8501",
            ],
            timeout=CURL_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return False, f"curl timeout: {exc}", "dashboard curl timed out"
    code_str = (proc.stdout or "").strip()
    try:
        code = int(code_str or "0")
    except ValueError:
        code = 0
    out = f"http_code={code_str!r} stderr={proc.stderr!r}"
    # Streamlit may 303 back to itself behind nginx, so accept anything < 400.
    if 200 <= code < 400:
        return True, out, None
    return False, out, f"dashboard returned http_code={code_str!r}"


def _stage_pm2_health() -> tuple[bool, str, str | None]:
    """`pm2 jlist` must show helm-dashboard online."""
    try:
        proc = run_cmd(["pm2", "jlist"], timeout=PM2_TIMEOUT_S, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return False, f"pm2 jlist failed: {exc}", "pm2 not reachable"
    if proc.returncode != 0:
        return False, f"pm2 exit {proc.returncode}\n{proc.stderr}", "pm2 jlist nonzero"
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return False, f"pm2 jlist non-JSON: {proc.stdout[:300]}", f"pm2 jlist JSON parse: {exc}"
    dash = None
    for entry in data:
        if entry.get("name") == "helm-dashboard":
            dash = entry
            break
    if dash is None:
        return False, f"no helm-dashboard entry in pm2 jlist (saw {[d.get('name') for d in data]})", \
               "helm-dashboard missing from pm2"
    status = (dash.get("pm2_env") or {}).get("status")
    restarts = (dash.get("pm2_env") or {}).get("restart_time", 0)
    excerpt = f"status={status} restarts={restarts}"
    if status != "online":
        return False, excerpt, f"helm-dashboard pm2 status={status!r}"
    return True, excerpt, None


def _stage_spot_check() -> tuple[bool, str, str | None]:
    """Every registered strategy must scan('TCS', SAMPLE_CANDLES) without raising."""
    # Local import — keeps tester importable even if strategies subtree is broken
    # at module-load (in which case the static_gates stage already caught it).
    from helm.strategies import ACTIVE
    from helm.strategies.base import Signal

    lines: list[str] = []
    for strat in ACTIVE:
        try:
            sig = strat.scan("TCS", SAMPLE_CANDLES)
        except Exception as exc:  # noqa: BLE001 — strategy raising is the failure
            return False, "\n".join(lines + [f"{strat.name}: RAISED {exc!r}"]), \
                   f"strategy {strat.name} raised on synthetic candles"
        if sig is not None and not isinstance(sig, Signal):
            return False, "\n".join(lines + [f"{strat.name}: bad return {type(sig)}"]), \
                   f"strategy {strat.name} returned non-Signal"
        lines.append(f"{strat.name}: {'fired' if sig else 'silent'}")
    return True, "\n".join(lines), None


def _stage_pnl_sanity(release: dict) -> tuple[bool, str, str | None]:
    """Skipped unless ≥24h elapsed since release.created_ts."""
    created = release["created_ts"]
    # `created_ts` is timestamptz; compare in UTC.
    now = datetime.now(timezone.utc)
    age = now - created
    if age < timedelta(hours=24):
        return True, f"skipped: release age {age} < 24h", None
    cutoff = now - timedelta(hours=24)
    with conn() as c:
        row = c.execute(
            """
            SELECT COALESCE(SUM(COALESCE(net_pnl_inr, pnl_inr)), 0) AS pnl
            FROM paper_trades
            WHERE status='CLOSED' AND exit_ts >= %s
            """,
            (cutoff,),
        ).fetchone()
    pnl = Decimal(row["pnl"])
    excerpt = f"24h realised pnl = ₹{pnl}"
    if pnl < Decimal("-500"):
        return False, excerpt, f"24h realised pnl {pnl} < -500"
    return True, excerpt, None


# ─── Public API ───────────────────────────────────────────────────────

def verify_release(release_id: int) -> dict:
    """Run every test stage for one release. Returns a result dict.

    Shape::

        {
          "passed": bool,
          "stages": {name -> {ok, duration_ms, output_excerpt, failure_reason}},
          "failure_reason": str | None,
        }

    Does NOT mark the release verified/reverted on its own — that's the
    `process_unverified()` orchestrator's job, so verify_release stays a pure
    function that's easy to call ad-hoc.
    """
    with conn() as c:
        rel = c.execute(
            "SELECT * FROM releases WHERE id = %s", (release_id,),
        ).fetchone()
    if rel is None:
        return {
            "passed": False,
            "stages": {},
            "failure_reason": f"no release id={release_id}",
        }

    stages: dict[str, dict] = {}
    failure_reason: str | None = None
    passed = True

    # Stages are fail-fast: first failure stops further checks. The remaining
    # stages are recorded as skipped so the dashboard grid still has the slot.
    for stage_name in STAGES_ORDER:
        if not passed:
            stages[stage_name] = {
                "ok": False, "duration_ms": 0,
                "output_excerpt": "(skipped — earlier stage failed)",
                "failure_reason": "skipped",
            }
            continue
        if stage_name == "static_gates":
            r = _run_stage(stage_name, _stage_static_gates)
        elif stage_name == "pipeline_smoke":
            r = _run_stage(stage_name, _stage_pipeline_smoke)
        elif stage_name == "dashboard_reachability":
            r = _run_stage(stage_name, _stage_dashboard_reachability)
        elif stage_name == "pm2_health":
            r = _run_stage(stage_name, _stage_pm2_health)
        elif stage_name == "spot_check":
            r = _run_stage(stage_name, _stage_spot_check)
        elif stage_name == "pnl_sanity":
            r = _run_stage(stage_name, lambda rel=rel: _stage_pnl_sanity(rel))
        else:  # unreachable — STAGES_ORDER is closed
            continue
        stages[stage_name] = r.to_dict()
        if not r.ok:
            passed = False
            failure_reason = r.failure_reason or f"stage {stage_name} failed"

    return {"passed": passed, "stages": stages, "failure_reason": failure_reason}


def _summarise_notes(rel: dict, result: dict) -> str:
    """One short paragraph summarising the verdict, used for tester_notes.

    Tries the LLM first (small complete_json call). On any failure falls
    back to a deterministic string. Charter: "the LLM is only used to
    summarise the verdict — never on the happy path of the decision itself."
    """
    stage_pieces = []
    for name in STAGES_ORDER:
        s = result["stages"].get(name)
        if not s:
            continue
        flag = "ok" if s["ok"] else "FAIL"
        stage_pieces.append(f"{name}={flag} ({s['duration_ms']}ms)")
    stage_line = "; ".join(stage_pieces)
    fallback = (
        f"release {rel['id']} {('verified' if result['passed'] else 'reverted')}: "
        f"{stage_line}"
    )
    if result["failure_reason"]:
        fallback += f". failure: {result['failure_reason']}"

    # LLM summary is opportunistic — we don't want to block the loop if Claude
    # is down. The charter explicitly allows the deterministic fallback.
    try:
        from helm.llm import complete_json
        schema = {
            "type": "object",
            "properties": {"notes": {"type": "string"}},
            "required": ["notes"],
        }
        system = (
            "You are the Tester agent's note-writer for an intraday trading bot's "
            "autonomous improvement loop. Summarise the verification result in one "
            "short factual sentence, max 200 chars. Do not editorialise. No markdown."
        )
        user = json.dumps({
            "release_id": rel["id"],
            "commit_sha": rel["commit_sha"],
            "passed": result["passed"],
            "failure_reason": result["failure_reason"],
            "stage_summary": stage_pieces,
        })
        out = complete_json(system, user, schema=schema, max_tokens=200, temperature=0.0)
        notes = (out.get("notes") or "").strip()
        if not notes:
            return fallback
        return f"{notes} [{stage_line}]"[:2000]
    except Exception:  # noqa: BLE001 — LLM is best-effort here
        return fallback[:2000]


def _previous_release_reverted(current_release_id: int) -> bool:
    """True if the most recent release BEFORE current is `reverted`.

    Used for the "two reverts in a row" autonomy-pause guard.
    """
    with conn() as c:
        row = c.execute(
            """
            SELECT status FROM releases
            WHERE id < %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (current_release_id,),
        ).fetchone()
    return bool(row and row["status"] == "reverted")


def _mark_release_failed(release_id: int, notes: str) -> None:
    """Used when `git revert` itself fails — neither verified nor reverted."""
    with conn() as c:
        c.execute(
            "UPDATE releases SET status='failed', tester_notes=%s WHERE id=%s",
            (notes[:2000], release_id),
        )
    insert_audit("agents", "release_failed",
                 {"release_id": release_id, "notes": notes[:200]})


# Files where a regression should trigger a PM2 reload after revert. Keep this
# narrow: process-level files only (anything imported by the running dashboard).
PM2_RELOAD_PATHS = (
    "helm/dashboard/",
    "helm/config.py",
    "helm/agents/base.py",
)


def _release_touched_pm2_paths(rel: dict) -> bool:
    """Inspect the release's diff_stat blob for paths that need a PM2 reload.

    We avoid re-running `git show` here: the diff_stat lines look like
    `helm/foo.py | 12 +-` so a substring match is sufficient.
    """
    diff_stat = (rel.get("diff_stat") or "")
    return any(p in diff_stat for p in PM2_RELOAD_PATHS)


def _file_bug_task(rel: dict, result: dict) -> int:
    """Create the bug_fix task that points the Engineer at the regression."""
    failing_stage = next(
        (name for name in STAGES_ORDER
         if not result["stages"].get(name, {}).get("ok", True)),
        "unknown",
    )
    error_excerpt = (
        result["stages"].get(failing_stage, {}).get("output_excerpt", "")
    )
    # We can't always identify a single target file (e.g. dashboard 503 → no
    # file). Best-effort: pull the first non-blank path out of diff_stat.
    target_file: str | None = None
    for line in (rel.get("diff_stat") or "").splitlines():
        line = line.strip()
        if "|" in line and not line.startswith("---"):
            target_file = line.split("|", 1)[0].strip()
            if target_file:
                break

    spec = {
        "target_file": target_file,
        "failing_stage": failing_stage,
        "error_excerpt": error_excerpt[:1500],
        "reverted_commit_sha": rel["commit_sha"],
        "release_id": rel["id"],
    }
    return create_task(
        created_by="tester",
        task_type="bug_fix",
        priority=5,
        parent_task_id=rel["task_id"],
        title=f"fix regression in release {rel['id']}",
        rationale=(result["failure_reason"] or "tester reverted release")[:500],
        spec=spec,
    )


def process_unverified() -> int:
    """Iterate every deployed-but-unverified release, oldest first.

    Returns the number of releases processed. A release counts as
    "processed" once it has moved out of `deployed` (either to `verified`,
    `reverted`, or `failed`).

    Wrapped in a Postgres advisory lock so two cron firings can't double-up
    on the same release.
    """
    processed = 0
    with conn() as lock_conn:
        got = lock_conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS ok", (TESTER_LOCK_KEY,),
        ).fetchone()["ok"]
        if not got:
            insert_audit("tester", "skipped_locked",
                         {"reason": "advisory_lock_held"})
            return 0

        try:
            for rel in unverified_releases():
                # Each release gets its own agent_runs row so the dashboard
                # can render a per-release stage grid.
                with record_run("tester", "verify_release") as run:
                    run.set(release_id=rel["id"])
                    result = verify_release(rel["id"])
                    run.add_trace(
                        release_id=rel["id"],
                        commit_sha=rel["commit_sha"],
                        stages=result["stages"],
                        passed=result["passed"],
                        failure_reason=result["failure_reason"],
                    )

                    if result["passed"]:
                        notes = _summarise_notes(rel, result)
                        mark_release_verified(rel["id"], notes=notes)
                        run.set(outcome="ok",
                                summary=f"release {rel['id']} verified")
                        processed += 1
                        continue

                    # ── Failure path ──
                    notes = _summarise_notes(rel, result)
                    try:
                        revert_sha = git_revert(rel["commit_sha"])
                    except subprocess.CalledProcessError as exc:
                        # Revert itself failed (e.g. merge conflict). This is
                        # the "needs_human" escalation in the charter.
                        err_blob = (
                            f"git revert {rel['commit_sha']} failed: "
                            f"exit={exc.returncode}\n"
                            f"stdout={(exc.stdout or '')[:500]}\n"
                            f"stderr={(exc.stderr or '')[:500]}"
                        )
                        _mark_release_failed(rel["id"], notes=err_blob)
                        create_task(
                            created_by="tester",
                            task_type="needs_human",
                            priority=5,
                            parent_task_id=rel["task_id"],
                            title=f"revert failed for release {rel['id']}",
                            rationale=err_blob[:500],
                            spec={
                                "release_id": rel["id"],
                                "commit_sha": rel["commit_sha"],
                                "failure_reason": result["failure_reason"],
                                "git_output": err_blob[:1500],
                            },
                        )
                        run.set(outcome="error",
                                summary=f"release {rel['id']} revert FAILED")
                        processed += 1
                        # Don't continue processing further releases — main
                        # is now in an unknown state.
                        return processed

                    # Revert succeeded.
                    mark_release_reverted(rel["id"], revert_sha=revert_sha,
                                          notes=notes)
                    bug_task_id = _file_bug_task(rel, result)
                    run.set(outcome="error",
                            summary=(f"release {rel['id']} reverted "
                                     f"(bug_fix task {bug_task_id})"))
                    run.add_trace(revert_sha=revert_sha,
                                  bug_fix_task_id=bug_task_id)

                    # PM2 reload if the reverted files were process-level.
                    if _release_touched_pm2_paths(rel):
                        pm2_reload()
                        insert_audit("tester", "pm2_reload_after_revert",
                                     {"release_id": rel["id"]})

                    processed += 1

                    # Two reverts in a row → pause the autonomy loop.
                    if _previous_release_reverted(rel["id"]):
                        set_setting("autonomy_paused", "true", actor="tester")
                        insert_audit(
                            "tester", "autonomy_paused",
                            {"trigger": "two_reverts_in_a_row",
                             "current_release_id": rel["id"]},
                        )
                        # Stop early — don't keep churning through deployed
                        # releases while the loop is paused.
                        return processed
        finally:
            # Lock auto-releases when the connection closes, but be explicit
            # in case anyone refactors `conn()` to pool.
            try:
                lock_conn.execute(
                    "SELECT pg_advisory_unlock(%s)", (TESTER_LOCK_KEY,),
                )
            except Exception:  # noqa: BLE001 — best-effort
                pass

    return processed


__all__ = [
    "REPO_ROOT",
    "SAMPLE_CANDLES",
    "STAGES_ORDER",
    "TESTER_LOCK_KEY",
    "process_unverified",
    "verify_release",
]
