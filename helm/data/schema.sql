-- Intraday trading bot — Postgres schema.
-- All times stored as IST (Asia/Kolkata) with timestamptz.

CREATE TABLE IF NOT EXISTS ticks (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT NOT NULL,
    ltp         NUMERIC(12, 2) NOT NULL,
    volume      BIGINT,
    raw         JSONB
);
CREATE INDEX IF NOT EXISTS ticks_symbol_ts ON ticks (symbol, ts DESC);

CREATE TABLE IF NOT EXISTS candles_1m (
    symbol      TEXT NOT NULL,
    bar_ts      TIMESTAMPTZ NOT NULL,
    open        NUMERIC(12, 2) NOT NULL,
    high        NUMERIC(12, 2) NOT NULL,
    low         NUMERIC(12, 2) NOT NULL,
    close       NUMERIC(12, 2) NOT NULL,
    tick_count  INTEGER NOT NULL,
    PRIMARY KEY (symbol, bar_ts)
);
CREATE INDEX IF NOT EXISTS candles_bar_ts ON candles_1m (bar_ts DESC);

CREATE TABLE IF NOT EXISTS signals (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    strategy    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    entry_price NUMERIC(12, 2) NOT NULL,
    stop_loss   NUMERIC(12, 2) NOT NULL,
    target      NUMERIC(12, 2),
    rationale   TEXT,
    payload     JSONB,
    consumed    BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS signals_unconsumed ON signals (consumed, ts) WHERE consumed = FALSE;

CREATE TABLE IF NOT EXISTS decisions (
    id           BIGSERIAL PRIMARY KEY,
    signal_id    BIGINT REFERENCES signals(id),
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor        TEXT NOT NULL,           -- 'claude-code', 'auto', 'manual'
    verdict      TEXT NOT NULL CHECK (verdict IN ('TAKE', 'SKIP', 'MODIFY')),
    qty          INTEGER,
    final_entry  NUMERIC(12, 2),
    final_stop   NUMERIC(12, 2),
    final_target NUMERIC(12, 2),
    reasoning    TEXT
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id            BIGSERIAL PRIMARY KEY,
    decision_id   BIGINT REFERENCES decisions(id),
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    qty           INTEGER NOT NULL,
    entry_price   NUMERIC(12, 2) NOT NULL,
    entry_ts      TIMESTAMPTZ NOT NULL,
    stop_loss     NUMERIC(12, 2) NOT NULL,
    target        NUMERIC(12, 2),
    exit_price    NUMERIC(12, 2),
    exit_ts       TIMESTAMPTZ,
    exit_reason   TEXT,                   -- 'STOP', 'TARGET', 'EOD', 'MANUAL'
    pnl_inr       NUMERIC(12, 2),         -- gross P&L (price diff × qty), pre-charges
    charges_inr   NUMERIC(12, 2),         -- round-trip Zerodha-MIS brokerage + statutory charges
    net_pnl_inr   NUMERIC(12, 2),         -- pnl_inr - charges_inr
    status        TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED'))
);
CREATE INDEX IF NOT EXISTS paper_trades_open ON paper_trades (status, symbol) WHERE status = 'OPEN';
CREATE INDEX IF NOT EXISTS paper_trades_entry ON paper_trades (entry_ts DESC);

-- Idempotent backfill for existing deployments.
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS charges_inr NUMERIC(12, 2);
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS net_pnl_inr NUMERIC(12, 2);

CREATE TABLE IF NOT EXISTS audit (
    id      BIGSERIAL PRIMARY KEY,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor   TEXT NOT NULL,
    event   TEXT NOT NULL,
    detail  JSONB
);

CREATE TABLE IF NOT EXISTS daily_state (
    trade_date    DATE PRIMARY KEY,
    starting_cash NUMERIC(12, 2),
    realized_pnl  NUMERIC(12, 2) NOT NULL DEFAULT 0,
    kill_engaged  BOOLEAN NOT NULL DEFAULT FALSE,
    notes         TEXT
);

-- Runtime-editable settings (max_open_positions, max_position_inr, etc.).
-- helm.config exposes code defaults; values written here override them live.
-- Edited from the Streamlit "Settings" page; no PM2 restart required.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_ts  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT NOT NULL DEFAULT 'system'
);

-- Retrospectives: written by scripts/retro_trades.py after a trade closes
-- (kind='TRADE') or after a SKIP decision can be evaluated against the day's
-- price path (kind='SKIP'). Layman-language summary + structured scores; the
-- raw LLM payload is kept for debugging and future re-scoring.
CREATE TABLE IF NOT EXISTS trade_retrospectives (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('TRADE', 'SKIP')),
    trade_id        BIGINT REFERENCES paper_trades(id),
    decision_id     BIGINT NOT NULL REFERENCES decisions(id),
    created_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    model           TEXT NOT NULL,
    llm_mode        TEXT NOT NULL,
    verdict_label   TEXT NOT NULL CHECK (verdict_label IN
                        ('GOOD_CALL', 'BAD_CALL', 'LUCKY', 'UNLUCKY', 'MIXED')),
    signal_quality_score     SMALLINT CHECK (signal_quality_score BETWEEN 1 AND 5),
    decision_quality_score   SMALLINT CHECK (decision_quality_score BETWEEN 1 AND 5),
    execution_quality_score  SMALLINT CHECK (execution_quality_score BETWEEN 1 AND 5),
    tags                JSONB,                     -- array of short string tags
    summary_layman      TEXT NOT NULL,
    why_we_acted        TEXT NOT NULL,             -- "why we entered" / "why we passed"
    what_happened       TEXT NOT NULL,
    verdict_reasoning   TEXT NOT NULL,
    learnings           JSONB NOT NULL,            -- array of 1-3 plain-English bullets
    raw_response        JSONB
);
CREATE UNIQUE INDEX IF NOT EXISTS retros_trade_id
    ON trade_retrospectives (trade_id)
    WHERE trade_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS retros_skip_decision_id
    ON trade_retrospectives (decision_id)
    WHERE kind = 'SKIP';
CREATE INDEX IF NOT EXISTS retros_recent
    ON trade_retrospectives (created_ts DESC);


-- Engineering improvement proposals derived from retrospectives.
-- The retro LLM call surfaces 0..N concrete change ideas per trade — prompt
-- tweaks, strategy rule changes, sizing/risk adjustments, etc. Each lands as
-- its own row here so the proposals_digest script can aggregate themes across
-- many retros and we can track which ones we've actually applied.
CREATE TABLE IF NOT EXISTS improvement_proposals (
    id               BIGSERIAL PRIMARY KEY,
    retro_id         BIGINT NOT NULL REFERENCES trade_retrospectives(id) ON DELETE CASCADE,
    created_ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    category         TEXT NOT NULL CHECK (category IN
                         ('strategy', 'decider_prompt', 'risk', 'sizing',
                          'execution', 'data', 'meta')),
    title            TEXT NOT NULL,                 -- short headline
    rationale        TEXT NOT NULL,                 -- 1-3 sentences of why
    proposed_change  TEXT NOT NULL,                 -- specific change to make
    evidence         JSONB,                         -- key facts from this trade
    confidence       SMALLINT CHECK (confidence BETWEEN 1 AND 5),
    status           TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open', 'accepted', 'rejected',
                                         'applied', 'superseded')),
    status_note      TEXT,
    status_ts        TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS proposals_open
    ON improvement_proposals (category, created_ts DESC)
    WHERE status = 'open';
CREATE INDEX IF NOT EXISTS proposals_by_retro
    ON improvement_proposals (retro_id);


-- ─── Autonomous improvement loop ──────────────────────────────────────
-- Three agents collaborate via the agent_tasks queue:
--   PM agent          — weekly review, picks proposals to ship, writes tasks
--   Engineer agent    — polls tasks, applies a typed mutator, commits, releases
--   Tester agent      — verifies each release; reverts + files bug on failure
-- All actor invocations are traced in agent_runs. Each ship lands one row in
-- releases. metrics_snapshots gives the goal-progress dashboard its data.

CREATE TABLE IF NOT EXISTS agent_tasks (
    id              BIGSERIAL PRIMARY KEY,
    created_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      TEXT NOT NULL,                    -- 'pm' | 'tester' | 'human'
    proposal_id     BIGINT REFERENCES improvement_proposals(id) ON DELETE SET NULL,
    parent_task_id  BIGINT REFERENCES agent_tasks(id) ON DELETE SET NULL,
    title           TEXT NOT NULL,
    rationale       TEXT NOT NULL,
    -- Closed set of task types the Engineer knows how to handle. Anything
    -- else is `needs_human` and surfaces in the digest for manual action.
    task_type       TEXT NOT NULL CHECK (task_type IN
                      ('prompt_tweak', 'param_change', 'add_filter',
                       'setting_override', 'add_strategy_variant',
                       'bug_fix', 'needs_human')),
    spec            JSONB NOT NULL,                   -- type-specific payload
    priority        SMALLINT NOT NULL DEFAULT 3
                      CHECK (priority BETWEEN 1 AND 5),
    status          TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open', 'in_progress', 'done',
                                        'failed', 'cancelled', 'needs_human')),
    claimed_by      TEXT,
    claimed_ts      TIMESTAMPTZ,
    completed_ts    TIMESTAMPTZ,
    release_id      BIGINT,                            -- FK added below
    error           TEXT
);
CREATE INDEX IF NOT EXISTS tasks_open
    ON agent_tasks (priority DESC, created_ts ASC)
    WHERE status = 'open';
CREATE INDEX IF NOT EXISTS tasks_by_proposal
    ON agent_tasks (proposal_id);

CREATE TABLE IF NOT EXISTS releases (
    id              BIGSERIAL PRIMARY KEY,
    task_id         BIGINT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    created_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    commit_sha      TEXT NOT NULL,
    branch          TEXT NOT NULL DEFAULT 'main',
    summary         TEXT NOT NULL,
    diff_stat       TEXT,                              -- short `git diff --stat`
    -- 'deployed' = local commit in place; 'verified' = tester signed off;
    -- 'reverted' = tester rolled it back; 'failed' = deploy itself failed.
    status          TEXT NOT NULL DEFAULT 'deployed'
                      CHECK (status IN ('deployed', 'verified',
                                        'reverted', 'failed')),
    verified_ts     TIMESTAMPTZ,
    reverted_ts     TIMESTAMPTZ,
    revert_sha      TEXT,
    tester_notes    TEXT
);
CREATE INDEX IF NOT EXISTS releases_recent
    ON releases (created_ts DESC);
CREATE INDEX IF NOT EXISTS releases_unverified
    ON releases (created_ts DESC) WHERE status = 'deployed';

-- Forward FK from agent_tasks to releases (set after the release lands).
DO $$ BEGIN
    ALTER TABLE agent_tasks
        ADD CONSTRAINT agent_tasks_release_fk
        FOREIGN KEY (release_id) REFERENCES releases(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS agent_runs (
    id              BIGSERIAL PRIMARY KEY,
    started_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_ts     TIMESTAMPTZ,
    agent           TEXT NOT NULL,                    -- 'pm' | 'engineer' | 'tester'
    invocation      TEXT,                              -- scripts/X.py invocation tag
    task_id         BIGINT REFERENCES agent_tasks(id) ON DELETE SET NULL,
    release_id      BIGINT REFERENCES releases(id) ON DELETE SET NULL,
    model           TEXT,
    llm_mode        TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    latency_ms      INTEGER,
    outcome         TEXT,                              -- 'ok' | 'error' | 'noop'
    summary         TEXT,                              -- one-line human summary
    trace           JSONB                              -- structured detail
);
CREATE INDEX IF NOT EXISTS agent_runs_recent
    ON agent_runs (started_ts DESC);
CREATE INDEX IF NOT EXISTS agent_runs_by_agent
    ON agent_runs (agent, started_ts DESC);

CREATE TABLE IF NOT EXISTS metrics_snapshots (
    snapshot_ts             TIMESTAMPTZ PRIMARY KEY DEFAULT now(),
    equity_inr              NUMERIC(12, 2) NOT NULL,
    initial_capital_inr     NUMERIC(12, 2) NOT NULL,
    goal_capital_inr        NUMERIC(12, 2) NOT NULL,
    progress_pct            NUMERIC(6, 2) NOT NULL,
    realised_net_pnl_inr    NUMERIC(12, 2) NOT NULL,
    open_positions          SMALLINT NOT NULL,
    locked_inr              NUMERIC(12, 2) NOT NULL,
    trades_total            INTEGER NOT NULL,
    wins                    INTEGER NOT NULL,
    losses                  INTEGER NOT NULL,
    win_rate_pct            NUMERIC(6, 2),
    avg_win_inr             NUMERIC(12, 2),
    avg_loss_inr            NUMERIC(12, 2),
    expectancy_inr          NUMERIC(12, 2),
    max_drawdown_inr        NUMERIC(12, 2),
    signals_today           INTEGER NOT NULL DEFAULT 0,
    decisions_today         INTEGER NOT NULL DEFAULT 0,
    take_rate_pct           NUMERIC(6, 2),
    proposals_open          INTEGER NOT NULL DEFAULT 0,
    tasks_open              INTEGER NOT NULL DEFAULT 0,
    releases_unverified     INTEGER NOT NULL DEFAULT 0,
    notes                   TEXT
);
CREATE INDEX IF NOT EXISTS metrics_recent
    ON metrics_snapshots (snapshot_ts DESC);


-- ─── Competition league ───────────────────────────────────────────────
-- Multiple agents (each a distinct LLM backend / model / persona) compete
-- on the same market data, each with its own isolated cash pool. The
-- existing single-portfolio bot is modelled as competitor 'house-claude'.
-- All additions here are ADDITIVE: existing tables gain a NULLABLE
-- competitor_id column (no FK, no default) so legacy rows and cron inserts
-- that don't set it keep working unchanged.

CREATE TABLE IF NOT EXISTS competitors (
    id              TEXT PRIMARY KEY,                 -- stable slug, e.g. 'house-claude'
    name            TEXT,                             -- human display name
    backend         TEXT,                             -- 'claude' | 'gemini' | 'qwen' | ...
    model           TEXT,                             -- concrete model id
    persona         TEXT,                             -- short style/strategy description
    autonomy_level  TEXT,                             -- 'freestyle' | 'incumbent' | ...
    status          TEXT,                             -- 'active' | 'paused' | 'retired'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One isolated wallet per competitor (PK = competitor_id, 1:1).
CREATE TABLE IF NOT EXISTS competitor_wallets (
    competitor_id       TEXT PRIMARY KEY REFERENCES competitors(id),
    initial_capital_inr NUMERIC(12, 2),
    available_inr       NUMERIC(12, 2),
    realized_pnl_inr    NUMERIC(12, 2) NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Weekly trading mandate per competitor (universe + strategy config), as
-- produced by that competitor's planner. One mandate per (competitor, week).
CREATE TABLE IF NOT EXISTS competitor_mandates (
    id              BIGSERIAL PRIMARY KEY,
    competitor_id   TEXT REFERENCES competitors(id),
    week_start      DATE,
    universe        JSONB,                            -- array of symbols
    strategy_config JSONB,                            -- strategy params for the week
    rationale       TEXT,                             -- plain-English reasoning
    raw             JSONB,                            -- raw planner payload
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS competitor_mandates_week
    ON competitor_mandates (competitor_id, week_start);

-- Every backend invocation (one LLM call) traced for audit + quota analysis.
CREATE TABLE IF NOT EXISTS agent_invocations (
    id              BIGSERIAL PRIMARY KEY,
    competitor_id   TEXT REFERENCES competitors(id),
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    backend         TEXT,
    model           TEXT,
    prompt          TEXT,
    raw_output      TEXT,
    latency_ms      INTEGER,
    ok              BOOLEAN,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS agent_invocations_by_competitor
    ON agent_invocations (competitor_id, ts DESC);

-- Per-backend rolling quota state so the league can throttle / pause a
-- backend that's hit its rate or usage window.
CREATE TABLE IF NOT EXISTS backend_quota_state (
    backend         TEXT PRIMARY KEY,
    window_start    TIMESTAMPTZ,
    calls_used      INTEGER NOT NULL DEFAULT 0,
    paused_until    TIMESTAMPTZ
);

-- Tag existing operational tables with an optional competitor dimension.
-- NULLABLE + no FK + no default on purpose: existing rows stay valid and the
-- single-portfolio cron path (which doesn't set competitor_id) is unaffected.
-- The migrate_competition.py backfill stamps legacy rows as 'house-claude'.
ALTER TABLE signals          ADD COLUMN IF NOT EXISTS competitor_id TEXT;
ALTER TABLE decisions        ADD COLUMN IF NOT EXISTS competitor_id TEXT;
ALTER TABLE paper_trades     ADD COLUMN IF NOT EXISTS competitor_id TEXT;
ALTER TABLE daily_state      ADD COLUMN IF NOT EXISTS competitor_id TEXT;
ALTER TABLE metrics_snapshots ADD COLUMN IF NOT EXISTS competitor_id TEXT;


-- ─── Per-agent self-improvement loop ──────────────────────────────────
-- Originally the PM→Engineer→Tester loop was house/code-only. Each agent in
-- the competition league (house + freestyle competitors) now flows through it
-- independently: the retro/proposal/task/release/run rows below gain an
-- optional competitor_id so the loop can be scoped per agent. NULL = house,
-- same convention as the operational tables above; the migrate_competition.py
-- backfill stamps legacy rows as 'house-claude'.
ALTER TABLE trade_retrospectives  ADD COLUMN IF NOT EXISTS competitor_id TEXT;
ALTER TABLE improvement_proposals ADD COLUMN IF NOT EXISTS competitor_id TEXT;
ALTER TABLE agent_tasks           ADD COLUMN IF NOT EXISTS competitor_id TEXT;
ALTER TABLE releases              ADD COLUMN IF NOT EXISTS competitor_id TEXT;
ALTER TABLE agent_runs            ADD COLUMN IF NOT EXISTS competitor_id TEXT;

-- Widen the agent_tasks task-type check to allow the two freestyle config
-- mutators. Superset of the original set, so dropping + re-adding is safe and
-- idempotent (re-running schema.sql just rewrites the same constraint).
ALTER TABLE agent_tasks DROP CONSTRAINT IF EXISTS agent_tasks_task_type_check;
ALTER TABLE agent_tasks ADD CONSTRAINT agent_tasks_task_type_check
    CHECK (task_type IN
        ('prompt_tweak', 'param_change', 'add_filter',
         'setting_override', 'add_strategy_variant',
         'bug_fix', 'needs_human',
         'persona_edit', 'strategy_config_edit'));

-- Freestyle "release analogue": every persona / strategy_config edit the
-- Engineer applies to a competitor lands here with a full before/after
-- snapshot so the Tester can roll it back deterministically (no git involved).
--   field='persona'          → old/new_value snapshot competitors.persona
--                              (mandate_week NULL).
--   field='strategy_config'  → old/new_value snapshot the competitor_mandates
--                              row's strategy_config; mandate_week is the
--                              competitor_mandates.week_start the edit targeted
--                              (reverts key off (competitor_id, week_start)).
CREATE TABLE IF NOT EXISTS competitor_config_versions (
    id              BIGSERIAL PRIMARY KEY,
    competitor_id   TEXT NOT NULL REFERENCES competitors(id),
    task_id         BIGINT REFERENCES agent_tasks(id) ON DELETE SET NULL,
    created_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    field           TEXT NOT NULL CHECK (field IN ('persona', 'strategy_config')),
    mandate_week    DATE,                              -- target week_start; NULL for persona
    old_value       JSONB,                             -- prior value (NULL on first edit)
    new_value       JSONB NOT NULL,                    -- value applied
    -- 'deployed' = applied, awaiting tester; 'verified' = tester signed off;
    -- 'reverted' = tester rolled it back; 'failed' = apply/revert itself failed.
    status          TEXT NOT NULL DEFAULT 'deployed'
                      CHECK (status IN ('deployed', 'verified',
                                        'reverted', 'failed')),
    verified_ts     TIMESTAMPTZ,
    reverted_ts     TIMESTAMPTZ,
    tester_notes    TEXT
);
CREATE INDEX IF NOT EXISTS config_versions_recent
    ON competitor_config_versions (competitor_id, created_ts DESC);
CREATE INDEX IF NOT EXISTS config_versions_unverified
    ON competitor_config_versions (created_ts DESC) WHERE status = 'deployed';


-- ─── Self-Improvement Loop v2 — proposal clustering (FRD G1/G2) ───────────
-- The retro pipeline emits ~hundreds of near-duplicate improvement_proposals
-- (the same idea restated from many trades — e.g. the cap-clamp fix recurred
-- ~90×). A flat backlog of 1,700+ open rows is unrankable: the loop can't tell
-- "proposed once" from "proposed 90×". A cluster is the DISTINCT idea; each
-- proposal points at its cluster, so recurrence = member count and the digest
-- ranks clusters, not raw rows.
--
-- G2 (escalation): target_surface routes a cluster to the agent that can fix
-- it. A freestyle agent that diagnoses shared house code sets surface='house'
-- + escalated=true (with origin_competitor_id), so a house-engineer can own a
-- fix the freestyle agent is walled off from — the exact failure that hid the
-- cap bug for weeks.
CREATE TABLE IF NOT EXISTS proposal_clusters (
    id                 BIGSERIAL PRIMARY KEY,
    theme              TEXT NOT NULL,                 -- canonical one-line idea
    layer              TEXT NOT NULL CHECK (layer IN
                          ('strategy', 'decider', 'risk', 'sizing',
                           'execution', 'data', 'meta', 'persona')),
    -- 'house' or a competitor_id — who owns the fix (G2 routing).
    target_surface     TEXT NOT NULL DEFAULT 'house',
    escalated          BOOLEAN NOT NULL DEFAULT false, -- surfaced from a freestyle agent
    origin_competitor_id TEXT,                          -- who first surfaced it
    confidence         NUMERIC(3,2) NOT NULL DEFAULT 0, -- max member conf, decayed
    recurrence         INT NOT NULL DEFAULT 0,          -- # member proposals
    economic_priority  NUMERIC(5,2) NOT NULL DEFAULT 1, -- digest-set $-impact weight
    status             TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'accepted', 'in_flight',
                                          'verified', 'rejected', 'superseded')),
    representative_proposal_id BIGINT REFERENCES improvement_proposals(id) ON DELETE SET NULL,
    last_member_ts     TIMESTAMPTZ NOT NULL DEFAULT now(), -- newest member; drives decay
    created_ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS clusters_open_ranked
    ON proposal_clusters (status, recurrence DESC, confidence DESC)
    WHERE status = 'open';
CREATE INDEX IF NOT EXISTS clusters_by_surface
    ON proposal_clusters (target_surface, status);

-- Each proposal points at the distinct idea it restates (NULL until clustered).
ALTER TABLE improvement_proposals
    ADD COLUMN IF NOT EXISTS cluster_id BIGINT REFERENCES proposal_clusters(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS proposals_by_cluster
    ON improvement_proposals (cluster_id) WHERE cluster_id IS NOT NULL;

-- G4 instinct ledger: when a cluster is VERIFIED (its fix shipped + passed the
-- eval-gate), the lesson is promoted into a durable per-agent "instinct" — what
-- this agent has learned and is actively applying. Confidence DECAYS if the same
-- theme keeps recurring after promotion (the retro loop re-flagging it = the
-- lesson isn't holding); a decayed instinct reopens its source cluster so the
-- loop re-fixes it. This is the self-correcting memory the loop lacked.
CREATE TABLE IF NOT EXISTS agent_instincts (
    id              BIGSERIAL PRIMARY KEY,
    competitor_id   TEXT NOT NULL,                 -- owning agent (house-claude or persona id)
    cluster_id      BIGINT REFERENCES proposal_clusters(id) ON DELETE SET NULL,
    statement       TEXT NOT NULL,                 -- the lesson (cluster theme)
    layer           TEXT,                          -- strategy/risk/decider/persona/...
    artifact_kind   TEXT,                          -- persona | config | decider_prompt | strategy_config
    confidence      NUMERIC(4,3) NOT NULL DEFAULT 1.0,
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'decayed', 'retired')),
    hits            INT NOT NULL DEFAULT 0,         -- eval passes with no recurrence
    misses          INT NOT NULL DEFAULT 0,        -- post-promotion recurrences (contradictions)
    promoted_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_eval_ts    TIMESTAMPTZ,
    updated_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (competitor_id, cluster_id)
);
CREATE INDEX IF NOT EXISTS instincts_by_agent
    ON agent_instincts (competitor_id, status);
