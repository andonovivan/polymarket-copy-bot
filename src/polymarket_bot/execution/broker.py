"""Broker interface — place / cancel limit orders, reconcile fills.

Two implementations: PaperBroker (simulates fills against the live book) and
LiveBroker (CLOB API). Same shape so the router/strategy is mode-agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from polymarket_bot.persistence.repo import Order
from polymarket_bot.strategy.base import PlaceLimit, WeatherEvent


class Broker(ABC):
    @abstractmethod
    def place_limit(self, action: PlaceLimit, strategy: str) -> Order | None: ...

    @abstractmethod
    def cancel(self, order_id: str) -> bool: ...

    @abstractmethod
    def reconcile_fills(self, event: WeatherEvent) -> int:
        """Detect fills since last call (per-bucket book check). Returns # of new fills."""
