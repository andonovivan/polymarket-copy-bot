"""Router: takes a list of OrderActions, dispatches through the broker."""

from __future__ import annotations

import structlog

from polymarket_bot.execution.broker import Broker
from polymarket_bot.strategy.base import CancelOrder, OrderAction, PlaceLimit

logger = structlog.get_logger()


class Router:
    def __init__(self, broker: Broker, strategy_name: str) -> None:
        self.broker = broker
        self.strategy_name = strategy_name

    def execute(self, actions: list[OrderAction]) -> int:
        n = 0
        for a in actions:
            if isinstance(a, PlaceLimit):
                if self.broker.place_limit(a, self.strategy_name):
                    n += 1
            elif isinstance(a, CancelOrder):
                if self.broker.cancel(a.order_id):
                    n += 1
            else:
                logger.warning("unknown_action", action=type(a).__name__)
        return n
