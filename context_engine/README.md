# Context Engine

Standalone FastAPI microservice that gives the trading bot an external-context
("news conviction") layer. Two processes, **one contract: HTTP**. The bot never
imports this package; it reaches the service only over `localhost:8601`,
**fail-open and flag-gated**, so with the flag off there is zero behavioral or
cost delta.

```
  ingest_context.py (cron) ─┐
                            ├─► run_ingest()  fetch → upsert → LLM-score
  POST /refresh ────────────┘                (context_items, context_scores)

  GET /context/{symbol}  ──►  latest score × time-decay  ──►  {score, stale, ...}
                                       ▲
  bot: helm/context_client.py (stdlib urllib, fail-open) ──┘  (only when CONTEXT_ENGINE_URL set)
```

## Architecture

- **Service** (`context_engine/`): owns ingestion, LLM scoring, persistence, and
  the read API. Runs as its own PM2 process on `127.0.0.1:8601`. MAY import
  `helm.llm` and `helm.data.store` (service → helm is the allowed one-way import;
  shares `dbname=helm` over the local socket). NEVER imported by the bot loop.
- **Bot consumer** (`helm/context_client.py`): a tiny stdlib-`urllib` client
  called from `scripts/decide_signals.py`. Localhost HTTP only, short timeout,
  fail-open.

## Storage (additive, idempotent)

`context_engine/schema_context.sql` — `context_items` (deduped on
`UNIQUE(symbol, url)`) and `context_scores` (`score NUMERIC(4,3)` in
`[-1.000, +1.000]`, `half_life_min`, rationale). The service runs this on
startup. The bot reads **nothing** from these tables directly — only via HTTP.

## API

All endpoints localhost-only. JSON over HTTP.

- `GET /health` → `{"status":"ok","service":"context_engine","db":true}` (always
  200; `db=false` if `SELECT 1` fails — observability only).
- `GET /context/{symbol}` → 200 with the decayed-conviction body (404
  `{"detail":"unknown symbol"}` if the symbol isn't in `WATCHLIST`). Symbol is
  upper-cased server-side. Body:
  ```json
  {"symbol":"RELIANCE","score":-0.412,"rationale":"...",
   "as_of":"2026-06-08T11:32:14+05:30","half_life_min":90,"stale":false}
  ```
  When `stale=true`, `score=0.0` and `rationale=""` — functionally identical to
  the flag being off, so the bot treats it as "no context".
- `POST /refresh` → 200. Optional body `{"symbols":["INFY","TCS"],"force":false}`.
  No-op (`skipped_window=true`) outside the IST market window / on weekends
  unless `force=true`.

Decayed score: `current = score * 0.5 ** (age_min / half_life_min)`. Stale when
no row, age > `CONTEXT_STALENESS_MIN`, or decayed magnitude < `CONTEXT_FRESHNESS_FLOOR`.

## Run it

Install deps into the shared repo venv (once):

```bash
.venv/bin/pip install -r context_engine/requirements.txt
```

Start the service:

```bash
# convenience wrapper (activates venv, sets cwd/PATH)
./context_engine/run.sh

# or directly
source .venv/bin/activate
uvicorn context_engine.app:app --host 127.0.0.1 --port 8601 --workers 1

# under PM2 (second app in ecosystem.config.js)
pm2 start ecosystem.config.js --only helm-context-engine
pm2 save
```

Smoke-test:

```bash
curl -s 127.0.0.1:8601/health
curl -s 127.0.0.1:8601/context/RELIANCE
curl -s -XPOST 127.0.0.1:8601/refresh -H 'content-type: application/json' \
     -d '{"symbols":["INFY"],"force":true}'
```

Ingest cron (manual one-shot; the cron line is wired later at P3a go-live):

```bash
python scripts/ingest_context.py --force --symbols INFY
```

## Shadow → live (A/B)

ONE knob: `CONTEXT_ENGINE_URL` (in `.env`).

- **UNSET (default)** → `fetch_context` returns `None` on its first line: no
  HTTP, no log, byte-identical decider prompt → prompt cache preserved → zero
  delta. This is the clean A/B baseline (**claude-blind**).
- **SET** (`http://127.0.0.1:8601`) → bot fetches per signal and injects context
  when non-stale (**claude-full**).

Run the service in **shadow** (bot NOT calling) for ≥ 2 weeks and validate
score-vs-next-move correlation (F1-style E2C ≥ 3, positive gross expectancy)
**before** flipping the flag. Revert = unset `CONTEXT_ENGINE_URL` (instant
fail-open to no-context); optionally `pm2 stop helm-context-engine`. Tables are
additive and can remain.

## Tests

```bash
pytest tests/context_engine tests/test_context_client.py
```

Hermetic — the source and LLM are mocked; no live network/LLM/DB is required for
the API/decay/score/client tests.
