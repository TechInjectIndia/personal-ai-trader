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

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

IST = ZoneInfo("Asia/Kolkata")

_DT_FMT = "%d %b %I:%M:%S %p IST"   # "06 May 02:32:05 PM IST"
_CLOCK_FMT = "%I:%M:%S %p"          # "02:32:05 PM"
_WINDOW_FMT = "%I:%M %p IST"        # "09:15 AM IST"


def fmt_ist(dt) -> str:
    """Full DB-timestamp display in IST. Accepts datetime, pd.Timestamp, None, NaT."""
    if dt is None or pd.isna(dt):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).strftime(_DT_FMT)


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
div.helm-tbl { width: 100%; }
div.helm-tbl table {
  border-collapse: collapse; width: 100%;
  font-size: 0.875rem; font-variant-numeric: tabular-nums;
}
div.helm-tbl thead th {
  text-align: left; padding: 8px 12px; font-weight: 600;
  background: rgba(127,127,127,0.10);
  border-bottom: 1px solid rgba(127,127,127,0.30);
  position: sticky; top: 0; z-index: 1;
  white-space: nowrap;
}
div.helm-tbl tbody td {
  padding: 8px 12px; vertical-align: top;
  border-bottom: 1px solid rgba(127,127,127,0.18);
  white-space: normal; word-break: normal; overflow-wrap: anywhere;
  line-height: 1.4;
}
div.helm-tbl tbody tr:hover td { background: rgba(127,127,127,0.06); }
div.helm-tbl tbody tr:last-child td { border-bottom: none; }
</style>
"""


def wrapped_table(df: pd.DataFrame, *, height: int | None = None) -> None:
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
    """
    html = df.to_html(index=False, escape=True, border=0)
    inner = (
        f'<div style="max-height:{int(height)}px;overflow:auto;">{html}</div>'
        if height
        else html
    )
    st.markdown(
        _WRAP_TABLE_CSS + f'<div class="helm-tbl">{inner}</div>',
        unsafe_allow_html=True,
    )
