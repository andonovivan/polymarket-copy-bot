"""Live broker — places real CLOB orders and polls fills."""

from __future__ import annotations

import time

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
from polymarket_bot.polymarket.client import PolymarketClient
from polymarket_bot.strategy.base import PlaceLimit, WeatherEvent

logger = structlog.get_logger()


class LiveBroker(Broker):
    def __init__(self, client: PolymarketClient) -> None:
        self.client = client

    def place_limit(self, action: PlaceLimit, strategy: str) -> Order | None:
        result = self.client.place_order(
            token_id=action.token_id, side=action.side,
            price=action.price, size=action.size,
        )
        if not result:
            return None
        order_id = str(result.get("orderID") or result.get("orderId") or result.get("id") or "")
        if not order_id:
            logger.warning("live_order_missing_id", result=result)
            return None
        order = Order(
            order_id=order_id, client_order_id=action.client_order_id,
            market_id=action.market_id, token_side=action.token_side, side=action.side,
            price=action.price, size=action.size, filled=0.0, status="open",
            placed_at=int(time.time()), ended_at=None, strategy=strategy,
        )
        insert_order(order)
        return order

    def cancel(self, order_id: str) -> bool:
        try:
            self.client.clob.cancel_order(order_id)
        except Exception as exc:
            logger.warning("live_cancel_failed", order_id=order_id[:18], error=str(exc)[:200])
            return False
        cancel_order_row(order_id)
        return True

    def reconcile_fills(self, event: WeatherEvent) -> int:
        n = 0
        for b in event.buckets:
            for o in open_orders_by_market(b.market_id):
                try:
                    status = self.client.clob.get_order(o.order_id)
                except Exception as exc:
                    logger.warning("live_status_fetch_failed", order_id=o.order_id[:18],
                                   error=str(exc)[:200])
                    continue
                filled_size = float(status.get("size_matched", 0))
                if filled_size <= o.filled:
                    continue
                new_size = filled_size - o.filled
                avg_price = float(status.get("price", o.price))
                insert_fill(Fill(
                    id=None, order_id=o.order_id, market_id=o.market_id,
                    token_side=o.token_side, side=o.side,
                    price=avg_price, size=new_size,
                    fill_ts=int(time.time()), strategy=o.strategy,
                ))
                new_status = "filled" if filled_size >= o.size - 1e-9 else "open"
                update_order_filled(o.order_id, filled=filled_size, status=new_status,
                                    ended_at=int(time.time()) if new_status == "filled" else None)
                n += 1
        return n
