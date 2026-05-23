"""
System Map page — live knowledge-graph view of the codebase.

Renders the graphify-built knowledge graph inline so you can explore the system's
structure straight from the dashboard: every function/module, the call & import
edges between them, the communities they cluster into, and the "god nodes" that
everything leans on.

The graph is rebuilt on every git commit by the graphify post-commit hook
(code-only AST extraction — no LLM, $0), so this view tracks the current code.

Read-only over the local ``graphify-out/`` artifacts — no Postgres, no LLM.
If the graph hasn't been built yet, the page explains how (one command).
"""
from __future__ import annotations

import json
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from helm.dashboard.format import wrapped_table
from helm.dashboard.theme import apply_theme, kpi_card, kpi_grid, page_header

IST = ZoneInfo("Asia/Kolkata")

# pages/ → dashboard/ → helm/ → repo root
GRAPHIFY_DIR = Path(__file__).resolve().parents[3] / "graphify-out"
GRAPH_HTML = GRAPHIFY_DIR / "graph.html"
GRAPH_JSON = GRAPHIFY_DIR / "graph.json"

# graphify's graph.html pulls the vis-network lib from this CDN. We inline it
# server-side (below) so the embed renders even when the client browser can't
# reach unpkg (ad-blockers, proxies, offline).
_VIS_URL = "https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"
_VIS_CDN_TAG = f'<script src="{_VIS_URL}"></script>'

st.set_page_config(page_title="Helm — System Map", page_icon="🗺️", layout="wide")
apply_theme()
page_header("System Map",
            "Live knowledge graph of the codebase · rebuilt on every commit", icon="🗺️")
st.caption("A navigable map of the system — functions, modules, and the call/import "
           "edges that connect them, clustered into subsystems. Built by graphify "
           "(AST-only, $0) and refreshed automatically by the git post-commit hook.")


# ─── data helpers ──────────────────────────────────────────────────────

def _load_graph() -> dict | None:
    if not GRAPH_HTML.exists() or not GRAPH_JSON.exists():
        return None
    return json.loads(GRAPH_JSON.read_text())


@st.cache_data(ttl=86400, show_spinner=False)
def _vis_network_js() -> str | None:
    """Fetch the vis-network lib once (server-side), cached for a day."""
    try:
        with urllib.request.urlopen(_VIS_URL, timeout=10) as r:
            js = r.read().decode("utf-8")
        # Guard against an embedded "</script>" prematurely closing the inline tag.
        return js.replace("</script", "<\\/script")
    except Exception:
        return None


def _graph_html() -> str:
    """The graph.html with vis-network inlined (falls back to the CDN tag)."""
    html = GRAPH_HTML.read_text()
    js = _vis_network_js()
    if js:
        html = html.replace(_VIS_CDN_TAG, f"<script>{js}</script>")
    return html


def _degrees(g: dict) -> Counter:
    deg: Counter = Counter()
    for link in g.get("links", []):
        deg[link["source"]] += 1
        deg[link["target"]] += 1
    return deg


def _god_nodes(g: dict, top: int = 12) -> pd.DataFrame:
    labels = {n["id"]: n.get("label", n["id"]) for n in g["nodes"]}
    files = {n["id"]: (n.get("source_file") or "") for n in g["nodes"]}
    deg = _degrees(g)
    rows = [
        {"Node": labels.get(nid, nid), "Edges": d, "File": files.get(nid, "")}
        for nid, d in deg.most_common(top)
    ]
    return pd.DataFrame(rows)


def _subsystems(g: dict) -> pd.DataFrame:
    """One row per community: a name derived from its dominant file + size.

    Derived live from the current graph so it stays correct across re-clustering
    (clustering can renumber communities on each rebuild).
    """
    members: dict[int, list[str]] = {}
    for n in g["nodes"]:
        cid = n.get("community")
        if cid is None:
            continue
        members.setdefault(cid, []).append(n.get("source_file") or "")
    rows = []
    for cid, srcs in members.items():
        stems = Counter(Path(s).stem for s in srcs if s)
        name = stems.most_common(1)[0][0].replace("_", " ").title() if stems else f"cluster {cid}"
        rows.append({"Subsystem": name, "Nodes": len(srcs)})
    df = pd.DataFrame(rows).sort_values("Nodes", ascending=False).reset_index(drop=True)
    return df


# ─── render ────────────────────────────────────────────────────────────

g = _load_graph()
if g is None:
    st.warning("No knowledge graph found yet — `graphify-out/` is empty on this host.")
    st.markdown(
        "Build it once (code-only, no LLM cost):\n\n"
        "```bash\n"
        "cd /home/ubuntu/work/personal-ai-trader\n"
        "graphify .            # or run /graphify . inside Claude Code\n"
        "```\n\n"
        "After the first build it refreshes automatically on every commit via the "
        "graphify post-commit hook."
    )
    st.stop()

n_nodes = len(g.get("nodes", []))
n_edges = len(g.get("links", []))
n_comms = len({n.get("community") for n in g["nodes"] if n.get("community") is not None})
built = datetime.fromtimestamp(GRAPH_JSON.stat().st_mtime, IST).strftime("%d %b %H:%M")
inferred = sum(1 for link in g.get("links", []) if link.get("confidence") == "INFERRED")

kpi_grid([
    kpi_card("Nodes", f"{n_nodes:,}", icon="target", sub="functions · classes · modules"),
    kpi_card("Edges", f"{n_edges:,}", icon="repeat", sub=f"{inferred:,} AST-inferred"),
    kpi_card("Subsystems", str(n_comms), icon="folder", sub="clustered communities"),
    kpi_card("Last built", f"{built} IST", icon="clock", sub="auto-refresh on commit"),
])

st.write("")
# Render the interactive graph at the top level — components iframes nested in
# st.tabs collapse to zero height, so the graph gets its own full-width block.
st.caption("Drag to pan · scroll to zoom · click a node to focus. Colours are "
           "communities; node size scales with how connected it is.")
components.html(_graph_html(), height=760, scrolling=True)

st.write("")
tab_gods, tab_subs = st.tabs(["⭐ God nodes", "🧩 Subsystems"])

with tab_gods:
    st.markdown("**The most-connected nodes — the core abstractions everything leans on.** "
                "These bridge many subsystems; refactor them with care.")
    wrapped_table(_god_nodes(g), right_align=["Edges"], clamp_cols={"File": 48})

with tab_subs:
    st.markdown("**Subsystems** — communities the graph detected, named by their dominant "
                "file and ranked by size.")
    wrapped_table(_subsystems(g), right_align=["Nodes"], height=460)
