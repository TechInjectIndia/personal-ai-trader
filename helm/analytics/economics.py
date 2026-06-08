"""Unit economics of the trading book (F1) — read-only.

Surfaces the truth the self-improvement loop was blind to: gross P&L is ~flat;
the loss is almost entirely transaction costs. Splits gross vs cost vs net and
computes the edge-to-cost ratio that actually governs profitability, sliced by
strategy / competitor / symbol / exit_reason. No DB writes.

The headline metric is **realised E2C** = avg |gross move| / avg charge. Below
~3 the fixed per-trade cost collapses the payoff ratio and the book bleeds even
when gross is flat.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from helm.config import HOUSE_COMPETITOR_ID, HOUSE_TRADE_FILTER
from helm.data.store import conn

# Test/smoke rows must never pollute economics (mirrors weekend_report.TEST_PREDICATE
# semantics; uses left() not LIKE so the '%' can't collide with psycopg placeholders).
_TEST_PREDICATE = "pt.competitor_id IS NOT NULL AND left(pt.competitor_id, 3) <> 'zzz'"

_GROUP_EXPR = {
    "strategy": "COALESCE(s.strategy, 'unattributed')",
    "competitor_id": "COALESCE(pt.competitor_id, 'house')",
    "symbol": "pt.symbol",
    "exit_reason": "COALESCE(pt.exit_reason, 'open')",
}

_Z = Decimal("0")

# Fallback for the ungrouped aggregate (a COUNT/agg query always returns one row,
# but fetchone() is typed dict|None — coalesce so the type-checker is happy).
_EMPTY_ROW = {
    "n": 0, "gross": None, "charges": None, "net": None, "net_wins": 0,
    "avg_abs_move": None, "avg_charge": None, "gross_avg_win": None,
    "gross_avg_loss": None, "net_avg_win": None, "net_avg_loss": None,
}


@dataclass(frozen=True)
class Economics:
    n: int
    gross_pnl: Decimal
    charges: Decimal
    net_pnl: Decimal
    gross_expectancy: Decimal      # gross_pnl / n
    net_expectancy: Decimal        # net_pnl / n
    cost_drag_pct: Decimal         # charges / |gross_pnl| * 100
    win_pct: Decimal               # net winners / n * 100
    gross_payoff: Decimal          # avg gross win / |avg gross loss|
    net_payoff: Decimal            # avg net win / |avg net loss|
    avg_abs_gross_move: Decimal    # avg |pnl_inr|
    realised_e2c: Decimal          # avg_abs_gross_move / avg charge

    def as_dict(self) -> dict[str, str | int]:
        return {
            "n": self.n,
            "gross_pnl": str(self.gross_pnl),
            "charges": str(self.charges),
            "net_pnl": str(self.net_pnl),
            "gross_expectancy": str(self.gross_expectancy),
            "net_expectancy": str(self.net_expectancy),
            "cost_drag_pct": str(self.cost_drag_pct),
            "win_pct": str(self.win_pct),
            "gross_payoff": str(self.gross_payoff),
            "net_payoff": str(self.net_payoff),
            "avg_abs_gross_move": str(self.avg_abs_gross_move),
            "realised_e2c": str(self.realised_e2c),
        }


def _ratio(num: Decimal | None, den: Decimal | None) -> Decimal:
    num = num or _Z
    den = den or _Z
    return (num / den) if den != 0 else _Z


def _quant(d: Decimal, places: str = "0.01") -> Decimal:
    return d.quantize(Decimal(places))


def _row_to_econ(r: dict) -> Economics:
    n = r["n"] or 0
    gross = r["gross"] or _Z
    charges = r["charges"] or _Z
    net = r["net"] or _Z
    nd = Decimal(n) if n else _Z
    return Economics(
        n=n,
        gross_pnl=_quant(gross),
        charges=_quant(charges),
        net_pnl=_quant(net),
        gross_expectancy=_quant(_ratio(gross, nd)),
        net_expectancy=_quant(_ratio(net, nd)),
        cost_drag_pct=_quant(_ratio(charges, abs(gross)) * 100),
        win_pct=_quant(_ratio(Decimal(r["net_wins"] or 0), nd) * 100),
        gross_payoff=_quant(_ratio(r["gross_avg_win"], abs(r["gross_avg_loss"] or _Z))),
        net_payoff=_quant(_ratio(r["net_avg_win"], abs(r["net_avg_loss"] or _Z))),
        avg_abs_gross_move=_quant(r["avg_abs_move"] or _Z),
        realised_e2c=_quant(_ratio(r["avg_abs_move"], r["avg_charge"])),
    )


def _competitor_clause(competitor_filter: str | None) -> tuple[str, tuple]:
    if competitor_filter is None:
        return "", ()
    if competitor_filter == HOUSE_COMPETITOR_ID:
        # HOUSE_TRADE_FILTER references bare competitor_id; alias to pt.
        return " AND " + HOUSE_TRADE_FILTER.replace("competitor_id", "pt.competitor_id"), ()
    return " AND pt.competitor_id = %s", (competitor_filter,)


_AGG = """
    count(*) n,
    sum(pt.pnl_inr) gross, sum(pt.charges_inr) charges, sum(pt.net_pnl_inr) net,
    sum((pt.net_pnl_inr > 0)::int) net_wins,
    avg(abs(pt.pnl_inr)) avg_abs_move,
    avg(pt.charges_inr) avg_charge,
    avg(pt.pnl_inr)     FILTER (WHERE pt.pnl_inr > 0)      gross_avg_win,
    avg(pt.pnl_inr)     FILTER (WHERE pt.pnl_inr <= 0)     gross_avg_loss,
    avg(pt.net_pnl_inr) FILTER (WHERE pt.net_pnl_inr > 0)  net_avg_win,
    avg(pt.net_pnl_inr) FILTER (WHERE pt.net_pnl_inr <= 0) net_avg_loss
"""


def book_economics(
    *,
    group_by: str | None = None,
    competitor_filter: str | None = None,
    since: datetime | None = None,
    last_n: int | None = None,
) -> dict[str, Economics] | Economics:
    """Economics over CLOSED real trades.

    - group_by ∈ {'strategy','competitor_id','symbol','exit_reason'} → dict keyed
      by group; None → a single aggregate Economics.
    - competitor_filter: a competitor_id; the house id expands to HOUSE_TRADE_FILTER.
    - since: only trades with exit_ts >= since.
    - last_n: only the most recent N trades (ungrouped only; ignored with group_by).
    """
    if group_by is not None and group_by not in _GROUP_EXPR:
        raise ValueError(f"bad group_by {group_by!r}")

    where = ["pt.status = 'CLOSED'", _TEST_PREDICATE]
    params: list = []
    comp_sql, comp_params = _competitor_clause(competitor_filter)
    base_where = " AND ".join(where) + comp_sql
    params.extend(comp_params)
    if since is not None:
        base_where += " AND pt.exit_ts >= %s"
        params.append(since)

    joins = ("LEFT JOIN decisions d ON d.id = pt.decision_id "
             "LEFT JOIN signals s ON s.id = d.signal_id")

    with conn() as c:
        if group_by:
            grp = _GROUP_EXPR[group_by]
            sql = (f"SELECT {grp} grp, {_AGG} FROM paper_trades pt {joins} "
                   f"WHERE {base_where} GROUP BY {grp} ORDER BY net")
            rows = c.execute(sql, tuple(params)).fetchall()
            return {str(r["grp"]): _row_to_econ(r) for r in rows}

        # Ungrouped — optionally restrict to the most recent N trades.
        if last_n:
            sql = (f"SELECT {_AGG} FROM (SELECT pt.* FROM paper_trades pt "
                   f"WHERE {base_where} ORDER BY pt.exit_ts DESC LIMIT %s) pt")
            row = c.execute(sql, (*params, last_n)).fetchone()
        else:
            sql = f"SELECT {_AGG} FROM paper_trades pt WHERE {base_where}"
            row = c.execute(sql, tuple(params)).fetchone()
        return _row_to_econ(row or _EMPTY_ROW)


# ── F5 measurement gate: decider confidence ↔ trade outcome (READ-ONLY) ──────
# The FRD (docs/frd/F5-conviction-weighted-sizing.md §7) gates conviction *scaling*
# on first *measuring* whether confidence predicts outcome. Confidence is NOT a
# column; the decider tags it into decisions.reasoning as `conf=0.NN`
# (scripts/decide_signals.py:298 — `[{source} {mode}/{model} conf={confidence:.2f}]`).
# We parse it back out and correlate against realised net P&L of the trade it opened.

# Postgres-side parse of the `conf=0.NN` tag (always 2 decimals via `:.2f`).
_CONF_EXPR = "(substring(d.reasoning from 'conf=([0-9]\\.[0-9]+)'))::numeric"

# Fixed confidence bands anchored on the FRD's CONVICTION_FLOOR=0.55 (§4).
# (low, high] half-open on the right; first band is closed on the left at 0.
_CONF_BANDS: tuple[tuple[str, Decimal, Decimal], ...] = (
    ("<0.55", Decimal("0.00"), Decimal("0.55")),
    ("0.55–0.65", Decimal("0.55"), Decimal("0.65")),
    ("0.65–0.75", Decimal("0.65"), Decimal("0.75")),
    ("0.75–0.85", Decimal("0.75"), Decimal("0.85")),
    ("≥0.85", Decimal("0.85"), Decimal("1.01")),
)


@dataclass(frozen=True)
class ConfBucket:
    band: str
    lo: Decimal
    hi: Decimal
    n: int
    win_pct: Decimal        # net winners / n * 100
    net_expectancy: Decimal  # sum(net_pnl) / n
    net_pnl: Decimal         # sum(net_pnl)

    def as_dict(self) -> dict[str, str | int]:
        return {
            "band": self.band,
            "lo": str(self.lo),
            "hi": str(self.hi),
            "n": self.n,
            "win_pct": str(self.win_pct),
            "net_expectancy": str(self.net_expectancy),
            "net_pnl": str(self.net_pnl),
        }


@dataclass(frozen=True)
class ConfidenceOutcome:
    n: int                       # total CLOSED TAKE trades carrying a conf tag
    corr: Decimal | None         # Pearson corr(conf, net_pnl_inr); None if undefined
    buckets: tuple[ConfBucket, ...]
    verdict: str                 # plain-English read of the gate

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "corr": (str(self.corr) if self.corr is not None else None),
            "verdict": self.verdict,
            "buckets": [b.as_dict() for b in self.buckets],
        }


def _conf_verdict(n: int, corr: Decimal | None, buckets: tuple[ConfBucket, ...]) -> str:
    """Translate the correlation sign + sample size into an F5 go/no-go read.

    Deliberately conservative: thin data (the live book has ~50 conf-tagged closed
    TAKEs) makes any single correlation noisy, so we never green-light scaling on
    weak/ambiguous evidence."""
    if n < 20 or corr is None:
        return (f"insufficient data (n={n}) — keep measuring, do NOT enable "
                "conviction scaling yet")
    if corr >= Decimal("0.15"):
        return (f"conf↔net corr = +{corr} → positive: higher confidence tends to "
                "win more; F5 scaling looks justified (confirm on more data)")
    if corr <= Decimal("-0.15"):
        return (f"conf↔net corr = {corr} → negative: confidence is anti-predictive; "
                "do NOT enable scaling")
    return (f"conf↔net corr = {corr} → ~0: no reliable signal; do NOT enable scaling")


def confidence_outcome_correlation(
    *,
    competitor_filter: str | None = None,
    since: datetime | None = None,
) -> ConfidenceOutcome:
    """Measure decider confidence vs realised outcome over CLOSED TAKE trades.

    READ-ONLY. Parses the `conf=0.NN` tag the decider writes into
    decisions.reasoning, joins to the paper_trades row that TAKE opened, and
    reports per-confidence-band n / win% / net expectancy plus a Pearson
    correlation sign between confidence and net P&L. This is the F5 measurement
    gate: scaling is only justified if the correlation is real and positive.

    - competitor_filter: a competitor_id; the house id expands to HOUSE_TRADE_FILTER.
    - since: only trades with exit_ts >= since.
    """
    where = [
        "pt.status = 'CLOSED'",
        "d.verdict = 'TAKE'",
        _TEST_PREDICATE,
        f"{_CONF_EXPR} IS NOT NULL",
    ]
    params: list = []
    comp_sql, comp_params = _competitor_clause(competitor_filter)
    base_where = " AND ".join(where) + comp_sql
    params.extend(comp_params)
    if since is not None:
        base_where += " AND pt.exit_ts >= %s"
        params.append(since)

    # INNER JOIN: we require both the conf tag (decisions) and an outcome
    # (paper_trades). Blocked/SKIP TAKEs never inserted a trade row, so they
    # drop out naturally and correctly (no outcome to measure).
    joins = "JOIN decisions d ON d.id = pt.decision_id"

    band_cases = " ".join(
        f"WHEN {_CONF_EXPR} > {lo} AND {_CONF_EXPR} <= {hi} THEN '{band}'"
        if i > 0 else
        f"WHEN {_CONF_EXPR} >= {lo} AND {_CONF_EXPR} <= {hi} THEN '{band}'"
        for i, (band, lo, hi) in enumerate(_CONF_BANDS)
    )
    band_expr = f"CASE {band_cases} END"

    sql = (
        f"SELECT {band_expr} AS band, count(*) n, "
        f"sum((pt.net_pnl_inr > 0)::int) net_wins, "
        f"sum(pt.net_pnl_inr) net "
        f"FROM paper_trades pt {joins} WHERE {base_where} GROUP BY band"
    )
    corr_sql = (
        f"SELECT count(*) n, corr({_CONF_EXPR}, pt.net_pnl_inr) corr "
        f"FROM paper_trades pt {joins} WHERE {base_where}"
    )

    with conn() as c:
        rows = c.execute(sql, tuple(params)).fetchall()
        crow = c.execute(corr_sql, tuple(params)).fetchone() or {"n": 0, "corr": None}

    by_band = {r["band"]: r for r in rows}
    buckets: list[ConfBucket] = []
    for band, lo, hi in _CONF_BANDS:
        r = by_band.get(band)
        n = (r["n"] if r else 0) or 0
        nd = Decimal(n) if n else _Z
        net = (r["net"] if r else None) or _Z
        wins = (r["net_wins"] if r else 0) or 0
        buckets.append(ConfBucket(
            band=band, lo=lo, hi=hi, n=n,
            win_pct=_quant(_ratio(Decimal(wins), nd) * 100),
            net_expectancy=_quant(_ratio(net, nd)),
            net_pnl=_quant(net),
        ))

    total_n = crow["n"] or 0
    corr_raw = crow["corr"]
    corr = _quant(Decimal(str(corr_raw))) if corr_raw is not None else None
    verdict = _conf_verdict(total_n, corr, tuple(buckets))
    return ConfidenceOutcome(n=total_n, corr=corr, buckets=tuple(buckets), verdict=verdict)
