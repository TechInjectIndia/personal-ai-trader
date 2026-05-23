"""Shared agent (competition-league) scoping for dashboard pages.

The league turned single-agent tables multi-agent. These helpers render a
consistent "Agent" selector and turn the choice into a SQL predicate + params,
so every page filters the competition-bearing tables (paper_trades, signals,
decisions, metrics_snapshots, agent_tasks, releases, agent_runs,
improvement_proposals — all carry a ``competitor_id``) the same way.

Selection sentinels: ``SCOPE_ALL`` (every agent, no filter) or a competitor id.
The House option keeps the legacy semantics (``competitor_id IS NULL OR
= 'house-claude'``) so house views stay consistent with the wallet.
"""

from __future__ import annotations

import streamlit as st

from helm.config import HOUSE_TRADE_FILTER
from helm.data.store import conn

SCOPE_ALL = "__all__"
HOUSE_ID = "house-claude"


def competitor_roster() -> list[tuple[str, str]]:
    """``(id, name)`` for every competitor, incumbent (house) first."""
    with conn() as c:
        rows = list(c.execute(
            "SELECT id, name FROM competitors "
            "ORDER BY (autonomy_level <> 'incumbent'), name"
        ))
    return [(r["id"], r["name"]) for r in rows]


def agent_name(cid: "str | None", roster: "dict[str, str] | None" = None) -> str:
    """Display name for a ``competitor_id`` (NULL/house-claude -> the house name)."""
    roster = roster if roster is not None else dict(competitor_roster())
    if cid is None or cid == HOUSE_ID:
        return roster.get(HOUSE_ID, "House (Claude)")
    return roster.get(cid, cid)


def agent_selectbox(
    *,
    label: str = "Agent",
    include_all: bool = True,
    default: str = HOUSE_ID,
    key: "str | None" = None,
    container=None,
) -> "tuple[str, dict[str, str]]":
    """Render the Agent selector; return ``(selection, id->name)``.

    ``selection`` is :data:`SCOPE_ALL` or a competitor id. ``default`` picks the
    initial option (house by default; pass :data:`SCOPE_ALL` for all-agents).
    ``container`` lets the caller place it inside a column.
    """
    roster = competitor_roster()
    names = dict(roster)
    options = ([SCOPE_ALL] if include_all else []) + [cid for cid, _ in roster]
    index = options.index(default) if default in options else 0
    widget = (container or st).selectbox(
        label, options, index=index,
        format_func=lambda o: "All agents" if o == SCOPE_ALL else names.get(o, o),
        key=key,
    )
    return widget, names


def agent_label(selection: str, names: "dict[str, str] | None" = None) -> str:
    """Human label for a selection (``SCOPE_ALL`` -> "All agents")."""
    if selection == SCOPE_ALL:
        return "All agents"
    return (names or dict(competitor_roster())).get(selection, selection)


def scope_predicate(selection: str, *, alias: str = "") -> "tuple[str, list]":
    """SQL predicate + params for a selection.

    ``alias`` qualifies the column for joined queries (e.g. ``"pt"`` ->
    ``pt.competitor_id``); ``""`` uses the bare ``competitor_id``. Returns a
    predicate safe to drop after ``AND`` and a params list to splice into the
    query args.
    """
    col = f"{alias}.competitor_id" if alias else "competitor_id"
    if selection == SCOPE_ALL:
        return "TRUE", []
    if selection == HOUSE_ID:
        return HOUSE_TRADE_FILTER.replace("competitor_id", col), []
    return f"{col} = %s", [selection]
