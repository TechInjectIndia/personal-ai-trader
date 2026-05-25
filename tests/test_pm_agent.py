"""
Tests for helm.agents.pm.

We hit the real local Postgres `helm` database (autocommit, no easy rollback)
and clean up our own rows in fixtures. The agent's writes go into
`improvement_proposals`, `agent_tasks`, `agent_runs`, `audit`, `releases`,
`trade_retrospectives`, `decisions`, `signals` — fixtures track every id they
insert and delete on teardown.

The LLM is always mocked via `monkeypatch.setattr` on `helm.agents.pm.complete_json`;
no live API or CLI call ever happens in the unit-test path.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from helm.data.store import conn, init_schema

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(scope="session", autouse=True)
def _ensure_schema() -> None:
    """Schema is idempotent; make sure the agent tables exist locally."""
    init_schema()


@pytest.fixture
def db_cleanup():
    """Track ids inserted during a test and delete them in reverse-FK order."""
    bag: dict[str, list[int]] = {
        "agent_runs": [],
        "agent_tasks": [],
        "releases": [],
        "improvement_proposals": [],
        "trade_retrospectives": [],
        "decisions": [],
        "signals": [],
        "audit": [],
    }
    yield bag
    # FK order: releases → agent_tasks → agent_runs (FK on task_id),
    # then proposals → retros → decisions → signals.
    with conn() as c:
        for table in (
            "agent_runs", "agent_tasks", "releases",
            "improvement_proposals", "trade_retrospectives",
            "decisions", "signals", "audit",
        ):
            ids = bag.get(table) or []
            if not ids:
                continue
            c.execute(
                f"DELETE FROM {table} WHERE id = ANY(%s)",  # noqa: S608 — table is hardcoded
                (ids,),
            )


# ─── helpers to set up rows ──────────────────────────────────────────


def _mk_signal(bag: dict, *, symbol: str = "RELIANCE") -> int:
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO signals (ts, strategy, symbol, side, entry_price,
                                 stop_loss, target, rationale, payload, consumed)
            VALUES (now(), 'orb', %s, 'BUY', 100, 99, 102, 'test', '{}'::jsonb, TRUE)
            RETURNING id
            """,
            (symbol,),
        ).fetchone()
    bag["signals"].append(row["id"])
    return int(row["id"])


def _mk_decision(bag: dict, signal_id: int, *, verdict: str = "TAKE") -> int:
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO decisions (signal_id, ts, actor, verdict, reasoning)
            VALUES (%s, now(), 'test', %s, 'test')
            RETURNING id
            """,
            (signal_id, verdict),
        ).fetchone()
    bag["decisions"].append(row["id"])
    return int(row["id"])


def _mk_retro(bag: dict, decision_id: int, *, kind: str = "TRADE",
              competitor_id: str = "house-claude") -> int:
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO trade_retrospectives
                (kind, trade_id, decision_id, model, llm_mode,
                 verdict_label, signal_quality_score, decision_quality_score,
                 execution_quality_score, tags,
                 summary_layman, why_we_acted, what_happened, verdict_reasoning,
                 learnings, raw_response, competitor_id)
            VALUES (%s, NULL, %s, 'test', 'test',
                    'GOOD_CALL', 3, 3, 3, '[]'::jsonb,
                    'summary', 'why', 'what', 'verdict',
                    '[]'::jsonb, '{}'::jsonb, %s)
            RETURNING id
            """,
            (kind, decision_id, competitor_id),
        ).fetchone()
    bag["trade_retrospectives"].append(row["id"])
    return int(row["id"])


def _mk_proposal(bag: dict, retro_id: int, *, title: str = "tighten orb stop",
                 category: str = "decider_prompt", confidence: int = 4,
                 status: str = "open", competitor_id: str = "house-claude") -> int:
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO improvement_proposals
                (retro_id, category, title, rationale, proposed_change,
                 evidence, confidence, status, competitor_id)
            VALUES (%s, %s, %s, 'because tests', 'do the thing',
                    '{}'::jsonb, %s, %s, %s)
            RETURNING id
            """,
            (retro_id, category, title, confidence, status, competitor_id),
        ).fetchone()
    bag["improvement_proposals"].append(row["id"])
    return int(row["id"])


def _mk_deployed_release(bag: dict) -> int:
    """Make a one-off release row + a parent task (FK requires task)."""
    with conn() as c:
        task = c.execute(
            """
            INSERT INTO agent_tasks (created_by, title, rationale, task_type,
                                     spec, status, priority)
            VALUES ('test', 'parent', 'parent', 'prompt_tweak',
                    '{}'::jsonb, 'done', 3)
            RETURNING id
            """,
        ).fetchone()
        task_id = int(task["id"])
        bag["agent_tasks"].append(task_id)
        rel = c.execute(
            """
            INSERT INTO releases (task_id, commit_sha, summary, status)
            VALUES (%s, 'deadbeef', 'unverified ship', 'deployed')
            RETURNING id
            """,
            (task_id,),
        ).fetchone()
    bag["releases"].append(int(rel["id"]))
    return int(rel["id"])


def _latest_pm_run() -> dict | None:
    with conn() as c:
        return c.execute(
            "SELECT * FROM agent_runs WHERE agent='pm' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()


# ─── tests ──────────────────────────────────────────────────────────


def test_creates_tasks_and_flips_statuses(monkeypatch, db_cleanup):
    """Happy path: LLM returns 2 suggestions — one create_task with an
    accepted proposal, one reject_proposal. Both must land in the DB and the
    agent_runs row must close with outcome='ok'."""

    sig = _mk_signal(db_cleanup)
    dec = _mk_decision(db_cleanup, sig)
    retro = _mk_retro(db_cleanup, dec)
    prop_a = _mk_proposal(db_cleanup, retro, title="widen orb stop on choppy mornings")
    prop_b = _mk_proposal(db_cleanup, retro, title="drop sbin from watchlist",
                          category="strategy", confidence=2)

    fake_llm_payload = {
        "suggestions": [
            {
                "action": "create_task",
                "proposal_id": prop_a,
                "task_type": "prompt_tweak",
                "title": "widen orb stop on choppy mornings",
                "rationale": "recurrence=1, but high-confidence retro and "
                             "consistent with prior PM hypothesis.",
                "priority": 4,
                "spec": {
                    "target": "decide_signals.SYSTEM_PROMPT",
                    "find": "Decision principles",
                    "insert_after": "Prefer wider stops on choppy mornings.",
                },
                "proposal_status_change": "accepted",
                "status_note": "queued via PM",
            },
            {
                "action": "reject_proposal",
                "proposal_id": prop_b,
                "task_type": "needs_human",
                "title": "reject sbin removal",
                "rationale": "noise — single retro, low confidence",
                "priority": 1,
                "spec": {"reason": "no signal"},
                "proposal_status_change": "rejected",
                "status_note": "single low-confidence retro",
            },
        ],
        "deferred_reason": "",
    }

    captured: dict = {}

    def fake_complete_json(system, user, *, schema, model, mode,
                           max_tokens, temperature):
        captured["called"] = True
        captured["model"] = model
        return fake_llm_payload

    monkeypatch.setattr("helm.agents.pm.complete_json", fake_complete_json)

    from helm.agents.pm import run_weekly_review
    result = run_weekly_review(model="test-model", mode="cli", force=True,
                               competitor_id="house-claude")

    assert captured.get("called") is True
    assert result["deferred"] is False
    assert len(result["tasks_created"]) == 1
    assert result["proposals_accepted"] == [prop_a]
    assert result["proposals_rejected"] == [prop_b]

    db_cleanup["agent_tasks"].extend(result["tasks_created"])
    db_cleanup["agent_runs"].append(result["run_id"])

    # Verify the agent_tasks row landed correctly.
    with conn() as c:
        task_row = c.execute(
            "SELECT created_by, task_type, proposal_id, priority, status, title "
            "FROM agent_tasks WHERE id = %s",
            (result["tasks_created"][0],),
        ).fetchone()
    assert task_row["created_by"] == "pm"
    assert task_row["task_type"] == "prompt_tweak"
    assert task_row["proposal_id"] == prop_a
    assert task_row["status"] == "open"
    assert task_row["priority"] == 4

    # Verify proposal statuses flipped.
    with conn() as c:
        statuses = {
            r["id"]: r["status"]
            for r in c.execute(
                "SELECT id, status FROM improvement_proposals "
                "WHERE id = ANY(%s)",
                ([prop_a, prop_b],),
            )
        }
    assert statuses[prop_a] == "accepted"
    assert statuses[prop_b] == "rejected"

    # Verify the agent_runs row.
    run_row = _latest_pm_run()
    assert run_row is not None
    assert run_row["id"] == result["run_id"]
    assert run_row["outcome"] == "ok"
    assert run_row["model"] == "test-model"
    assert run_row["finished_ts"] is not None


def test_defers_when_unverified_release_present(monkeypatch, db_cleanup):
    """If a release is unverified the PM must skip cleanly without an LLM call."""

    _mk_deployed_release(db_cleanup)

    called: dict = {"n": 0}

    def fake_complete_json(*a, **kw):
        called["n"] += 1
        raise AssertionError("LLM must not be called on deferred path")

    monkeypatch.setattr("helm.agents.pm.complete_json", fake_complete_json)

    from helm.agents.pm import run_weekly_review
    result = run_weekly_review(model="test-model", mode="cli", force=False,
                               competitor_id="house-claude")

    assert called["n"] == 0
    assert result["deferred"] is True
    assert "unverified" in result["reason"]
    assert result["tasks_created"] == []
    assert result["proposals_accepted"] == []
    assert result["proposals_rejected"] == []

    db_cleanup["agent_runs"].append(result["run_id"])

    # Verify the agent_runs row was written with outcome='noop'.
    run_row = _latest_pm_run()
    assert run_row is not None
    assert run_row["id"] == result["run_id"]
    assert run_row["outcome"] == "noop"


def test_defers_when_no_new_trades_or_proposals(monkeypatch, db_cleanup):
    """Second skip path: a prior successful PM run exists, and nothing has
    happened since. Must skip without an LLM call."""

    # Seed a prior successful PM run with finished_ts in the FUTURE so the
    # "anything new since" check sees nothing fresh, regardless of what's in
    # the local DB. (The PM uses finished_ts as the watermark — a future
    # value means "nothing has happened since".)
    with conn() as c:
        prior = c.execute(
            """
            INSERT INTO agent_runs (agent, invocation, model, llm_mode,
                                    competitor_id,
                                    started_ts, finished_ts, outcome)
            VALUES ('pm', 'weekly_review', 'test', 'cli',
                    'house-claude',
                    now() + interval '1 minute',
                    now() + interval '1 minute', 'ok')
            RETURNING id
            """,
        ).fetchone()
    db_cleanup["agent_runs"].append(int(prior["id"]))

    # The first deferral check is unverified-releases — if the local DB has
    # any deployed-but-unverified releases that would trip first, skip this
    # test (the unverified-release test already covers that path).
    from helm.agents.base import unverified_releases
    if unverified_releases():
        pytest.skip("local DB has unverified releases — covered by the "
                    "unverified-release test instead.")

    called: dict = {"n": 0}

    def fake_complete_json(*a, **kw):
        called["n"] += 1
        raise AssertionError("LLM must not be called on deferred path")

    monkeypatch.setattr("helm.agents.pm.complete_json", fake_complete_json)

    from helm.agents.pm import run_weekly_review
    result = run_weekly_review(model="test-model", mode="cli", force=False,
                               competitor_id="house-claude")

    assert called["n"] == 0
    assert result["deferred"] is True
    assert "no new" in result["reason"]

    db_cleanup["agent_runs"].append(result["run_id"])


# Used so the type-checkers see Decimal/datetime as referenced. The fixtures
# only call into psycopg which doesn't need these explicit imports, but the
# module-level signal/decision setup uses literals where the DB casts ints to
# Decimal under the hood; keeping the imports here documents the intent.
_ = (Decimal, datetime)
