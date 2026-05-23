"""
Common datetime formatters for the dashboard.

Single source of truth for how timestamps are rendered in the UI:
  - DB timestamps (psycopg `timestamptz`) → readable IST strings via `fmt_ist`
  - Live wall-clock display ("now") → `fmt_clock`
  - `datetime.time` trading-window labels → `fmt_window`

All output is in Asia/Kolkata. Every datetime shown to the user must pass
through one of these functions — don't `.strftime()` directly in app code.

Also exports `wrapped_table` — a drop-in replacement for `st.dataframe` that
renders an HTML table where cells wrap (Excel/Sheets-style) instead of
truncating with ellipsis. `st.dataframe` is canvas-based and can't be made
to wrap via CSS, hence this helper.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

IST = ZoneInfo("Asia/Kolkata")

_DT_FMT = "%d %b %I:%M:%S %p IST"   # "06 May 02:32:05 PM IST"
_DT_SHORT_FMT = "%d %b %I:%M %p"    # "06 May 02:32 PM" — compact, for dense tables
_CLOCK_FMT = "%I:%M:%S %p"          # "02:32:05 PM"
_WINDOW_FMT = "%I:%M %p IST"        # "09:15 AM IST"


def fmt_ist(dt) -> str:
    """Full DB-timestamp display in IST. Accepts datetime, pd.Timestamp, None, NaT."""
    if dt is None or pd.isna(dt):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).strftime(_DT_FMT)


def fmt_ist_short(dt) -> str:
    """Compact DB-timestamp ("06 May 02:32 PM") — for dense/narrow table cells
    where the full ``fmt_ist`` (with seconds + IST suffix) wraps awkwardly."""
    if dt is None or pd.isna(dt):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).strftime(_DT_SHORT_FMT)


def fmt_clock(dt: datetime | None = None) -> str:
    """Wall-clock time only (no date, no suffix). Defaults to now()."""
    dt = dt or datetime.now(IST)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).strftime(_CLOCK_FMT)


def fmt_window(t: time) -> str:
    """Time-of-day for trading-window labels (TRADING_START / END / SQUARE_OFF_AT)."""
    return t.strftime(_WINDOW_FMT)


_WRAP_TABLE_CSS = """
<style>
div.helm-tbl {
  width: 100%; border: 1px solid #232B3A; border-radius: 12px;
  overflow-x: auto; background: #11161F;
}
div.helm-tbl table {
  border-collapse: collapse; width: 100%;
  font-size: 0.86rem; font-variant-numeric: tabular-nums;
  font-family: 'Inter', -apple-system, 'Segoe UI', sans-serif;
}
div.helm-tbl thead th {
  text-align: left; padding: 10px 14px; font-weight: 600;
  font-size: 0.72rem; letter-spacing: 0.05em; text-transform: uppercase;
  color: #94A2B8;
  background: #161C27;
  border-bottom: 1px solid #232B3A;
  position: sticky; top: 0; z-index: 1;
  white-space: nowrap;
}
div.helm-tbl tbody td {
  padding: 9px 14px; vertical-align: top; color: #DCE3EE;
  border-bottom: 1px solid rgba(255,255,255,0.05);
  /* break-word (not anywhere): wrap long free-text, but never collapse a
     short cell into one-character-per-line in a narrow column. */
  white-space: normal; word-break: normal; overflow-wrap: break-word;
  line-height: 1.45;
}
div.helm-tbl tbody tr:nth-child(even) td { background: rgba(255,255,255,0.015); }
div.helm-tbl tbody tr:hover td { background: rgba(99,102,241,0.08); }
div.helm-tbl tbody tr:last-child td { border-bottom: none; }
</style>
"""


def wrapped_table(
    df: pd.DataFrame,
    *,
    height: int | None = None,
    right_align: "set[str] | list[str] | None" = None,
    clamp_cols: "dict[str, int] | set[str] | list[str] | None" = None,
    single_line: bool = False,
) -> None:
    """Render a DataFrame as a wrapping HTML table — Excel/Sheets-like cells.

    Drop-in replacement for ``st.dataframe(df, use_container_width=True,
    hide_index=True)``. Cells wrap and the row grows vertically to fit
    long text (rationale, audit detail, summaries) instead of being
    truncated with an ellipsis until double-click.

    Tradeoff: loses ``st.dataframe``'s interactive sort/resize. Use the
    native widget for purely numeric tables where those matter more.

    Parameters
    ----------
    df : pd.DataFrame
        The frame to render. The index is hidden.
    height : int, optional
        If given, wraps the table in a vertically scrollable box of that
        pixel height (header stays sticky). Omit to let the table grow.
    right_align : set or list of str, optional
        Column headers to right-align (numeric / currency columns read better
        right-aligned). Matched against ``df.columns``.
    clamp_cols : dict[str, int] or set/list of str, optional
        Free-text columns (e.g. "Reasoning") to render as a single ellipsized
        line so a long value can't blow up the row height. As a dict, maps
        column -> max px width; as a set/list, uses a 360px default.
    single_line : bool, default False
        Keep every cell on one line (no wrapping); the table keeps its natural
        width and scrolls horizontally instead of growing rows tall. Use for
        dense detail logs. ``clamp_cols`` still ellipsize their long columns.
    """
    html = df.to_html(index=False, escape=True, border=0)
    cols = list(df.columns)
    rules: list[str] = []
    tid = "t" + uuid.uuid4().hex[:8]

    if single_line:
        rules.append(f"#{tid} td{{white-space:nowrap;}}")
    if right_align:
        idxs = [cols.index(c) + 1 for c in right_align if c in cols]
        if idxs:
            sel = ",".join(
                f"#{tid} td:nth-child({i}),#{tid} th:nth-child({i})" for i in idxs
            )
            rules.append(f"{sel}{{text-align:right;}}")
    if clamp_cols:
        widths = clamp_cols if isinstance(clamp_cols, dict) else {c: 360 for c in clamp_cols}
        for col, px in widths.items():
            if col in cols:
                i = cols.index(col) + 1
                # single ellipsized line: the cell can't grow the row height,
                # and max-width caps it so it never dominates the table.
                rules.append(
                    f"#{tid} td:nth-child({i}){{max-width:{int(px)}px;white-space:nowrap;"
                    "overflow:hidden;text-overflow:ellipsis;}"
                )

    extra = ""
    if rules:
        extra = f"<style>{''.join(rules)}</style>"
        html = html.replace("<table", f'<table id="{tid}"', 1)
    inner = (
        f'<div style="max-height:{int(height)}px;overflow:auto;">{html}</div>'
        if height
        else html
    )
    st.markdown(
        _WRAP_TABLE_CSS + extra + f'<div class="helm-tbl">{inner}</div>',
        unsafe_allow_html=True,
    )
