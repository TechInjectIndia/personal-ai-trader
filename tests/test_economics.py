"""F1 — unit economics analytics."""
from __future__ import annotations

from decimal import Decimal

from helm.analytics.economics import Economics, _ratio, _row_to_econ, book_economics


def test_ratio_zero_guard():
    assert _ratio(Decimal("5"), Decimal("0")) == Decimal("0")
    assert _ratio(None, None) == Decimal("0")
    assert _ratio(Decimal("10"), Decimal("4")) == Decimal("2.5")


def test_row_to_econ_cost_drag_case():
    """Gross positive but net negative because charges exceed gross — the core
    pathology F1 exists to expose."""
    row = {
        "n": 10,
        "gross": Decimal("100"), "charges": Decimal("250"), "net": Decimal("-150"),
        "net_wins": 3,
        "avg_abs_move": Decimal("30"), "avg_charge": Decimal("25"),
        "gross_avg_win": Decimal("50"), "gross_avg_loss": Decimal("-20"),
        "net_avg_win": Decimal("38"), "net_avg_loss": Decimal("-35"),
    }
    e = _row_to_econ(row)
    assert e.gross_pnl == Decimal("100.00")
    assert e.net_pnl == Decimal("-150.00")
    assert e.gross_expectancy == Decimal("10.00")
    assert e.net_expectancy == Decimal("-15.00")
    assert e.cost_drag_pct == Decimal("250.00")      # 250 / |100| * 100
    assert e.win_pct == Decimal("30.00")             # 3/10
    assert e.gross_payoff == Decimal("2.50")         # 50 / 20
    assert e.realised_e2c == Decimal("1.20")         # 30 / 25


def test_row_to_econ_empty():
    e = _row_to_econ({"n": 0, "gross": None, "charges": None, "net": None,
                      "net_wins": 0, "avg_abs_move": None, "avg_charge": None,
                      "gross_avg_win": None, "gross_avg_loss": None,
                      "net_avg_win": None, "net_avg_loss": None})
    assert e.n == 0 and e.net_pnl == Decimal("0.00") and e.realised_e2c == Decimal("0.00")


def test_book_economics_smoke():
    """Runs against the live DB read-only — shapes, not exact values."""
    agg = book_economics()
    assert isinstance(agg, Economics)
    by_strat = book_economics(group_by="strategy")
    assert isinstance(by_strat, dict)
    assert all(isinstance(v, Economics) for v in by_strat.values())
