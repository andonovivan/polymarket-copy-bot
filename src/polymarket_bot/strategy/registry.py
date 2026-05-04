"""Name → BettingStrategy class registry."""

from __future__ import annotations

from polymarket_bot.strategy.base import BettingStrategy
from polymarket_bot.strategy.weather_forecast import WeatherForecastStrategy

_REGISTRY: dict[str, type[BettingStrategy]] = {
    WeatherForecastStrategy.name: WeatherForecastStrategy,
}


def register_strategy(cls: type[BettingStrategy]) -> type[BettingStrategy]:
    _REGISTRY[cls.name] = cls
    return cls


def get_strategy_class(name: str) -> type[BettingStrategy]:
    if name not in _REGISTRY:
        raise KeyError(f"unknown strategy: {name!r}. Known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def list_strategies() -> list[str]:
    return sorted(_REGISTRY)
