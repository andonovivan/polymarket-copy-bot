"""Wire up Strategy → Broker → persistence into one path."""

from __future__ import annotations

import time

import structlog

from polymarket_bot.execution.broker import Broker
from polymarket_bot.persistence.repo import Bet as DbBet, insert_bet
from polymarket_bot.strategy.base import Bet

logger = structlog.get_logger()


class Router:
    """Take a Bet from a strategy, submit it through a broker, persist on success."""

    def __init__(self, broker: Broker) -> None:
        self.broker = broker

    def execute(self, bet: Bet) -> bool:
        fill = self.broker.submit(bet)
        if not fill.success:
            logger.warning("bet_rejected", market_id=bet.market_id[:12], error=fill.error)
            return False
        now = int(time.time())
        insert_bet(DbBet(
            id=None, market_id=bet.market_id, side=bet.side,
            shares=fill.filled_shares, entry_price=fill.filled_price,
            stake=fill.filled_shares * fill.filled_price,
            predicted_p=bet.predicted_p, market_p=bet.market_p, edge=bet.edge,
            strategy=bet.strategy, model_version=bet.model_version,
            opened_at=now, status="open",
        ))
        return True
