"""Thin wrapper around the Polymarket CLOB client."""

from __future__ import annotations

from typing import Any

import structlog
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams, OrderArgs

from polymarket_bot.config import BotConfig

logger = structlog.get_logger()


class PolymarketClient:
    """Manages the CLOB connection and exposes high-level helpers.

    Dry-run / paper behavior lives in `execution.paper_broker.PaperBroker`; this
    client always submits the real call when invoked. The bot never instantiates
    a `LiveBroker` (the only thing that calls `place_order`) outside live mode.
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        creds = ApiCreds(
            api_key=config.api_key,
            api_secret=config.api_secret,
            api_passphrase=config.api_passphrase,
        )
        self.clob = ClobClient(
            host=config.clob_api_url,
            chain_id=config.chain_id,
            key=config.private_key,
            creds=creds,
        )
        logger.info("polymarket_client_initialized", host=config.clob_api_url)

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    def get_balance_usdc(self) -> float | None:
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = self.clob.get_balance_allowance(params)
            return float(resp.get("balance", 0)) if resp else None
        except Exception:
            logger.warning("balance_fetch_failed")
            return None

    # ------------------------------------------------------------------
    # Market reads
    # ------------------------------------------------------------------

    def get_market(self, condition_id: str) -> dict[str, Any]:
        return self.clob.get_market(condition_id)

    def get_orderbook(self, token_id: str) -> dict[str, Any]:
        return self.clob.get_order_book(token_id)

    def get_midpoint(self, token_id: str) -> float | None:
        try:
            mid = self.clob.get_midpoint(token_id)
            value = float(mid.get("mid", 0)) if mid else 0
            return value if value > 0 else None
        except Exception as exc:
            logger.warning("midpoint_fetch_failed", token_id=token_id, error=str(exc)[:120])
            return None

    # ------------------------------------------------------------------
    # Trade execution (live only)
    # ------------------------------------------------------------------

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> dict[str, Any] | None:
        """Place a limit order on the CLOB. Returns the API response or None on failure."""
        try:
            order_args = OrderArgs(token_id=token_id, price=price, size=size, side=side)
            signed_order = self.clob.create_order(order_args)
            result = self.clob.post_order(signed_order)
            logger.info("order_placed", token_id=token_id, side=side, price=price, size=size,
                        result=result)
            return result
        except Exception as exc:
            logger.error("order_failed", token_id=token_id, side=side, price=price, size=size,
                         error=str(exc))
            return None
