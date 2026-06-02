"""
Tests for the per-agent (house + freestyle) self-improvement loop.

These exercise the freestyle data-edit path that mirrors the house code path:

  (a) a freestyle persona_edit task flows Engineer → competitor_config_versions
      (no git commit, no releases row) and the task lands `done`;
  (b) verify_config_version passes a well-formed persona and fails + reverts a
      malformed one, restoring the captured old_value;
  (c) the retro layer stamps competitor_id on the retro + its proposals;
  (d) PM per-agent scoping creates a task tagged with the right competitor_id.

We hit the real local Postgres `helm` DB (autocommit). Every test seeds its own
isolated competitor (`zzz-freestyle-test`) with a wallet + current-week mandate
and tears it down afterwards, so we never touch the live league rows. The LLM
and the competitor backend are always monkeypatched — no real call ever fires.
"""

from __future__ import annotations

import pytest

from helm.agents import base
from helm.agents import engineer as eng_mod
from helm.agents import pm as pm_mod
from helm.agents import tester as test_mod
from helm.competition.competitors import week_start
from helm.data.store import conn, init_schema

TEST_CID = "zzz-freestyle-test"
ORIG_PERSONA = (
    "Test persona: a disciplined intraday momentum trader that buys clean "
    "morning breakouts on the strongest large caps and cuts losers fast."
)
ORIG_STRATEGY_CONFIG = {"max_positions": 2, "stop_atr_mult": 1.5}


@pytest.fixture(scope="session", autouse=True)
def _ensure_schema() -> None:
    init_schema()


@pytest.fixture
def freestyle_competitor():
    """Seed an isolated freestyle competitor + wallet + this-week mandate.

    Yields the competitor id; tears down every row it (and the loop) created in
    reverse-FK order so the live league is untouched.
    """
    wk = week_start()
    with conn() as c:
        c.execute(
            """
            INSERT INTO competitors (id, name, backend, model, persona,
                                     autonomy_level, status)
            VALUES (%s, 'ZZZ Freestyle Test', 'claude', NULL, %s,
                    'freestyle', 'active')
            ON CONFLICT (id) DO UPDATE SET persona = EXCLUDED.persona,
                                           status = 'active'
            """,
            (TEST_CID, ORIG_PERSONA),
        )
        c.execute(
            """
            INSERT INTO competitor_wallets
                (competitor_id, initial_capital_inr, available_inr, realized_pnl_inr)
            VALUES (%s, 50000, 50000, 0)
            ON CONFLICT (competitor_id) DO NOTHING
            """,
            (TEST_CID,),
        )
        c.execute(
            """
            INSERT INTO competitor_mandates
                (competitor_id, week_start, universe, strategy_config, rationale)
            VALUES (%s, %s, '["RELIANCE","TCS"]'::jsonb, %s::jsonb, 'test')
            ON CONFLICT (competitor_id, week_start)
            DO UPDATE SET strategy_config = EXCLUDED.strategy_config
            """,
            (TEST_CID, wk, _json(ORIG_STRATEGY_CONFIG)),
        )

    yield TEST_CID

    with conn() as c:
        # FK order: agent_runs / config_versions / agent_tasks reference each
        # other; null the cross-refs by deleting children first.
        c.execute("DELETE FROM agent_runs WHERE competitor_id = %s", (TEST_CID,))
        c.execute("DELETE FROM competitor_config_versions WHERE competitor_id = %s",
                  (TEST_CID,))
        c.execute("DELETE FROM agent_tasks WHERE competitor_id = %s", (TEST_CID,))
        c.execute("DELETE FROM improvement_proposals WHERE competitor_id = %s",
                  (TEST_CID,))
        c.execute("DELETE FROM trade_retrospectives WHERE competitor_id = %s",
                  (TEST_CID,))
        c.execute("DELETE FROM competitor_mandates WHERE competitor_id = %s",
                  (TEST_CID,))
        c.execute("DELETE FROM competitor_wallets WHERE competitor_id = %s",
                  (TEST_CID,))
        c.execute("DELETE FROM settings WHERE key = %s",
                  (f"autonomy_paused:{TEST_CID}",))
        c.execute("DELETE FROM competitors WHERE id = %s", (TEST_CID,))


def _json(obj) -> str:
    import json
    return json.dumps(obj)


def _live_persona(cid: str) -> str | None:
    with conn() as c:
        row = c.execute("SELECT persona FROM competitors WHERE id = %s",
                        (cid,)).fetchone()
    return row["persona"] if row else None


# ─── (a) Engineer: persona_edit → config_version, no git/no release ────


def test_persona_edit_creates_config_version_no_release(
        freestyle_competitor, monkeypatch):
    cid = freestyle_competitor
    new_persona = (
        "Updated persona: now a patient mean-reversion trader that fades "
        "sharp opening spikes back toward the day's average price."
    )

    # Guard: if the engineer ever shells out to git/ruff/pytest on this path,
    # the test must fail loudly.
    def boom(*a, **kw):
        raise AssertionError("freestyle config path must not run subprocesses")

    monkeypatch.setattr(eng_mod, "run_cmd", boom, raising=True)
    monkeypatch.setattr(eng_mod, "git_commit_all",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("no git commit on config path")),
                        raising=True)
    monkeypatch.setattr(eng_mod, "record_release",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("no release row on config path")),
                        raising=True)
    monkeypatch.setattr(eng_mod, "unverified_releases", lambda: [], raising=True)
    monkeypatch.setattr(eng_mod, "unverified_config_versions", lambda *a, **kw: [],
                        raising=True)

    task_id = base.create_task(
        created_by="pm", title="rewrite persona", rationale="test",
        task_type="persona_edit",
        spec={"new_persona": new_persona, "diff_summary": "switch to mean-reversion"},
        competitor_id=cid,
    )

    result = eng_mod.process_one_task(only_task_id=task_id)
    assert result is not None, "engineer claimed nothing"
    assert result["ok"] is True, result
    assert result["task_id"] == task_id
    assert result.get("release_id") is None
    version_id = result["config_version_id"]
    assert version_id

    # Live persona updated.
    assert _live_persona(cid) == new_persona

    # Config version row is deployed with the right snapshot; NO release row.
    with conn() as c:
        ver = c.execute(
            "SELECT * FROM competitor_config_versions WHERE id = %s",
            (version_id,)).fetchone()
        task = c.execute("SELECT status, release_id FROM agent_tasks WHERE id = %s",
                         (task_id,)).fetchone()
        rel_count = c.execute(
            "SELECT COUNT(*) AS n FROM releases WHERE task_id = %s",
            (task_id,)).fetchone()["n"]
    assert ver["status"] == "deployed"
    assert ver["field"] == "persona"
    assert ver["competitor_id"] == cid
    assert ver["old_value"] == ORIG_PERSONA
    assert ver["new_value"] == new_persona
    assert task["status"] == "done"
    assert task["release_id"] is None
    assert int(rel_count) == 0


# ─── (b) Tester: verify good persona, fail+revert a malformed one ──────


def _mk_deployed_version(cid: str, *, field: str, old_value, new_value,
                         mandate_week=None) -> int:
    """Insert a deployed config version directly (bypassing the engineer)."""
    return base.record_config_version(
        competitor_id=cid, task_id=None, field=field,
        mandate_week=mandate_week, old_value=old_value, new_value=new_value,
    )


def test_verify_config_version_passes_well_formed(freestyle_competitor,
                                                   monkeypatch):
    cid = freestyle_competitor
    # Skip the dry-run cycle stage by stubbing it NEUTRAL (paused) so the test
    # is hermetic — we're checking the well_formed gate here.
    monkeypatch.setattr(test_mod, "_config_stage_dry_run",
                        lambda ver: (True, "skipped (stubbed)", None),
                        raising=True)
    # Live persona is the seeded ORIG_PERSONA (well-formed).
    version_id = _mk_deployed_version(
        cid, field="persona", old_value="old persona text here padded out long",
        new_value=ORIG_PERSONA)
    result = test_mod.verify_config_version(version_id)
    assert result["passed"] is True, result
    assert result["stages"]["well_formed"]["ok"] is True


def test_process_unverified_config_reverts_malformed(freestyle_competitor,
                                                      monkeypatch):
    cid = freestyle_competitor
    # Stub the dry-run stage NEUTRAL so only well_formed decides.
    monkeypatch.setattr(test_mod, "_config_stage_dry_run",
                        lambda ver: (True, "skipped (stubbed)", None),
                        raising=True)

    # Corrupt the live persona to something malformed (too short → fails the
    # well_formed gate), with a captured GOOD old_value to restore.
    with conn() as c:
        c.execute("UPDATE competitors SET persona = %s WHERE id = %s",
                  ("bad", cid))
    version_id = _mk_deployed_version(
        cid, field="persona", old_value=ORIG_PERSONA, new_value="bad")

    processed = test_mod.process_unverified_config()
    assert processed >= 1

    with conn() as c:
        ver = c.execute(
            "SELECT status FROM competitor_config_versions WHERE id = %s",
            (version_id,)).fetchone()
        nh = c.execute(
            "SELECT COUNT(*) AS n FROM agent_tasks "
            "WHERE competitor_id = %s AND task_type = 'needs_human'",
            (cid,)).fetchone()["n"]
    assert ver["status"] == "reverted"
    # Old value restored to the live record.
    assert _live_persona(cid) == ORIG_PERSONA
    # A needs_human task was filed for this competitor.
    assert int(nh) >= 1


def test_strategy_config_revert_restores_old_value(freestyle_competitor,
                                                    monkeypatch):
    cid = freestyle_competitor
    wk = week_start()
    # Make the live strategy_config malformed (value out of bounds) so the
    # well_formed gate fails and the revert restores ORIG_STRATEGY_CONFIG.
    with conn() as c:
        c.execute(
            "UPDATE competitor_mandates SET strategy_config = %s::jsonb "
            "WHERE competitor_id = %s AND week_start = %s",
            (_json({"max_positions": 999}), cid, wk),
        )
    monkeypatch.setattr(test_mod, "_config_stage_dry_run",
                        lambda ver: (True, "skipped (stubbed)", None),
                        raising=True)
    version_id = _mk_deployed_version(
        cid, field="strategy_config", old_value=ORIG_STRATEGY_CONFIG,
        new_value={"max_positions": 999}, mandate_week=wk)

    processed = test_mod.process_unverified_config()
    assert processed >= 1

    with conn() as c:
        ver = c.execute(
            "SELECT status FROM competitor_config_versions WHERE id = %s",
            (version_id,)).fetchone()
        cfg = c.execute(
            "SELECT strategy_config FROM competitor_mandates "
            "WHERE competitor_id = %s AND week_start = %s",
            (cid, wk)).fetchone()["strategy_config"]
    assert ver["status"] == "reverted"
    assert cfg == ORIG_STRATEGY_CONFIG


# ─── (c) retro stamps competitor_id ────────────────────────────────────


def test_retro_persists_competitor_id(freestyle_competitor, monkeypatch):
    """run_for_trade must read the trade's competitor_id and stamp it on the
    retro and every proposal it creates."""
    cid = freestyle_competitor
    created: dict = {}

    # Seed a CLOSED trade for this competitor with its decision + signal.
    with conn() as c:
        sig = c.execute(
            """
            INSERT INTO signals (strategy, symbol, side, entry_price,
                                 stop_loss, target, rationale, payload,
                                 consumed, competitor_id)
            VALUES ('orb', 'RELIANCE', 'BUY', 100, 95, 110, 'r', '{}'::jsonb,
                    TRUE, %s)
            RETURNING id
            """,
            (cid,),
        ).fetchone()
        dec = c.execute(
            """
            INSERT INTO decisions (signal_id, actor, verdict, reasoning,
                                   competitor_id)
            VALUES (%s, %s, 'TAKE', 'r', %s)
            RETURNING id
            """,
            (sig["id"], cid, cid),
        ).fetchone()
        trade = c.execute(
            """
            INSERT INTO paper_trades
                (decision_id, symbol, side, qty, entry_price, entry_ts,
                 stop_loss, target, exit_price, exit_ts, exit_reason,
                 pnl_inr, charges_inr, net_pnl_inr, status, competitor_id)
            VALUES (%s, 'RELIANCE', 'BUY', 10, 100, now() - interval '2 hours',
                    95, 110, 108, now() - interval '1 hour', 'TARGET',
                    80, 5, 75, 'CLOSED', %s)
            RETURNING id
            """,
            (dec["id"], cid),
        ).fetchone()
    created["trade_id"] = int(trade["id"])

    fake_raw = {
        "verdict_label": "GOOD_CALL",
        "summary_layman": "It worked.",
        "why_we_acted": "Clean breakout.",
        "what_happened": "Hit target.",
        "verdict_reasoning": "Sound and confirmed.",
        "learnings": ["Breakouts on strong names tend to follow through."],
        "signal_quality_score": 4,
        "decision_quality_score": 4,
        "execution_quality_score": 4,
        "tags": ["hit-target"],
        "improvement_proposals": [{
            "category": "strategy",
            "title": "test proposal title",
            "rationale": "because this trade",
            "proposed_change": "do the concrete thing",
            "confidence": 3,
        }],
    }
    monkeypatch.setattr("helm.retro.complete_json",
                        lambda *a, **kw: fake_raw, raising=True)

    from helm.retro import run_for_trade
    res = run_for_trade(created["trade_id"], force=True)
    assert res is not None
    assert res.proposals_count == 1

    with conn() as c:
        retro = c.execute(
            "SELECT competitor_id FROM trade_retrospectives WHERE id = %s",
            (res.retro_id,)).fetchone()
        props = list(c.execute(
            "SELECT competitor_id FROM improvement_proposals WHERE retro_id = %s",
            (res.retro_id,)))
    assert retro["competitor_id"] == cid
    assert props and all(p["competitor_id"] == cid for p in props)


# ─── (d) PM per-agent scoping tags the task with the competitor ────────


def test_pm_freestyle_review_tags_task_with_competitor(freestyle_competitor,
                                                       monkeypatch):
    cid = freestyle_competitor
    # Seed a retro + open proposal for this freestyle competitor so PM has
    # something to react to.
    with conn() as c:
        sig = c.execute(
            """
            INSERT INTO signals (strategy, symbol, side, entry_price,
                                 stop_loss, target, rationale, payload,
                                 consumed, competitor_id)
            VALUES ('orb', 'TCS', 'BUY', 100, 95, 110, 'r', '{}'::jsonb,
                    TRUE, %s)
            RETURNING id
            """,
            (cid,),
        ).fetchone()
        dec = c.execute(
            """
            INSERT INTO decisions (signal_id, actor, verdict, reasoning,
                                   competitor_id)
            VALUES (%s, %s, 'SKIP', 'r', %s)
            RETURNING id
            """,
            (sig["id"], cid, cid),
        ).fetchone()
        retro = c.execute(
            """
            INSERT INTO trade_retrospectives
                (kind, decision_id, model, llm_mode, verdict_label,
                 signal_quality_score, decision_quality_score,
                 execution_quality_score, tags, summary_layman, why_we_acted,
                 what_happened, verdict_reasoning, learnings, competitor_id)
            VALUES ('SKIP', %s, 'm', 'm', 'BAD_CALL', 3, 3, 3, '[]'::jsonb,
                    's', 'w', 'wh', 'v', '[]'::jsonb, %s)
            RETURNING id
            """,
            (dec["id"], cid),
        ).fetchone()
        prop = c.execute(
            """
            INSERT INTO improvement_proposals
                (retro_id, category, title, rationale, proposed_change,
                 evidence, confidence, status, competitor_id)
            VALUES (%s, 'strategy', 'persona is too aggressive', 'losses',
                    'soften it', '{}'::jsonb, 4, 'open', %s)
            RETURNING id
            """,
            (retro["id"], cid),
        ).fetchone()
    proposal_id = int(prop["id"])

    def fake_llm(system, user, *, schema=None, model=None, mode=None,
                 max_tokens=None, temperature=None):
        return {
            "suggestions": [{
                "action": "create_task",
                "proposal_id": proposal_id,
                "task_type": "persona_edit",
                "title": "soften persona",
                "rationale": "recurrence and recent losses",
                "priority": 4,
                "spec": {
                    "new_persona": (
                        "A calmer mean-reversion trader that waits for a clear "
                        "exhaustion before fading, sizes small, and never "
                        "chases. Avoids the lunch lull and the close."
                    ),
                    "diff_summary": "soften aggression",
                },
                "proposal_status_change": "accepted",
                "status_note": "queued",
            }],
            "deferred_reason": "",
        }

    monkeypatch.setattr(pm_mod, "complete_json", fake_llm, raising=True)

    result = pm_mod.run_weekly_review(model="test-model", mode="cli",
                                      force=True, competitor_id=cid)
    assert isinstance(result, dict)
    assert result["competitor_id"] == cid
    assert len(result["tasks_created"]) == 1

    task_id = result["tasks_created"][0]
    with conn() as c:
        task = c.execute(
            "SELECT competitor_id, task_type FROM agent_tasks WHERE id = %s",
            (task_id,)).fetchone()
    assert task["competitor_id"] == cid
    assert task["task_type"] == "persona_edit"


def test_pm_freestyle_rejects_house_task_type(freestyle_competitor, monkeypatch):
    """A freestyle review must reject a code (house-only) task_type suggestion
    rather than queue it."""
    cid = freestyle_competitor

    def fake_llm(system, user, *, schema=None, model=None, mode=None,
                 max_tokens=None, temperature=None):
        return {
            "suggestions": [{
                "action": "create_task",
                "proposal_id": None,
                "task_type": "prompt_tweak",   # house-only → must be rejected
                "title": "edit the decider prompt",
                "rationale": "n/a",
                "priority": 3,
                "spec": {"file": "scripts/decide_signals.py", "anchor": "x",
                         "action": "append", "text": "y"},
                "proposal_status_change": None,
                "status_note": "",
            }],
            "deferred_reason": "",
        }

    monkeypatch.setattr(pm_mod, "complete_json", fake_llm, raising=True)

    result = pm_mod.run_weekly_review(model="test-model", mode="cli",
                                      force=True, competitor_id=cid)
    assert isinstance(result, dict)
    assert result["tasks_created"] == []
