# Workarounds — POC tech-debt ledger

We are deliberately taking shortcuts during the research/POC phase to keep
recurring costs at or near zero. Each entry below describes a workaround
that is **knowingly non-best-practice** and how to undo it once the bot has
proven itself.

The point of this file is to make sure these don't quietly become permanent.
When swapping a workaround out, delete the entry — don't leave history rot.

---

## 1. LLM decider runs through `claude -p` CLI on subscription auth

**Where:** `helm/llm.py` `_decide_cli()`, called from `scripts/decide_signals.py`.
Switched on by `LLM_MODE=cli` in `.env` (the current POC default).

**Why we did it:** the user already pays for a Claude Code subscription. Routing
the decider through the CLI uses that quota instead of paying API tokens
out-of-pocket while we're still proving signal quality. With ~165 decisions
per trading day on Sonnet-4.6, the imputed token cost would be a few USD/day —
small in absolute terms, but unjustified before the bot has demonstrated edge.

**What we sacrificed:**

- **Prompt-cache control.** SDK mode pins `cache_control: ephemeral` on the
  system message; CLI mode has no equivalent knob. Smoke test showed
  ~13.5k cache-creation tokens per call from Claude Code's harness loading
  even with `--system-prompt` overriding the conversational portion.
- **Subscription rate-limit risk.** Max plan limits are sized for human dev
  work; intraday cron at every-2-minutes is automated load. If we hit caps
  mid-session the bot silently errors and signals stay unconsumed.
- **Latency.** CLI startup adds ~1–2s vs a direct SDK call. Fine at our
  cadence, but bad if we ever need sub-second decisions.
- **Output reliability.** Despite `--system-prompt` instructing JSON-only,
  the model still wraps the result in ```json fences. Handled today by the
  tolerant `_parse_verdict` parser, but it's a surface that can break.
- **ToS gray area.** Claude Code is positioned as a developer assistant.
  Using it as a programmatic LLM backend for an automated trading bot is
  off-label use. Not enforced today, but worth noting.
- **Observability gaps.** No native Anthropic usage dashboard for these
  calls; we only see local audit rows.

**How to undo (post-POC):**

1. Set `LLM_MODE=api` and `ANTHROPIC_API_KEY=sk-ant-…` in `.env`.
2. Restart cron / pm2 — the CLI path is dead code at that point.
3. (Optional) Delete `_decide_cli()` and the `claude` CLI dependency entirely
   from `helm/llm.py` once we're confident we won't toggle back.
4. Remove the `~/.local/bin` PATH export from `scripts/run_in_venv.sh`.
5. Delete this entry.

**Removal trigger:** when paper-trading shows positive expectancy over a
multi-week sample, OR when subscription rate limits start blocking decisions.
