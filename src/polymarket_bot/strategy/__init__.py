"""Betting strategies: interface, registry, built-ins."""

from polymarket_bot.strategy.base import (
    BetState,
    BettingStrategy,
    Bucket,
    CancelOrder,
    OpenOrder,
    OrderAction,
    PlaceLimit,
    WeatherEvent,
)
from polymarket_bot.strategy.registry import (
    get_strategy_class,
    list_strategies,
    register_strategy,
)
from polymarket_bot.strategy.weather_forecast import WeatherForecastStrategy

__all__ = [
    "BettingStrategy", "BetState", "Bucket", "WeatherEvent", "OpenOrder",
    "OrderAction", "PlaceLimit", "CancelOrder",
    "WeatherForecastStrategy",
    "register_strategy", "get_strategy_class", "list_strategies",
]
