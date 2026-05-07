"""Paper broker — buys at the live YES ask immediately when we BUY at-or-above ask.

Fill model (taker semantics, conservative):
  • BUY at P fills in full at P when live yes_ask ≤ P (we crossed the spread).
  • Otherwise the order rests; we re-check on each tick.
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
from polymarket_bot.polymarket.book import fetch_book
from polymarket_bot.polymarket.quotes import parse_book
from polymarket_bot.strategy.base import PlaceLimit, WeatherEvent

logger = structlog.get_logger()


class PaperBroker(Broker):

    def place_limit(self, action: PlaceLimit, strategy: str) -> Order | None:
        if not (0.0 < action.price < 1.0) or action.size <= 0:
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
        logger.info("paper_order_placed",
                    order_id=order.order_id[:18], market_id=order.market_id[:12],
                    side=order.side, token=order.token_side,
                    price=order.price, size=order.size)
        return order

    def cancel(self, order_id: str) -> bool:
        cancel_order_row(order_id)
        logger.info("paper_order_cancelled", order_id=order_id[:18])
        return True

    def reconcile_fills(self, event: WeatherEvent) -> int:
        """For each bucket, fetch the appropriate (YES or NO) book per open
        order and fill any orders that cross."""
        n_filled = 0
        for b in event.buckets:
            yes_book = None
            no_book = None
            for o in open_orders_by_market(b.market_id):
                if o.token_side == "YES":
                    if yes_book is None:
                        yes_bid, yes_ask, _ = parse_book(fetch_book(b.yes_token_id))
                        yes_book = (yes_bid, yes_ask)
                    bid, ask = yes_book
                else:   # NO
                    if no_book is None:
                        no_bid, no_ask, _ = parse_book(fetch_book(b.no_token_id))
                        no_book = (no_bid, no_ask)
                    bid, ask = no_book
                if not _crossed(o, bid, ask):
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
                logger.info("paper_order_filled",
                            order_id=o.order_id[:18], bucket=b.label,
                            token=o.token_side, side=o.side,
                            price=o.price, size=o.size)
        return n_filled


def _crossed(order: Order, bid: float | None, ask: float | None) -> bool:
    """Did the order's book cross our limit since we placed?

    Works for either YES or NO tokens — caller passes the matching book.
    BUY at P: fills if ask <= P (someone is selling at or below our bid).
    SELL at P: fills if bid >= P (someone bidding at or above our ask).
    """
    if order.side == "BUY":
        return ask is not None and ask <= order.price
    return bid is not None and bid >= order.price
