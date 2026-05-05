"""
IBKR adapter — wraps `ib_insync`.

Same surface as the Zerodha adapter so the orchestrator can stay broker-agnostic.

PRD reference: §11 brokers/ibkr.py module.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class IBKRConfig:
    host: str = "127.0.0.1"
    port: int = 7497         # 7497 paper, 7496 live
    client_id: int = 1


class IBKR:
    """TODO Phase 3 — ib_insync-backed adapter."""

    def __init__(self, config: IBKRConfig) -> None:
        self.config = config
        # self._ib = IB()
        # self._ib.connect(config.host, config.port, clientId=config.client_id)

    def account_summary(self) -> dict:
        raise NotImplementedError

    def positions(self) -> list[dict]:
        raise NotImplementedError

    def orders(self) -> list[dict]:
        raise NotImplementedError

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "MARKET",
        price: Decimal | None = None,
        client_tag: str | None = None,
    ) -> str:
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> None:
        raise NotImplementedError

    def cancel_all_orders(self) -> int:
        raise NotImplementedError
