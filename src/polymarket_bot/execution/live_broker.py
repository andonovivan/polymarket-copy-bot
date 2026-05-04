"""Live broker — places real CLOB orders and polls fills."""

from __future__ import annotations

import time

import structlog

from polymarket_bot.execution.broker import Broker
from polymarket_bot.execution.error_codes import classify
from polymarket_bot.persistence.repo import (
    Fill,
    Order,
    append_equity,
    cancel_order_row,
    insert_fill,
    insert_order,
    open_orders_by_market,
    update_order_filled,
)
from polymarket_bot.polymarket.client import PolymarketClient
from polymarket_bot.strategy.base import PlaceLimit, WeatherEvent

logger = structlog.get_logger()


# Polymarket CLOB returns sizes/prices in fixed-math with 6 decimals
# (e.g., size_matched="1500000" means 1.5 shares, makingAmount="5000000" means $5).
DECIMALS = 1_000_000.0


def _fm6(value, default: float = 0.0) -> float:
    """Convert a 6-decimal fixed-math string/int from the CLOB to a float."""
    try:
        return float(value) / DECIMALS
    except (TypeError, ValueError):
        return default


# Module-level flag set when we hit a HALT-class error. Strategy can read this
# to stop placing new orders. (Cleared only on bot restart.)
_HALTED = False


def is_halted() -> bool:
    return _HALTED


def reset_halt_for_test() -> None:
    """Test-only — clears the HALT flag."""
    global _HALTED
    _HALTED = False


def _handle_clob_failure(context: str, http_status: int | None, message: str | None) -> str:
    """Classify a CLOB failure and side-effect the global HALT flag."""
    global _HALTED
    err = classify(http_status, message)
    log_kw = {"context": context, "http_status": err.http_status,
              "message": err.message[:200], "action": err.action}
    if err.action == "HALT":
        _HALTED = True
        logger.error("clob_halt", **log_kw)
    elif err.action == "RETRY":
        logger.warning("clob_retry", **log_kw)
    else:
        logger.warning("clob_skip", **log_kw)
    return err.action


class LiveBroker(Broker):
    def __init__(self, client: PolymarketClient) -> None:
        self.client = client

    def place_limit(self, action: PlaceLimit, strategy: str) -> Order | None:
        if _HALTED:
            logger.warning("place_skipped_halted", market_id=action.market_id[:12])
            return None

        result = self.client.place_order(
            token_id=action.token_id, side=action.side,
            price=action.price, size=action.size,
        )
        if result is None:
            _handle_clob_failure("place_order", None, "no response")
            return None

        # Per docs: { "success": bool, "orderID": str, "status": "live"|"matched"|"delayed",
        #             "errorMsg": str, "makingAmount": str, "takingAmount": str, ... }
        if not result.get("success", False):
            err_msg = result.get("errorMsg") or result.get("error") or "unknown rejection"
            # Synthetic 400 — this is an *explicit* server rejection (server was
            # reachable, said no), not a transport error. The classifier treats
            # unknown 4xx as SKIP, which prevents infinite re-placing of orders
            # whose errorMsg we don't recognize.
            _handle_clob_failure("place_order", 400, err_msg)
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

        # If the CLOB matched the order immediately (status="matched"), the next
        # tick's reconcile_fills will pick it up via get_order's size_matched.
        # No special handling needed here.
        clob_status = result.get("status", "live")
        logger.info("live_order_placed",
                    order_id=order_id[:18], market_id=order.market_id[:12],
                    side=order.side, token=order.token_side,
                    price=order.price, size=order.size, clob_status=clob_status)
        return order

    def cancel(self, order_id: str) -> bool:
        if _HALTED:
            logger.warning("cancel_skipped_halted", order_id=order_id[:18])
            return False
        try:
            # py-clob-client method is `cancel`, NOT `cancel_order`.
            result = self.client.clob.cancel(order_id)
        except Exception as exc:
            _handle_clob_failure("cancel", None, str(exc)[:200])
            return False
        # Cancel response is typically { "canceled": [...], "not_canceled": {} }.
        not_cancelled = (result or {}).get("not_canceled") or {}
        if order_id in not_cancelled:
            _handle_clob_failure("cancel", None, str(not_cancelled.get(order_id)))
            return False
        cancel_order_row(order_id)
        logger.info("live_order_cancelled", order_id=order_id[:18])
        return True

    def reconcile_fills(self, event: WeatherEvent) -> int:
        """Poll CLOB for each open order, persist any new fill amount.

        Per docs, get_order returns size_matched/original_size in 6-decimal
        fixed-math (string). Convert via DECIMALS before comparing to o.size,
        which is in real shares.
        """
        n = 0
        for b in event.buckets:
            for o in open_orders_by_market(b.market_id):
                try:
                    status = self.client.clob.get_order(o.order_id)
                except Exception as exc:
                    _handle_clob_failure("get_order", None, str(exc)[:200])
                    continue
                if status is None:
                    continue
                filled_real = _fm6(status.get("size_matched", 0))
                if filled_real <= o.filled:
                    continue
                new_size = filled_real - o.filled
                # CLOB get_order doesn't return a fill price; for a maker order at
                # our limit, fill price ≈ limit. The exact average is in get_trades.
                fill_price = float(status.get("price", o.price))
                insert_fill(Fill(
                    id=None, order_id=o.order_id, market_id=o.market_id,
                    token_side=o.token_side, side=o.side,
                    price=fill_price, size=new_size,
                    fill_ts=int(time.time()), strategy=o.strategy,
                ))
                done = filled_real >= o.size - 1e-9
                update_order_filled(
                    o.order_id, filled=filled_real,
                    status="filled" if done else "open",
                    ended_at=int(time.time()) if done else None,
                )
                n += 1
                logger.info("live_order_fill",
                            order_id=o.order_id[:18], market=b.label,
                            new_shares=round(new_size, 4),
                            total_filled=round(filled_real, 4),
                            done=done)
        return n


def sync_wallet_balance(client: PolymarketClient) -> float | None:
    """Read the wallet's free USDC (Polymarket collateral) and append to equity_curve.

    NOTE: this is *cash only* — does not include the value of held YES tokens.
    For total live equity you also need the position value (see
    `compute_live_equity` in main.py).

    Call once on startup and periodically (e.g. every 5 min) in live mode.
    """
    balance = client.get_balance_usdc()
    if balance is None:
        logger.warning("wallet_balance_fetch_failed")
        return None
    append_equity(int(time.time()), float(balance))
    logger.info("wallet_balance_synced", usdc=round(float(balance), 4))
    return float(balance)
