"""Paper broker — simulates resting limit orders + cross-fills against live book.

Fill model (v1, conservative): an open order fills in full at its limit price
when the live book touches that price during a tick:

  - BUY  at P fills when live best_ask ≤ P  (seller is willing to hit our bid)
  - SELL at P fills when live best_bid ≥ P  (buyer is willing to lift our ask)

This captures the adverse-selection cost (when the book crosses your level
because the underlying moved against you, you still get filled at your stale
price) without needing a real trade-tape subscription.
"""

from __future__ import annotations

import time
import uuid

import structlog

from polymarket_bot.execution.broker import Broker
from polymarket_bot.persistence.repo import (
    Fill,
    Order,
    cancel_order_row,
    insert_fill,
    insert_order,
    open_orders_by_market,
    update_order_filled,
)
from polymarket_bot.polymarket.markets import DiscoveredMarket
from polymarket_bot.polymarket.quotes import Quote
from polymarket_bot.strategy.base import PlaceLimit

logger = structlog.get_logger()


class PaperMMBroker(Broker):
    """Paper-mode market-making broker. Resting orders, simulated fills."""

    def place_limit(self, action: PlaceLimit, market: DiscoveredMarket, strategy: str) -> Order | None:
        if not (0.0 < action.price < 1.0):
            logger.warning("paper_reject_price", price=action.price)
            return None
        if action.size <= 0:
            return None
        order = Order(
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
            client_order_id=action.client_order_id,
            market_id=action.market_id,
            token_side=action.token_side,
            side=action.side,
            price=action.price,
            size=action.size,
            filled=0.0,
            status="open",
            placed_at=int(time.time()),
            ended_at=None,
            strategy=strategy,
        )
        insert_order(order)
        logger.info("paper_order_placed", order_id=order.order_id[:18],
                    market_id=order.market_id[:12], token=order.token_side,
                    side=order.side, price=order.price, size=order.size)
        return order

    def cancel(self, order_id: str) -> bool:
        cancel_order_row(order_id)
        logger.info("paper_order_cancelled", order_id=order_id[:18])
        return True

    def reconcile_fills(self, market: DiscoveredMarket, quote: Quote) -> int:
        """For each open order, check whether the live book has crossed our limit."""
        n_filled = 0
        for o in open_orders_by_market(market.market_id):
            if not _crossed(o, quote):
                continue
            insert_fill(Fill(
                id=None, order_id=o.order_id, market_id=o.market_id,
                token_side=o.token_side, side=o.side,
                price=o.price, size=o.size,
                fill_ts=int(time.time()), strategy=o.strategy,
            ))
            update_order_filled(o.order_id, filled=o.size, status="filled",
                                ended_at=int(time.time()))
            n_filled += 1
            logger.info("paper_order_filled", order_id=o.order_id[:18],
                        market_id=o.market_id[:12], token=o.token_side,
                        side=o.side, price=o.price, size=o.size)
        return n_filled


def _crossed(order: Order, quote: Quote) -> bool:
    """Has the live book crossed this order's limit since it was placed?"""
    if order.token_side == "YES":
        best_bid, best_ask = quote.yes_bid, quote.yes_ask
    else:
        best_bid, best_ask = quote.no_bid, quote.no_ask
    if order.side == "BUY":
        return best_ask is not None and best_ask <= order.price
    return best_bid is not None and best_bid >= order.price
