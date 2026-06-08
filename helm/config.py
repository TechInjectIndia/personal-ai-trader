"""
Single source of truth for runtime config.

Values are intentionally hardcoded in code (not a YAML file) — for a single-user
bot, code is the config. Edit and PM2-reload to change.
"""

from dataclasses import dataclass
from datetime import time
from decimal import Decimal


# --- Watchlist ---
# Mix of (a) Nifty-50 large caps and (b) liquid NSE-listed ETFs that give us
# commodity / index exposure without leaving the equity segment. Everything
# here trades intraday MIS on Zerodha and has a usable .NS quote on yfinance.
# All names target >5 crore daily turnover so slippage on small-size orders
# stays minimal.
WATCHLIST: list[str] = [
    # Large-cap equities
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "INFY",
    "TCS",
    "SBIN",
    "BHARTIARTL",
    "ITC",
    "LT",
    "KOTAKBANK",
    # ETFs — broad-index and commodity exposure via the equity segment
    "NIFTYBEES",     # Nifty 50 index ETF
    "BANKBEES",      # Nifty Bank index ETF
    "ITBEES",        # Nifty IT index ETF
    "GOLDBEES",      # Gold ETF
    "SILVERBEES",    # Silver ETF
]

EXCHANGE = "NSE"


# --- Competition tradable universe ---
# The candidate symbol set that freestyle competitors pick their weekly mandate
# from (≤ MAX_MANDATE_SYMBOLS each — see helm.competition.mandate). A curated
# superset of WATCHLIST: liquid NSE large/mid caps + index/commodity ETFs, all
# with reliable {SYM}.NS yfinance quotes and intraday-MIS eligibility. Kept in
# code (not a YAML file) per the single-source-of-truth config rule. Tickers are
# deliberately "clean" (no '&'/'-') to avoid yfinance/.NS edge cases. The
# dynamic poller (scripts/poll_competition.py) polls the union of all active
# mandates' symbols that fall OUTSIDE WATCHLIST, so candle data exists for
# whatever the competitors chose — the house poll_market.py stays untouched.
_EXTRA_TRADABLE: list[str] = [
    "AXISBANK", "BAJFINANCE", "MARUTI", "HINDUNILVR", "ASIANPAINT",
    "TITAN", "SUNPHARMA", "TATASTEEL", "WIPRO",
    "HCLTECH", "TECHM", "ULTRACEMCO", "NESTLEIND", "POWERGRID",
    "NTPC", "ONGC", "COALINDIA", "ADANIPORTS", "JSWSTEEL",
    "GRASIM", "CIPLA", "DRREDDY", "EICHERMOT", "HEROMOTOCO",
    "BRITANNIA", "HINDALCO", "INDUSINDBK", "SBILIFE", "HDFCLIFE",
]
# Invariant: WATCHLIST ⊆ TRADABLE_UNIVERSE (house symbols are always tradable).
TRADABLE_UNIVERSE: list[str] = WATCHLIST + [s for s in _EXTRA_TRADABLE if s not in WATCHLIST]


# --- Trading hours (IST) ---
# We deliberately skip the first 15 minutes (volatility / spread blowouts) and
# square off well before the 15:25 IST regulatory auto-square-off for MIS.
MARKET_OPEN = time(9, 15)
TRADING_START = time(9, 30)            # earliest entry
TRADING_END = time(14, 45)             # latest entry — no new positions after this
SQUARE_OFF_AT = time(15, 15)           # close all open paper positions at this time
MARKET_CLOSE = time(15, 30)


# --- Risk limits ---
@dataclass(frozen=True)
class RiskLimits:
    max_open_positions: int = 5
    max_position_inr: Decimal = Decimal("15000")   # base notional cap per trade
    daily_loss_kill_inr: Decimal = Decimal("1000") # kill switch trips here (paper)
    per_symbol_cooldown_min: int = 30              # no re-entry on same symbol
    max_signals_per_symbol_per_day: int = 5


# --- Dynamic position-sizing ladder ---
# As realised equity grows above initial capital, lift the per-trade notional
# cap so winners compound rather than the bot trading the same ₹15k forever.
# Effective cap = clamp(base + step × realised_pnl / step_size, base, hard_max).
DYNAMIC_CAP_STEP_SIZE_INR: Decimal = Decimal("5000")   # every +₹5k of equity…
DYNAMIC_CAP_STEP_BOOST_INR: Decimal = Decimal("1500")  # …adds ₹1.5k to the cap
DYNAMIC_CAP_HARD_MAX_INR: Decimal = Decimal("25000")   # ceiling (50% of initial)


# --- Exit lock-in (give-back protection) ---
# Real-trade retros (task #681) show winners repeatedly run 50-83% of the way to
# target and then bleed back to a flat or losing EOD/STOP exit. Two stateless,
# zero-migration levers (applied in scripts/manage_positions.py by ratcheting the
# existing stop_loss column in place) recover most of that give-back:
#   (A) a breakeven lock once price reaches half the entry->target distance
#       (proposal #1062, "accepted") — covers the most then-lost trades; and
#   (B) an end-of-day wind-down tighten that, in the final ~45 min before
#       SQUARE_OFF_AT, ratchets the stop from breakeven toward LTP so a faded-
#       but-still-green trade locks a STOP exit before the 15:15 flat square-off.
# These are code-only Decimals (matching the DYNAMIC_CAP_* idiom) rather than
# RiskLimits fields, so they need no live_risk_limits()/EDITABLE_RISK_KEYS wiring.
# Treat the thresholds as a starting point pending forward paper validation.
EXIT_BREAKEVEN_TRIGGER_R: Decimal = Decimal("0.50")   # lock at 50% of entry->target
EXIT_BREAKEVEN_CUSHION_R: Decimal = Decimal("0.02")   # thin cushion above entry (post-charge buffer)
EXIT_TIME_DECAY_START_MIN: Decimal = Decimal("45")    # begin wind-down tighten 45 min pre-square-off
EXIT_TIME_DECAY_MAX_LOCK: Decimal = Decimal("0.80")   # near 15:15, lock up to 80% of open profit


def dynamic_position_cap(realised_pnl_inr: Decimal, base_cap_inr: Decimal) -> Decimal:
    """Per-trade notional cap, scaled by realised profit.

    Losses do NOT shrink the cap below `base_cap_inr` — the wallet-available
    check in risk.evaluate already prevents trades that don't fit the pool.
    """
    if realised_pnl_inr <= 0:
        return base_cap_inr
    steps = realised_pnl_inr // DYNAMIC_CAP_STEP_SIZE_INR
    boosted = base_cap_inr + steps * DYNAMIC_CAP_STEP_BOOST_INR
    return min(boosted, DYNAMIC_CAP_HARD_MAX_INR)

# Code defaults. Live values come from `live_risk_limits()` which overlays
# rows from the Postgres `settings` table on top of these defaults — so the
# Streamlit Settings page can change limits without a PM2 restart.
RISK = RiskLimits()


# --- Wallet (capital pool) ---
# The bot trades out of a single fixed pool of cash: it starts with
# `initial_capital_inr`, locks notional in open positions, and rolls realised
# net P&L back into the pool. Available cash is the hard ceiling on new
# trades — there is no separate margin or borrow.
@dataclass(frozen=True)
class WalletConfig:
    initial_capital_inr: Decimal = Decimal("50000")
    goal_capital_inr: Decimal = Decimal("100000")   # 2× initial — "double it"


WALLET = WalletConfig()


# Settings keys editable from the dashboard. Kept in code so the UI can render
# a stable schema and we can validate types on write.
EDITABLE_RISK_KEYS: tuple[str, ...] = (
    "max_open_positions",
    "max_position_inr",
    "daily_loss_kill_inr",
    "per_symbol_cooldown_min",
    "max_signals_per_symbol_per_day",
)

EDITABLE_WALLET_KEYS: tuple[str, ...] = (
    "initial_capital_inr",
    "goal_capital_inr",
)


def live_risk_limits() -> RiskLimits:
    """RISK overlaid with any overrides stored in the Postgres `settings` table.

    Falls back to the code defaults (`RISK`) for any unset key. Imported lazily
    inside the function to avoid a circular dependency with helm.data.store.
    """
    from helm.data.store import all_settings  # local import: see docstring

    overrides = all_settings()
    return RiskLimits(
        max_open_positions=int(overrides.get("max_open_positions", RISK.max_open_positions)),
        max_position_inr=Decimal(str(overrides.get("max_position_inr", RISK.max_position_inr))),
        daily_loss_kill_inr=Decimal(str(overrides.get("daily_loss_kill_inr", RISK.daily_loss_kill_inr))),
        per_symbol_cooldown_min=int(
            overrides.get("per_symbol_cooldown_min", RISK.per_symbol_cooldown_min)
        ),
        max_signals_per_symbol_per_day=int(
            overrides.get("max_signals_per_symbol_per_day", RISK.max_signals_per_symbol_per_day)
        ),
    )


def live_wallet_config() -> WalletConfig:
    """WALLET overlaid with any settings-table overrides."""
    from helm.data.store import all_settings  # local import: see live_risk_limits

    overrides = all_settings()
    return WalletConfig(
        initial_capital_inr=Decimal(str(
            overrides.get("initial_capital_inr", WALLET.initial_capital_inr)
        )),
        goal_capital_inr=Decimal(str(
            overrides.get("goal_capital_inr", WALLET.goal_capital_inr)
        )),
    )


# --- Polling ---
TICK_POLL_SECONDS = 30          # how often poll_market.py samples LTP
SCAN_EVERY_MINUTES = 5          # how often scan_signals.py runs


# --- Database ---
PG_DSN = "dbname=helm"          # unix socket, current user


# --- Competition league ---
# The incumbent single-pool bot is competitor 'house-claude'. Its live cron
# path doesn't stamp competitor_id (inserts NULL), while legacy rows were
# backfilled to 'house-claude'; so "house" rows are (NULL OR 'house-claude').
# The wallet/risk house path filters on exactly that set so competitor trades
# never leak into the house bot's accounting or position counts.
HOUSE_COMPETITOR_ID = "house-claude"
HOUSE_TRADE_FILTER = "(competitor_id IS NULL OR competitor_id = 'house-claude')"


# --- Competition: backend quotas ---
# Each league backend runs on a different vendor's free/subscription tier. To
# keep $0 spend we throttle every backend to a rolling per-window call ceiling
# (RPD-style); when a backend is exhausted — or a call returns a rate-limit /
# quota error — the quota subsystem pauses it until the window resets and then
# auto-resumes. These are conservative POC ceilings; tune as real limits show
# up in `backend_quota_state` / `agent_invocations`.
@dataclass(frozen=True)
class BackendQuota:
    max_calls: int          # calls allowed per rolling window
    window_minutes: int     # window length; on LOCAL exhaustion we pause until it rolls
    # Backoff applied when the backend *returns* a rate-limit/quota error
    # (quota.note_error). For daily-cap tiers a 429 means the day is spent, so
    # this defaults to the full window. For backends whose 429s are transient
    # upstream saturation (OpenRouter free models), set a SHORT backoff so the
    # agent retries within the session instead of benching for the whole window.
    error_backoff_minutes: int | None = None


# Subscription / free-gateway paths (claude, opencode) get generous ceilings;
# the free tiers get tighter daily windows so we never blow past a free
# allowance. A 5-min cadence over a ~5h trading day is ~63 cycles, so anything
# ≥ ~100/day comfortably covers one agent for a full session.
#
# qwen + nemotron run via OpenRouter free models, which share ONE account-wide
# daily request cap (≈50/day with <$10 ever-purchased, ≈1000/day at ≥$10). Our
# per-backend ceilings below assume the 1000/day tier; if the account is on the
# 50/day tier, OpenRouter returns 429 first and the quota subsystem auto-pauses
# the backend (quota.note_error) — so we degrade gracefully either way.
BACKEND_QUOTAS: dict[str, "BackendQuota"] = {
    "claude":   BackendQuota(max_calls=2000, window_minutes=24 * 60),
    "opencode": BackendQuota(max_calls=2000, window_minutes=24 * 60),
    "gemini":   BackendQuota(max_calls=200,  window_minutes=24 * 60),
    # Kiro CLI — treat conservatively like gemini until real daily limits are
    # known; tune upward once Kiro's quota policy is confirmed.
    "kiro":     BackendQuota(max_calls=200,  window_minutes=24 * 60),
    # OpenRouter free models: daily ceiling stays 24h, but a returned 429 is
    # transient upstream saturation — back off only ~10 min and retry, don't
    # bench for the day.
    "qwen":     BackendQuota(max_calls=400,  window_minutes=24 * 60, error_backoff_minutes=10),
    "nemotron": BackendQuota(max_calls=400,  window_minutes=24 * 60, error_backoff_minutes=10),
}
# Fallback for any backend without an explicit entry above.
DEFAULT_BACKEND_QUOTA = BackendQuota(max_calls=500, window_minutes=24 * 60)


def backend_quota(backend: str) -> "BackendQuota":
    """Quota config for a backend, falling back to DEFAULT_BACKEND_QUOTA."""
    return BACKEND_QUOTAS.get(backend, DEFAULT_BACKEND_QUOTA)


# --- Claude decider ---
# scripts/decide_signals.py asks Claude whether to take each unconsumed
# signal. Sonnet 4.6 is the default — faster/cheaper than Opus, and the
# decision space (TAKE / SKIP a single trade) doesn't need Opus-grade
# reasoning. Override via DECIDER_MODEL env var if you want to test Opus.
DECIDER_MODEL_DEFAULT = "claude-sonnet-4-6"
DECIDER_MAX_TOKENS = 800
DECIDER_TEMPERATURE = 0.0       # deterministic-ish; we want the same call to repeat
DECIDER_RECENT_BARS = 30        # how many recent 1-min bars to send to the model

# --- PM planning brain (weekly review + backlog drain) ---
# The PM does the genuinely hard, low-frequency JUDGMENT in the self-improvement
# loop: reasoning over the whole open-proposal backlog (retro history, engineer
# runs) to screen redundant restatements and decide accept/supersede/reject.
# That screening runs on Opus 4.8. The BUILDER (Engineer) and the trade decider
# stay on cheap Sonnet 4.6 — execution and per-trade TAKE/SKIP don't need
# Opus-grade reasoning and fire far more often. Override via AGENT_MODEL env var.
AGENT_MODEL_DEFAULT = "claude-opus-4-8"
