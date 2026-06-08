# FRD F8 — League Gross-Expectancy Feedback

_Phase 3 · Owner: PM · Status: proposed · Depends on: F1 · Constraint: league autonomy (do NOT impose house policy)_

## 1. Problem
The 5 freestyle competitors are 165/189 trades and most of the loss, with the same cost pathology. But the league's premise is that **each agent manages its own risk** and is judged on it — so we must not force the house cost/edge rules on them. Today their loops optimize net P&L **blind to the gross/cost split**, so they can't even see why they lose.

## 2. Goal & success metrics
Give each competitor's own loop the **visibility** to self-correct, without enforcement.
- Driver: each agent's retro/PM inputs include its own gross expectancy, cost drag, E2C, payoff (per F1).
- Outcome: agents' own proposals start targeting move-size/cost (observable in `improvement_proposals`); leaderboard ranks by net **and** gross expectancy.
- Guardrail: zero house-authored stops/sizing applied to competitor trades (verified — #681's `_is_house` gate already enforces this for stops).

## 3. HLD
Three additive, advice-only surfaces — all read-only w.r.t. competitor decision-making:
```
F1 economics(per competitor_id) ─► (a) retro/PM prompt context per agent
                                  ─► (b) leaderboard columns (net + gross expectancy + E2C)
                                  ─► (c) mandate generator: SUGGEST bigger-move framing (never force)
```
The agent's own PM still accepts/rejects its own proposals (per `feedback_agents_decide_autonomously`). We change what the agent can *see*, not what it must *do*.

## 4. LLD
**(a) Per-agent economics in the loop inputs** — in `helm/agents/pm.py` and the retro input builder, when `competitor_id` is set, inject `economics.book_economics(competitor_filter=cid, last_n=30)` (from F1) into the prompt context block: `"Your last 30 trades: gross +/-X/trade, cost drag Y%, E2C Z, net W/trade."` This is the whole lever — the agent now sees costs are the killer.

**(b) Leaderboard** — `helm/competition/leaderboard.py` + dashboard `8_Competition_League.py`: add `gross_expectancy`, `cost_drag_pct`, `realised_e2c` columns alongside net. Reuse `book_economics(group_by='competitor_id')`.

**(c) Mandate suggestions** — `scripts/plan_mandates.py`: when an agent's E2C < 3 over the prior week, the generated mandate **prose** includes an advisory ("recent trades captured moves too small to clear costs; consider higher-conviction, larger-move setups"). It is text guidance the agent may heed or ignore — not a hard cap.

**Non-goals (explicit):** no `MIN_EDGE_TO_COST`/`MIN_TARGET_PCT`/conviction-sizing enforcement on competitor trades; no editing competitor stops. Those remain house-only (F2/F4/F5).

## 5. Test plan
`tests/test_league_feedback.py`: economics block rendered per competitor_id and excluded for house view; leaderboard query returns the new columns; mandate advisory text appears only when E2C<3. Assert no code path mutates competitor stops/sizing (regression guard alongside #681's `_is_house`).

## 6. Rollout / revert
Pure additive (inputs + display + advisory text). Ship incrementally. Revert = remove the context block/columns/advisory. No effect on live trading mechanics.

## 7. Risks
Agents may ignore the advice (acceptable — that's autonomy; it becomes a finding about that agent). Risk of *implicitly* steering all agents to the same behavior via identical advice → keep advice generic and let each agent's strategy differ. The experiment's value is preserved precisely because we inform rather than enforce.
