"""
Single source of truth for runtime config.

Values are intentionally hardcoded in code (not a YAML file) — for a single-user
bot, code is the config. Edit and PM2-reload to change.
"""

from dataclasses import dataclass, replace
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


# --- Multi-market enablement (FRD M1) ---
# Which markets the cron loop runs. IN (NSE) is the incumbent and the only one
# enabled by default; US and CRYPTO are registered (helm.markets.registry) but
# stay dark until their phases are validated and the human flips the flag here.
# Editing this + PM2 reload is the single switch that turns a venue on/off.
MARKET_ENABLED: dict[str, bool] = {
    "IN": True,
    "US": False,
    "CRYPTO": False,
}


# --- Crypto market (FRD M6) — paper only; enable via MARKET_ENABLED["CRYPTO"] ---
CRYPTO_WATCHLIST: list[str] = ["BTC", "ETH"]   # majors: best liquidity + free data
CRYPTO_EXCHANGE = "binance"                     # ccxt public OHLCV venue
CRYPTO_QUOTE = "USDT"                            # BTC -> BTC/USDT
CRYPTO_TAKER_BPS: Decimal = Decimal("0.0010")   # 0.10% Binance spot taker (per leg)
CRYPTO_MAX_HOLD_MIN = 240                        # 24/7 time-stop (4h): the EOD-flat analog


# --- US equities market (FRD M7) — paper only; enable via MARKET_ENABLED["US"] ---
# Liquid US large caps + index ETFs (the US analog of the NIFTYBEES/BANKBEES
# picks). Bare tickers (yfinance US / Alpaca; no .NS). Full strategy set applies
# (NYSE is sessioned, so ORB/gap-fade run too).
US_WATCHLIST: list[str] = ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"]


# --- Per-market paper wallet seed (FRD M2/M6/M7) ---
# IN uses WALLET / live_wallet_config() (settings-overridable). Non-IN markets
# read the `wallets` table; migrate_multimarket seeds these (currency, initial,
# goal) rows ON CONFLICT DO NOTHING so re-runs never clobber a live balance.
MARKET_WALLET_SEED: dict[str, tuple[str, str, str]] = {
    "CRYPTO": ("USD", "1000", "2000"),
    "US": ("USD", "5000", "10000"),
}


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
    per_symbol_cooldown_min: int = 45              # re-entry cooldown, NOW ENFORCED in risk.evaluate (2026-06-08)
    max_signals_per_symbol_per_day: int = 3        # F4 fewer entries (was 5→2); rebalanced to 3 now cooldown also throttles


# --- Per-market risk overrides (S2) ---
# Risk limits are INR-shaped for the incumbent IN book. US/CRYPTO trade in USD,
# so their per-trade notional cap + daily-loss kill must be venue-currency
# values, not the ₹15k/₹1k IN defaults. Count-based limits (max_open_positions,
# cooldown, signals/day) are currency-agnostic and stay shared. IN is
# intentionally ABSENT → it keeps using live_risk_limits() (settings-overridable,
# byte-identical). USD figures are placeholders pending operator calibration.
@dataclass(frozen=True)
class MarketRisk:
    max_position: Decimal        # per-trade notional cap, venue currency
    daily_loss_kill: Decimal     # daily realised-loss kill, venue currency


MARKET_RISK: dict[str, "MarketRisk"] = {
    "US":     MarketRisk(max_position=Decimal("1500"), daily_loss_kill=Decimal("100")),  # USD
    "CRYPTO": MarketRisk(max_position=Decimal("300"),  daily_loss_kill=Decimal("50")),   # USD
}


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


# --- Cost-aware minimum-edge gate (F2) ---
# Real-money diagnosis: gross P&L is ~flat but ~Rs13/trade of charges eats ~43%
# of the avg Rs31 gross move, so trades whose target reward is small relative to
# round-trip cost are guaranteed net losers. Refuse to open any house trade whose
# gross reward to target is below this multiple of the expected round-trip cost
# (E2C = |target-entry|*qty / round_trip_breakdown.total). Code-only Decimal,
# same idiom as DYNAMIC_CAP_*/EXIT_*. Set to Decimal("0") to disable (kill switch).
MIN_EDGE_TO_COST: Decimal = Decimal("3.0")


# --- Bigger-move reframe (F4) ---
# Strategies scalp ~0.25% moves where a fixed ~Rs13 round-trip cost eats ~43% of
# the gross. Floor every emitted Signal's target at >= MIN_TARGET_PCT of the entry
# price so each trade aims at a materially bigger move (paired with the lower
# signal caps above for fewer, higher-quality entries). Momentum/reclaim
# strategies WIDEN their natural target up to this floor; mean-reversion DROPS the
# signal when its mean target sits inside the floor (widening past the mean breaks
# the reversion thesis). Code-only Decimal, same idiom as MIN_EDGE_TO_COST. Set to
# Decimal("0") to disable (revert).
MIN_TARGET_PCT: Decimal = Decimal("0.006")   # >= 0.6% gross target move on entry
# Mean-reversion F4 policy. Default "none": EXEMPT reversion from the target
# floor — its target IS the mean (can't aim bigger without breaking the thesis),
# and a 0.6% floor near-disables it on low-vol large caps (it would gut the
# bbands A/B). Reversion's cost discipline is the F2 E2C gate at execution.
# "drop" (skip sub-floor signals) / "widen" (past the mean) are opt-in experiments.
MEANREV_WIDEN_OR_DROP: str = "none"          # "none" | "drop" | "widen"


# --- Conviction-weighted sizing (F5) — FLAG-GATED, default OFF ---
# Scale per-trade notional by the decider's confidence and skip sub-floor
# convictions. SHIPPED OFF: the conf-vs-outcome correlation is only ~+0.28 at
# n=24 (see scripts/review_digest.py "F5 gate"); enabling sizing on an
# uncalibrated signal just adds variance. Flip CONVICTION_SIZING_ENABLED=True
# only once the correlation holds over >=50 closed TAKEs. When False (or when no
# confidence is supplied), execute_signal sizes EXACTLY as today.
CONVICTION_SIZING_ENABLED: bool = False
CONVICTION_FLOOR: Decimal = Decimal("0.55")        # skip TAKEs below this confidence
CONVICTION_SIZE_MIN_MULT: Decimal = Decimal("0.5") # cap multiple at the floor; ramps to 1.0 at conf=1


# --- Context-driven signals (F7-P3c) — FLAG-GATED, default OFF ---
# The ContextMomentum strategy emits BUY on a strong bullish external context
# score confirmed by price. Gated SEPARATELY from the engine: even with the
# engine ON (feeding the decider, P3b), context SIGNALS stay off until the
# shadow data validates that scores predict moves. When False, scan_signals
# SKIPS requires_context strategies entirely → provably inert (no calls, no firing).
CONTEXT_SIGNALS_ENABLED: bool = False
CONTEXT_SIGNAL_THRESHOLD: Decimal = Decimal("0.5")  # min bullish context score to fire


# --- Per-(symbol,strategy) position slots (F6 A/B unblock) — FLAG-GATED, OFF ---
# Today the HOUSE risk gate allows ONE open position per symbol, so a 5-min
# variant and its 1-min twin contend for the same slot (whichever fires first
# blocks the other), confounding the multi-timeframe A/B. When True, the house
# `has_open_position` check keys on (symbol, strategy) so distinct strategies can
# hold concurrent positions in the same name — max_open_positions still caps
# TOTAL exposure. OFF by default: this raises same-symbol concurrency, so enable
# deliberately (and ideally after an adversarial review). Competition path is
# unaffected (each competitor stays one-position-per-symbol).
HOUSE_STRATEGY_KEYED_SLOTS: bool = False
# When HOUSE_STRATEGY_KEYED_SLOTS is on, distinct strategies may hold concurrent
# positions in one symbol — but several correlated 1m/5m strategy pairs could
# otherwise pile up to max_open_positions into a SINGLE name. This caps the
# concurrent open positions per symbol so the book stays diversified. Only
# enforced when the keyed-slots flag is on (OFF → has_open_position already caps
# at 1/symbol). Code-only constant (applies only once the flag is enabled).
MAX_OPEN_POSITIONS_PER_SYMBOL: int = 2

# --- Self-Improvement Loop v2 — proposal clustering (FRD G1/G2) ---
# When ON, every improvement_proposal is assigned to a proposal_cluster at
# creation time (helm.agents.clustering.assign_and_persist), so the backlog
# stays a small set of ranked distinct ideas instead of re-growing to thousands
# of restatements. Each assignment is one cheap LLM call made AFTER the retro
# commits (never inside the retro transaction) and fail-safe (a clustering error
# never breaks the retro). Default OFF: shipped dark until the backfill has run
# and the runtime is proven. Toggle live from the dashboard Control Center.
CLUSTER_ON_EMIT: bool = False
# When ON, the Tester runs a deterministic eval-gate stage (helm.eval.gate):
# re-prices the recent house book over recorded candles and HOLDs (reverts) a
# release that regresses the modeled economics vs the last verified baseline.
# Default OFF: shipped dark until the engine is trusted on live releases. When
# OFF the stage is a clean no-op (the release path is byte-identical to today).
EVAL_GATE_ENABLED: bool = False
EVAL_GATE_WINDOW_DAYS: int = 30   # look-back the gate re-prices
# S4 trading-safety guard: when ON, paper_execute runs an independent pre-trade
# backstop (helm.safety.pre_trade_check) on top of the risk gate. OFF by default
# so the paper path is byte-identical; flip ON per the live-funding runbook.
SAFETY_GUARD_ENABLED: bool = False
# Hard notional ceiling = this multiple of the per-market per-trade cap (a
# mis-config backstop independent of the risk cap).
SAFETY_NOTIONAL_CEILING_MULT: Decimal = Decimal("1.5")
# Circuit breaker: halt new trades for the day after this many consecutive
# losing closes (the daily-loss arm is already enforced by risk.daily_loss_kill).
SAFETY_MAX_CONSECUTIVE_LOSSES: int = 4
# A cluster whose fix was escalated from a freestyle agent to the house surface
# (G2) and that has recurred at least this many times with no in-surface owner
# is surfaced to the human via the Action Center — the visible form of insight
# that would otherwise sit structurally stuck (the cap-bug failure mode).
ESCALATE_RECURRENCE: int = 5
# G4 instinct ledger: when a verified cluster's theme RECURS this many times AFTER
# promotion (the retro loop re-flagging it = the lesson isn't holding), the
# instinct's confidence is decayed by INSTINCT_DECAY_FACTOR. Below
# INSTINCT_CONFIDENCE_FLOOR the instinct is marked 'decayed' and its source
# cluster is reopened so the loop re-fixes it — self-correcting memory.
INSTINCT_RECURRENCE_DECAY_AT: int = 3
INSTINCT_DECAY_FACTOR: float = 0.5
INSTINCT_CONFIDENCE_FLOOR: float = 0.25


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


def _safe_int(overrides: dict, key: str, default: int) -> int:
    """Override cast to int, degrading to `default` (audited) on a bad value."""
    if key not in overrides:
        return default
    try:
        return int(overrides[key])
    except Exception as err:
        _audit_bad_override(key, overrides[key], err)
        return default


def _safe_dec(overrides: dict, key: str, default: Decimal) -> Decimal:
    """Override cast to a finite Decimal, degrading to `default` (audited)."""
    if key not in overrides:
        return default
    try:
        d = Decimal(str(overrides[key]))
        if not d.is_finite():
            raise ValueError("non-finite")
        return d
    except Exception as err:
        _audit_bad_override(key, overrides[key], err)
        return default


def live_risk_limits() -> RiskLimits:
    """RISK overlaid with any overrides stored in the Postgres `settings` table.

    Falls back to the code defaults (`RISK`) for any unset OR malformed key, so a
    bad override (manual DB edit) degrades to the constant rather than raising on
    the trade path. Imported lazily to avoid a circular dependency with store.
    """
    try:
        from helm.data.store import all_settings  # local import: see docstring
        overrides = all_settings()
    except Exception:
        return RISK
    return RiskLimits(
        max_open_positions=_safe_int(overrides, "max_open_positions", RISK.max_open_positions),
        max_position_inr=_safe_dec(overrides, "max_position_inr", RISK.max_position_inr),
        daily_loss_kill_inr=_safe_dec(overrides, "daily_loss_kill_inr", RISK.daily_loss_kill_inr),
        per_symbol_cooldown_min=_safe_int(
            overrides, "per_symbol_cooldown_min", RISK.per_symbol_cooldown_min),
        max_signals_per_symbol_per_day=_safe_int(
            overrides, "max_signals_per_symbol_per_day", RISK.max_signals_per_symbol_per_day),
    )


def live_risk_limits_for(market: str) -> RiskLimits:
    """Risk limits for a market. IN — and any market without a MARKET_RISK entry —
    uses live_risk_limits() unchanged (settings-overridable, byte-identical to the
    single-market path). US/CRYPTO overlay their venue-currency per-trade cap +
    daily-loss kill onto the shared count-based limits."""
    base = live_risk_limits()
    mr = MARKET_RISK.get(market)
    if mr is None:
        return base
    return replace(base, max_position_inr=mr.max_position,
                   daily_loss_kill_inr=mr.daily_loss_kill)


def live_wallet_config() -> WalletConfig:
    """WALLET overlaid with any settings-table overrides (fail-safe to WALLET)."""
    try:
        from helm.data.store import all_settings  # local import: see live_risk_limits
        overrides = all_settings()
    except Exception:
        return WALLET
    return WalletConfig(
        initial_capital_inr=_safe_dec(overrides, "initial_capital_inr", WALLET.initial_capital_inr),
        goal_capital_inr=_safe_dec(overrides, "goal_capital_inr", WALLET.goal_capital_inr),
    )


# --- Live-toggleable feature flags + tunables (dashboard Control Center) ---
# Same pattern as live_risk_limits: the module constants above are the DEFAULT;
# a row in the Postgres `settings` table overrides at runtime (next cron tick,
# no PM2 restart). Consumers call live_flag()/live_tunable() at the USE site
# (NOT at import), so with no override the value equals the constant and
# behaviour is byte-identical to today. Strategy-internal constants
# (MIN_TARGET_PCT, CONTEXT_SIGNAL_THRESHOLD, MEANREV_WIDEN_OR_DROP) are NOT here:
# strategies must stay pure (no DB), so those remain code+PM2-reload (shown
# read-only in the UI).
EDITABLE_FLAG_KEYS: tuple[str, ...] = (
    "CONVICTION_SIZING_ENABLED",
    "CONTEXT_SIGNALS_ENABLED",
    "HOUSE_STRATEGY_KEYED_SLOTS",
    "CLUSTER_ON_EMIT",
    "EVAL_GATE_ENABLED",
    "SAFETY_GUARD_ENABLED",
)
EDITABLE_TUNABLE_KEYS: tuple[str, ...] = (
    "MIN_EDGE_TO_COST",
    "CONVICTION_FLOOR",
    "CONVICTION_SIZE_MIN_MULT",
)
_FLAG_DEFAULTS: dict[str, bool] = {
    "CONVICTION_SIZING_ENABLED": CONVICTION_SIZING_ENABLED,
    "CONTEXT_SIGNALS_ENABLED": CONTEXT_SIGNALS_ENABLED,
    "HOUSE_STRATEGY_KEYED_SLOTS": HOUSE_STRATEGY_KEYED_SLOTS,
    "CLUSTER_ON_EMIT": CLUSTER_ON_EMIT,
    "EVAL_GATE_ENABLED": EVAL_GATE_ENABLED,
    "SAFETY_GUARD_ENABLED": SAFETY_GUARD_ENABLED,
}
_TUNABLE_DEFAULTS: dict[str, Decimal] = {
    "MIN_EDGE_TO_COST": MIN_EDGE_TO_COST,
    "CONVICTION_FLOOR": CONVICTION_FLOOR,
    "CONVICTION_SIZE_MIN_MULT": CONVICTION_SIZE_MIN_MULT,
}
_TRUTHY = {"true", "1", "yes", "on"}


def _audit_bad_override(key: str, value: object, err: object) -> None:
    """Best-effort audit of a malformed settings override (never raises)."""
    try:
        from helm.data.store import insert_audit
        insert_audit("config", "bad_override",
                     {"key": key, "value": str(value)[:80], "error": str(err)[:120]})
    except Exception:
        pass


def live_flag(name: str) -> bool:
    """Live value of a boolean feature flag (settings override else code default).

    FAILS SAFE: any DB error or unknown key degrades to the code default. The
    bool parse itself never raises (a non-truthy/garbage value reads as False),
    so a malformed flag row can never crash the trade path."""
    default = bool(_FLAG_DEFAULTS.get(name, False))
    try:
        from helm.data.store import all_settings  # local: avoid circular import
        v = all_settings().get(name)
    except Exception:
        return default
    if v is None:
        return default
    return str(v).strip().strip('"').lower() in _TRUTHY


def live_tunable(name: str) -> Decimal:
    """Live value of a Decimal tunable (settings override else code default).

    FAILS SAFE: a malformed/non-finite override (e.g. 'maybe', '', NaN, Infinity)
    or a DB error degrades to the code default and is audited — it must never
    raise on the live trade path (a NaN MIN_EDGE_TO_COST could otherwise silently
    invert the F2 cost gate)."""
    default = _TUNABLE_DEFAULTS[name]
    try:
        from helm.data.store import all_settings  # local: avoid circular import
        v = all_settings().get(name)
    except Exception:
        return default
    if v is None:
        return default
    try:
        d = Decimal(str(v))
        if not d.is_finite():
            raise ValueError("non-finite")
        return d
    except Exception as err:
        _audit_bad_override(name, v, err)
        return default


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

# --- Context Engine (external conviction layer) ---
# A decoupled FastAPI microservice (context_engine/, own PM2 process on
# 127.0.0.1:8601) ingests/scores news per symbol and exposes GET /context/{sym}.
# The bot consumer (helm/context_client.py, stdlib-only) reaches it over
# localhost HTTP, fail-open and flag-gated. ONE KNOB controls live/blind:
#   CONTEXT_ENGINE_URL — env-only (per-environment → .env), NOT housed here.
#     UNSET (default)  => fetch returns None on line 1: no HTTP, no log, no
#                         latency, byte-identical prompt → prompt cache preserved
#                         (claude-blind A/B baseline, zero cost/behaviour delta).
#     SET (e.g. http://127.0.0.1:8601) => bot fetches + injects non-stale context
#                         (claude-full).
# Only CONTEXT_ENGINE_TIMEOUT_S is read by the bot client; the rest are SERVICE
# knobs (kept here per single-source-of-truth). Short timeout hard-caps added
# latency on the inline scan→decide path; on any timeout the client fails open.
CONTEXT_ENGINE_TIMEOUT_S: float = 1.5    # bot client HTTP timeout (seconds)
CONTEXT_STALENESS_MIN: int = 180         # SERVICE: age beyond which score is stale
CONTEXT_FRESHNESS_FLOOR: float = 0.05    # SERVICE: decayed |score| below → stale
CONTEXT_INGEST_CADENCE_MIN: int = 15     # SERVICE: ingest/score cadence (minutes)


# --- PM planning brain (weekly review + backlog drain) ---
# The PM does the genuinely hard, low-frequency JUDGMENT in the self-improvement
# loop: reasoning over the whole open-proposal backlog (retro history, engineer
# runs) to screen redundant restatements and decide accept/supersede/reject.
# That screening runs on Opus 4.8. The BUILDER (Engineer) and the trade decider
# stay on cheap Sonnet 4.6 — execution and per-trade TAKE/SKIP don't need
# Opus-grade reasoning and fire far more often. Override via AGENT_MODEL env var.
AGENT_MODEL_DEFAULT = "claude-opus-4-8"
