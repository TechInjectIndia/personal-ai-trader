#!/bin/bash
# Start the Context Engine FastAPI service on 127.0.0.1:8601.
# Activates the repo venv and runs uvicorn with the package on PYTHONPATH
# (cwd = repo root makes the top-level `context_engine` package importable,
# exactly like `helm`). Mirrors scripts/run_in_venv.sh's env setup.
set -euo pipefail
cd /home/ubuntu/work/personal-ai-trader
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
source .venv/bin/activate
exec uvicorn context_engine.app:app \
    --host 127.0.0.1 \
    --port 8601 \
    --workers 1
