"""Helm dashboard — navigation router.

Entry point launched by ``ecosystem.config.js`` / ``streamlit run``. Defines the
sidebar via ``st.navigation`` so we control every page's title, route (url_path),
and grouping — instead of the auto ``pages/`` convention, which derived the labels
(and the ugly top-level "app" entry) from filenames.

Contract: this router issues **no** Streamlit command before ``.run()``. Each page
script keeps its own ``st.set_page_config`` as its first command during its run,
so we don't have to touch the existing page files.
"""
import streamlit as st

# Default / landing page (served at "/").
_overview = st.Page("views/overview.py", title="Overview", icon="🧭", default=True)

# Trading
_summary = st.Page("pages/2_Summary.py", title="Summary", icon="📊", url_path="summary")
_charts = st.Page("pages/4_Charts.py", title="Charts", icon="📈", url_path="charts")
_activity = st.Page("pages/3_Activity_Log.py", title="Activity Log", icon="📜",
                    url_path="activity")
_retros = st.Page("pages/6_Retrospectives.py", title="Retrospectives", icon="🔍",
                  url_path="retros")

# Autonomy
_league = st.Page("pages/8_Competition_League.py", title="Competition League", icon="🏆",
                  url_path="league")
_improve = st.Page("pages/7_Self_Improvement_Loop.py", title="Self-Improvement", icon="🔁",
                   url_path="improve")

# System
_system_map = st.Page("pages/9_System_Map.py", title="System Map", icon="🗺️",
                      url_path="system-map")
_settings = st.Page("pages/5_Settings.py", title="Settings", icon="⚙️", url_path="settings")
_how = st.Page("pages/1_How_It_Works.py", title="How It Works", icon="📖",
               url_path="how-it-works")

st.navigation(
    {
        "": [_overview],
        "Trading": [_summary, _charts, _activity, _retros],
        "Autonomy": [_league, _improve],
        "System": [_system_map, _settings, _how],
    }
).run()
