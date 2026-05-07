"""Name → BettingStrategy class registry."""

from __future__ import annotations

from polymarket_bot.strategy.base import BettingStrategy
from polymarket_bot.strategy.bucket_arbitrage import BucketArbitrageStrategy
from polymarket_bot.strategy.weather_forecast import WeatherForecastStrategy

_REGISTRY: dict[str, type[BettingStrategy]] = {
    WeatherForecastStrategy.name: WeatherForecastStrategy,
    BucketArbitrageStrategy.name: BucketArbitrageStrategy,
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


def get_display_name(name: str) -> str:
    """Human-readable label for a strategy name, falling back to the raw key
    when the strategy is no longer registered (e.g., legacy fills.strategy
    values from an old build)."""
    cls = _REGISTRY.get(name)
    return cls.display_name if cls is not None else name


def get_enabled_strategies_helper() -> set[str]:
    """Return the set of currently-enabled strategy names (default: all
    registered). Pure convenience to avoid every caller importing both
    `repo.get_enabled_strategies` and `list_strategies`."""
    from polymarket_bot.persistence.repo import get_enabled_strategies
    return get_enabled_strategies(default_all=list_strategies())
