# Helm — personal investment automation
# Common dev tasks. All targets assume you're in the repo root with a venv active.

.PHONY: help install configure verify dry-run-zerodha dry-run-ibkr dry-run-concurrent dry-run-killswitch dry-run-all backtest dashboard test lint clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-22s\033[0m %s\n", $$1, $$2}'

install:  ## Create venv and install deps
	python3 -m venv .venv
	. .venv/bin/activate && pip install --upgrade pip && pip install -e ".[dev]"
	@echo "Done. Activate with: source .venv/bin/activate"

configure:  ## Walk through credential entry into OS keychain
	. .venv/bin/activate && python -m helm.orchestrator.cli configure

verify:  ## Ping Kite + IBKR; print account summary
	. .venv/bin/activate && python -m helm.orchestrator.cli verify

# --- Phase 1 dry run gates ---
dry-run-zerodha:  ## G1 — place 1 NIFTYBEES via pykiteconnect
	. .venv/bin/activate && python scripts/dry_run_zerodha.py

dry-run-ibkr:  ## G2 — place 1 VOO via ib_insync (paper)
	. .venv/bin/activate && python scripts/dry_run_ibkr.py

dry-run-concurrent:  ## G3 — both adapters in one process
	. .venv/bin/activate && python scripts/dry_run_concurrent.py

dry-run-killswitch:  ## G4 — place + cancel limit orders on both
	. .venv/bin/activate && python scripts/dry_run_killswitch.py

dry-run-all: dry-run-zerodha dry-run-ibkr dry-run-concurrent dry-run-killswitch  ## Run G1–G4 in order

# --- Operations ---
backtest:  ## Run backtests for all active strategies
	. .venv/bin/activate && python -m helm.backtest.runner --all

dashboard:  ## Start the local Streamlit dashboard
	. .venv/bin/activate && streamlit run helm/dashboard/app.py

# --- Quality ---
test:  ## Run pytest with coverage
	. .venv/bin/activate && pytest --cov=helm --cov-report=term-missing

lint:  ## Run ruff + mypy
	. .venv/bin/activate && ruff check helm tests && mypy helm

clean:  ## Remove venv, build artefacts, caches
	rm -rf .venv build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
