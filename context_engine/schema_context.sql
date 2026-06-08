-- Context Engine storage — additive, service-owned, idempotent.
--
-- Lives separately from helm/data/schema.sql so ownership is clear: only the
-- context_engine service writes/reads these tables in SQL. The bot reads them
-- ONLY over HTTP (GET /context/{symbol}). Shares dbname=helm over the local
-- socket, so isolation is logical-by-convention, not physical.
--
-- All timestamps TIMESTAMPTZ; score NUMERIC(4,3) in [-1.000, +1.000].

CREATE TABLE IF NOT EXISTS context_items (
    id           BIGSERIAL PRIMARY KEY,
    symbol       TEXT NOT NULL,
    source       TEXT NOT NULL,
    url          TEXT,
    headline     TEXT,
    body         TEXT,
    published_ts TIMESTAMPTZ,
    ingested_ts  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (symbol, url)          -- dedupe boundary
);
CREATE INDEX IF NOT EXISTS context_items_recent ON context_items (symbol, ingested_ts DESC);

CREATE TABLE IF NOT EXISTS context_scores (
    id            BIGSERIAL PRIMARY KEY,
    symbol        TEXT NOT NULL,
    scored_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
    score         NUMERIC(4,3) NOT NULL,   -- -1.000 .. +1.000
    half_life_min INT NOT NULL,
    rationale     TEXT,
    item_ids      BIGINT[]
);
CREATE INDEX IF NOT EXISTS context_scores_recent ON context_scores (symbol, scored_ts DESC);
