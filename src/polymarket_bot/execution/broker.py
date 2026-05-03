"""Broker interface. One submit() shape for paper, live, and backtest."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from polymarket_bot.strategy.base import Bet


@dataclass
class Fill:
    """The result of attempting to place a Bet."""

    success: bool
    filled_price: float       # average per-share price actually paid
    filled_shares: float      # shares actually filled
    fees: float               # $ fees taken on this fill
    slippage: float           # $ slippage relative to expected
    error: str | None = None


class Broker(ABC):
    """Submits bets and reports fills."""

    @abstractmethod
    def submit(self, bet: Bet) -> Fill: ...
