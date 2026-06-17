# PRD: Multi-Agent Debate Decider

| | |
|---|---|
| **Status** | Draft — for later consideration (not scheduled) |
| **Author** | Claude (engineering delegate) |
| **Date** | 2026-05-20 |
| **Surface** | `scripts/decide_signals.py`, `helm/llm`, `decisions` table, `settings` |
| **Origin** | Assessment of [HKUDS/AI-Trader](https://github.com/HKUDS/AI-Trader); the durable idea borrowed from [TauricResearch/TradingAgents](https://github.com/tauricresearch/tradingagents) |

---

## 1. One-liner

Before booking a paper trade, have the LLM **argue both sides of the signal** (bull vs. skeptic) and resolve to a verdict, instead of the current single-shot TAKE/SKIP call — and measure whether the extra deliberation improves win rate / expectancy enough to justify the added latency.

## 2. Background & motivation

The current decider (`scripts/decide_signals.py`) is a **single Anthropic call**: context blob in → strict JSON `{verdict, confidence, reasoning}` out, with prompt caching on the system prompt. It's cheap and fast (~5–10s inline) and works, but it commits to a verdict in one pass with no adversarial pressure on its own reasoning.

While evaluating whether HKUDS/AI-Trader had anything reusable, the conclusion was: **no** — it's a social/copy-trading platform (FastAPI + React, signal sharing, agent debates *as a marketplace*), not a strategy library, and its repo has open issues flagging missing backtests and possible look-ahead leakage. The one genuinely transferable idea is the **multi-agent debate** pattern popularised by TradingAgents: separate bull / bear / risk-manager personas debate, then a judge decides. Debate-style prompting tends to surface failure modes a single pass glosses over (e.g. "breakout into resistance," "signal arriving minutes before square-off").

This PRD captures that idea so the PM → Engineer loop can rank it against other `improvement_proposals` rather than us grafting it in ad hoc.

## 3. Goals / Non-goals

**Goals**
- Improve **decision quality** on marginal signals — measured as win rate and expectancy on TAKEs, plus fewer BAD_CALL retros.
- Keep it **flag-gated and A/B-able** so we can prove (or kill) it with data, never a default-on guess.
- Stay inside the existing decider contract: same `decisions` row shape, same `consumed` dedupe, same risk gate as final guard.

**Non-goals**
- ❌ Adopting AI-Trader's platform, copy-trading, signal-sharing, or any FastAPI/React surface.
- ❌ Multi-symbol portfolio reasoning or cross-signal debate. One signal, one decision, unchanged.
- ❌ Introducing a second LLM provider or an agent framework (LangGraph etc.). Reuse `helm.llm`.
- ❌ Going long-and-short. Bot stays long-only v1; the "bear" persona argues *against taking*, not for shorting.

## 4. Current state (what we're changing)

`decide_signal_inline(signal_id)` builds context (signal details, recent candles via `_summarize_candles`, `_risk_snapshot`, `_todays_decisions_summary`) and calls `helm.llm.decide()` once. Result → `decisions` row → on TAKE, `paper_execute.execute_signal` (which re-runs `risk.evaluate`). The inline path is **synchronous inside `scan_signals.py`**, which runs every minute — so total latency matters.

`helm.llm.decide()` abstracts transport: `LLM_MODE=cli` (Claude Code subscription, the **POC default** per `WORKAROUNDS.md`) or `LLM_MODE=api` (Anthropic SDK, per-token billing). **This distinction is central to the cost analysis below.**

## 5. Proposed design — three tiers (pick one to ship first)

All tiers reuse `helm.llm` and emit the **same** final `{verdict, confidence, reasoning}`. They differ in how many passes precede it.

### Tier 1 — Self-critique (1 call, cheapest)
Single call, but the system prompt instructs the model to internally state the bull case, then the strongest skeptic rebuttal, then resolve. Output adds a `dissent` field summarising the rejected side. **No extra calls, no latency hit** beyond a longer completion. Effectively a prompt rewrite.

### Tier 2 — Two-pass bull + skeptic (2 calls) — **recommended first ship**
- **Pass A (Bull):** "Make the strongest case to TAKE this signal." → bull thesis + entry/stop rationale.
- **Pass B (Skeptic/Judge):** given the signal context **and** the bull thesis, "Poke holes, then deliver the final verdict." → `{verdict, confidence, reasoning, dissent}`.
- Cache the shared system + context prefix across both passes so only the role instruction differs.

### Tier 3 — Full debate (3–4 calls)
Bull, Bear, Risk-manager passes → a Judge pass that reads all three and rules. Closest to TradingAgents. Highest latency; reserve for if Tier 2 shows a real lift and we want to push further.

## 6. Cost & latency analysis (the make-or-break)

Per the cost-minimization mandate, this is the gating section.

| Mode | Tier 1 | Tier 2 | Tier 3 |
|---|---|---|---|
| **CLI (POC, subscription-flat)** | ~no Δ cost; slightly longer call | **no extra $**, +1 call latency (~+5–10s) | no extra $, +2–3 calls latency (~+15–30s) |
| **API (per-token)** | ~1.1× tokens | ~2× calls (caching softens prefix cost) | ~3–4× calls |

Key insight: **in POC CLI mode the dollar cost of extra passes is ~zero** — the real budget being spent is **inline latency**. `scan_signals.py` decides synchronously every minute; Tier 2 pushes a decision from ~5–10s toward ~15–20s. That's still well under the 1-min cron cycle for the expected handful of signals/day, but if multiple signals fire in one scan, debate should fall back to the async catch-up path (`decide_signals.py` cron) rather than blocking the scan.

**Mitigation:** ship Tier 2 **only in the inline path when ≤1 new signal is pending**; otherwise enqueue for the 2-min catch-up cron. Keep Tier 3 API-only-off until Tier 2 earns it.

## 7. Integration & data model

- **Flag:** add `settings` key `decider_mode ∈ {single, selfcritique, debate2, debate3}`, default `single`. Read via the existing settings idiom (same pattern as `autonomy_paused`). Lets us flip modes without redeploy and lets the A/B harness alternate.
- **`decisions` table:** no schema change required for verdict. Persist the bull thesis / dissent / transcript to `audit` via `insert_audit(actor="decider", event="debate", detail={...})` so the dashboard can show *why* without widening the hot table. (Optional later: a `decisions.deliberation JSONB` column if we want it queryable.)
- **`helm.llm`:** add a thin `debate()` helper or just orchestrate N× `complete_json` in `decide_signals.py`. No new transport.
- **Risk gate unchanged:** `execute_signal` still runs `risk.evaluate` as the final hard guard regardless of how the verdict was reached.

## 8. Experiment / rollout plan

1. Land behind `decider_mode=single` default — zero behavior change in prod.
2. **A/B by alternating mode per signal** (e.g. odd/even signal id, or date-based) for N trading days, tagging each `decisions` row's actor with the mode used.
3. Compare on the metrics already computed by the goal brief / `metrics_snapshots`: win rate, expectancy, take rate, and BAD_CALL retro count, segmented by mode.
4. **Decision rule:** keep debate only if it lifts expectancy OR cuts BAD_CALL retros without crushing take rate. Otherwise revert to `single` and close the proposal `superseded`.

## 9. Success metrics

- Primary: **expectancy per TAKE** (₹/trade) over the experiment window, debate vs. single.
- Secondary: BAD_CALL SKIP/TRADE retro rate ↓; take rate stays within ±10% of baseline (we don't want debate to make the bot timid).
- Guardrail: inline decision p95 latency < 30s; no missed signals due to scan blocking.

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Debate makes the bot **more timid** → take rate collapses | Judge prompt inherits the existing "take more, not fewer" asymmetry (principle 5 in current system prompt); monitor take rate as a guardrail metric |
| Inline latency blocks `scan_signals` | Cap debate to ≤1 pending signal inline; else defer to catch-up cron |
| API-mode token cost balloons | Default off in API mode; CLI-only until proven; prompt-cache the shared prefix |
| Added complexity for marginal lift | Tier 1 (prompt-only) is the cheap probe; only escalate to Tier 2/3 on evidence |
| Look-ahead / leakage (the AI-Trader sin) | No design change to data; debate sees the **same** context the single decider sees — no future bars |

## 11. Open questions

- Do we A/B by signal-id parity (clean attribution) or by day (avoids same-day regime confounds)? Leaning parity.
- Is Tier 1 (self-critique) enough? It may capture 80% of the benefit at 0% extra latency — worth running *first* as a one-line prompt experiment before building Tier 2 plumbing.
- Persist full transcript to `audit` always, or only on TAKE? (Audit volume vs. debuggability.)

## 12. Mapping to the self-improvement loop

This PRD decomposes into engineer-surface tasks:
- **Tier 1** → `task_type: prompt_tweak` (rewrite `SYSTEM_PROMPT`, add `dissent` field). One PR, fully reversible — the "ship the cheap reversible thing first" PM principle.
- **Tier 2/3** → `task_type: needs_human` initially (new multi-pass control flow + settings flag is beyond the prompt/param mutator surface), then a scoped engineering task once approved.

**Recommended path:** file Tier 1 as an `improvement_proposals` row (category `decider_prompt`), run it as a 1-line A/B, and only build Tier 2 if Tier 1 shows signal.
