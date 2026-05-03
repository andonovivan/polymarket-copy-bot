"""Market-making strategy interface, types, and built-in strategies."""

from polymarket_bot.strategy.base import (
    CancelOrder,
    Inventory,
    MMState,
    MMStrategy,
    OpenOrder,
    OrderAction,
    PlaceLimit,
)
from polymarket_bot.strategy.registry import (
    get_strategy_class,
    list_strategies,
    register_strategy,
)
from polymarket_bot.strategy.spread_only import SpreadOnlyMM

__all__ = [
    "MMStrategy", "MMState", "Inventory", "OpenOrder",
    "OrderAction", "PlaceLimit", "CancelOrder",
    "SpreadOnlyMM",
    "get_strategy_class", "list_strategies", "register_strategy",
]
