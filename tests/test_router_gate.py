"""Phase A.3 — Router suppresses BUY actions for disabled strategies but
lets SELL and CancelOrder pass through so existing positions can wind down."""

from __future__ import annotations

from typing import Any

from polymarket_bot.execution.broker import Broker
from polymarket_bot.execution.router import Router
from polymarket_bot.persistence.repo import (
    Order,
    set_enabled_strategies,
)
from polymarket_bot.strategy.base import CancelOrder, PlaceLimit


class _FakeBroker(Broker):
    """Records calls without touching the DB or external APIs."""

    def __init__(self) -> None:
        self.placed: list[tuple[PlaceLimit, str]] = []
        self.cancelled: list[str] = []

    def place_limit(self, action: PlaceLimit, strategy: str) -> Order | None:
        self.placed.append((action, strategy))
        return Order(
            order_id="o-x", client_order_id=action.client_order_id,
            market_id=action.market_id, token_side=action.token_side,
            side=action.side, price=action.price, size=action.size,
            filled=0.0, status="open",
            placed_at=0, ended_at=None, strategy=strategy,
        )

    def cancel(self, order_id: str) -> bool:
        self.cancelled.append(order_id)
        return True

    def reconcile_fills(self, event: Any) -> int:
        return 0


def _buy(label: str = "16°C") -> PlaceLimit:
    return PlaceLimit(
        market_id=f"m-{label}", token_id=f"t-{label}",
        token_side="YES", side="BUY",
        price=0.20, size=10.0, client_order_id=f"cid-{label}",
    )


def _sell(label: str = "16°C") -> PlaceLimit:
    return PlaceLimit(
        market_id=f"m-{label}", token_id=f"t-{label}",
        token_side="YES", side="SELL",
        price=0.50, size=10.0, client_order_id=f"cid-{label}",
    )


def test_buy_passes_when_strategy_enabled():
    set_enabled_strategies({"weather_forecast", "bucket_arbitrage"})
    broker = _FakeBroker()
    n = Router(broker, "weather_forecast").execute([_buy()])
    assert n == 1
    assert len(broker.placed) == 1
    assert broker.placed[0][1] == "weather_forecast"


def test_buy_dropped_when_strategy_disabled():
    set_enabled_strategies({"bucket_arbitrage"})   # weather_forecast OFF
    broker = _FakeBroker()
    n = Router(broker, "weather_forecast").execute([_buy()])
    assert n == 0
    assert broker.placed == []


def test_sell_passes_when_strategy_disabled():
    """Profit-taking SELLs continue even with the strategy off — that's
    the whole point of the feature: existing positions wind down."""
    set_enabled_strategies(set())   # everything disabled
    broker = _FakeBroker()
    n = Router(broker, "weather_forecast").execute([_sell()])
    assert n == 1
    assert broker.placed[0][0].side == "SELL"


def test_cancel_passes_when_strategy_disabled():
    set_enabled_strategies(set())
    broker = _FakeBroker()
    n = Router(broker, "weather_forecast").execute([CancelOrder(order_id="abc")])
    assert n == 1
    assert broker.cancelled == ["abc"]


def test_mixed_actions_buy_dropped_others_pass():
    set_enabled_strategies({"bucket_arbitrage"})   # weather off
    broker = _FakeBroker()
    n = Router(broker, "weather_forecast").execute([
        _buy("a"),       # dropped
        _sell("b"),      # placed
        CancelOrder(order_id="c"),
    ])
    assert n == 2
    assert [p[0].side for p in broker.placed] == ["SELL"]
    assert broker.cancelled == ["c"]


def test_default_all_strategies_enabled_when_meta_unset():
    """No row in meta → defaults to all-enabled (so a fresh DB doesn't
    silently block trading)."""
    # Don't call set_enabled_strategies; meta is empty.
    broker = _FakeBroker()
    n = Router(broker, "weather_forecast").execute([_buy()])
    assert n == 1
