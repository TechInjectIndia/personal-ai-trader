# Helm — Profitability Sprint Plan

_Owner: PM (Claude). Created 2026-06-08. Status: active._
_North star: move Helm from −₹2,622 net to **positive net expectancy per trade**._

---

## 1. Why we are not profitable (the unit economics)

Real money, 189 closed trades (test rows excluded):

| Metric | Value | Read |
|---|---|---|
| **Gross** P&L (pre-cost) | **−₹160** | entries are ≈ a coin flip |
| **Transaction costs** | **−₹2,463** (₹13.2/trade) | realistic Zerodha intraday charges |
| **Net** P&L | **−₹2,622** | ≈ entirely the costs |
| Avg \|gross move\| captured | ₹30.9/trade | costs = **43%** of the move |
| Avg notional | ₹12,322 | sizing is fine; move size is not |
| Win rate / gross payoff / net payoff | 27% / ~2.3:1 / ~1.1:1 | **costs collapse the payoff** |

**The mechanism:** at a 27% win rate, profitability needs a *net* payoff > 2.7:1. The book earns ~2.3:1 **gross** — nearly there — but ₹13 of cost on a ₹31 move crushes it to ~1.1:1 net. Trading 189 times multiplies the per-trade loss.

**The reframe that governs this whole plan:** stop asking _"which strategy wins more often"_ and start managing _**expected move per trade ÷ cost per trade**_. Today that ratio is ~2.3; we need it ≥ 3–4, AND we need at least slightly positive gross edge. 165/189 trades are the freestyle **league**; the house strategy book is ~24 trades. The cost pathology is identical in both.

---

## 2. Success metrics

**North-star:** `net_expectancy_per_trade = avg(net_pnl_inr) > 0` over a trailing 30-trade window per book.

**Driver metrics (these are what we actually move):**
- **Edge-to-cost ratio** `E2C = (target − entry) / expected_round_trip_cost` ≥ 3 at entry; realised `|gross move| / cost` ≥ 3 at exit.
- **Gross expectancy/trade** > 0 (proves a real edge exists before scaling).
- **Trades/day** ↓ (fewer, bigger) — a *lower* number is better here.
- **Cost drag %** = `total_charges / |gross_pnl|` trending down.

**Guardrails (must not regress):** daily-loss kill switch intact; max open positions; no live-bot breakage; league autonomy preserved (no house policy imposed on competitors).

---

## 3. Operating cadence — **decide faster (SHIPPED 2026-06-08)**

The learning loop no longer waits for Sunday. It now runs **post-close every trading day (Mon–Fri)**:

| IST | Step | Cron (UTC) |
|---|---|---|
| 15:30–16:15 | Retrospectives (all agents, today's closed trades) | `*/15 10 * * 1-5` |
| 16:30 | PM review → tasks (per agent) | `0 11 * * 1-5` |
| 16:30–18:20 | Engineer drains task queue | `*/10 11-12 * * 1-5` |
| 16:35–19:25 | Tester verifies/reverts releases | `5-55/10 11-13 * * 1-5` |
| 19:00 | Daily loop report → `logs/weekend-reports/<date>.md` | `30 13 * * 1-5` |

Weekly (Sunday) retains only: competition **mandate revision** (`plan_mandates --next-week`) and a **cumulative week-in-review** (`weekend_report --weekly`). Edits land overnight while the market is shut and are Tester-verified before the next open — same safety as the old weekend cycle, 5× the iteration rate. **Cost note:** more LLM calls/week; retro ticks are capped to 4/day to bound spend. Pause anytime via `settings.autonomy_paused`.

---

## 4. Roadmap — three phases

| Phase | Theme | Features | Exit gate |
|---|---|---|---|
| **1 — Measure truth & decide faster** (this week) | See the cost killer; stop the cheap bleed | [F1](../frd/F1-cost-gross-analytics.md) Cost/gross analytics · [F2](../frd/F2-cost-aware-min-edge-gate.md) Min-edge gate · [F3](../frd/F3-daily-postclose-cadence.md) Daily cadence ✅ | Gross & net expectancy visible per strategy/agent; min-edge gate live; sub-3× E2C trades no longer opened |
| **2 — Fix the cost ratio** (next 2 weeks) | Fewer, bigger trades so costs stop collapsing the payoff | [F4](../frd/F4-bigger-move-reframe.md) Bigger-move reframe · [F5](../frd/F5-conviction-weighted-sizing.md) Conviction sizing · [F6](../frd/F6-multi-timeframe-ab.md) Multi-timeframe A/B | Realised E2C ≥ 3 on the house book; net expectancy ≥ 0 over a 30-trade window |
| **3 — Create real edge** (1–2 months) | Information the chart lacks | [F7](../frd/F7-context-engine.md) Context Engine · [F8](../frd/F8-league-gross-expectancy-feedback.md) League gross-expectancy feedback | A strategy/agent shows **positive gross expectancy out-of-sample** with E2C ≥ 3 |

**Sequencing logic:** F1 is the prerequisite for everything (you can't manage what you can't see). F2 stops the worst bleed immediately and is the cheapest win. F4–F6 restore the payoff ratio that costs are eating. F7 is the only genuine *edge* bet — everything before it is risk/cost management on a coin flip, necessary but not sufficient. F8 keeps the league honest and turns it into an idea engine.

---

## 5. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Min-edge gate starves the book of trades | Med | Threshold tunable in `config.py`; monitor trades/day; gate only blocks sub-K× setups |
| Daily engineer cadence ships a regression overnight | Med | Tester verifies/reverts pre-open; pytest gate; no market-hours edits |
| Bigger targets → lower hit rate, longer holds risk EOD square-off | Med | Pair with #681 EOD-tighten; cap hold time; A/B before trusting defaults |
| Context Engine cost/complexity (news APIs) | High | Start with free/cheap sources; black-box behind an interface (per WORKAROUNDS ethos) |
| Imposing house logic on the league (autonomy breach) | Low | All cost/edge policy is house-scoped or fed as *advice* to agent loops, never forced |
| More LLM spend from daily cadence | Med | Cap retro ticks; PM defers when no new trades; monitor token cost in `agent_runs` |

---

## 6. FRD index
- [F1 — Cost & gross-expectancy analytics](../frd/F1-cost-gross-analytics.md) ✅ shipped (1d35c77)
- [F2 — Cost-aware minimum-edge gate](../frd/F2-cost-aware-min-edge-gate.md) ✅ shipped (9bd8031)
- [F3 — Daily post-close cadence](../frd/F3-daily-postclose-cadence.md) ✅ shipped
- [F4 — Bigger-move strategy reframe](../frd/F4-bigger-move-reframe.md)
- [F5 — Conviction-weighted sizing](../frd/F5-conviction-weighted-sizing.md)
- [F6 — Multi-timeframe A/B](../frd/F6-multi-timeframe-ab.md)
- [F7 — Context Engine](../frd/F7-context-engine.md)
- [F8 — League gross-expectancy feedback](../frd/F8-league-gross-expectancy-feedback.md)
