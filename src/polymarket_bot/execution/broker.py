"""Broker interface for market-making.

Two implementations: PaperMMBroker (simulates fills against the live book) and
LiveMMBroker (calls the Polymarket CLOB). Both share the same shape so the
strategy + router code is mode-agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from polymarket_bot.persistence.repo import Order
from polymarket_bot.polymarket.markets import DiscoveredMarket
from polymarket_bot.polymarket.quotes import Quote
from polymarket_bot.strategy.base import PlaceLimit


class Broker(ABC):
    """Place and cancel resting limit orders. Must persist to the orders table."""

    @abstractmethod
    def place_limit(self, action: PlaceLimit, market: DiscoveredMarket, strategy: str) -> Order | None:
        """Place a single limit order. Returns the persisted Order on success."""

    @abstractmethod
    def cancel(self, order_id: str) -> bool: ...

    @abstractmethod
    def reconcile_fills(self, market: DiscoveredMarket, quote: Quote) -> int:
        """Detect fills since the last call and persist them. Returns # of new fills."""
