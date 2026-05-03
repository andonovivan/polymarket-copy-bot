"""Paper broker: simulates fills against the live ask. No real orders."""

from __future__ import annotations

import structlog

from polymarket_bot.execution.broker import Broker, Fill
from polymarket_bot.strategy.base import Bet

logger = structlog.get_logger()


class PaperBroker(Broker):
    """Fill at the ask the strategy planned for; no slippage, no fees by default.

    Realism toggles are deferred to backtest_broker; paper exists to prove the
    pipeline works against live quotes without risking capital.
    """

    def __init__(self, fee_bps: float = 0.0, slip_bps: float = 0.0) -> None:
        self.fee_bps = fee_bps
        self.slip_bps = slip_bps

    def submit(self, bet: Bet) -> Fill:
        if bet.entry_price <= 0 or bet.stake <= 0:
            return Fill(False, 0.0, 0.0, 0.0, 0.0, error="invalid bet")
        slip = bet.entry_price * (self.slip_bps / 10_000)
        filled_price = min(1.0, bet.entry_price + slip)
        shares = bet.stake / filled_price if filled_price > 0 else 0.0
        fees = bet.stake * (self.fee_bps / 10_000)
        logger.info(
            "paper_fill",
            market_id=bet.market_id[:12], side=bet.side, stake=round(bet.stake, 4),
            price=round(filled_price, 4), shares=round(shares, 4),
        )
        return Fill(True, filled_price, shares, fees, slip * shares)
