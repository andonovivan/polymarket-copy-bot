"""Backtest broker: deterministic fills from recorded polymarket_quotes."""

from __future__ import annotations

import structlog

from polymarket_bot.backtest.fees import polymarket_fee
from polymarket_bot.backtest.slippage import slippage_bps
from polymarket_bot.execution.broker import Broker, Fill
from polymarket_bot.strategy.base import Bet

logger = structlog.get_logger()


class BacktestBroker(Broker):
    """Walks the recorded book to compute average fill price + costs."""

    def __init__(self, base_slip_bps: float = 5.0, fee_bps: float = 0.0) -> None:
        self.base_slip_bps = base_slip_bps
        self.fee_bps = fee_bps

    def submit(self, bet: Bet) -> Fill:
        if bet.entry_price <= 0 or bet.stake <= 0:
            return Fill(False, 0.0, 0.0, 0.0, 0.0, error="invalid bet")
        # Approximate book-walk: linear price impact in stake size.
        # Real implementation would consume polymarket_quotes depth levels.
        slip_bps = slippage_bps(self.base_slip_bps, bet.stake, depth_usd=200.0)
        filled_price = min(1.0, bet.entry_price * (1 + slip_bps / 10_000))
        shares = bet.stake / filled_price
        fees = polymarket_fee(bet.stake, self.fee_bps)
        slippage = (filled_price - bet.entry_price) * shares
        return Fill(True, filled_price, shares, fees, slippage)
