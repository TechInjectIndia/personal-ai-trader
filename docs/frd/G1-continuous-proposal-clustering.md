# FRD G1 — Continuous Proposal Clustering

_Self-Improvement Loop v2 · Owner: PM loop · Status: proposed · Depends on: helm.agents.pm.run_backlog_drain (exists), proposals_digest.py (exists)_

## 1. Problem
The retro pipeline emits ~1,547 proposals/cycle that are mostly restatements of a
few dozen ideas (the cap fix alone recurs ~90×). Semantic dedup *exists*
(`run_backlog_drain`, Opus) but runs only as a rare manual nudge, so the backlog
re-grows to **1,736 open rows**. A 1,736-row backlog is unrankable and
unactionable — the loop can't tell "proposed once" from "proposed 90 times."

## 2. Goal & success metrics
Keep the backlog as a small, continuously-maintained set of confidence-ranked
clusters.
- Driver metric: **open `proposal_clusters` ≤ ~40 at all times.**
- Outcome metric: a restatement increments an existing cluster's `recurrence`
  instead of creating an orphan (orphan-rate → ~0).

## 3. HLD
Two layers, reusing the existing semantic PM (lexical clustering stays demoted per
`helm/agents/backlog.py`):
1. **Incremental assignment (cheap model, per proposal):** at retro time, assign
   each new proposal to the nearest existing open cluster *or* open a new one.
   O(new), not O(backlog).
2. **Weekly re-balance (Opus):** `run_backlog_drain`, refactored to merge/split
   *clusters* rather than triage raw rows.

```
new proposal ─► PM.assign_to_cluster(theme, layer, target_surface)
                   ├─ match existing open cluster ─► recurrence += 1; confidence = max(decayed)
                   └─ no match ─► INSERT proposal_clusters (status='open')
weekly ───────► PM.rebalance_clusters()  (merge near-dupes, split overbroad)
digest ───────► rank clusters by recurrence × confidence × economic_priority
```

## 4. LLD
**New table `proposal_clusters`:**
```sql
CREATE TABLE proposal_clusters (
  id              BIGSERIAL PRIMARY KEY,
  theme           TEXT NOT NULL,
  layer           TEXT NOT NULL,   -- strategy|risk|decider|execution|data|meta|persona
  target_surface  TEXT NOT NULL,   -- 'house' | competitor_id
  confidence      NUMERIC(3,2) NOT NULL DEFAULT 0,
  recurrence      INT NOT NULL DEFAULT 0,
  economic_priority NUMERIC(4,2) NOT NULL DEFAULT 1,  -- set by digest from $ impact
  status          TEXT NOT NULL DEFAULT 'open',       -- open|accepted|in_flight|verified|rejected|superseded
  representative_proposal_id BIGINT REFERENCES improvement_proposals(id),
  created_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_ts      TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE improvement_proposals ADD COLUMN cluster_id BIGINT REFERENCES proposal_clusters(id);
```
**`helm/agents/pm.py`:**
- `assign_to_cluster(proposal) -> cluster_id` — cheap-model call; prompt = the
  proposal + a compact list of open-cluster `(id, theme, layer, target_surface)`;
  returns an existing id or `NEW`. Pure-ish: DB read for clusters, one LLM call.
- `rebalance_clusters()` — extends `run_backlog_drain` to operate on cluster rows.
- Confidence decay: `confidence = max(member confidences) × 0.95^weeks_since_last_member`.
**Migration:** one-shot `scripts/cluster_backfill.py` runs `assign_to_cluster` over
the 1,736 existing open proposals (batched), collapsing them to clusters.
**`scripts/proposals_digest.py`:** rank `proposal_clusters`, not raw proposals.

## 5. Acceptance
- Backfill collapses 1,736 open proposals to ≤ ~40 clusters; every proposal has a
  `cluster_id`.
- A synthetic proposal restating an existing theme increments `recurrence` and
  adds no orphan.
- Digest emits a ranked cluster list; unit tests pin assignment idempotence
  (same proposal → same cluster) and decay math (pure function, no DB).

## 6. Out of scope / guardrails
No lexical clustering on the authoritative path (known-useless). No change to how
retros *generate* proposals. Cheap model for per-item assignment; Opus only for
the weekly re-balance (cost discipline, POC phase).
