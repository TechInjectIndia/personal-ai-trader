"""
Integration smoke for the live scan → decide → paper-execute → manage pipeline.

What this exercises (end-to-end against the real Postgres dev DB):

  1. `OpeningRangeBreakout.scan(...)` emits a BUY on a synthetic clean breakout.
  2. `decide_signal_inline(...)` — with the LLM monkeypatched to a deterministic
     TAKE — writes a `decisions` row and books a `paper_trades` row.
  3. The `manage_positions` close-on-target logic (`_close_trade`) marks the
     trade CLOSED with `exit_reason='TARGET'` when price hits the target.

Why we don't use a transactional rollback fixture: `helm.data.store.conn()`
is autocommit, so wrapping each test in a single BEGIN/ROLLBACK isn't possible
without rewriting store.py. Instead each test uses a `ZZZTEST*` symbol
prefix and the module-level fixture deletes every ZZZ-prefixed row in
candles_1m / signals / decisions / paper_trades / audit on setup AND teardown.

The Tester agent's `_stage_pipeline_smoke` shells out to
`pytest -q tests/test_pipeline_smoke.py` to run this file; the file must
pass standalone too (no module fixtures depended on by external code).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from helm.data.store import conn

IST = ZoneInfo("Asia/Kolkata")
TEST_SYMBOL = "ZZZTEST"
TEST_SYMBOL_LIKE = "ZZZ%"


# ─── Synthetic candle helpers ─────────────────────────────────────────

def _build_orb_breakout_candles(symbol: str = TEST_SYMBOL,
                                base_ts: datetime | None = None) -> list[dict]:
    """16 1-min bars that fit the ORB-15m rule: 15 bars in a tight range,
    then a clean breakout bar that closes above the range high.

    OR window: high=101, low=99, width=2.
    Breakout bar: closes 101.80 → entry 101.80, stop 99.00, target 104.80.
    """
    base = base_ts or datetime(2026, 5, 19, 9, 15, tzinfo=IST)
    candles = []
    for i in range(15):
        candles.append({
            "bar_ts": base + timedelta(minutes=i),
            "symbol": symbol,
            "open": Decimal("100.00"),
            "high": Decimal("101.00"),
            "low": Decimal("99.00"),
            "close": Decimal("100.50"),
            "tick_count": 5,
        })
    candles.append({
        "bar_ts": base + timedelta(minutes=15),
        "symbol": symbol,
        "open": Decimal("100.80"),
        "high": Decimal("102.00"),
        "low": Decimal("100.80"),
        "close": Decimal("101.80"),
        "tick_count": 8,
    })
    return candles


def _delete_zzz_rows() -> None:
    """Remove every ZZZ-prefixed row from every table we touch.

    Deletion order respects FK chains: paper_trades → decisions → signals.
    audit and candles_1m have no FKs but we wipe them so the dashboard
    doesn't show test noise.
    """
    with conn() as c:
        # paper_trades / decisions linked to signals on ZZZ symbol
        c.execute(
            """
            DELETE FROM paper_trades
            WHERE symbol LIKE %s
               OR decision_id IN (
                    SELECT d.id FROM decisions d
                    JOIN signals s ON s.id = d.signal_id
                    WHERE s.symbol LIKE %s
                  )
            """,
            (TEST_SYMBOL_LIKE, TEST_SYMBOL_LIKE),
        )
        c.execute(
            """
            DELETE FROM decisions
            WHERE signal_id IN (
                SELECT id FROM signals WHERE symbol LIKE %s
            )
            """,
            (TEST_SYMBOL_LIKE,),
        )
        c.execute("DELETE FROM signals WHERE symbol LIKE %s",
                  (TEST_SYMBOL_LIKE,))
        c.execute("DELETE FROM candles_1m WHERE symbol LIKE %s",
                  (TEST_SYMBOL_LIKE,))
        c.execute("DELETE FROM ticks WHERE symbol LIKE %s",
                  (TEST_SYMBOL_LIKE,))
        # audit is cosmetic; clean entries that mention our symbol to keep
        # the dashboard tidy.
        c.execute(
            "DELETE FROM audit WHERE detail::text LIKE %s",
            (f"%{TEST_SYMBOL}%",),
        )


@pytest.fixture(autouse=True)
def _scrub_zzz_rows():
    """Each test starts and ends with no ZZZ-prefixed rows anywhere."""
    _delete_zzz_rows()
    yield
    _delete_zzz_rows()


# ─── Test 1: strategy emits a signal on synthetic candles ─────────────

def test_scan_emits_signal_for_zzz():
    """OpeningRangeBreakout(or_minutes=15).scan(...) must fire BUY on the
    breakout bar of a hand-built 16-bar series."""
    from helm.strategies import OpeningRangeBreakout
    candles = _build_orb_breakout_candles()

    sig = OpeningRangeBreakout(or_minutes=15).scan(TEST_SYMBOL, candles)

    assert sig is not None, "strategy did not fire on a clean breakout"
    assert sig.side == "BUY"
    assert sig.symbol == TEST_SYMBOL
    assert sig.strategy == "orb_15m"
    assert sig.entry_price == Decimal("101.80")
    assert sig.stop_loss == Decimal("99.00")
    # target = entry + 1.5 × width = 101.80 + 1.5 × 2.00 = 104.80
    assert sig.target == Decimal("104.80")


# ─── Test 2: inline decide → TAKE → paper_trades row ──────────────────

def test_inline_decide_takes_synthetic_signal(monkeypatch):
    """Insert a synthetic ZZZTEST signal, monkeypatch the LLM to TAKE, and
    verify decide_signal_inline writes a decisions row + paper_trades row.

    Monkeypatching strategy: `scripts.decide_signals` imports `decide` from
    `helm.llm` as the local name `llm_decide`. Replacing the symbol on the
    module object that already imported it is what intercepts the call.
    Also patches `helm.llm.decide` itself so any future refactor that
    re-imports at call time still hits the stub.
    """
    import scripts.decide_signals as decide_mod
    import helm.llm as llm_mod

    def fake_decide(system, user, **kwargs):  # noqa: ANN001 — match real sig loosely
        return {"verdict": "TAKE", "confidence": 0.8,
                "reasoning": "smoke test fake TAKE"}

    monkeypatch.setattr(decide_mod, "llm_decide", fake_decide)
    monkeypatch.setattr(llm_mod, "decide", fake_decide)

    # Insert a fresh signal "right now" so it isn't auto-SKIPped as stale.
    # entry_price is sized so a single share fits the wallet comfortably.
    now_ist = datetime.now(IST)
    with conn() as c:
        sig_row = c.execute(
            """
            INSERT INTO signals
                (ts, strategy, symbol, side, entry_price, stop_loss, target,
                 rationale, payload, consumed)
            VALUES (%s, 'orb_15m', %s, 'BUY', %s, %s, %s, %s, %s::jsonb, FALSE)
            RETURNING id
            """,
            (now_ist, TEST_SYMBOL,
             Decimal("101.80"), Decimal("99.00"), Decimal("104.80"),
             "synthetic smoke-test signal",
             '{"smoke": true}'),
        ).fetchone()
    sig_id = sig_row["id"]

    res = decide_mod.decide_signal_inline(sig_id, source="test")
    assert res["verdict"] == "TAKE", f"expected TAKE, got {res!r}"

    # Decisions row exists, marked TAKE, references our signal.
    with conn() as c:
        dec = c.execute(
            "SELECT * FROM decisions WHERE signal_id=%s", (sig_id,),
        ).fetchone()
        assert dec is not None, "no decisions row written"
        assert dec["verdict"] == "TAKE"

        # paper_trades row exists, OPEN, references the decision.
        pt = c.execute(
            "SELECT * FROM paper_trades WHERE decision_id=%s", (dec["id"],),
        ).fetchone()
        assert pt is not None, "no paper_trades row written"
        assert pt["status"] == "OPEN"
        assert pt["symbol"] == TEST_SYMBOL
        assert pt["side"] == "BUY"
        assert int(pt["qty"]) >= 1


# ─── Test 3: manage_positions closes on TARGET ────────────────────────

def test_manage_positions_closes_on_target():
    """Open a synthetic paper_trades row, then call the close helper with a
    price at/above target. Trade must end CLOSED with exit_reason='TARGET'.

    We exercise `manage_positions._close_trade` directly because the cron
    `main()` is window-gated (no-op outside market hours) and pulls LTP
    from yfinance — neither suits a deterministic test. The close logic
    we care about lives in `_close_trade`, called by `main()` once the
    target/stop comparison hits.
    """
    from scripts import manage_positions

    # First, we need a decision to hang the paper trade off (FK constraint).
    with conn() as c:
        sig = c.execute(
            """
            INSERT INTO signals
                (ts, strategy, symbol, side, entry_price, stop_loss, target,
                 rationale, payload, consumed)
            VALUES (now(), 'orb_15m', %s, 'BUY',
                    100.00, 95.00, 105.00, 'smoke', '{}'::jsonb, TRUE)
            RETURNING id
            """,
            (TEST_SYMBOL,),
        ).fetchone()
        dec = c.execute(
            """
            INSERT INTO decisions
                (signal_id, actor, verdict, qty, final_entry, final_stop,
                 final_target, reasoning)
            VALUES (%s, 'test', 'TAKE', 10, 100.00, 95.00, 105.00, 'smoke')
            RETURNING id
            """,
            (sig["id"],),
        ).fetchone()
        trade = c.execute(
            """
            INSERT INTO paper_trades
                (decision_id, symbol, side, qty, entry_price, entry_ts,
                 stop_loss, target, status)
            VALUES (%s, %s, 'BUY', 10, 100.00, now(), 95.00, 105.00, 'OPEN')
            RETURNING *
            """,
            (dec["id"], TEST_SYMBOL),
        ).fetchone()

    assert trade["status"] == "OPEN"

    # Price hits target → close at 105.00 with reason='TARGET'.
    with conn() as c:
        manage_positions._close_trade(c, trade, Decimal("105.00"), "TARGET")

    with conn() as c:
        closed = c.execute(
            "SELECT * FROM paper_trades WHERE id=%s", (trade["id"],),
        ).fetchone()

    assert closed["status"] == "CLOSED"
    assert closed["exit_reason"] == "TARGET"
    assert Decimal(closed["exit_price"]) == Decimal("105.00")
    # gross pnl = (105 - 100) * 10 = 50.00
    assert Decimal(closed["pnl_inr"]) == Decimal("50.00")
    # net_pnl_inr = gross - round-trip MIS charges; should be ≤ gross.
    assert Decimal(closed["net_pnl_inr"]) <= Decimal("50.00")
    # `exit_ts` populated (within the last minute, sanity check).
    assert closed["exit_ts"] is not None
    now_utc = datetime.now(timezone.utc)
    delta = abs((now_utc - closed["exit_ts"]).total_seconds())
    assert delta < 120, f"exit_ts not recent: delta={delta}s"
