"""How-it-works explainer — written for a reader with no tech / finance background.

Lives at /pages/1_How_It_Works.py so it appears in the sidebar between the main
live view (app.py) and the Summary page. All numbers are pulled from helm.config
so they stay correct if the bot is reconfigured.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from helm.config import (
    MARKET_CLOSE,
    MARKET_OPEN,
    SCAN_EVERY_MINUTES,
    SQUARE_OFF_AT,
    TRADING_END,
    TRADING_START,
    WATCHLIST,
    live_risk_limits,
)
from helm.dashboard.format import wrapped_table

RISK = live_risk_limits()

st.set_page_config(page_title="Helm — How it works", page_icon="📘", layout="wide")
st.title("How this bot works")
st.caption("Plain-English explanation. No tech or finance jargon required.")

# ───────────────────────── 1. one-paragraph summary ─────────────────────────
st.markdown(
    """
**The short version.** This is a small computer program (a "bot") that watches
the prices of a handful of big Indian companies and a few index/commodity
"baskets" during the trading day and **pretends** to buy and sell them. No
real money moves. Every time it sees an interesting
moment, it asks an AI assistant ("should we?"), and if the AI says yes,
the bot writes a trade into its own notebook and watches it until the end of
the day. The whole point is to test whether this approach would actually make
money — *before* we ever risk a single rupee.
"""
)

# ───────────────────────── 2. the watchlist ─────────────────────────
st.header("What we watch")
st.markdown(
    "We track two kinds of things on India's National Stock Exchange — both "
    "trade the same way (you can buy and sell them during market hours like "
    "a stock):\n\n"
    "1. **Large, well-known companies** — easy to buy and sell, tight prices, "
    "free and reliable quotes.\n"
    "2. **ETFs** — these are baskets that hold many things at once. We use "
    "them to get exposure to *gold*, *silver*, and broad market indices "
    "without having to pick individual gold-mining or banking stocks."
)
NAMES = {
    # Equities
    "RELIANCE": "Reliance Industries (oil, retail, telecom)",
    "HDFCBANK": "HDFC Bank (private bank)",
    "ICICIBANK": "ICICI Bank (private bank)",
    "INFY": "Infosys (IT services)",
    "TCS": "Tata Consultancy Services (IT services)",
    "SBIN": "State Bank of India (public-sector bank)",
    "BHARTIARTL": "Bharti Airtel (telecom)",
    "ITC": "ITC (cigarettes, FMCG, hotels)",
    "LT": "Larsen & Toubro (engineering, construction)",
    "KOTAKBANK": "Kotak Mahindra Bank (private bank)",
    # ETFs
    "NIFTYBEES": "ETF tracking the Nifty 50 (top-50 Indian companies basket)",
    "BANKBEES": "ETF tracking the Nifty Bank index (basket of bank stocks)",
    "ITBEES": "ETF tracking the Nifty IT index (basket of IT stocks)",
    "GOLDBEES": "ETF backed by physical gold (gold-price exposure)",
    "SILVERBEES": "ETF backed by physical silver (silver-price exposure)",
}
watch_df = pd.DataFrame(
    [{"Ticker": s, "Company": NAMES.get(s, s)} for s in WATCHLIST]
)
wrapped_table(watch_df)

# ───────────────────────── 3. the day in the life ─────────────────────────
st.header("A day in the life of the bot")
st.markdown(
    f"""
The Indian stock market is only open on weekdays, from
**{MARKET_OPEN.strftime("%H:%M")}** to **{MARKET_CLOSE.strftime("%H:%M")}**
local time (IST). Outside those hours the bot is asleep — even though it
"checks in" every minute, it just looks at the clock and goes back to sleep.

Here's how a typical trading day plays out:

| Time (IST) | What happens |
|---|---|
| 06:10 | The bot quietly refreshes its broker login (this token expires daily). |
| Before {MARKET_OPEN.strftime("%H:%M")} | Bot is awake but idle — no checking, no thinking, no trading. |
| {MARKET_OPEN.strftime("%H:%M")} | Market opens. Bot starts pulling live prices and looking for opportunities. |
| {TRADING_START.strftime("%H:%M")} | Earliest moment a *new* trade can be opened. (The first 15 min are usually too chaotic.) |
| {TRADING_END.strftime("%H:%M")} | "Last call." After this, no new trades open — there isn't enough time left in the day for them to play out. |
| {SQUARE_OFF_AT.strftime("%H:%M")} | Anything still open is closed automatically. We never carry trades overnight. |
| {MARKET_CLOSE.strftime("%H:%M")} | Market closes. Bot shuts up shop until tomorrow. |
"""
)

# ───────────────────────── 4. the four helpers ─────────────────────────
st.header("Four little helpers, each doing one job")
st.markdown(
    """
The bot is really four tiny programs that take turns running. Think of them
as four shifts at a small shop — each one has a single, narrow job, and they
hand off to each other through a shared notebook (the database).
"""
)
helpers = pd.DataFrame(
    [
        {
            "Helper": "Price watcher",
            "How often": "Every minute",
            "What it does (in plain English)": (
                "Looks up the latest price of each of the 5 stocks and writes "
                "it down. Also bundles the recent prices into 1-minute "
                "candlesticks so the next helpers can see patterns."
            ),
        },
        {
            "Helper": "Idea spotter",
            "How often": f"Every {SCAN_EVERY_MINUTES} minutes",
            "What it does (in plain English)": (
                "Looks at the recent price patterns and asks: \"Does this look "
                "like one of the trade setups we know?\" If yes, it writes "
                "down a new *signal* — a candidate trade with an entry price, "
                "a stop-loss, and a target."
            ),
        },
        {
            "Helper": "Decision-maker (with AI)",
            "How often": "Every 2 minutes",
            "What it does (in plain English)": (
                "Picks up any signals nobody has answered yet and shows them "
                "to an AI assistant along with the recent price action and "
                "today's trading history. The AI replies with TAKE or SKIP "
                "and a one-line reason. If TAKE, the bot writes a paper trade."
            ),
        },
        {
            "Helper": "Position minder",
            "How often": "Every minute",
            "What it does (in plain English)": (
                "Walks through every open paper trade. If the price has hit "
                f"the stop-loss or target, the trade is closed. At {SQUARE_OFF_AT.strftime('%H:%M')} "
                "all remaining open trades are forcibly closed regardless."
            ),
        },
    ]
)
wrapped_table(helpers)

st.info(
    f"**So how often is each one looked at?** Prices are pulled every minute "
    f"({len(WATCHLIST)} instruments × ~390 minutes of market = ~{len(WATCHLIST) * 390} "
    "price checks per day). Trade ideas are searched for every "
    f"{SCAN_EVERY_MINUTES} minutes. Decisions on those ideas are made every 2 "
    "minutes. Open positions are checked every minute. Everything pauses "
    "outside market hours."
)

# ───────────────────────── 4b. what the idea spotter looks for ──────────────
st.header("What the idea spotter is looking for")
st.markdown(
    """
The "idea spotter" is really three small recipes running side by side. Each
one watches the price chart for a very specific pattern and raises its hand
the moment it sees one. None of them is magical — they're just three classic
trader playbooks turned into rules a computer can check every five minutes.

For each strategy below: what it watches for, when it fires, where the
"safety net" stop is, and where the take-profit target is. **All three are
buy-only for now** (we don't short stocks).
"""
)

with st.container():
    st.subheader("1. Opening Range Breakout (`orb_15m`)")
    st.markdown(
        """
**The story.** Between 9:15 and 9:30 IST, when the market has just opened,
the price bounces around inside a small box — a high and a low. Traders
call this the *opening range*, and many treat it like a fence: while price
stays inside, the day is undecided. The moment price *breaks out* above
the top of that fence, it often keeps running in that direction.

**The trigger (in order).**
1. Wait for the first 15 minutes (9:15–9:30) to finish — that's our fence.
2. Note the **highest price** and **lowest price** during those 15 minutes.
3. After 9:30, watch every 1-minute bar.
4. The **moment** a bar closes *above* the fence's high — and the bar
   before it did **not** — fire a BUY signal. (The "bar before" check stops
   us from firing again on every bar after the breakout.)

**Stop and target.**
- **Stop-loss** = the bottom of the fence (the 9:15–9:30 low). If the
  breakout was fake and price falls all the way back through the fence,
  we exit.
- **Target** = entry + 1.5 × (height of the fence). So if the fence was
  ₹10 wide, we aim for ₹15 of profit.

**Worked example.**

> RELIANCE between 9:15 and 9:30 makes a high of ₹2,500 and a low of ₹2,490
> (fence is ₹10 wide). At 9:42 a 1-min bar closes at ₹2,503 — above ₹2,500.
> Signal fires. Stop at ₹2,490. Target at ₹2,503 + ₹15 = ₹2,518.

*Note: the breakout doesn't have to happen in the morning. If a stock
coils inside its 9:15–9:30 fence until 14:00 and then breaks out, that
bar still fires — it's the same setup, just delayed.*
"""
    )

with st.container():
    st.subheader("2. VWAP Reclaim (`vwap_reclaim`)")
    st.markdown(
        """
**The story.** VWAP — Volume-Weighted Average Price — is the average price
of the day so far, anchored at the open and re-computed every minute, with
busier minutes counting more than quiet ones. Pros treat it as the day's
"fair value" line: most trading happens around it. When price *dips below*
VWAP and then *climbs back above* it with a strong green bar, it often
means the dip was a fake-out and buyers have taken control again.

**The trigger (in order).**
1. We need at least one earlier bar today where the close was **below**
   today's VWAP (so there was an actual dip to fade — not a stock that has
   only ever been above the line).
2. The most recent bar must close **above** VWAP, and the bar before it
   must have closed **at or below** VWAP. That's the "reclaim".
3. The reclaim bar itself must be **green** (close higher than open) —
   confirmation that buyers are pushing, not just drifting up.

**Stop and target.**
- **Stop-loss** = the lowest price seen in the last 5 minutes before the
  reclaim — i.e. the bottom of the dip.
- **Target** = entry + 1.5 × (entry − stop). So if the dip put us
  ₹4 at risk, we aim for ₹6 of profit.

**Worked example.**

> By 11:00 IST, INFY's VWAP has worked out to ₹780. Around 10:30 the
> stock dipped to a low of ₹775 — well below VWAP. At 11:15 a green
> 1-min bar closes at ₹781, while the bar before it closed at ₹779.50
> (just under VWAP). Signal fires. Stop ₹775, target ₹781 + ₹9 = ₹790.

*Note: yfinance doesn't give us true traded volume, so we use tick count
as a stand-in. For the largest stocks on the watchlist this is a decent
proxy — busier bars do correspond to busier tape.*
"""
    )

with st.container():
    st.subheader("3. Gap-Down Fade (`gap_fade`)")
    st.markdown(
        """
**The story.** Sometimes a stock opens noticeably lower than where it
closed the previous evening — a "gap down". This is often an overnight
news reaction or futures-led selling that turns out to be overdone for
liquid blue chips. The classic fade is to wait for the stock to claw back
to yesterday's closing price during the morning, on the bet that the gap
will "fill" within the same session.

**The trigger (in order).**
1. Today must have opened at least **0.5% below** yesterday's close.
   (Smaller gaps aren't worth the trade.)
2. We're still in the first 30 minutes of trading (before 9:45 IST). After
   that, a delayed reclaim is a different animal and we don't trade it.
3. A 1-minute bar closes **at or above** yesterday's close, while the
   bar before it was still **below** yesterday's close — the moment the
   gap is filled.

**Stop and target.**
- **Stop-loss** = today's lowest price so far. If the stock makes a fresh
  low after we enter, the fade thesis is wrong and we exit.
- **Target** = entry + 1.5 × (entry − stop).

**Worked example.**

> TCS closed at ₹3,500 yesterday. This morning it opened at ₹3,475 — a
> 0.71% gap-down. By 9:32 it dipped further to ₹3,470 (today's low so far).
> At 9:38 a 1-min bar closes at ₹3,502, climbing back above ₹3,500. The
> bar before it closed at ₹3,498. Signal fires. Stop ₹3,470, target
> ₹3,502 + ₹48 = ₹3,550.
"""
    )

st.info(
    "**Why these three?** They're well-studied retail playbooks with decades "
    "of public data behind them. We aren't claiming any of them is profitable "
    "out-of-the-box — we're claiming they're *measurable*: each has a crisp "
    "trigger, a fixed stop, and a fixed target, so we can tell within weeks "
    "whether they actually work for our watchlist with the AI's filtering on top.\n\n"
    "**Why all three target 1.5× the risk?** Deliberate consistency. By using "
    "the same risk-to-reward ratio across strategies, we can compare them on "
    "hit rate alone — apples-to-apples — without one looking 'better' just "
    "because it took bigger swings."
)

# ───────────────────────── 5. the decision pipeline ─────────────────────────
st.header("The path a trade takes")
st.markdown(
    """
Picture an assembly line. A trade idea moves left to right, and only the
ones that survive every station become real (paper) trades.

```
PRICE  ─► PATTERN  ─► AI DECISION  ─► SAFETY  ─► PAPER TRADE  ─► EXIT
 tick     "signal"    TAKE / SKIP    rules        booked        watched
```

1. **Price** — the price watcher records the latest tick for each stock.
2. **Pattern** — the idea spotter notices a known setup (e.g. a breakout
   above the morning's high) and writes down a candidate trade.
3. **AI decision** — the AI sees the full picture (recent prices, today's
   trades, current risk) and says TAKE or SKIP. **A TAKE is not a buy yet.**
4. **Safety** — even after the AI says yes, the bot runs hard-coded safety
   checks (see next section). Any failure here turns the TAKE into a SKIP.
5. **Paper trade** — only now is a row written to the trade log. Pretend
   money in, pretend price out.
6. **Exit** — the position minder watches it until the price hits the stop
   or target, or until the end-of-day cleanup.

Every step is recorded in an audit log so we can always trace why something
did or didn't happen.
"""
)

# ───────────────────────── 6. safety rails ─────────────────────────
st.header("The safety rails (these are non-negotiable)")
st.markdown(
    "Even if every strategy and the AI all scream BUY, these limits stop the "
    "bot. They are the difference between a trading bot and a runaway robot."
)
rails = pd.DataFrame(
    [
        {
            "Rule": "No more than a few trades open at once",
            "Setting": f"Max {RISK.max_open_positions} open positions",
            "Why it exists": "Keeps the bot focused and prevents one bad day from compounding.",
        },
        {
            "Rule": "Cap the size of any single trade",
            "Setting": f"Max ₹{RISK.max_position_inr:,.0f} of pretend money per trade",
            "Why it exists": "Even on paper, big sizes give us misleading P&L and unrealistic confidence.",
        },
        {
            "Rule": "Daily loss kill-switch",
            "Setting": f"If the day's losses exceed ₹{RISK.daily_loss_kill_inr:,.0f}, the bot stops opening new trades",
            "Why it exists": "Bad days happen. The bot accepts the loss and walks away rather than 'revenge trading'.",
        },
        {
            "Rule": "Cooldown after a trade in the same stock",
            "Setting": f"Wait {RISK.per_symbol_cooldown_min} minutes before trading the same stock again",
            "Why it exists": "Stops the bot from immediately re-entering a trade it just lost on.",
        },
        {
            "Rule": "Daily cap per stock",
            "Setting": f"At most {RISK.max_signals_per_symbol_per_day} trades per stock per day",
            "Why it exists": "Forces diversity across the day. One stock can't dominate.",
        },
        {
            "Rule": "End-of-day flatten",
            "Setting": f"Everything is closed at {SQUARE_OFF_AT.strftime('%H:%M')} IST",
            "Why it exists": "We don't carry positions overnight in this style of trading. News risk is too unpredictable.",
        },
    ]
)
wrapped_table(rails)

# ───────────────────────── 7. what "paper" means ─────────────────────────
st.header('What "paper trading" means')
st.markdown(
    """
*Paper trading* = pretend trading.

* **Real prices** — we use the actual market price of each stock, in real
  time during the day.
* **Pretend buys and sells** — when the bot decides to "buy", it writes a
  row in its own notebook saying "I bought 10 shares of XYZ at ₹2,500."
  No order is sent to the broker. No money moves.
* **Real-feeling P&L** — when the trade closes, the bot calculates the
  profit or loss as if it had been real, and stores that number.

This lets us test the entire system — strategies, AI decisions, safety
rails, end-to-end — without any financial risk. If the paper P&L over
several weeks looks consistently positive, *then* we'd consider taking it
live with small real money. Until then, every number you see is hypothetical.
"""
)

# ───────────────────────── 8. role of the AI ─────────────────────────
st.header("Why an AI in the middle?")
st.markdown(
    """
The "idea spotter" is mechanical — it sees a chart pattern and raises its
hand. But charts alone don't tell the full story. The AI's job is to look
at the bigger picture:

* Is the broader market fighting this trade?
* Have we already had two losers in this stock today?
* Is this signal arriving five minutes before square-off, with no time
  to play out?
* Does the recent tape actually *confirm* the pattern, or is it ambiguous?

It's a second pair of eyes that defaults to **SKIP when in doubt**. Most
candidate signals end up rejected — and that's by design. Each rejection is
logged with a reason so we can review why, the same way a manager might
review a trader's daily notes.
"""
)

# ───────────────────────── 9. where to look on this dashboard ────────────────
st.header("Where to look on this dashboard")
st.markdown(
    """
* **Main page (Helm — intraday bot)** — what's happening *right now*: latest
  prices, today's signals, open positions, recent decisions.
* **Summary page** — how the bot has performed over a date range you choose.
  Hit rate, total P&L, breakdowns by strategy and by stock.
* **This page** — what you're reading, the explainer.

The numbers refresh every 15–30 seconds. If something looks off, the
"Recent audit log" block at the bottom of the main page is the bot's diary —
it records every action with a timestamp.
"""
)

# ───────────────────────── 10. mini-glossary ─────────────────────────
st.header("Words you'll see on the other pages")
glossary = pd.DataFrame(
    [
        {"Term": "Tick", "Means": "A single price reading. We take one per minute per stock."},
        {"Term": "Candle / 1-min bar", "Means": "Open / high / low / close prices over one minute. The bot's basic unit of pattern recognition."},
        {"Term": "Signal", "Means": "A candidate trade flagged by a strategy. Not yet decided on or executed."},
        {"Term": "Decision", "Means": "The AI's verdict on a signal — TAKE or SKIP, plus a reason."},
        {"Term": "Paper trade", "Means": "A pretend trade booked in the bot's own notebook (database)."},
        {"Term": "Position", "Means": "An open paper trade — entered but not yet exited."},
        {"Term": "Stop-loss", "Means": "The price at which we'd close a losing trade to limit damage."},
        {"Term": "Target", "Means": "The price at which we'd close a winning trade to lock in profit."},
        {"Term": "VWAP", "Means": "Volume-Weighted Average Price — the day's average price so far, with busier minutes counting more than quiet ones. Often treated as 'fair value' for the day."},
        {"Term": "Risk:Reward (R:R)", "Means": "How much we aim to make for every rupee we're risking. All three strategies use 1.5:1 — risk ₹1 to chase ₹1.50."},
        {"Term": "P&L", "Means": "Profit-and-loss. Positive = we made money on paper; negative = we lost."},
        {"Term": "Square-off", "Means": "Forcibly closing all open positions, regardless of whether they're winning or losing. Happens at " + SQUARE_OFF_AT.strftime("%H:%M") + " every day."},
        {"Term": "Hit rate", "Means": "The percentage of closed trades that made money."},
        {"Term": "Expectancy", "Means": "Average rupees made per trade across wins and losses combined. The single most important number long-term."},
    ]
)
wrapped_table(glossary)

st.divider()
st.caption(
    "All numbers on this page (cadences, stocks, risk limits, market hours) "
    "are pulled live from the bot's configuration — if the bot is reconfigured, "
    "this page updates automatically."
)
