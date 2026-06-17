"""
Deterministic trade simulator (FRD G3, eval-gate core).

Given a trade's parameters (side/entry/stop/target/qty) and the 1-minute candles
that followed it, replay the exact exit the live bot WOULD have booked — so any
deterministic change (a sizing rule, a stop/exit policy, a strategy filter that
changes which signals exist) can be re-priced on real history and compared.

Fidelity to the live loop (`scripts/manage_positions.py`):
  * The live cron samples ONE last-price per minute. We mirror that with each
    candle's CLOSE as the per-minute LTP (NOT intrabar high/low — that would
    book fills the once-a-minute live loop never sees). This is the faithful
    estimate of live behaviour, the whole point of the gate.
  * Precedence per minute: EOD square-off (≥ SQUARE_OFF_AT) → else #681 stop
    ratchet (house only) → else STOP → else TARGET. Identical to the live chain.
  * The #681 give-back ratchet is mirrored as a PURE function (`ratchet_stop`)
    of (entry, stop, target, side, ltp, t_rem_min) — the live `_tighten_stop`
    minus its DB side effects, so it produces byte-identical stop moves.
  * Charges use the real `helm.charges` model, so `net` is the live net.

Everything here is a pure function of its inputs (no DB, no clock) → unit-tested
with synthetic candles and reused by the backtest CLI over real candles_1m.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from helm.charges import round_trip_charges
from helm.config import (
    EXIT_BREAKEVEN_CUSHION_R,
    EXIT_BREAKEVEN_TRIGGER_R,
    EXIT_TIME_DECAY_MAX_LOCK,
    EXIT_TIME_DECAY_START_MIN,
    SQUARE_OFF_AT,
)

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class SimOutcome:
    """The replayed result of one trade."""
    side: str
    entry: Decimal
    exit_price: Decimal
    qty: int
    exit_reason: str          # STOP | TARGET | EOD | UNCLOSED
    exit_ts: datetime | None
    bars_held: int
    gross: Decimal
    charges: Decimal
    net: Decimal
    closed: bool              # False only for UNCLOSED (window ended mid-trade)


def ratchet_stop(side: str, entry: Decimal, stop: Decimal, target: Decimal,
                 ltp: Decimal, t_rem_min: Decimal) -> Decimal:
    """Pure mirror of manage_positions._tighten_stop (#681) — no DB side effects.

    Monotone toward the favorable side; never crosses LTP. Returns the (possibly
    tightened) stop. Identical math to the live loop so the simulator books the
    same exits.
    """
    span = (target - entry) if side == "BUY" else (entry - target)
    if span <= 0:
        return stop
    prog = ((ltp - entry) if side == "BUY" else (entry - ltp)) / span
    floor: Decimal | None = None

    if prog >= EXIT_BREAKEVEN_TRIGGER_R:
        cushion = EXIT_BREAKEVEN_CUSHION_R * span
        floor = entry + cushion if side == "BUY" else entry - cushion

    in_profit = (ltp > entry) if side == "BUY" else (ltp < entry)
    if t_rem_min <= EXIT_TIME_DECAY_START_MIN and in_profit:
        raw = (EXIT_TIME_DECAY_START_MIN - t_rem_min) / EXIT_TIME_DECAY_START_MIN
        lock_frac = max(Decimal("0"), min(raw, EXIT_TIME_DECAY_MAX_LOCK))
        td_floor = entry + lock_frac * (ltp - entry)  # BUY; SELL mirrors via signs
        if floor is None:
            floor = td_floor
        else:
            floor = max(floor, td_floor) if side == "BUY" else min(floor, td_floor)

    if floor is None:
        return stop
    floor = floor.quantize(Decimal("0.01"))
    tighter = (floor > stop) if side == "BUY" else (floor < stop)
    if not tighter:
        return stop
    if (side == "BUY" and floor >= ltp) or (side == "SELL" and floor <= ltp):
        return stop
    return floor


def _t_rem_min(bar_dt_ist: datetime, square_off: time) -> Decimal:
    """Minutes from this bar to the square-off cutoff (same clock the live loop
    uses for the time-decay ratchet)."""
    cutoff = datetime.combine(bar_dt_ist.date(), square_off, tzinfo=IST)
    return Decimal(str((cutoff - bar_dt_ist).total_seconds())) / Decimal("60")


def simulate_trade(
    side: str,
    entry: Decimal,
    stop: Decimal,
    target: Decimal | None,
    qty: int,
    candles: list[dict],
    *,
    apply_ratchet: bool = True,
    square_off_at: time = SQUARE_OFF_AT,
    cost_model=None,
) -> SimOutcome:
    """Replay one trade over the candles that followed its entry.

    `candles` are chronological 1-min bars AFTER the entry bar, each a dict with
    `bar_ts` (tz-aware) and `close`. The exit is booked at the first minute that
    EOD-squares, hits the (possibly ratcheted) stop, or hits the target — in that
    precedence. If no exit fires before the candles run out, the trade is
    UNCLOSED and marked-to-last-close (excluded from honest metrics).

    `apply_ratchet` mirrors the house-only #681 lock-in; pass False to simulate a
    freestyle book (competitors own their stops) or to A/B the ratchet itself.
    """
    side = side.upper()
    entry = Decimal(entry)
    stop = Decimal(stop)
    target = Decimal(target) if target is not None else None

    exit_price: Decimal | None = None
    exit_reason = "UNCLOSED"
    exit_ts: datetime | None = None
    bars_held = 0
    last_close = entry

    for i, bar in enumerate(candles, 1):
        ltp = Decimal(bar["close"])
        last_close = ltp
        bar_dt = bar["bar_ts"].astimezone(IST)
        bars_held = i

        if bar_dt.time() >= square_off_at:
            exit_price, exit_reason, exit_ts = ltp, "EOD", bar_dt
            break

        if apply_ratchet and target is not None:
            stop = ratchet_stop(side, entry, stop, target, ltp,
                                _t_rem_min(bar_dt, square_off_at))

        if side == "BUY":
            if ltp <= stop:
                exit_price, exit_reason, exit_ts = ltp, "STOP", bar_dt
                break
            if target is not None and ltp >= target:
                exit_price, exit_reason, exit_ts = ltp, "TARGET", bar_dt
                break
        else:  # SELL
            if ltp >= stop:
                exit_price, exit_reason, exit_ts = ltp, "STOP", bar_dt
                break
            if target is not None and ltp <= target:
                exit_price, exit_reason, exit_ts = ltp, "TARGET", bar_dt
                break

    closed = exit_price is not None
    if exit_price is None:
        exit_price = last_close  # mark-to-last for an unclosed window

    gross = ((exit_price - entry) if side == "BUY" else (entry - exit_price)) * Decimal(qty)
    # Default (cost_model=None) is the NSE charge model — byte-identical to the
    # eval-gate's prior behaviour; M5 passes a market's cost model for US/crypto.
    if qty > 0:
        charges = (cost_model.round_trip_charges(side, qty, entry, exit_price)
                   if cost_model is not None
                   else round_trip_charges(side, qty, entry, exit_price))
    else:
        charges = Decimal("0")
    net = gross - charges
    return SimOutcome(
        side=side, entry=entry, exit_price=exit_price, qty=qty,
        exit_reason=exit_reason, exit_ts=exit_ts, bars_held=bars_held,
        gross=gross.quantize(Decimal("0.01")),
        charges=charges.quantize(Decimal("0.01")),
        net=net.quantize(Decimal("0.01")),
        closed=closed,
    )


__all__ = ["SimOutcome", "ratchet_stop", "simulate_trade"]
