"""Take a list of OrderActions from the strategy and dispatch through the broker."""

from __future__ import annotations

import structlog

from polymarket_bot.execution.broker import Broker
from polymarket_bot.polymarket.markets import DiscoveredMarket
from polymarket_bot.strategy.base import CancelOrder, OrderAction, PlaceLimit

logger = structlog.get_logger()


class MMRouter:
    def __init__(self, broker: Broker, strategy_name: str) -> None:
        self.broker = broker
        self.strategy_name = strategy_name

    def execute(self, actions: list[OrderAction], market: DiscoveredMarket) -> int:
        n = 0
        for a in actions:
            if isinstance(a, PlaceLimit):
                if self.broker.place_limit(a, market, self.strategy_name):
                    n += 1
            elif isinstance(a, CancelOrder):
                if self.broker.cancel(a.order_id):
                    n += 1
            else:
                logger.warning("unknown_action", action=type(a).__name__)
        return n
