"""Live broker — places real CLOB orders and polls fills."""

from __future__ import annotations

import time

import structlog

from polymarket_bot.execution.broker import Broker
from polymarket_bot.execution.error_codes import classify
from polymarket_bot.persistence.repo import (
    Fill,
    Order,
    cancel_order_row,
    fills_for_order,
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


_RETRY_MAX = 3
_RETRY_BASE_SLEEP = 1.0


class LiveBroker(Broker):
    def __init__(self, client: PolymarketClient) -> None:
        self.client = client

    def _place_with_retry(self, action: PlaceLimit) -> dict | None:
        """Call `client.place_order` with bounded retry on RETRY-class errors.

        Behavior:
          * HALT → bail immediately (also flips the global halt flag).
          * RETRY → sleep `base * 2^attempt` and try again, up to RETRY_MAX.
          * SKIP / explicit success-false → bubble back as `result` so the
            caller can record/log it.
          * Transport exceptions (None response, raised exceptions in the
            client wrapper) → treated like RETRY.

        Returns the final dict response (which may have success=False) or
        None if every attempt failed.
        """
        delay = _RETRY_BASE_SLEEP
        last_msg = "no response"
        for attempt in range(_RETRY_MAX):
            try:
                result = self.client.place_order(
                    token_id=action.token_id, side=action.side,
                    price=action.price, size=action.size,
                )
            except Exception as exc:
                action_class = _handle_clob_failure(
                    "place_order", None, str(exc)[:200])
                if action_class == "HALT":
                    return None
                last_msg = str(exc)[:200]
                time.sleep(delay)
                delay *= 2
                continue
            if result is None:
                # No response from the wrapper — treat as a transport blip.
                _handle_clob_failure("place_order", None, "no response")
                last_msg = "no response"
                time.sleep(delay)
                delay *= 2
                continue
            if result.get("success"):
                return result
            # Server returned an explicit rejection — let `place_limit`
            # classify and decide whether to retry. We only retry RETRY-class
            # errors here; SKIP / HALT are returned for the caller to handle.
            from polymarket_bot.execution.error_codes import classify
            err = classify(400, result.get("errorMsg") or result.get("error"))
            if err.action == "RETRY":
                logger.warning("place_retry", attempt=attempt + 1,
                               message=err.message[:160])
                time.sleep(delay)
                delay *= 2
                continue
            # SKIP / HALT / unknown → return so the caller logs and moves on.
            return result
        logger.warning("place_gave_up", retries=_RETRY_MAX, last=last_msg[:160])
        return None

    def place_limit(self, action: PlaceLimit, strategy: str) -> Order | None:
        if _HALTED:
            logger.warning("place_skipped_halted", market_id=action.market_id[:12])
            return None

        # Phase D.2 — retry on transient errors (rate limits, matching-engine
        # restarts, transport blips) with exponential backoff. HALT errors
        # short-circuit immediately; SKIP errors propagate as a single
        # rejection (don't keep retrying a bad-payload order).
        result = self._place_with_retry(action)
        if result is None:
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

        Phase D.1: the new fill chunk's price is computed as the weighted
        average of *only the trades since the last reconcile* (delta of the
        cumulative weighted-avg). For taker orders against a static book
        this matches the limit price; for maker orders that filled at
        improved prices it captures the actual average correctly.
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
                fill_price = self._estimate_new_chunk_price(o, new_size)
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
                            new_price=round(fill_price, 4),
                            total_filled=round(filled_real, 4),
                            done=done)
        return n

    def _estimate_new_chunk_price(self, o: Order, new_size: float) -> float:
        """Weighted-avg fill price for the chunk of `new_size` shares that
        filled since the last reconcile.

        Strategy:
          1. Fetch all trades for `o.order_id`.
          2. Sum (size, size·price) across them.
          3. Subtract what we've already recorded (existing fills for this
             order) to isolate the new chunk's weighted total.
          4. Divide → price for the new chunk.

        Falls back to `o.price` if trades aren't available — this is the
        correct approximation for taker fills against a static book, which
        is the bot's predominant order shape (BUY at yes_ask, SELL at
        yes_bid).
        """
        trades = self.client.get_trades_for_order(o.order_id)
        if not trades:
            return float(o.price)

        cum_size = 0.0
        cum_paid = 0.0
        for t in trades:
            sz = _fm6(t.get("size", 0))
            try:
                pr = float(t.get("price", 0) or 0)
            except (TypeError, ValueError):
                pr = 0.0
            if sz > 0 and 0.0 <= pr <= 1.0:
                cum_size += sz
                cum_paid += sz * pr
        if cum_size <= 0:
            return float(o.price)

        prev_size = 0.0
        prev_paid = 0.0
        for f in fills_for_order(o.order_id):
            prev_size += float(f.size)
            prev_paid += float(f.size) * float(f.price)

        chunk_size = cum_size - prev_size
        chunk_paid = cum_paid - prev_paid
        if chunk_size <= 0:
            return float(o.price)
        chunk_price = chunk_paid / chunk_size
        # Sanity-clamp: prices outside [0.001, 0.999] would be a bug or a
        # stale-trade artefact; falling back to limit is safer than logging
        # a corrupt fill row.
        if not (0.001 <= chunk_price <= 0.999):
            return float(o.price)
        return chunk_price


EQUITY_DRIFT_WARN_THRESHOLD = 1.0


def sync_wallet_balance(client: PolymarketClient,
                        starting_bankroll: float = 100.0) -> float | None:
    """Read the wallet's free USDC and reconcile it with our derived equity.

    Phase D.3: previously this wrote cash-only into the equity curve while
    `_maybe_sample_equity` (in main.py) wrote MTM-aware equity from the
    same curve — two writers, divergent curve. Now we *don't* append a
    cash-only point; instead we compare wallet cash against our derived
    realized cash and warn when they drift more than
    `EQUITY_DRIFT_WARN_THRESHOLD`. The MTM-aware sampler stays the single
    writer of `equity_curve`.

    Returns the wallet cash for caller logging.
    """
    from polymarket_bot.main import _realized_cash   # avoid import cycle
    balance = client.get_balance_usdc()
    if balance is None:
        logger.warning("wallet_balance_fetch_failed")
        return None
    cash_real = float(balance)
    cash_derived = _realized_cash(starting_bankroll)
    drift = cash_real - cash_derived
    if abs(drift) > EQUITY_DRIFT_WARN_THRESHOLD:
        logger.warning("equity_drift",
                       wallet_cash=round(cash_real, 4),
                       derived_cash=round(cash_derived, 4),
                       drift=round(drift, 4),
                       hint="check for missing fills or settlements")
    else:
        logger.info("wallet_balance_synced",
                    wallet_cash=round(cash_real, 4),
                    derived_cash=round(cash_derived, 4))
    return cash_real
