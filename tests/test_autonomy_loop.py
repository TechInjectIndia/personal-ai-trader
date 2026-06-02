"""
End-to-end integration smoke for the PM → Engineer → Tester loop.

This test wires the three agents together against the real local Postgres
schema, but with the LLM mocked and the file system pointed at a tmp
worktree so we never commit to the user's actual repo.

What it proves:
  1. PM creates a task from a synthetic improvement_proposal.
  2. Engineer claims that task, applies a `setting_override` mutator (the
     cheapest path with no file diff), records a release.
  3. Tester verifies the release (using a deliberately easy stage harness
     where we monkeypatch the heavy stages — we are testing the orchestration
     here, not the underlying pytest/git stages, which are covered by the
     per-agent tests already).
  4. The full lifecycle leaves every queue row in the expected terminal state.

We deliberately keep the LLM mocked. If you want to see the agents call real
Claude end-to-end, run `python scripts/pm_review.py --force` and let the cron
do its thing.
"""

from __future__ import annotations

import pytest

from helm.agents import base
from helm.agents import pm as pm_mod
from helm.agents import engineer as eng_mod
from helm.agents import tester as test_mod
from helm.data.store import conn


# ─── teardown ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_synthetic_rows():
    """Tag every row we create so the cleanup pass at the end is precise.

    The loop touches: improvement_proposals (we need a parent retro and
    decision too), agent_tasks, releases, agent_runs, settings.
    """
    sentinel = "AUTONOMY-SMOKE"
    # No setup work; teardown only.
    yield sentinel

    with conn() as c:
        c.execute("DELETE FROM agent_runs WHERE summary LIKE %s OR invocation LIKE %s",
                  (f"%{sentinel}%", f"%{sentinel}%"))
        # agent_tasks → releases via FK is ON DELETE SET NULL on agent_tasks,
        # but releases.task_id is ON DELETE CASCADE, so deleting tasks first
        # removes their releases. We delete by title prefix for safety.
        c.execute("DELETE FROM agent_tasks WHERE title LIKE %s",
                  (f"%{sentinel}%",))
        c.execute("DELETE FROM improvement_proposals WHERE title LIKE %s",
                  (f"%{sentinel}%",))
        c.execute("DELETE FROM trade_retrospectives WHERE summary_layman LIKE %s",
                  (f"%{sentinel}%",))
        # Drop the synthetic decision + signal too
        c.execute(
            "DELETE FROM decisions WHERE reasoning LIKE %s",
            (f"%{sentinel}%",),
        )
        c.execute(
            "DELETE FROM signals WHERE rationale LIKE %s",
            (f"%{sentinel}%",),
        )
        c.execute(
            "DELETE FROM settings WHERE key = %s",
            (f"smoke_key_{sentinel.lower()}",),
        )


def _seed_synthetic_proposal(sentinel: str) -> int:
    """Insert a retro + a single improvement_proposal we can have PM pick up."""
    with conn() as c:
        # Need a decision + signal for the retro FK
        sig = c.execute(
            """
            INSERT INTO signals (strategy, symbol, side, entry_price,
                                 stop_loss, target, rationale)
            VALUES ('orb_15m', 'ZZZSMOKE', 'BUY', 100, 95, 105, %s)
            RETURNING id
            """,
            (f"{sentinel} synthetic signal",),
        ).fetchone()
        dec = c.execute(
            """
            INSERT INTO decisions (signal_id, actor, verdict, qty,
                                   final_entry, final_stop, final_target,
                                   reasoning)
            VALUES (%s, 'test', 'TAKE', 1, 100, 95, 105, %s)
            RETURNING id
            """,
            (sig["id"], f"{sentinel} synthetic decision"),
        ).fetchone()
        retro = c.execute(
            """
            INSERT INTO trade_retrospectives
                (kind, decision_id, model, llm_mode, verdict_label,
                 signal_quality_score, decision_quality_score,
                 execution_quality_score, tags,
                 summary_layman, why_we_acted, what_happened,
                 verdict_reasoning, learnings, competitor_id)
            VALUES ('SKIP', %s, 'mock', 'mock', 'BAD_CALL',
                    3, 3, 3, '[]'::jsonb,
                    %s, 'why', 'what', 'verdict-reasoning',
                    '[]'::jsonb, 'house-claude')
            RETURNING id
            """,
            (dec["id"], f"{sentinel} synthetic retro"),
        ).fetchone()
        prop = c.execute(
            """
            INSERT INTO improvement_proposals
                (retro_id, category, title, rationale, proposed_change,
                 evidence, confidence, status, competitor_id)
            VALUES (%s, 'meta',
                    %s,
                    'Synthetic smoke test — raise smoke_key value.',
                    'Set smoke_key_autonomy-smoke=100',
                    '{"smoke": true}'::jsonb, 4, 'open', 'house-claude')
            RETURNING id
            """,
            (retro["id"], f"{sentinel} smoke proposal — bump smoke key"),
        ).fetchone()
    return prop["id"]


# ─── the test ──────────────────────────────────────────────────────────

def test_pm_engineer_tester_round_trip(monkeypatch, _isolate_synthetic_rows):
    sentinel = _isolate_synthetic_rows
    proposal_id = _seed_synthetic_proposal(sentinel)
    smoke_key = f"smoke_key_{sentinel.lower()}"

    # ─── PM stage: stub the LLM so it picks our proposal ────────────────
    def fake_pm_llm(system, user, *, schema=None, model=None, mode=None,
                    max_tokens=None, temperature=None):
        return {
            "suggestions": [{
                "action": "create_task",
                "proposal_id": proposal_id,
                "task_type": "setting_override",
                "title": f"{sentinel} ship: bump smoke key",
                "rationale": f"{sentinel} PM picked recurring theme",
                "priority": 3,
                "spec": {"key": smoke_key, "value": 100,
                         "note": "smoke-test override"},
                "proposal_status_change": "accepted",
                "status_note": "queued by PM smoke test",
            }],
            "deferred": False,
            "reason": "smoke test",
        }

    # PM imports complete_json via helm.llm; patch at both call sites to be safe.
    monkeypatch.setattr("helm.llm.complete_json", fake_pm_llm, raising=True)
    monkeypatch.setattr(pm_mod, "complete_json", fake_pm_llm, raising=False)

    pm_result = pm_mod.run_weekly_review(force=True, competitor_id="house-claude")
    assert pm_result["tasks_created"], f"PM did not create a task: {pm_result}"
    task_id = pm_result["tasks_created"][0]

    # Verify proposal flipped to accepted
    with conn() as c:
        prop = c.execute("SELECT status FROM improvement_proposals WHERE id=%s",
                          (proposal_id,)).fetchone()
    assert prop["status"] == "accepted"

    # ─── Engineer stage: stub git + run_cmd so we don't touch the real repo ─
    fake_sha = "deadbeefcafe"

    def fake_run_cmd(args, *, cwd=None, timeout=120, check=True):
        # ruff / pytest / git: always succeed
        class _R:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return _R()

    monkeypatch.setattr(base, "run_cmd", fake_run_cmd, raising=True)
    monkeypatch.setattr(eng_mod, "run_cmd", fake_run_cmd, raising=False)
    monkeypatch.setattr(base, "git_head_sha", lambda: fake_sha, raising=True)
    monkeypatch.setattr(eng_mod, "git_head_sha", lambda: fake_sha, raising=False)
    monkeypatch.setattr(base, "git_diff_stat", lambda rev="HEAD~1": "",
                        raising=True)
    monkeypatch.setattr(eng_mod, "git_diff_stat", lambda rev="HEAD~1": "",
                        raising=False)
    monkeypatch.setattr(base, "git_commit_all",
                        lambda message, **kw: fake_sha, raising=True)
    monkeypatch.setattr(eng_mod, "git_commit_all",
                        lambda message, **kw: fake_sha, raising=False)
    monkeypatch.setattr(base, "pm2_reload", lambda app="helm-dashboard": None,
                        raising=True)
    monkeypatch.setattr(eng_mod, "pm2_reload",
                        lambda app="helm-dashboard": None, raising=False)

    # Neutralise live-DB backpressure: process_one_task() bails to None when the
    # real queue has ≥2 unverified releases/config versions. This test owns its
    # own seeded task, so pin both to empty to stay hermetic.
    monkeypatch.setattr(eng_mod, "unverified_releases", lambda: [], raising=True)
    monkeypatch.setattr(eng_mod, "unverified_config_versions",
                        lambda *a, **kw: [], raising=True)
    # record_release() appends to the real RELEASES.md as a side effect; stub
    # that out so the test never mutates the live release log on disk.
    monkeypatch.setattr(base, "_append_releases_md", lambda *a, **kw: None,
                        raising=True)

    eng_result = eng_mod.process_one_task(only_task_id=task_id)
    assert eng_result is not None, "Engineer found no task to claim"
    assert eng_result.get("ok") is True, f"Engineer task did not complete: {eng_result}"
    release_id = eng_result["release_id"]
    assert release_id is not None

    # Verify settings row was written
    with conn() as c:
        s = c.execute("SELECT value FROM settings WHERE key=%s",
                      (smoke_key,)).fetchone()
    assert s is not None, "setting_override mutator did not write settings row"

    # Verify task moved to done
    with conn() as c:
        t = c.execute("SELECT status, release_id FROM agent_tasks WHERE id=%s",
                      (task_id,)).fetchone()
    assert t["status"] == "done"
    assert t["release_id"] == release_id

    # ─── Tester stage: stub each verify_release stage to pass ─────────────
    def fake_verify(release_id_arg):
        return {
            "passed": True,
            "stages": {
                "static_gates": {"ok": True, "duration_ms": 1, "output_excerpt": ""},
                "pipeline_smoke": {"ok": True, "duration_ms": 1, "output_excerpt": ""},
                "dashboard_reachability": {"ok": True, "duration_ms": 1,
                                            "output_excerpt": "200"},
                "pm2_health": {"ok": True, "duration_ms": 1,
                                "output_excerpt": "online"},
                "spot_check": {"ok": True, "duration_ms": 1,
                                "output_excerpt": "4 strategies"},
                "pnl_sanity": {"ok": True, "duration_ms": 1,
                                "output_excerpt": "skipped (release < 24h old)"},
            },
            "failure_reason": None,
        }

    monkeypatch.setattr(test_mod, "verify_release", fake_verify, raising=True)

    # Also stub _summarise_notes to be deterministic so the test is hermetic
    monkeypatch.setattr(test_mod, "_summarise_notes",
                        lambda *args, **kwargs: f"{sentinel} all stages passed",
                        raising=False)

    processed = test_mod.process_unverified()
    assert processed >= 1, f"Tester did not process release: processed={processed}"

    # Verify release reached terminal 'verified' state
    with conn() as c:
        r = c.execute("SELECT status, tester_notes FROM releases WHERE id=%s",
                      (release_id,)).fetchone()
    assert r["status"] == "verified", f"release not verified: {r}"
    assert sentinel in (r["tester_notes"] or ""), "tester notes missing sentinel"

    # ─── End-state sanity: agent_runs we wrote have ok outcomes ─────────
    # Scope to the rows linked to OUR task/release — unrelated parallel runs
    # in the live DB must not affect this assertion.
    with conn() as c:
        runs = list(c.execute(
            "SELECT agent, outcome FROM agent_runs "
            "WHERE (task_id = %s OR release_id = %s) "
            "ORDER BY started_ts",
            (task_id, release_id),
        ))
    agents_seen = {r["agent"] for r in runs}
    # PM run won't have task_id set (PM creates tasks, doesn't process them);
    # so we accept its presence indirectly via task creation above.
    assert "engineer" in agents_seen, f"no engineer run recorded: {runs}"
    assert "tester" in agents_seen, f"no tester run recorded: {runs}"
    assert all(r["outcome"] in ("ok", "noop") for r in runs), \
        f"unexpected outcome in test-scoped runs: {runs}"
