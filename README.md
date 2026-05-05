# Helm

Personal investment automation across Zerodha and Interactive Brokers.

A single-user Python service that runs systematic strategies on cash equity and ETFs, gated by portfolio-wide and sleeve-aware risk limits, with a kill-switch you can hit from the dashboard or the command line.

See `Helm_PRD_v1.3.docx` in the parent folder for the full spec. This README is a shorter operator's guide.

---

## Architecture in one paragraph

Helm is a single Python process. It hosts the strategies, runs backtests against locally cached OHLCV using [`backtrader`](https://www.backtrader.com/), and places live orders directly via [`pykiteconnect`](https://github.com/zerodha/pykiteconnect) (Zerodha) and [`ib_insync`](https://ib-insync.readthedocs.io/) (IBKR). A portfolio-wide and sleeve-aware risk module gates every order pre-trade and watches exposures post-trade. A kill-switch and an intraday-sleeve halt provide defense-in-depth controls. A local Streamlit dashboard is the user-facing surface.

No external trading-platform vendor. The only recurring infra cost is the Zerodha Kite Connect API fee (₹2,000/month).

## Two sleeves of capital

| Sleeve | Capital | Horizon | Product | Strategies |
|--------|---------|---------|---------|------------|
| A — Passive Core | 85–90% | months to decades | CNC | Core SIP IN/US, Drift Rebalance, Quality Screen, Momentum Tilt, Mean-Reversion Filter |
| B — Intraday Lab | 10–15% (default 12.5%) | hours (always intraday) | MIS | Opening-Range Breakout, Intraday Momentum |

Sleeve B is treated as a contained experiment with written success and retirement criteria; auto-retired at 6-month review if criteria fail.

---

## Repo layout

```
helm/
├── pyproject.toml             # Package config, deps, dev tools
├── Makefile                   # Common dev tasks
├── README.md                  # This file
├── .gitignore
│
├── helm/                      # The Python package
│   ├── orchestrator/          # The orchestrator core
│   │   ├── allocator.py       # Sleeve-aware target-weight allocator
│   │   ├── drift_detector.py  # Passive-sleeve drift monitor
│   │   ├── risk.py            # Portfolio + sleeve hard limits (pre + post-trade)
│   │   ├── intraday_gate.py   # Pre-market intraday arming
│   │   ├── sleeve_halt.py     # Mid-session intraday breach handler
│   │   ├── kill_switch.py     # Full-system halt + cross-broker order cancel
│   │   ├── audit.py           # Append-only event log (SQLite)
│   │   ├── notify.py          # Email + Telegram dispatch
│   │   └── cli.py             # `helm <command>` CLI
│   │
│   ├── brokers/               # Broker adapters
│   │   ├── zerodha.py         # Wraps pykiteconnect (CNC + MIS)
│   │   └── ibkr.py            # Wraps ib_insync
│   │
│   ├── strategies/            # Strategy registry
│   │   ├── base.py            # Strategy interface
│   │   ├── passive/           # Sleeve A strategies
│   │   └── intraday/          # Sleeve B strategies
│   │
│   ├── backtest/              # backtrader integration
│   │   └── runner.py          # Loads cached OHLCV, runs strategy, emits HTML report
│   │
│   └── data/                  # Market data layer (cached to DuckDB)
│       ├── kite_history.py    # Kite Connect /historical/ client
│       └── yfinance_history.py
│
├── scripts/                   # Phase 1 dry-run gates G1–G4
│   ├── kite_login.py          # Daily access-token refresh helper
│   ├── dry_run_zerodha.py     # G1 — pykiteconnect places 1 NIFTYBEES
│   ├── dry_run_ibkr.py        # G2 — ib_insync places 1 VOO on paper
│   ├── dry_run_concurrent.py  # G3 — both adapters in one process
│   └── dry_run_killswitch.py  # G4 — place + cancel limit orders both sides
│
└── tests/                     # pytest suite (risk module gets ≥80% coverage)
```

---

## Phase 1 dry run — gate by gate

| Gate | What it proves | Run |
|------|----------------|-----|
| G1   | `pykiteconnect` can place a real order on Zerodha | `make dry-run-zerodha` |
| G2   | `ib_insync` can place a real order on IBKR paper | `make dry-run-ibkr` |
| G3   | Both adapters work in one process | `make dry-run-concurrent` |
| G4   | Kill-switch path (place + cancel) works programmatically | `make dry-run-killswitch` |

Full step-by-step instructions are in `Helm_Dry_Run_v2.docx` in the parent folder.

---

## Quick start

```bash
make install          # create venv, install deps
# add your secrets (interactive prompt; stored in OS keychain)
make configure
# verify Kite + IBKR connectivity
make verify
# run the four dry-run gates
make dry-run-all
```

## After all four gates pass

Phase 2 begins — populate the empty modules in `helm/orchestrator/`, `helm/strategies/passive/`, and `helm/backtest/`. The dry-run scripts in `scripts/` are the seeds: they already implement the broker adapter calls we need.

---

## Status (May 2026)

- PRD: v1.3
- Phase: 1 (dry run)
- Real capital deployed: 0 (intentional — none until G1+G2+G4 pass on the relevant broker)
# personal-ai-trader
