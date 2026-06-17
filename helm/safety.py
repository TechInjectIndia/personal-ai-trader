"""
Trading-safety guard (S4) — pre-capital, defense-in-depth for the execution path.

These are INDEPENDENT backstops layered on top of the risk gate, for when a
market is funded with real money. They are flag-gated (`SAFETY_GUARD_ENABLED`,
default OFF) so the paper path stays byte-identical until the operator funds a
market. Everything here is a pure function — unit-tested, no DB / clock / RNG.

Covers the llm-trading-agent-security essentials:
  * spend ceiling   — a hard notional cap above the risk cap (mis-config backstop)
  * worst-case loss — entry→stop loss bounded per trade
  * stop sanity     — stop on the correct side of entry (guards the wrong-side bug)
  * circuit breaker — halt the day on a hard loss or a consecutive-loss streak
  * input hygiene   — neutralize prompt-injection in untrusted signal text
"""

from __future__ import annotations

import re
from decimal import Decimal


def pre_trade_check(
    side: str, qty, entry, stop, market: str, *,
    hard_ceiling: Decimal, max_loss: Decimal,
) -> tuple[bool, str]:
    """Independent pre-trade backstop. Returns (ok, reason). Stricter than, and
    additional to, risk.evaluate — a trade must pass BOTH."""
    side = side.upper()
    q, entry, stop = Decimal(qty), Decimal(entry), Decimal(stop)
    if q <= 0:
        return False, "non-positive qty"
    if entry <= 0:
        return False, "non-positive entry price"
    # Stop must sit on the losing side of entry (BUY below, SELL above). A
    # wrong-side stop either self-fills at a better-than-market price or never
    # protects — a real bug class in the live book.
    if side == "BUY" and not stop < entry:
        return False, f"BUY stop {stop} not below entry {entry}"
    if side == "SELL" and not stop > entry:
        return False, f"SELL stop {stop} not above entry {entry}"
    notional = entry * q
    if notional > hard_ceiling:
        return False, f"notional {notional} exceeds hard ceiling {hard_ceiling}"
    worst = abs(entry - stop) * q
    if worst > max_loss:
        return False, f"worst-case loss {worst} exceeds max {max_loss}"
    return True, "ok"


def should_halt(
    realised_loss: Decimal, consecutive_losses: int, *,
    max_daily_loss: Decimal, max_consecutive: int,
) -> bool:
    """Circuit-breaker decision (pure). `realised_loss` is the day's realised P&L
    (negative = loss). Trips on a hard daily loss OR a consecutive-loss streak."""
    if realised_loss <= -abs(Decimal(max_daily_loss)):
        return True
    return bool(max_consecutive) and consecutive_losses >= max_consecutive


# Instruction-like markers an attacker might smuggle through a signal rationale /
# payload to hijack the decider prompt. Matched case-insensitively.
_INJECTION = re.compile(
    r"(?i)(ignore\s+(all|previous|prior)|disregard\s+(the|all|previous)|"
    r"system\s*:|assistant\s*:|</?(system|instructions?|prompt)>|```)"
)


def sanitize_for_prompt(text, max_len: int = 2000) -> str:
    """Neutralize prompt-injection vectors in UNTRUSTED signal text before it
    enters the decider prompt. Identity for normal rationales; drops control
    chars, defangs instruction markers, and caps length."""
    if not text:
        return ""
    cleaned = "".join(ch for ch in str(text) if ch == "\n" or ch >= " ")
    cleaned = _INJECTION.sub("[redacted]", cleaned)
    return cleaned[:max_len]


__all__ = ["pre_trade_check", "should_halt", "sanitize_for_prompt"]
