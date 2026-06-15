# FRD G2 — Escalation Surface (cross-layer routing)

_Self-Improvement Loop v2 · Owner: PM + house-engineer loop · Status: proposed · Depends on: G1 (proposal_clusters), helm.agents.engineer (exists)_

## 1. Problem
A freestyle agent's mutator surface is its own persona/config only. When it
diagnoses a bug in **shared house code** (the cap hard-block, the sizing formula),
it can only emit another proposal it cannot act on — so the cap cluster recurred
~90× and shipped zero times until a human intervened (commit `9614ae9`). Insight
that can't reach the owning code is wasted.

## 2. Goal & success metrics
Route every cluster to an agent that *can* fix it; auto-escalate shared-code
clusters to a house owner.
- Driver metric: **0 recurrence-≥5 clusters left unowned.**
- Outcome metric: shared-code bugs (cap-class) ship via the loop, not by hand.

## 3. HLD
```
cluster (target_surface) ─┬─ 'house'        ─► house-engineer queue (wide surface: helm/, scripts/)
                          └─ <competitor_id> ─► that competitor's engineer (persona/config only)

freestyle cluster whose fix is shared code ─► PM sets target_surface='house',
                                              escalated=true, links origin competitor
recurrence ≥ ESCALATE_RECURRENCE (5) AND no in-surface owner ─► auto-escalate + Action Center push
```
A new **`house-engineer`** agent type claims `target_surface='house'` clusters on
the Engineer cadence. Escalated changes take the **full** Tester gauntlet **plus**
the G3 eval-gate, are queued (never auto-merged to a live path during market
hours — `project_drain_incident_lessons`), and are committed locally on a branch
for the human to push (existing convention).

## 4. LLD
**Schema (extends G1 `proposal_clusters`):**
```sql
ALTER TABLE proposal_clusters ADD COLUMN escalated BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE proposal_clusters ADD COLUMN origin_competitor_id TEXT;  -- who surfaced it
```
**`helm/config.py`:** `ESCALATE_RECURRENCE = 5`.
**`helm/agents/pm.py`:** during clustering, classify `target_surface`. Heuristic +
LLM: if the proposed change names a shared module (`helm/orchestrator/`,
`scripts/paper_execute.py`, `helm/wallet.py`, `helm/config.py`) or the competitor
explicitly says "out of freestyle surface," set `target_surface='house'`,
`escalated=true`, `origin_competitor_id=<id>`.
**`helm/agents/engineer.py`:** add a `house-engineer` invocation that selects the
top-ranked `target_surface='house'` open cluster (auto-claim, see G5). Mutator
surface = repo-wide under `helm/` + `scripts/` (vs freestyle's persona files).
**Action Center:** push `cluster_escalated_unowned` when recurrence ≥ threshold and
no eligible owner, so the human sees structurally-stuck insight (the exact failure
mode that hid the cap bug).

## 5. Acceptance
- A synthetic freestyle proposal naming `scripts/paper_execute.py` creates an
  escalated `house` cluster with `origin_competitor_id` set.
- `house-engineer` claims it, ships through gauntlet + eval-gate, commits on a
  branch.
- A recurrence-≥5 cluster with no eligible owner fires exactly one Action Center
  item; in-surface clusters never escalate.
- Retro of the cap-class on historical data would have auto-escalated (replay the
  needs_human cap cluster as a fixture).

## 6. Out of scope / guardrails
No auto-merge to `main` or to a live cron path during market hours. House changes
keep the human-pushes-remote convention. Worktree isolation for any parallel
house build (`project_no_parallel_builders_one_worktree`).
