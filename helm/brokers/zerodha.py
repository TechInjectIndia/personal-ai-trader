"""
Zerodha adapter — wraps `pykiteconnect`.

Single point of contact between the orchestrator and Kite Connect. Handles:
  - Daily access-token refresh
  - Idempotent order placement (client tag dedupe)
  - Holdings / positions / orders sync
  - Cancel-all and square-off-all-MIS for kill-switch + sleeve halt

PRD reference: §11 brokers/zerodha.py module.

The dry-run scripts (scripts/dry_run_zerodha.py and dry_run_killswitch.py)
already implement the core SDK calls — those snippets are the seed of this
adapter. Phase 3 lifts them into a class with idempotency and retry logic.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class ZerodhaConfig:
    api_key: str
    api_secret: str
    access_token: str       # daily; refreshed by orchestrator
    redirect_url: str


class Zerodha:
    """TODO Phase 3 — pykiteconnect-backed adapter."""

    def __init__(self, config: ZerodhaConfig) -> None:
        self.config = config
        # self._kite = KiteConnect(api_key=config.api_key)
        # self._kite.set_access_token(config.access_token)

    def profile(self) -> dict:
        raise NotImplementedError

    def holdings(self) -> list[dict]:
        raise NotImplementedError

    def positions(self) -> dict:
        raise NotImplementedError

    def orders(self) -> list[dict]:
        raise NotImplementedError

    def margins(self) -> dict:
        raise NotImplementedError

    def place_order(
        self,
        symbol: str,
        side: str,                   # 'buy' | 'sell'
        quantity: int,
        product: str,                # 'CNC' | 'MIS'
        order_type: str = "MARKET",
        price: Decimal | None = None,
        client_tag: str | None = None,
    ) -> str:
        """Idempotent: caller passes a deterministic client_tag; duplicates are no-ops."""
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> None:
        raise NotImplementedError

    def cancel_all_orders(self) -> int:
        """Cancels every open order. Returns count cancelled."""
        raise NotImplementedError

    def square_off_all_mis(self) -> int:
        """Closes every open MIS position with market orders. Returns count closed."""
        raise NotImplementedError
