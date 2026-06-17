```
PORTFOLIO ENTRY

Title: Helm — Intraday Trading Bot With Claude as the Decision Layer

Cover image: 16:9 architecture diagram. Five cron jobs on the left feeding one Postgres cylinder in the center; a "Claude Sonnet 4.6" box drawn twice (once on the decide path, once on the retrospective path) so the LLM is visibly the same component used at both ends; a Streamlit dashboard on top reading from Postgres. Light background, "Helm" wordmark top-left.

Problem:
A pure rule-based trading bot is blind. A signal fires, the bot trades, no second look. That works until the market does something the rule didn't anticipate and you lose money on a setup any human would have skipped. And once a decision has been logged, the operator has no easy way to audit *why* it was made or what to learn from it the next day. I wanted to keep the mechanical signal layer (fast, deterministic, cheap), bolt an LLM onto the front as the final judge on whether each idea is actually worth acting on, and bolt a second LLM onto the back to grade every decision in plain English so a non-engineer can read the day's tape and learn from it.

Solution:
Helm is a five-cron, one-Postgres, one-dashboard system, live in production at helm.techinject.co.in.

1. A minute poller samples prices for five NSE large-caps (RELIANCE, HDFCBANK, ICICIBANK, INFY, TCS) and rolls them into 1-min OHLC bars.
2. Three strategies (Opening Range Breakout, VWAP reclaim, gap-fade) scan candles every five minutes and write mechanical signals to Postgres. Each strategy only fires on the triggering bar, so re-running the scan never re-emits the same idea.
3. Every two minutes a worker picks up each unconsumed signal, builds a context blob (recent 30 bars, today's open positions, current risk state, today's prior decisions on the same symbol) and asks Claude Sonnet 4.6 for TAKE or SKIP plus one-line reasoning. The verdict is written to a `decisions` row regardless of outcome, so every SKIP is auditable.
4. On TAKE, the executor books a paper trade behind a hard risk gate: max open positions, daily-loss kill, per-symbol cooldown, max signals per symbol per day, available wallet cash. Caps are live-editable from the dashboard with no PM2 restart.
5. After a trade closes — or after a SKIP can be judged against the rest of the day's tape — a second Claude call writes a plain-English retrospective: why we acted, what the price did, whether it was a GOOD_CALL, BAD_CALL, LUCKY, UNLUCKY, or MIXED outcome, plus one to three concrete learnings and three quality scores (signal, decision, execution). SKIPs are graded against a counterfactual replay engine that walks the day's candles forward, treats the proposed entry as a touch-fill, and reports what the trade would have done.
6. A six-page Streamlit dashboard (How It Works, Summary, Activity Log, Charts, Settings, Retrospectives) renders the whole database back to the operator.

The trust signals that make it production-grade and not a tutorial: a `consumed` flag flipped inside the same transaction as the decision row so the decider cron can never double-act; a Postgres advisory lock on the retro cron so an overshooting run cannot race the next firing and burn LLM quota on items the other is already doing; unique indexes on the retrospectives table that are idempotent on `trade_id` for trade reviews and on `decision_id` for skip reviews; an audit table that captures every state change; a tolerant JSON parser for the days the model wraps its reply in markdown anyway; and a 17-word banned-jargon list in the retro system prompt that forces the LLM to translate "ORB", "VWAP reclaim", "R:R" into language a non-trader can actually read. LLM transport itself is hidden behind a single function with two interchangeable backends — Anthropic SDK direct for production, Claude Code CLI for POC cost-savings — selected by an env var, so the rest of the codebase never branches on it.

Result:
Runs unattended weekdays through the NSE session and silently no-ops outside the window. Deployed behind PM2, nginx, and Let's Encrypt at helm.techinject.co.in on a single Linux box. Eight-table Postgres schema, around 3,500 lines of Python, no background workers, no message queue, no Redis. The reusable piece is the pattern, not the trading code: mechanical signals propose candidates, an LLM gates the action, a hard validator has the final word, and a second LLM grades the loop in plain English so a non-engineer can audit every decision after the fact. Same scaffold drops cleanly into lead qualification, support ticket triage, content moderation, document review — anywhere a stream of automated decisions needs both pre-action judgment and post-action explanation.

Stack: Claude Sonnet 4.6 (Anthropic API), Python 3.11, PostgreSQL, Streamlit, cron, PM2, nginx, Zerodha Kite Connect.

Live demo: helm.techinject.co.in (operator login).
Repo: Private, code walkthrough on request.
```

```
IMAGE ASSETS

1. case-study-assets/cover.png
   What: 16:9 architecture diagram. Five cron boxes on the left (poll, scan, decide, manage, retro) feeding into a Postgres cylinder in the center. A "Claude Sonnet 4.6" box sits between the decide cron and Postgres, and again between the retro cron and Postgres, drawn the same way both times so the LLM is visibly one component used twice. A Streamlit dashboard sits on top of Postgres reading from it. Light background, "Helm" wordmark top-left.
   Tool: Excalidraw. 20 minutes.

2. case-study-assets/architecture.png
   What: Detailed version of the cover. Same layout, every arrow labeled with the payload it carries: "tick", "1-min OHLC", "signal row", "context JSON (30 bars + risk state)", "TAKE/SKIP JSON", "paper_trade row", "closed trade event", "retro JSON". A small "risk gate" diamond between the decider and the paper_trades cylinder makes the validator visibly part of the flow.
   Tool: Excalidraw. 40 minutes.

3. case-study-assets/screenshot-dashboard-summary.png
   What: The dashboard Summary page mid-session. Crop to the wallet card, today's open positions table, and today's P&L sparkline. Redact symbol-level exact prices if desired; keep the layout.
   Tool: Real screenshot from helm.techinject.co.in.

4. case-study-assets/sample-output-retro.png
   What: One closed-trade retrospective rendered in the dashboard's Retrospectives page. Must show the verdict label badge (e.g. UNLUCKY), the layman summary, the three score bars (signal / decision / execution), and the learnings list. Single most persuasive asset — the buyer literally sees what AI-generated output from this system looks like. Pick a retro with a non-obvious verdict (UNLUCKY or LUCKY) so the model is visibly doing judgment work, not pattern-matching P&L sign.
   Tool: Real screenshot.

5. case-study-assets/code-retro-prompt.png
   What: The retro system prompt from helm/retro.py lines 84–146 — the language constraints with the 17-word banned-jargon list and the five-way verdict taxonomy. Proves engineering judgment: this is a prompt with a real rubric, not a generic "review this trade" string.
   Tool: carbon.now.sh. Dark theme (One Dark), window controls off, no shadow, no background gradient.
```
