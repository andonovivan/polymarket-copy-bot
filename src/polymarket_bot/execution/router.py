"""Router: takes a list of OrderActions, dispatches through the broker.

Two safety gates fire here, before the broker sees the order:

  1. **Strategy enable/disable** (Phase A.3) — if `enabled_strategies` in
     the meta table doesn't include this router's strategy_name, entry
     orders (BUY) are dropped with `router_buy_blocked`. SELLs and Cancels
     pass through so existing positions wind down.

  2. **Max-notional cap** (Phase D.6) — if `price × size` exceeds
     `MAX_ORDER_NOTIONAL_USD`, the order is dropped with
     `router_notional_blocked`. Catches fat-finger sizing bugs before
     they hit the live CLOB.
"""

from __future__ import annotations

import structlog

from polymarket_bot.execution.broker import Broker
from polymarket_bot.strategy.base import CancelOrder, OrderAction, PlaceLimit

logger = structlog.get_logger()


class Router:
    def __init__(self, broker: Broker, strategy_name: str,
                 max_notional_usd: float | None = None) -> None:
        self.broker = broker
        self.strategy_name = strategy_name
        self.max_notional_usd = max_notional_usd

    def _is_enabled(self) -> bool:
        # Imported lazily so unit tests don't need a DB just to construct
        # a Router (the gate only triggers when execute() actually runs).
        from polymarket_bot.persistence.repo import get_enabled_strategies
        from polymarket_bot.strategy.registry import list_strategies
        enabled = get_enabled_strategies(default_all=list_strategies())
        return self.strategy_name in enabled

    def execute(self, actions: list[OrderAction]) -> int:
        if not actions:
            return 0
        gate_evaluated = False
        enabled = True
        n = 0
        for a in actions:
            if isinstance(a, PlaceLimit):
                # Notional cap (D.6) — applies to BOTH BUY and SELL. Even
                # profit-take SELLs shouldn't quietly send a $500 order if
                # the cap is $50.
                if (self.max_notional_usd is not None
                        and a.price * a.size > self.max_notional_usd + 1e-9):
                    logger.warning(
                        "router_notional_blocked",
                        strategy=self.strategy_name,
                        market_id=a.market_id[:14],
                        side=a.side, token_side=a.token_side,
                        price=a.price, size=a.size,
                        notional=round(a.price * a.size, 2),
                        cap=self.max_notional_usd,
                    )
                    continue

                # Strategy-disabled gate (A.3) — only blocks BUYs so existing
                # positions can still wind down via SELLs.
                if a.side == "BUY":
                    if not gate_evaluated:
                        enabled = self._is_enabled()
                        gate_evaluated = True
                    if not enabled:
                        logger.info(
                            "router_buy_blocked",
                            strategy=self.strategy_name,
                            market_id=a.market_id[:14],
                            token_side=a.token_side,
                            price=a.price, size=a.size,
                        )
                        continue
                if self.broker.place_limit(a, self.strategy_name):
                    n += 1
            elif isinstance(a, CancelOrder):
                if self.broker.cancel(a.order_id):
                    n += 1
            else:
                logger.warning("unknown_action", action=type(a).__name__)
        return n
