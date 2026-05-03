"""Name → Strategy class registry."""

from __future__ import annotations

from polymarket_bot.strategy.base import Strategy
from polymarket_bot.strategy.momentum_logit import MomentumLogitStrategy

_REGISTRY: dict[str, type[Strategy]] = {
    MomentumLogitStrategy.name: MomentumLogitStrategy,
}


def register_strategy(cls: type[Strategy]) -> type[Strategy]:
    _REGISTRY[cls.name] = cls
    return cls


def get_strategy_class(name: str) -> type[Strategy]:
    if name not in _REGISTRY:
        raise KeyError(f"unknown strategy: {name!r}. Known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def list_strategies() -> list[str]:
    return sorted(_REGISTRY)
