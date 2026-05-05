# helm — dry_run_ibkr.py
# Phase 1, Gate G2 — proves QuantConnect ↔ Interactive Brokers auth works end to end.
#
# What this does (in plain English):
#   - Buys exactly one (1) share of VOO (Vanguard S&P 500 ETF).
#   - Logs the order placement to QC's live log.
#   - Sets the brokerage model to IBKR so QC routes through the IBKR adapter.
#
# Pass criterion (G2):
#   The fill must appear in BOTH the QuantConnect log AND your IBKR Client
#   Portal "Trades" view. Cross-account confirmation is the whole point.
#
# Capital used: $5,000 of paper cash. VOO is roughly $480/share, so one share
# is comfortable headroom and easy to verify visually.

from AlgorithmImports import *


class DryRunIBKR(QCAlgorithm):
    def Initialize(self):
        self.SetStartDate(2026, 5, 1)
        self.SetCash(5000)  # paper cash in USD

        # Tell QC to model IBKR's commissions, slippage, and fee structure.
        self.SetBrokerageModel(
            BrokerageName.InteractiveBrokersBrokerage,
            AccountType.Cash,
        )

        # VOO defaults to the US market on QC, so no Market.* needed here.
        self.symbol = self.AddEquity("VOO", Resolution.Daily).Symbol
        self.bought = False

    def OnData(self, data):
        if not self.Portfolio.Invested and not self.bought:
            self.MarketOrder(self.symbol, 1)
            self.bought = True
            self.Log(f"DRY-RUN-I: placed BUY 1 VOO at {self.Time}")

    def OnOrderEvent(self, orderEvent):
        self.Log(f"DRY-RUN-I: order event — {orderEvent}")
