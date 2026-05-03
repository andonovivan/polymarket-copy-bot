"""Name → Model class registry."""

from __future__ import annotations

from polymarket_bot.model.base import Model
from polymarket_bot.model.logit import LogitModel

_REGISTRY: dict[str, type[Model]] = {
    LogitModel.name: LogitModel,
}


def register_model(cls: type[Model]) -> type[Model]:
    _REGISTRY[cls.name] = cls
    return cls


def get_model_class(name: str) -> type[Model]:
    if name not in _REGISTRY:
        raise KeyError(f"unknown model: {name!r}")
    return _REGISTRY[name]
