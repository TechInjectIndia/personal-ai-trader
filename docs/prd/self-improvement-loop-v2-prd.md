# Self-Improvement Loop v2 — the Converging Apply-Arm (ECC-inspired)

**Status:** Draft for sequencing — 2026-06-15
**Owner:** Helm (autonomous PM → Engineer → Tester loop), human as trigger/final gate
**Scope:** the loop **machinery**, not the trading-quality backlog. The content
backlog already has a plan (`docs/prd/self-improvement-integration-prd.md`, the
0→8 phase map). This PRD fixes *why that plan never shipped*.
**Reference architecture:** `github.com/affaan-m/ecc` — a Claude Code operator
system whose converging apply-arm (instinct clustering + confidence scoring +
`/evolve` learning→skill promotion + eval-harness pass@k + quality gates) is
exactly what our loop lacks. We borrow the mechanisms, not the code.

---

## 1. Why this document exists

The 2026-06-15 deep dive measured the loop end-to-end and found it is a **real,
high-quality diagnosis engine bolted to a broken execution arm**:

| Stage | All-time |
|---|---|
| Retrospectives written | 1,287 (substantive, quality-scored) |
| Improvement proposals generated | 2,340 |
| → accepted | 222 |
| → **verified code releases EVER** | **6** (all 2026-06-02/03, 2–4 line edits) |
| → **releases since the 06-08 reset** | **0** |
| Proposals stuck `open` | 1,736 |
| Freestyle config versions | ~7 verified vs ~48 reverted |

Three hard facts the data forces:

1. **The loop diagnoses what it cannot fix.** The #1 profit bug (notional cap
   hard-reject) was correctly proposed **~90 times** as near-duplicate rows over
   weeks — and shipped *zero* times, because the fix lives in shared house code
   outside every freestyle agent's mutator surface. A human shipped it in ~20 min
   (commit `9614ae9`). It was even pre-planned as Phase 6e of the integration PRD
   on 2026-06-02 and still never executed. **Insight ≫ reach.**
2. **No learning curve.** Weekly `decision_quality_score` is flat/noisy across all
   agents over 5 weeks; every freestyle agent is still net-negative.
3. **Verification is the wrong kind.** The Tester gauntlet (`helm/agents/tester.py`)
   proves a change *compiles and passes smoke tests* — never that it *improves
   P&L*. So good economic changes can't be distinguished from bad ones, and the
   freestyle config arm reverts ~7:1.

The generation side is not the problem and needs no work. **Everything below is
about converting insight into shipped, verified, economically-validated change.**

---

## 2. Current state — what exists vs the five gaps

**Already built (reuse, don't rebuild):**
- **Semantic dedup** — `helm.agents.pm.run_backlog_drain` (Opus 4.8) reads batches
  of raw proposals and accepts/supersedes/rejects *semantically*. Lexical
  clustering is known-useless (`helm/agents/backlog.py` documents why) and is
  demoted to a dry-run preview. **The dedup works — but only as a rare manual
  "nudge," so the backlog re-grows to 1,736 open.**
- **Deterministic Tester gauntlet** — static gates → pipeline smoke → dashboard,
  fail-fast, auto-revert, two-revert autonomy pause.
- **Per-LLM config versioning** — `competitor_config_versions` with verify/revert.
- **Ranking** — `scripts/proposals_digest.py` ranks by recurrence × confidence.

**The five gaps (this PRD's workstreams):**

| # | Gap | Workstream |
|---|---|---|
| G1 | Dedup is a rare manual nudge → backlog re-grows to 1,736 | **Continuous clustering** |
| G2 | Agents diagnose shared-code bugs they're walled off from → 90 orphans | **Escalation surface** |
| G3 | Tester proves "compiles," never "improves P&L" → 7:1 revert | **Replay eval-gate** |
| G4 | Confirmed learnings die as DB rows; no durable per-LLM memory | **Instinct promotion** |
| G5 | One-change-in-flight + Engineer noop most days → 0 releases | **Throughput** |

---

## 3. ECC mapping (what we borrow, concretely)

| ECC mechanism | Our equivalent | Workstream |
|---|---|---|
| Instinct-based Continuous Learning v2 (confidence-scored, auto-clustered) | Cluster proposals into confidence-ranked `proposal_clusters`, continuously | G1 |
| `/evolve` — clusters instincts → production skills | Promote a confirmed cluster → a durable persona/mandate rule | G4 |
| Eval-harness, deterministic pass@k | Replay a candidate change over `candles_1m`, require a metric lift | G3 |
| `/quality-gate` before progression | Eval-gate sits in the Tester gauntlet as a new stage | G3 |
| 64 specialized agents w/ broad surface | A `house-engineer` escalation lane with wide mutator surface | G2 |

---

## 4. Workstreams

### G1 — Continuous clustering (kill the orphan backlog)

**Goal:** the backlog is *always* a small set of ranked clusters, never 1,736 raw
rows. Make the existing semantic drain incremental and scheduled instead of a
rare manual nudge.

**Spec:**
- New table `proposal_clusters` (`id, theme, layer ∈ {strategy,risk,decider,
  execution,data,meta,persona}, target_surface ∈ {house,<competitor_id>},
  confidence, recurrence, status, representative_proposal_id, created/updated_ts`).
- Add `cluster_id` FK to `improvement_proposals`.
- **Incremental assignment:** when a retro emits a proposal, the PM (cheap model)
  assigns it to the nearest existing cluster *or* opens a new one — so clustering
  is O(new proposals), not a periodic O(backlog) drain. Recurrence = member count;
  confidence = max member confidence (decayed).
- Weekly the PM (Opus) re-balances clusters (merge/split) — the existing
  `run_backlog_drain` logic, now operating on clusters not raw rows.
- Ranking moves to clusters: `proposals_digest.py` ranks `proposal_clusters` by
  `recurrence × confidence × economic_priority`.

**Acceptance:** open *clusters* ≤ ~40 at all times; a new proposal restating an
existing theme increments a cluster's recurrence rather than adding an orphan;
the 1,736 existing open rows collapse to their clusters on first run.

**Folds in:** the lesson from `helm/agents/backlog.py` (semantic-only dedup) +
`proposals_digest.py` ranking.

---

### G2 — Escalation surface (let insight reach shared code)

**Goal:** a freestyle agent that diagnoses a bug outside its mutator surface can
*route* it to an agent that can fix it — instead of emitting the 91st orphan.

**Spec:**
- Each cluster carries `target_surface`. A freestyle cluster whose fix is in
  shared house code is tagged `target_surface='house'` and `escalated=true`.
- A **`house-engineer`** agent (wide mutator surface over `helm/`, `scripts/`)
  claims escalated clusters on the Engineer cadence — the automated form of the
  manual cap-clamp fix shipped today.
- Guardrails: escalated changes go through the **full** Tester gauntlet *plus* the
  G3 eval-gate; they are queued, never auto-merged to a live path during market
  hours (the drain-incident lesson — see `project_drain_incident_lessons`).
- A cluster reaching `recurrence ≥ N` (default 5) with no eligible in-surface
  agent **auto-escalates** and surfaces in the dashboard Action Center.

**Acceptance:** a synthetic freestyle proposal targeting shared code creates an
escalated house cluster, gets claimed by `house-engineer`, and ships through the
eval-gate; recurrence-≥5 shared-code clusters never sit unowned.

**Folds in:** the ~90-proposal cap cluster (the canonical failure this fixes);
`needs_human` escalation tickets #932/938/940/941/942/947/948/949/960/962/968/970.

---

### G3 — Replay eval-gate (verify P&L, not compilation)

**Goal:** a change must *prove* it improves an economic metric on historical data
before it ships. This is the single highest-quality lever — it turns the 7:1
revert lottery into evidence-based promotion.

**Spec:**
- Build a deterministic **replay harness**: given a code/config change and a date
  window, re-run the scan→decide→size→manage pipeline over recorded `candles_1m`
  (decisions can be replayed from the stored `decisions` rows to avoid LLM spend,
  or re-scored with a cheap model behind a flag).
- Metric set: net expectancy/trade, E2C, win%, max drawdown, trade count (guard
  against "improves expectancy by trading nothing").
- **pass@k semantics (ECC):** run the window in k folds; require the change to be
  net-positive on the metric in ≥⌈k·0.6⌉ folds and non-regressive on drawdown.
- New Tester stage `eval_gate` runs **after** static/smoke and **before**
  `verified`. Fail → revert with the metric delta in `tester_notes`.
- House code → replay on house book; freestyle config → replay on that
  competitor's book.

**Acceptance:** a change that flat-lines or worsens replayed net is auto-reverted
with a numeric reason; a change that lifts replayed net in ≥k·0.6 folds verifies;
two firings on the same release/window produce identical verdicts (determinism).

**Folds in:** F1 cost/gross analytics (metric definitions); the integration PRD's
"measure in paper-mode" principle, made pre-merge instead of post-hoc.

---

### G4 — Instinct promotion (durable per-LLM memory)

**Goal:** a cluster that survives the eval-gate becomes a *durable rule the agent
always applies*, not a closed DB row whose lesson evaporates.

**Spec:**
- A `verified` cluster is **promoted** into the right durable artifact:
  - house → a named `config.py` constant + gate (defense-in-depth pattern from the
    integration PRD), or a decider-prompt rubric line.
  - freestyle → a line in that competitor's **mandate/persona** (`competitor_mandates`
    / `competitor_config_versions`) so the model carries the learning into every
    future decision — ECC's instinct→skill promotion.
- Promoted instincts get a confidence that **decays** if later trades contradict
  them (re-opens the cluster), so the memory self-corrects.
- Dashboard: per-LLM "instinct ledger" — what each model has learned and is
  actively applying, with hit/miss since promotion.

**Acceptance:** a verified cluster appears as a durable artifact + a per-LLM
instinct-ledger row; a contradicting streak decays its confidence and re-opens it;
the ledger renders per agent.

**Folds in:** `competitor_mandates`, the F8 mandate-advisory mechanism.

---

### G5 — Throughput (stop the 0-releases-per-week stall)

**Goal:** clusters actually get claimed and shipped on cadence.

**Spec:**
- Auto-claim: the Engineer pulls the top-ranked eligible cluster each tick instead
  of waiting for a hand-authored task.
- Widen one-change-in-flight to a small bounded queue, isolated per surface
  (house vs each competitor) so they don't contend — using worktree isolation for
  parallel house changes (`project_no_parallel_builders_one_worktree` lesson).
- Track and alert on **throughput** (verified releases/week) and **conversion**
  (clusters verified ÷ clusters opened) as first-class loop-health metrics.

**Acceptance:** ≥1 verified release/trading-week sustained; conversion rate
visible and trending; no two builders share a worktree.

**Folds in:** F3 daily-postclose cadence; the drain-incident isolation fixes.

---

## 5. Sequencing

```
G1 Continuous clustering   ── makes the backlog actionable (precondition for all)
   │
G2 Escalation surface      ── lets clusters reach the code that owns them
   │
G3 Replay eval-gate        ── the quality bar; the biggest single quality lever
   │
G4 Instinct promotion      ── makes verified learnings durable + self-correcting
   │
G5 Throughput              ── sustains flow once the above is trustworthy
```

**80/20:** G1 + G2 alone close the loop's biggest hole (insight that can't reach
code) and are buildable without the replay harness. G3 is the highest-value but
heaviest lift (a real backtest/replay engine — adjacent to the de-scoped
`backtrader` work, so scope carefully). Build G1+G2 first, prove the cap-class of
bugs now ships automatically, then invest in G3.

---

## 6. Definition of done — "the loop is converging"

The loop graduates from gimmick to engine when, sustained over a month:

1. Open **clusters** ≤ ~40 (vs 1,736 raw proposals today). *(G1)*
2. Every recurrence-≥5 cluster has an owner that *can* fix it. *(G2)*
3. ≥1 verified release/trading-week, each with a positive replayed-metric delta
   recorded in `tester_notes`. *(G3, G5)*
4. Per-LLM `decision_quality_score` and net expectancy trend **up** over 4+ weeks
   — the learning curve that is flat today. *(G4)*
5. No human has to manually drain the backlog or hand-ship a 90×-diagnosed bug.

Until #4 shows a real slope, this remains "a diagnosis engine," honestly labelled.
```
