"""
Helm dashboard design system — "dark fintech terminal".

One import, two calls per page:

    from helm.dashboard.theme import apply_theme, page_header
    st.set_page_config(...)          # must stay the first st.* call
    apply_theme()                    # injects the global stylesheet
    page_header("Title", "subtitle") # branded header band

`apply_theme()` restyles native Streamlit widgets (metrics → cards, alerts,
buttons, tabs, expanders, progress, sidebar, tables) so every page lifts at
once without rewriting page bodies. `page_header()` and `pill()` add the
brand chrome. Colour tokens are exported for chart theming (Plotly) so the
charts match the rest of the UI.

Selectors target Streamlit's stable ``data-testid`` hooks (not hashed class
names) so the styling survives minor Streamlit upgrades.
"""

from __future__ import annotations

import html
import re

import streamlit as st
import streamlit.components.v1 as components

# ─── palette (keep in sync with .streamlit/config.toml) ────────────────
BG = "#0B0E14"           # app background (near-black navy)
SURFACE = "#151A23"      # base card / secondary background
SURFACE_2 = "#1B212C"    # elevated card
BORDER = "#232B3A"       # hairline borders
TEXT = "#E6EDF3"         # primary text
TEXT_MUTED = "#94A2B8"   # secondary text / labels
TEXT_FAINT = "#5E6B80"   # captions / tertiary

PRIMARY = "#6366F1"      # indigo — brand / actions
ACCENT = "#818CF8"       # lighter indigo — links / highlights
POS = "#22C55E"          # gains (emerald)
NEG = "#F43F5E"          # losses (rose)
WARN = "#F59E0B"         # caution (amber)
INFO = "#38BDF8"         # info (sky)

# Convenience grouping for Plotly charts that want to match the theme.
CHART = {
    "bg": BG,
    "surface": SURFACE,
    "grid": "rgba(255,255,255,0.06)",
    "text": TEXT_MUTED,
    "primary": PRIMARY,
    "pos": POS,
    "neg": NEG,
    "palette": [PRIMARY, POS, "#38BDF8", "#F59E0B", "#F43F5E", "#A78BFA"],
}


_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');

/* ── base typography & canvas ─────────────────────────────────────── */
html, body, [data-testid="stAppViewContainer"], .stApp,
[data-testid="stMarkdownContainer"], [data-testid="stWidgetLabel"] {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}
[data-testid="stAppViewContainer"] {
  background:
    radial-gradient(1100px 560px at 85% -12%, rgba(99,102,241,0.12), transparent 60%),
    radial-gradient(820px 460px at -8% 8%, rgba(34,197,94,0.06), transparent 55%),
    #0B0E14;
}
.block-container, [data-testid="stMainBlockContainer"] {
  padding-top: 2.0rem; padding-bottom: 4rem; max-width: 1400px;
}

/* tighter, sharper headings */
h1 { font-weight: 800 !important; letter-spacing: -0.025em !important; }
h2 { font-weight: 700 !important; letter-spacing: -0.015em !important; }
h3 { font-weight: 700 !important; letter-spacing: -0.01em !important;
     color: #E6EDF3; }
[data-testid="stHeading"] { scroll-margin-top: 4rem; }
a, a:visited { color: #818CF8; text-decoration: none; }
a:hover { color: #A5B4FC; text-decoration: underline; }
code, kbd, pre, [data-testid="stCode"] * { font-family: 'JetBrains Mono', monospace !important; }

/* ── hide default Streamlit chrome for a product feel ─────────────── */
/* The smooth-scroll injector (a 0-height components.html iframe) is a flex
   item in the main block, so even at 0 height it consumes the block's `gap`
   and pushes content down. Pull it out of flow (it still runs its script). */
[data-testid="stElementContainer"]:has(> [data-testid="stIFrame"]) {
  position: absolute !important; height: 0 !important; width: 0 !important;
  margin: 0 !important; overflow: hidden !important; pointer-events: none;
}
[data-testid="stDecoration"] { display: none !important; }
[data-testid="stToolbar"]    { display: none !important; }
#MainMenu                    { display: none !important; }
[data-testid="stHeader"]     { background: transparent !important; height: 0; }
footer                       { display: none !important; }

/* ── metric → KPI card ────────────────────────────────────────────── */
[data-testid="stMetric"] {
  background: linear-gradient(180deg, #171D28 0%, #12161F 100%);
  border: 1px solid #232B3A;
  border-radius: 14px;
  padding: 16px 18px 14px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.04), 0 8px 22px rgba(0,0,0,0.28);
  transition: transform .16s ease, border-color .16s ease, box-shadow .16s ease;
}
[data-testid="stMetric"]:hover {
  transform: translateY(-2px);
  border-color: #33405A;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 12px 28px rgba(0,0,0,0.36);
}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] p {
  font-size: 0.72rem !important; font-weight: 600 !important;
  letter-spacing: 0.07em; text-transform: uppercase; color: #94A2B8 !important;
}
[data-testid="stMetricValue"] {
  font-size: 1.7rem !important; font-weight: 700 !important;
  letter-spacing: -0.02em; font-variant-numeric: tabular-nums;
  color: #F2F6FB !important;
}
[data-testid="stMetricDelta"] { font-weight: 600 !important; font-size: 0.82rem !important; }

/* ── buttons ──────────────────────────────────────────────────────── */
[data-testid="stButton"] button, [data-testid="stFormSubmitButton"] button {
  border-radius: 10px; font-weight: 600; border: 1px solid #2A3344;
  transition: transform .12s ease, box-shadow .12s ease, background .12s ease;
}
[data-testid="stButton"] button:hover { transform: translateY(-1px); }
button[kind="primary"], [data-testid="stBaseButton-primary"] {
  background: linear-gradient(180deg, #6D6FF5 0%, #4F46E5 100%) !important;
  border: none !important; color: #fff !important;
  box-shadow: 0 6px 18px rgba(79,70,229,0.40) !important;
}
button[kind="primary"]:hover, [data-testid="stBaseButton-primary"]:hover {
  box-shadow: 0 10px 24px rgba(79,70,229,0.55) !important;
}
button[kind="secondary"], [data-testid="stBaseButton-secondary"] {
  background: #1A2130 !important; color: #DCE3EE !important;
}

/* ── inputs ───────────────────────────────────────────────────────── */
[data-testid="stTextInput"] input, [data-testid="stNumberInput"] input,
[data-testid="stDateInput"] input, [data-baseweb="select"] > div,
textarea {
  background: #11161F !important; border-radius: 10px !important;
  border: 1px solid #283142 !important; color: #E6EDF3 !important;
}
[data-testid="stTextInput"] input:focus, textarea:focus {
  border-color: #6366F1 !important; box-shadow: 0 0 0 3px rgba(99,102,241,0.22) !important;
}

/* ── expander → card ──────────────────────────────────────────────── */
[data-testid="stExpander"] details {
  border: 1px solid #232B3A; border-radius: 14px;
  background: #11161F; overflow: hidden;
}
[data-testid="stExpander"] summary { padding: 0.85rem 1.1rem; font-weight: 600; }
[data-testid="stExpander"] summary:hover { color: #A5B4FC; }

/* ── alerts ───────────────────────────────────────────────────────── */
[data-testid="stAlert"], [data-testid="stAlertContainer"] {
  border-radius: 12px; border: 1px solid #232B3A;
}

/* ── progress bar ─────────────────────────────────────────────────── */
[data-testid="stProgress"] > div > div {
  background: #1B212C; border-radius: 999px; height: 10px;
}
[data-testid="stProgress"] > div > div > div {
  background: linear-gradient(90deg, #6366F1 0%, #22C55E 100%);
  border-radius: 999px;
}

/* ── tabs ─────────────────────────────────────────────────────────── */
[data-baseweb="tab-list"] { gap: 6px; border-bottom: 1px solid #232B3A; }
[data-baseweb="tab"] {
  border-radius: 9px 9px 0 0; padding: 8px 16px; color: #94A2B8;
  font-weight: 600;
}
[data-baseweb="tab"][aria-selected="true"] { color: #fff; }
[data-baseweb="tab-highlight"] { background: #6366F1 !important; height: 3px; border-radius: 3px; }

/* ── sidebar ──────────────────────────────────────────────────────── */
[data-testid="stSidebar"] {
  background: #0E121B; border-right: 1px solid #1B2230;
}
[data-testid="stSidebarNav"] a {
  border-radius: 9px; margin: 1px 6px; transition: background .12s ease;
}
[data-testid="stSidebarNav"] a:hover { background: rgba(99,102,241,0.12); }
[data-testid="stSidebarNav"] a[aria-current="page"] {
  background: rgba(99,102,241,0.18);
}
[data-testid="stSidebarNav"] a[aria-current="page"] span { color: #C7D2FE !important; font-weight: 600; }

/* ── dataframe + dividers + captions ──────────────────────────────── */
[data-testid="stDataFrame"] { border: 1px solid #232B3A; border-radius: 12px; overflow: hidden; }
hr, [data-testid="stDivider"] hr { border-color: #1F2733 !important; }
[data-testid="stCaptionContainer"] { color: #94A2B8; }

/* ── scrollbars ───────────────────────────────────────────────────── */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: #2A3344; border-radius: 999px; }
::-webkit-scrollbar-thumb:hover { background: #3A465E; }
::-webkit-scrollbar-track { background: transparent; }

/* ── brand header band (page_header) ──────────────────────────────── */
.helm-topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding-bottom: 0.55rem; margin-bottom: 0.2rem;
}
.helm-brand {
  display: flex; align-items: center; gap: 0.55rem;
  font-weight: 800; font-size: 1.02rem; letter-spacing: -0.01em; color: #F2F6FB;
}
.helm-logo {
  display: inline-grid; place-items: center; width: 30px; height: 30px;
  border-radius: 9px; font-size: 1rem;
  background: linear-gradient(150deg, #6366F1, #4338CA);
  box-shadow: 0 4px 14px rgba(79,70,229,0.45);
}
.helm-brand-tag {
  font-size: 0.66rem; font-weight: 600; letter-spacing: 0.12em;
  text-transform: uppercase; color: #6B7892;
  border-left: 1px solid #2A3344; padding-left: 0.55rem; margin-left: 0.15rem;
}
.helm-title {
  font-size: 1.9rem; font-weight: 800; letter-spacing: -0.03em;
  color: #F4F7FB; line-height: 1.15; margin: 0.1rem 0 0.15rem;
}
.helm-subtitle { color: #94A2B8; font-size: 0.95rem; margin-bottom: 0.4rem; }
.helm-rule {
  height: 1px; border: 0; margin: 0.5rem 0 1.4rem;
  background: linear-gradient(90deg, rgba(99,102,241,0.55), rgba(99,102,241,0.05) 60%, transparent);
}

/* ── hero card (wallet headline on the main page) ─────────────────── */
.helm-hero {
  display: flex; align-items: flex-end; justify-content: space-between; gap: 1.5rem;
  flex-wrap: wrap;
  padding: 1.35rem 1.6rem; margin-bottom: 0.5rem;
  border: 1px solid #232B3A; border-radius: 18px;
  background:
    radial-gradient(620px 220px at 92% -50%, rgba(99,102,241,0.20), transparent 70%),
    linear-gradient(180deg, #171D29 0%, #10141D 100%);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.04), 0 14px 34px rgba(0,0,0,0.36);
}
.helm-hero-label {
  font-size: 0.72rem; font-weight: 600; letter-spacing: 0.12em;
  text-transform: uppercase; color: #94A2B8;
}
.helm-hero-value {
  font-size: 2.6rem; font-weight: 800; letter-spacing: -0.035em;
  color: #F4F7FB; font-variant-numeric: tabular-nums; line-height: 1.08;
  margin-top: 0.2rem;
}
.helm-hero-meta { color: #94A2B8; font-size: 0.9rem; margin-top: 0.45rem; }
.helm-hero-meta b { color: #C7D2FE; font-weight: 600; }
.helm-hero-side { display: flex; flex-direction: column; align-items: flex-end; gap: 0.5rem; }

/* ── status pills ─────────────────────────────────────────────────── */
.helm-pill {
  display: inline-flex; align-items: center; gap: 0.4rem;
  font-size: 0.74rem; font-weight: 600; letter-spacing: 0.02em;
  padding: 0.28rem 0.7rem; border-radius: 999px; white-space: nowrap;
  border: 1px solid transparent;
}
.helm-pill .dot {
  width: 7px; height: 7px; border-radius: 999px; display: inline-block;
}
.helm-pill--live   { color: #4ADE80; background: rgba(34,197,94,0.12);  border-color: rgba(34,197,94,0.30); }
.helm-pill--live .dot   { background: #22C55E; box-shadow: 0 0 0 3px rgba(34,197,94,0.20); animation: helmpulse 1.8s infinite; }
.helm-pill--closed { color: #94A2B8; background: rgba(148,162,184,0.10); border-color: rgba(148,162,184,0.25); }
.helm-pill--closed .dot { background: #94A2B8; }
.helm-pill--warn   { color: #FBBF24; background: rgba(245,158,11,0.12);  border-color: rgba(245,158,11,0.30); }
.helm-pill--warn .dot   { background: #F59E0B; }
.helm-pill--info   { color: #7DD3FC; background: rgba(56,189,248,0.12);  border-color: rgba(56,189,248,0.30); }
.helm-pill--info .dot   { background: #38BDF8; }
.helm-pill--pos    { color: #4ADE80; background: rgba(34,197,94,0.12);  border-color: rgba(34,197,94,0.30); }
.helm-pill--neg    { color: #FB7185; background: rgba(244,63,94,0.12);  border-color: rgba(244,63,94,0.30); }
@keyframes helmpulse { 0%,100% { opacity: 1; } 50% { opacity: 0.45; } }

/* ── navbar right cluster: logged-in user chip + status pill ───────── */
.helm-nav-right {
  display: flex; align-items: center; gap: 0.55rem;
  flex-wrap: wrap; justify-content: flex-end;
}
.helm-user {
  display: inline-flex; align-items: center; gap: 0.5rem;
  padding: 0.26rem 0.72rem 0.26rem 0.34rem; border-radius: 999px;
  background: #141A24; border: 1px solid #232B3A;
}
.helm-user-av {
  width: 25px; height: 25px; border-radius: 999px; flex: none;
  display: inline-grid; place-items: center;
  font-size: 0.66rem; font-weight: 700; color: #fff; letter-spacing: 0.02em;
  background: linear-gradient(150deg, #6366F1, #4338CA);
}
.helm-user-text { display: flex; flex-direction: column; line-height: 1.12; }
.helm-user-name { font-size: 0.8rem; font-weight: 600; color: #E6EDF3; }
.helm-user-sub  { font-size: 0.66rem; color: #7E8BA3; font-variant-numeric: tabular-nums; }

/* ── KPI card grid (equal-height, icon'd, never-truncating) ────────── */
.helm-kpis {
  display: grid;
  grid-template-columns: repeat(var(--cols, 4), minmax(0, 1fr));
  gap: 14px; margin: 0.15rem 0 0.5rem;
}
@media (max-width: 1180px) { .helm-kpis { grid-template-columns: repeat(3, minmax(0,1fr)); } }
@media (max-width: 820px)  { .helm-kpis { grid-template-columns: repeat(2, minmax(0,1fr)); } }
.helm-kpi {
  display: flex; flex-direction: column; min-height: 112px; overflow: hidden;
  background: linear-gradient(180deg, #171D28 0%, #12161F 100%);
  border: 1px solid #232B3A; border-radius: 14px; padding: 14px 16px 13px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.04), 0 8px 22px rgba(0,0,0,0.28);
  transition: transform .16s ease, border-color .16s ease, box-shadow .16s ease;
}
.helm-kpi:hover {
  transform: translateY(-2px); border-color: #33405A;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 12px 28px rgba(0,0,0,0.36);
}
.helm-kpi-head { display: flex; align-items: center; gap: 0.5rem; }
.helm-kpi-ic {
  display: inline-grid; place-items: center; width: 28px; height: 28px;
  border-radius: 8px; flex: none; color: #A5B4FC;
  background: linear-gradient(155deg, rgba(99,102,241,0.22), rgba(99,102,241,0.08));
  border: 1px solid rgba(99,102,241,0.28);
}
.helm-kpi-ic svg { width: 16px; height: 16px; display: block; }
.helm-kpi-dot { width: 7px; height: 7px; border-radius: 999px; background: #6366F1; }
.helm-kpi-label {
  font-size: 0.67rem; font-weight: 600; letter-spacing: 0.06em;
  text-transform: uppercase; color: #94A2B8; line-height: 1.25;
}
.helm-kpi-body { margin-top: auto; padding-top: 0.55rem; }
.helm-kpi-value {
  font-size: 1.5rem; font-weight: 700; letter-spacing: -0.02em; color: #F2F6FB;
  font-variant-numeric: tabular-nums; white-space: nowrap; line-height: 1.15;
}
.helm-kpi-value.sm  { font-size: 1.18rem; }
.helm-kpi-value.xs  { font-size: 0.98rem; }
.helm-kpi-sub { font-size: 0.76rem; font-weight: 600; margin-top: 0.28rem; white-space: nowrap; }
.helm-kpi-sub.pos   { color: #4ADE80; }
.helm-kpi-sub.neg   { color: #FB7185; }
.helm-kpi-sub.muted { color: #7E8BA3; }
.helm-kpi-sub.info  { color: #7DD3FC; }
.helm-kpi-sub.warn  { color: #FBBF24; }
</style>
"""


# Lenis inertial smooth-scroll, injected into the *parent* document. Streamlit
# scrolls an inner element ([data-testid=stMain]), not the window, so Lenis is
# bound to that wrapper. Runs from a 0-height component iframe whose srcdoc is
# same-origin with the app, so window.parent is reachable. Idempotent across
# Streamlit reruns via the __helmLenis* guards.
_LENIS_VERSION = "1.1.14"
_SMOOTH_SCROLL_JS = """
<script>
(function () {
  var pwin;
  try { pwin = window.parent; } catch (e) { return; }
  // Install the controller ONCE, and run it in the PARENT document. Critical:
  // Streamlit recreates this component iframe on every rerun / route change. If
  // the rAF loop & URL poll live in the iframe realm, that teardown kills them
  // while the on-parent guard flags persist — Lenis exists but nothing drives
  // its raf(), so scroll freezes after the first SPA navigation. Living in the
  // parent realm, the loop + poll + Lenis instance survive iframe churn.
  if (pwin.__helmScrollInstalled) return;
  pwin.__helmScrollInstalled = true;

  function CONTROLLER() {
    var W = window, D = document;   // parent window/document when this runs
    function els() {
      return {
        w: D.querySelector('[data-testid="stMain"]'),
        c: D.querySelector('[data-testid="stMainBlockContainer"]')
      };
    }
    function destroy() {            // tear the old instance down — no listener leak
      if (W.__helmLenis) { try { W.__helmLenis.destroy(); } catch (e) {} W.__helmLenis = null; }
    }
    function create() {             // fresh Lenis bound to the current wrapper+content
      if (!W.Lenis) return false;
      var e = els(); if (!e.w) return false;
      destroy();
      var lenis = new W.Lenis({
        wrapper: e.w, content: e.c || e.w,
        duration: 1.05,
        easing: function (t) { return Math.min(1, 1.001 - Math.pow(2, -10 * t)); },
        smoothWheel: true, gestureOrientation: 'vertical'
      });
      lenis.__w = e.w; lenis.__c = e.c; W.__helmLenis = lenis;
      return true;
    }
    function bootCreate() { if (!create()) W.setTimeout(bootCreate, 150); }
    function raf(t) { if (W.__helmLenis) W.__helmLenis.raf(t); W.requestAnimationFrame(raf); }
    W.requestAnimationFrame(raf);   // single persistent rAF loop (parent realm)
    var path = W.location.pathname; // SPA pushState nav → reattach Lenis to the new page
    W.setInterval(function () {
      if (W.location.pathname !== path) {
        path = W.location.pathname;
        destroy();                  // native overflow:auto scroll works in the gap
        W.setTimeout(bootCreate, 400);
      }
    }, 150);
    if (W.Lenis) { bootCreate(); }
    else {
      var sc = D.createElement('script');
      sc.src = 'https://cdn.jsdelivr.net/npm/lenis@__VER__/dist/lenis.min.js';
      sc.onload = bootCreate;
      D.head.appendChild(sc);
    }
  }

  var s = pwin.document.createElement('script');
  s.textContent = '(' + CONTROLLER.toString() + ')();';
  pwin.document.head.appendChild(s);
})();
</script>
""".replace("__VER__", _LENIS_VERSION)


def enable_smooth_scroll() -> None:
    """Add Lenis inertial smooth-scrolling to the page (idempotent)."""
    components.html(_SMOOTH_SCROLL_JS, height=0)


def apply_theme() -> None:
    """Inject the global stylesheet + smooth scrolling. Call once, after
    set_page_config."""
    st.markdown(_CSS, unsafe_allow_html=True)
    enable_smooth_scroll()


def pill(text: str, kind: str = "info", *, dot: bool = True) -> str:
    """Return HTML for a status pill. ``kind`` ∈ live/closed/warn/info/pos/neg.

    Returns a string so callers can drop it inside their own
    ``st.markdown(..., unsafe_allow_html=True)`` or the header band.
    """
    dot_html = '<span class="dot"></span>' if dot else ""
    return f'<span class="helm-pill helm-pill--{kind}">{dot_html}{text}</span>'


def _initials(name: str) -> str:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def user_chip(name: str, sub: str | None = None) -> str:
    """HTML for the navbar's logged-in-user chip (avatar initials + name + sub)."""
    sub_html = f'<span class="helm-user-sub">{html.escape(sub)}</span>' if sub else ""
    return (
        '<div class="helm-user">'
        f'<span class="helm-user-av">{html.escape(_initials(name))}</span>'
        '<span class="helm-user-text">'
        f'<span class="helm-user-name">{html.escape(name)}</span>{sub_html}'
        "</span></div>"
    )


# ── duotone, theme-matched icon set (inline SVG; no emoji font needed) ──
# Lucide-style line glyphs. Rendered in light-indigo on the indigo-tinted chip
# (.helm-kpi-ic) — two theme tones, identical on every browser/OS.
_ICONS: dict[str, str] = {
    "bank": '<polygon points="12 3 21 8 3 8"/><line x1="5" y1="8" x2="5" y2="17"/>'
            '<line x1="10" y1="8" x2="10" y2="17"/><line x1="14" y1="8" x2="14" y2="17"/>'
            '<line x1="19" y1="8" x2="19" y2="17"/><line x1="3" y1="20" x2="21" y2="20"/>',
    "wallet": '<path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"/>'
              '<path d="M3 5v14a2 2 0 0 0 2 2h16v-5"/>'
              '<path d="M18 12a2 2 0 0 0 0 4h4v-4Z"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/>'
            '<path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "rocket": '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91'
              'a2.18 2.18 0 0 0-2.91-.09z"/>'
              '<path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 '
              '7.5-6 11a22.35 22.35 0 0 1-4 2z"/>'
              '<path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/>'
              '<path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 16 14"/>',
    "folder": '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.5l-2-3H4a2 2 0 0 0-2 2'
              'v13a2 2 0 0 0 2 2Z"/>',
    "check": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>'
             '<polyline points="22 4 12 14.01 9 11.01"/>',
    "trend-up": '<polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/>'
                '<polyline points="16 7 22 7 22 13"/>',
    "trend-down": '<polyline points="22 17 13.5 8.5 8.5 13.5 2 7"/>'
                  '<polyline points="16 17 22 17 22 11"/>',
    "pin": '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/>'
           '<circle cx="12" cy="10" r="3"/>',
    "buy": '<circle cx="12" cy="12" r="9"/><path d="M12 8v8"/><path d="m8 12 4 4 4-4"/>',
    "sell": '<circle cx="12" cy="12" r="9"/><path d="M12 16V8"/><path d="m8 12 4-4 4 4"/>',
    "receipt": '<path d="M5 3v18l2-1.2L9 21l2-1.2L13 21l2-1.2L17 21l2-1.2V3l-2 1.2L15 3'
               'l-2 1.2L11 3 9 4.2 7 3Z"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    "bar": '<line x1="6" y1="20" x2="6" y2="14"/><line x1="12" y1="20" x2="12" y2="9"/>'
           '<line x1="18" y1="20" x2="18" y2="4"/><line x1="3" y1="20" x2="21" y2="20"/>',
    "repeat": '<path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/>'
              '<path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/>',
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/>'
              '<circle cx="12" cy="12" r="1.5"/>',
    "scale": '<path d="M12 3v18"/><path d="M5 7h14"/><path d="M7 7 4 14h6Z"/>'
             '<path d="M17 7l-3 7h6Z"/><path d="M8 21h8"/>',
    "trophy": '<path d="M7 4h10v5a5 5 0 0 1-10 0Z"/>'
              '<path d="M7 6H4.5a2 2 0 0 0 0 4H7"/><path d="M17 6h2.5a2 2 0 0 1 0 4H17"/>'
              '<path d="M12 14v3"/><path d="M8 21h8"/><path d="M9.5 21a2.5 2.5 0 0 1 5 0"/>',
    "medal": '<circle cx="12" cy="14" r="6"/><path d="M12 11v3l2 1"/>'
             '<path d="M8.5 8 6 3h12l-2.5 5"/>',
}


def _icon_svg(name: str) -> str:
    inner = _ICONS.get(name)
    if not inner:
        return '<span class="helm-kpi-dot"></span>'
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        f"{inner}</svg>"
    )


def kpi_card(
    label: str,
    value: str,
    *,
    icon: str = "",
    sub: str | None = None,
    sub_kind: str = "muted",
    help: str | None = None,
) -> str:
    """One equal-height KPI card: icon + label, big value, optional sub-line.

    ``icon`` is a name from :data:`_ICONS` (duotone theme SVG). The value never
    truncates — long values auto-shrink (``.sm`` / ``.xs``) rather than ellipsing.
    Returns HTML; render a list via :func:`kpi_grid`. ``sub_kind`` ∈
    pos/neg/muted/info/warn. ``help`` becomes a hover tooltip.
    """
    plain = re.sub(r"<[^>]+>", "", value)
    size = " xs" if len(plain) > 18 else " sm" if len(plain) > 12 else ""
    title = f' title="{html.escape(help)}"' if help else ""
    sub_html = f'<div class="helm-kpi-sub {sub_kind}">{sub}</div>' if sub else ""
    return (
        f'<div class="helm-kpi"{title}>'
        f'<div class="helm-kpi-head"><span class="helm-kpi-ic">{_icon_svg(icon)}</span>'
        f'<span class="helm-kpi-label">{label}</span></div>'
        f'<div class="helm-kpi-body"><div class="helm-kpi-value{size}">{value}</div>'
        f"{sub_html}</div></div>"
    )


def kpi_grid(cards: list[str], *, cols: int | None = None) -> None:
    """Render KPI cards in a responsive equal-height grid (single markdown block)."""
    cols = cols or len(cards)
    st.markdown(
        f'<div class="helm-kpis" style="--cols:{cols}">{"".join(cards)}</div>',
        unsafe_allow_html=True,
    )


def page_header(
    title: str,
    subtitle: str | None = None,
    *,
    icon: str = "🧭",
    status: str | None = None,
    status_kind: str = "live",
    user: str | None = None,
    user_sub: str | None = None,
) -> None:
    """Render the branded navbar + page title.

    Navbar: logo + wordmark on the left; on the right an optional logged-in
    ``user`` chip (with ``user_sub`` underneath, e.g. broker id / cash) and an
    optional ``status`` pill (market open / weekend / closed …). Followed by the
    page title, optional subtitle, and an accent rule.
    """
    right = ""
    if user or status:
        right = (
            '<div class="helm-nav-right">'
            + (user_chip(user, user_sub) if user else "")
            + (pill(status, status_kind) if status else "")
            + "</div>"
        )
    subtitle_html = f'<div class="helm-subtitle">{subtitle}</div>' if subtitle else ""
    st.markdown(
        f"""
        <div class="helm-topbar">
          <div class="helm-brand">
            <span class="helm-logo">{icon}</span>Helm
            <span class="helm-brand-tag">Autonomous Trading</span>
          </div>
          {right}
        </div>
        <div class="helm-title">{title}</div>
        {subtitle_html}
        <hr class="helm-rule"/>
        """,
        unsafe_allow_html=True,
    )


__all__ = [
    "apply_theme", "enable_smooth_scroll", "page_header", "pill", "user_chip",
    "kpi_card", "kpi_grid", "CHART",
    "BG", "SURFACE", "SURFACE_2", "BORDER", "TEXT", "TEXT_MUTED", "TEXT_FAINT",
    "PRIMARY", "ACCENT", "POS", "NEG", "WARN", "INFO",
]
