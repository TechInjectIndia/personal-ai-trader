# helm — dry_run_zerodha.py
# Phase 1, Gate G1 — proves QuantConnect ↔ Zerodha auth works end to end.
#
# What this does (in plain English):
#   - Buys exactly one (1) unit of NIFTYBEES (the Nifty 50 ETF on NSE).
#   - Logs the order placement to QC's live log.
#   - Sets the brokerage model to Zerodha so QC routes through Kite Connect.
#
# Pass criterion (G1):
#   The fill must appear in BOTH the QuantConnect log AND your Zerodha Kite
#   "Orders" view. If it shows up in only one, the broker mapping is broken.
#
# Capital used: ₹50,000 of paper cash inside QC. NIFTYBEES is roughly ₹250/unit,
# so even one unit is well under any sane risk threshold.

from AlgorithmImports import *


class DryRunZerodha(QCAlgorithm):
    def Initialize(self):
        # Backtest start date — irrelevant for live deploy, required by QC.
        self.SetStartDate(2026, 5, 1)
        self.SetCash(50000)  # paper cash in INR

        # Tell QC to model Zerodha's commissions, slippage, and fee structure.
        self.SetBrokerageModel(BrokerageName.Zerodha, AccountType.Cash)

        # Add NIFTYBEES on NSE (the Nippon India ETF Nifty BeES — the most
        # liquid Nifty 50 ETF on the Indian exchange).
        self.symbol = self.AddEquity("NIFTYBEES", Resolution.Daily, Market.India).Symbol

        # Guard so we only place ONE order per session.
        self.bought = False

    def OnData(self, data):
        # If we don't already hold the position and we haven't bought yet,
        # place a single market buy for 1 unit. That's it. The whole point of
        # this algorithm is to prove the round-trip works, not to make money.
        if not self.Portfolio.Invested and not self.bought:
            self.MarketOrder(self.symbol, 1)
            self.bought = True
            self.Log(f"DRY-RUN-Z: placed BUY 1 NIFTYBEES at {self.Time}")

    def OnOrderEvent(self, orderEvent):
        # Log every status change for the order so we have a clean trail of
        # submitted → filled (or rejected, with reason).
        self.Log(f"DRY-RUN-Z: order event — {orderEvent}")
