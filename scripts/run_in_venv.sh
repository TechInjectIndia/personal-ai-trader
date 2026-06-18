#!/bin/bash
# Cron wrapper — activates venv and runs the given Python module/script.
# All cron entries call this, so we have one place to manage env + concurrency.
set -euo pipefail
cd /home/ubuntu/work/personal-ai-trader
# ~/.local/bin holds the `claude` CLI, which the decider shells out to in
# LLM_MODE=cli. ~/.npm-global/bin holds the competition league CLIs (gemini,
# opencode). Cron's default PATH (/usr/bin:/bin) won't find either otherwise.
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
source .venv/bin/activate

# --- Per-job overlap guard -------------------------------------------------
# The trading loop now runs every minute 24/7 (crypto) and the learning agents
# shell out to builds/LLMs. A job that hits tail latency (a yfinance hang, an
# LLM backlog, a slow build) must NOT have the next cron tick stack a second
# copy on top of it — that pile-up is what would delay a stop/target exit or
# have two runners race. We take a NON-BLOCKING lock keyed on the script name,
# so each job only serialises against ITSELF: poll/scan/manage/decide still run
# concurrently within a minute (different locks), but a still-running instance
# makes the next tick skip cleanly instead of doubling up.
LOCK_DIR="/tmp/helm-locks"
mkdir -p "$LOCK_DIR"
_lockname="$(basename "${1:-job}" .py)"
_lock="${LOCK_DIR}/${_lockname}.lock"

# --- CPU/IO priority -------------------------------------------------------
# Trading-critical jobs (price polling, scanning, position management, the
# decider, the competition execution loop, the Kite-token jobs) run at NORMAL
# priority so a live stop/target exit is never starved. Everything else — the
# learning agents (PM/Engineer/Tester, retros), backtests, and the Context
# Engine ingest, which can be CPU/IO-heavy and now overlap the 24/7 crypto and
# US sessions — runs de-prioritised (nice + best-effort-low IO) so it yields to
# trading. Unquoted on purpose: this is our own controlled string, and it must
# word-split into the command (empty = no prefix for trading jobs).
case " poll_market scan_signals manage_positions decide_signals poll_competition run_competitors kite_auto_login check_kite_token " in
    *" ${_lockname} "*) PRIO="" ;;
    *)                  PRIO="nice -n 10 ionice -c2 -n7" ;;
esac

set +e
# shellcheck disable=SC2086  # $PRIO must word-split into the command
flock -n -E 99 "$_lock" $PRIO python "$@"
rc=$?
set -e
if [ "$rc" -eq 99 ]; then
    # Previous run still active — skip this tick (exit 0 so cron sees a clean
    # no-op, not a failure). Leave a breadcrumb so pile-ups are visible.
    echo "[$(date -u +%FT%TZ)] skip: ${_lockname} — previous run still active"
    exit 0
fi
exit "$rc"
