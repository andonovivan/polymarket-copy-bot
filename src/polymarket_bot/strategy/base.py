"""Strategy ABC + lightweight types shared across the codebase."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from polymarket_bot.persistence.repo import Bar
from polymarket_bot.polymarket.markets import DiscoveredMarket
from polymarket_bot.polymarket.quotes import Quote


@dataclass
class Bet:
    """A bet produced by a strategy. Sized in $; the broker converts to shares."""

    market_id: str
    side: str            # 'YES' or 'NO'
    stake: float         # $ committed
    entry_price: float   # the per-share ask the strategy expects to pay
    predicted_p: float   # P(BTC up) per the model
    market_p: float      # market-implied P(up) at decision time
    edge: float          # predicted_p − market_p
    strategy: str
    model_version: str


@dataclass
class StrategyContext:
    """Read-only snapshot the strategy gets each tick."""

    bankroll: float
    edge_threshold: float
    kelly_fraction: float
    max_bet_pct: float
    min_market_depth_usd: float


class Strategy(ABC):
    """A strategy turns a market + bar history into an optional Bet."""

    name: str = "abstract"

    @abstractmethod
    def on_market(
        self,
        market: DiscoveredMarket,
        bars: list[Bar],
        quote: Quote,
        ctx: StrategyContext,
    ) -> Bet | None: ...
