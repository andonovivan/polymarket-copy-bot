"""Name → MMStrategy class registry."""

from __future__ import annotations

from polymarket_bot.strategy.base import MMStrategy
from polymarket_bot.strategy.spread_only import SpreadOnlyMM

_REGISTRY: dict[str, type[MMStrategy]] = {
    SpreadOnlyMM.name: SpreadOnlyMM,
}


def register_strategy(cls: type[MMStrategy]) -> type[MMStrategy]:
    _REGISTRY[cls.name] = cls
    return cls


def get_strategy_class(name: str) -> type[MMStrategy]:
    if name not in _REGISTRY:
        raise KeyError(f"unknown strategy: {name!r}. Known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def list_strategies() -> list[str]:
    return sorted(_REGISTRY)
