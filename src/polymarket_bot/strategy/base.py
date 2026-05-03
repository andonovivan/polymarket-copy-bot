"""Market-making strategy interface and shared types.

A strategy is a pure function of `MMState → list[OrderAction]`. The router/broker
turns those actions into real or simulated orders on the Polymarket CLOB.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Union

from polymarket_bot.polymarket.markets import DiscoveredMarket
from polymarket_bot.polymarket.quotes import Quote


# ---------------------------------------------------------------------------
# Order actions — what a strategy emits each tick.
# ---------------------------------------------------------------------------


@dataclass
class PlaceLimit:
    """Place a resting limit order on YES or NO at `price` for `size` shares.

    `side` = BUY accumulates inventory; SELL reduces it (or short-sells against
    collateral). For v1 we only BUY YES and BUY NO since binary MM is naturally
    expressed as "accumulate balanced inventory; resolution clears it".
    """
    market_id: str
    token_side: Literal["YES", "NO"]
    side: Literal["BUY", "SELL"]
    price: float                          # 0 < price < 1
    size: float                           # shares
    client_order_id: str                  # strategy-assigned id; broker echoes back


@dataclass
class CancelOrder:
    order_id: str                         # broker-assigned order id


OrderAction = Union[PlaceLimit, CancelOrder]


# ---------------------------------------------------------------------------
# Inventory + open-order state visible to the strategy each tick.
# ---------------------------------------------------------------------------


@dataclass
class Inventory:
    """Net position per binary side, plus the average cost basis."""
    yes_shares: float = 0.0
    no_shares: float = 0.0
    yes_cost_basis: float = 0.0           # avg $/share paid for held YES shares
    no_cost_basis: float = 0.0

    @property
    def is_balanced(self) -> bool:
        return abs(self.yes_shares - self.no_shares) < 1e-6

    @property
    def imbalance(self) -> float:
        """Signed: positive = more YES than NO (delta-long BTC up)."""
        return self.yes_shares - self.no_shares

    def mark_to_market(self, yes_mid: float) -> float:
        """$ value of inventory at the given YES mid (NO mid = 1 − yes_mid)."""
        return self.yes_shares * yes_mid + self.no_shares * (1.0 - yes_mid)


@dataclass
class OpenOrder:
    order_id: str
    client_order_id: str
    market_id: str
    token_side: Literal["YES", "NO"]
    side: Literal["BUY", "SELL"]
    price: float
    size: float
    filled: float = 0.0                   # shares already filled
    placed_at: int = 0                    # unix seconds


@dataclass
class MMState:
    """Read-only snapshot the strategy gets each tick."""
    market: DiscoveredMarket
    quote: Quote
    inventory: Inventory
    open_orders: list[OpenOrder]
    bankroll: float
    seconds_to_resolution: int
    # Strategy parameters from BotConfig (carried in, not mutable):
    base_spread: float
    max_inventory_shares: float
    inventory_skew: float
    lock_buffer_seconds: int


# ---------------------------------------------------------------------------
# Strategy ABC.
# ---------------------------------------------------------------------------


class MMStrategy(ABC):
    """A market-making strategy: state in, list of actions out."""

    name: str = "abstract"

    @abstractmethod
    def tick(self, state: MMState) -> list[OrderAction]: ...
