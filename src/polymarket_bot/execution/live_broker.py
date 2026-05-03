"""Live broker: places real orders on the Polymarket CLOB. Gated by --live + env."""

from __future__ import annotations

import structlog

from polymarket_bot.execution.broker import Broker, Fill
from polymarket_bot.persistence.repo import get_market
from polymarket_bot.polymarket.client import PolymarketClient
from polymarket_bot.strategy.base import Bet

logger = structlog.get_logger()


class LiveBroker(Broker):
    """Submits real BUY orders for YES or NO tokens via the CLOB.

    The runtime gate (`POLYMARKET_BOT_LIVE=1` + `--live`) is enforced in main.py;
    by the time this broker is constructed, that gate has already passed.
    """

    def __init__(self, client: PolymarketClient) -> None:
        self.client = client

    def submit(self, bet: Bet) -> Fill:
        market = get_market(bet.market_id)
        if market is None:
            return Fill(False, 0.0, 0.0, 0.0, 0.0, error="market not in DB")
        token_id = market.yes_token_id if bet.side == "YES" else market.no_token_id
        size = bet.stake / bet.entry_price if bet.entry_price > 0 else 0.0
        if size <= 0:
            return Fill(False, 0.0, 0.0, 0.0, 0.0, error="invalid size")
        result = self.client.place_order(
            token_id=token_id, side="BUY", price=bet.entry_price, size=size,
        )
        if result is None:
            return Fill(False, 0.0, 0.0, 0.0, 0.0, error="order rejected")
        # CLOB fills are async — we record the order at the requested price.
        # A follow-up reconciler can true-up actual fill prices from order history.
        logger.info("live_order_accepted", market_id=bet.market_id[:12], side=bet.side,
                    price=bet.entry_price, size=size, result=result)
        return Fill(True, bet.entry_price, size, 0.0, 0.0)
