#!/bin/bash
# Cron wrapper — activates venv and runs the given Python module/script.
# All cron entries call this, so we have one place to manage env.
set -euo pipefail
cd /home/ubuntu/work/personal-ai-trader
# ~/.local/bin holds the `claude` CLI, which the decider shells out to in
# LLM_MODE=cli. Cron's default PATH (/usr/bin:/bin) won't find it otherwise.
export PATH="$HOME/.local/bin:$PATH"
source .venv/bin/activate
exec python "$@"
