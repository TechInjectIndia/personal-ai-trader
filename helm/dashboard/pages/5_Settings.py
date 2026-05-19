"""
Settings page — edit risk limits from the browser.

Writes go into the Postgres `settings` table; helm.config.live_risk_limits()
reads from it on every call, so changes take effect on the next cron tick
without a PM2 restart. Code defaults (helm/config.py:RISK) act as the
fallback for any key that hasn't been overridden.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from helm.config import (
    EDITABLE_RISK_KEYS,
    EDITABLE_WALLET_KEYS,
    RISK,
    WALLET,
    live_risk_limits,
    live_wallet_config,
)
from helm.dashboard.format import fmt_ist, wrapped_table
from helm.data.store import all_settings, conn, insert_audit, set_setting

st.set_page_config(page_title="Helm — Settings", page_icon="⚙️", layout="wide")
st.title("Settings")
st.caption(
    "Risk limits stored in Postgres. Cron scripts re-read on every run — "
    "no PM2 restart needed. Empty fields fall back to code defaults."
)

# Per-key UI metadata. Bounds are sanity rails, not policy; tighten later.
WALLET_FIELDS = {
    "initial_capital_inr": {
        "label": "Initial capital (₹)",
        "help": "Starting size of the bot's single cash pool. Changing this "
                "shifts all wallet math — current equity, available cash, "
                "and goal progress all recompute. Pick the amount you'd want "
                "to actually risk if this went live.",
        "kind": "decimal",
        "min": 1_000.0,
        "max": 10_000_000.0,
        "step": 500.0,
    },
    "goal_capital_inr": {
        "label": "Goal wallet value (₹)",
        "help": "Target wallet size — drives the progress bar on the main "
                "page. Default is 2× initial ('double the money'). Doesn't "
                "stop the bot when hit; just a milestone marker.",
        "kind": "decimal",
        "min": 1_000.0,
        "max": 100_000_000.0,
        "step": 1_000.0,
    },
}

FIELDS = {
    "max_open_positions": {
        "label": "Max open paper trades",
        "help": "Cap on the number of paper positions OPEN at once.",
        "kind": "int",
        "min": 1,
        "max": 20,
        "step": 1,
    },
    "max_position_inr": {
        "label": "Max investment per trade (₹)",
        "help": "Per-trade notional cap. Qty is sized as floor(this ÷ entry_price).",
        "kind": "decimal",
        "min": 100.0,
        "max": 1_000_000.0,
        "step": 100.0,
    },
    "daily_loss_kill_inr": {
        "label": "Daily-loss kill threshold (₹)",
        "help": "If today's realised P&L drops to ≤ -this, no new trades open today.",
        "kind": "decimal",
        "min": 50.0,
        "max": 1_000_000.0,
        "step": 50.0,
    },
    "per_symbol_cooldown_min": {
        "label": "Per-symbol cooldown (minutes)",
        "help": "Minutes to wait before re-entering the same symbol "
                "(not currently enforced by risk.evaluate but consumed by future logic).",
        "kind": "int",
        "min": 0,
        "max": 240,
        "step": 5,
    },
    "max_signals_per_symbol_per_day": {
        "label": "Max trades per symbol per day",
        "help": "Hard cap on how many paper trades any one stock can have in a day.",
        "kind": "int",
        "min": 1,
        "max": 10,
        "step": 1,
    },
}

# Sanity: only render keys we know how to edit. Mismatches between FIELDS and
# EDITABLE_RISK_KEYS would be a coding error.
assert set(FIELDS) == set(EDITABLE_RISK_KEYS), (
    f"FIELDS keys {set(FIELDS)} != EDITABLE_RISK_KEYS {set(EDITABLE_RISK_KEYS)}"
)
assert set(WALLET_FIELDS) == set(EDITABLE_WALLET_KEYS), (
    f"WALLET_FIELDS keys {set(WALLET_FIELDS)} != "
    f"EDITABLE_WALLET_KEYS {set(EDITABLE_WALLET_KEYS)}"
)

current = live_risk_limits()
current_wallet = live_wallet_config()
overrides = all_settings()


# Shared bookkeeping for both forms below.
def _save_changes(
    submitted: dict[str, object],
    field_specs: dict,
    keys: tuple[str, ...],
    snapshot,
    note: str,
) -> dict[str, dict]:
    changed: dict[str, dict] = {}
    for key in keys:
        new_val = submitted[key]
        if field_specs[key]["kind"] == "int":
            new_val = int(new_val)
        else:
            new_val = float(new_val)
        old_val = getattr(snapshot, key)
        # Stringify Decimals so the comparison is value-based, not type-based.
        if str(new_val) != str(old_val):
            set_setting(key, new_val, actor="dashboard")
            changed[key] = {"from": str(old_val), "to": str(new_val)}
    if changed:
        insert_audit("settings", "updated", {"changes": changed, "note": note or None})
    return changed


def _render_form(
    title: str,
    form_key: str,
    keys: tuple[str, ...],
    field_specs: dict,
    snapshot,
) -> None:
    st.subheader(title)
    with st.form(form_key):
        submitted: dict[str, object] = {}
        cols = st.columns(2)
        for i, key in enumerate(keys):
            spec = field_specs[key]
            col = cols[i % 2]
            existing = getattr(snapshot, key)
            if spec["kind"] == "int":
                submitted[key] = col.number_input(
                    spec["label"],
                    min_value=int(spec["min"]),
                    max_value=int(spec["max"]),
                    value=int(existing),
                    step=int(spec["step"]),
                    help=spec["help"],
                    key=f"in_{key}",
                )
            else:
                submitted[key] = col.number_input(
                    spec["label"],
                    min_value=float(spec["min"]),
                    max_value=float(spec["max"]),
                    value=float(existing),
                    step=float(spec["step"]),
                    help=spec["help"],
                    key=f"in_{key}",
                )
        note = st.text_input(
            "Audit note (optional)", "",
            placeholder="e.g. bumping cap for week of …",
            key=f"note_{form_key}",
        )
        if st.form_submit_button("Save changes", type="primary"):
            changed = _save_changes(submitted, field_specs, keys, snapshot, note)
            if changed:
                st.success(f"Saved {len(changed)} change(s): {', '.join(changed)}")
                st.rerun()
            else:
                st.info("Nothing changed.")


# ───────────────────────── current state ─────────────────────────
st.subheader("Current effective values")
rows = []
for key in EDITABLE_WALLET_KEYS:
    rows.append({
        "Setting": WALLET_FIELDS[key]["label"],
        "Effective": getattr(current_wallet, key),
        "Code default": getattr(WALLET, key),
        "Overridden?": "yes" if key in overrides else "no",
    })
for key in EDITABLE_RISK_KEYS:
    rows.append({
        "Setting": FIELDS[key]["label"],
        "Effective": getattr(current, key),
        "Code default": getattr(RISK, key),
        "Overridden?": "yes" if key in overrides else "no",
    })
wrapped_table(pd.DataFrame(rows))

# ───────────────────────── editor forms ─────────────────────────
_render_form(
    "Wallet (capital pool)",
    "edit_wallet",
    EDITABLE_WALLET_KEYS,
    WALLET_FIELDS,
    current_wallet,
)
_render_form(
    "Risk limits",
    "edit_limits",
    EDITABLE_RISK_KEYS,
    FIELDS,
    current,
)

# ───────────────────────── reset controls ─────────────────────────
editable_all = EDITABLE_WALLET_KEYS + EDITABLE_RISK_KEYS
field_lookup = {**WALLET_FIELDS, **FIELDS}
default_lookup = {
    **{k: getattr(WALLET, k) for k in EDITABLE_WALLET_KEYS},
    **{k: getattr(RISK, k) for k in EDITABLE_RISK_KEYS},
}
overridden_editable = [k for k in editable_all if k in overrides]
if overridden_editable:
    with st.expander("Reset to code defaults"):
        st.caption(
            "Deleting an override returns the key to its code default in "
            "helm/config.py. Cron picks it up on next run."
        )
        for key in overridden_editable:
            cols = st.columns([3, 2, 1])
            cols[0].write(field_lookup[key]["label"])
            cols[1].write(
                f"override: **{overrides[key]}** · default: {default_lookup[key]}"
            )
            if cols[2].button("Reset", key=f"reset_{key}"):
                with conn() as c:
                    c.execute("DELETE FROM settings WHERE key = %s", (key,))
                insert_audit(
                    "settings", "reset",
                    {"key": key, "was": str(overrides[key])},
                )
                st.success(f"Reset {key} to code default.")
                st.rerun()

# ───────────────────────── audit trail ─────────────────────────
st.subheader("Recent settings changes")
with conn() as c:
    audit_rows = list(c.execute(
        "SELECT ts, event, detail FROM audit "
        "WHERE actor = 'settings' ORDER BY ts DESC LIMIT 25"
    ))
if audit_rows:
    audit_df = pd.DataFrame(audit_rows)
    audit_df["ts"] = audit_df["ts"].apply(fmt_ist)
    wrapped_table(audit_df)
else:
    st.caption("No settings changes recorded yet.")
