"""Phase D — live-mode prep tests.

Coverage:
  • LiveBroker `_estimate_new_chunk_price` — weighted-avg fill price math.
  • LiveBroker `_place_with_retry` — retry on transient errors, halt on
    HALT-class, give up after RETRY_MAX.
  • Router `MAX_ORDER_NOTIONAL_USD` cap — blocks both BUY and SELL above
    the cap; pass-through under it.
  • `sync_wallet_balance` drift warning when wallet cash diverges from
    derived realized cash.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

import polymarket_bot.execution.live_broker as lb
from polymarket_bot.execution.broker import Broker
from polymarket_bot.execution.live_broker import LiveBroker, sync_wallet_balance
from polymarket_bot.execution.router import Router
from polymarket_bot.persistence.repo import (
    Fill,
    Market,
    Order,
    insert_fill,
    insert_order,
    set_enabled_strategies,
    upsert_market,
)
from polymarket_bot.strategy.base import CancelOrder, PlaceLimit, WeatherEvent


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """Don't actually sleep between retries during tests."""
    monkeypatch.setattr(lb, "_RETRY_BASE_SLEEP", 0.0)
    lb.reset_halt_for_test()
    yield
    lb.reset_halt_for_test()


# ---------------------------------------------------------------------------
# D.1 — fill price from get_trades
# ---------------------------------------------------------------------------


def _stub_market_and_order(mid: str, *, size: float, price: float) -> None:
    upsert_market(Market(
        market_id=mid, slug=f"slug-{mid}", resolution_ts=1_700_000_000,
        yes_token_id=f"yes-{mid}", no_token_id=f"no-{mid}",
    ))
    insert_order(Order(
        order_id=f"o-{mid}", client_order_id="c1", market_id=mid,
        token_side="YES", side="BUY", price=price, size=size, filled=0.0,
        status="open", placed_at=1_700_000_000, ended_at=None,
        strategy="weather_forecast",
    ))


def _trade(size_real: float, price: float, order_id: str = "o-m1") -> dict[str, Any]:
    """A CLOB trade dict (size in 6-decimal fixed-math, price as float)."""
    return {"order_id": order_id, "size": str(int(size_real * 1_000_000)), "price": price}


def test_fill_price_falls_back_to_limit_when_no_trades_available():
    _stub_market_and_order("m1", size=10.0, price=0.30)
    client = MagicMock()
    client.get_trades_for_order.return_value = None   # transport failure
    broker = LiveBroker(client)
    order = Order(
        order_id="o-m1", client_order_id="c1", market_id="m1",
        token_side="YES", side="BUY", price=0.30, size=10.0, filled=0.0,
        status="open", placed_at=1_700_000_000, ended_at=None,
        strategy="weather_forecast",
    )
    px = broker._estimate_new_chunk_price(order, new_size=5.0)
    assert px == 0.30


def test_fill_price_uses_weighted_avg_of_trades():
    _stub_market_and_order("m1", size=10.0, price=0.50)
    # 4 shares at 0.40 + 6 shares at 0.30 → weighted avg = 0.34
    client = MagicMock()
    client.get_trades_for_order.return_value = [
        _trade(4.0, 0.40), _trade(6.0, 0.30),
    ]
    broker = LiveBroker(client)
    order = Order(
        order_id="o-m1", client_order_id="c1", market_id="m1",
        token_side="YES", side="BUY", price=0.50, size=10.0, filled=0.0,
        status="open", placed_at=1_700_000_000, ended_at=None,
        strategy="weather_forecast",
    )
    px = broker._estimate_new_chunk_price(order, new_size=10.0)
    assert abs(px - 0.34) < 1e-9


def test_fill_price_isolates_new_chunk_after_partial_fill():
    """Existing fills shouldn't be re-counted when computing the new chunk."""
    _stub_market_and_order("m1", size=10.0, price=0.50)
    # Existing recorded fill: 4 shares at 0.40 (paid 1.60).
    insert_fill(Fill(
        id=None, order_id="o-m1", market_id="m1",
        token_side="YES", side="BUY", price=0.40, size=4.0,
        fill_ts=1_700_000_000, strategy="weather_forecast",
    ))
    # Trades report cumulative: original 4 @ 0.40 + new 6 @ 0.30
    client = MagicMock()
    client.get_trades_for_order.return_value = [
        _trade(4.0, 0.40), _trade(6.0, 0.30),
    ]
    broker = LiveBroker(client)
    order = Order(
        order_id="o-m1", client_order_id="c1", market_id="m1",
        token_side="YES", side="BUY", price=0.50, size=10.0, filled=4.0,
        status="open", placed_at=1_700_000_000, ended_at=None,
        strategy="weather_forecast",
    )
    px = broker._estimate_new_chunk_price(order, new_size=6.0)
    # New chunk paid: 6 × 0.30 = 1.80; over 6 shares → 0.30.
    assert abs(px - 0.30) < 1e-9


def test_fill_price_clamps_when_trades_return_garbage():
    """Out-of-range computed price falls back to the limit (defensive)."""
    _stub_market_and_order("m1", size=10.0, price=0.20)
    client = MagicMock()
    client.get_trades_for_order.return_value = [
        {"order_id": "o-m1", "size": "5000000", "price": 99.0},   # 99 > 1
    ]
    broker = LiveBroker(client)
    order = Order(
        order_id="o-m1", client_order_id="c1", market_id="m1",
        token_side="YES", side="BUY", price=0.20, size=10.0, filled=0.0,
        status="open", placed_at=1_700_000_000, ended_at=None,
        strategy="weather_forecast",
    )
    px = broker._estimate_new_chunk_price(order, new_size=5.0)
    assert px == 0.20    # clamped → falls back to limit


# ---------------------------------------------------------------------------
# D.2 — retry loop
# ---------------------------------------------------------------------------


def _action() -> PlaceLimit:
    return PlaceLimit(
        market_id="m1", token_id="yes-m1",
        token_side="YES", side="BUY",
        price=0.30, size=10.0, client_order_id="c-1",
    )


def test_place_with_retry_succeeds_after_transient_failures():
    upsert_market(Market(
        market_id="m1", slug="ev::20°C", resolution_ts=1_700_000_000,
        yes_token_id="yes-m1", no_token_id="no-m1",
    ))
    client = MagicMock()
    client.place_order.side_effect = [
        None,   # transport blip
        {"success": False, "errorMsg": "Too Many Requests"},   # RETRY-class
        {"success": True, "orderID": "o-2", "status": "live"},
    ]
    broker = LiveBroker(client)
    order = broker.place_limit(_action(), strategy="weather_forecast")
    assert order is not None
    assert order.order_id == "o-2"
    assert client.place_order.call_count == 3


def test_place_with_retry_gives_up_after_max():
    upsert_market(Market(
        market_id="m1", slug="ev::20°C", resolution_ts=1_700_000_000,
        yes_token_id="yes-m1", no_token_id="no-m1",
    ))
    client = MagicMock()
    client.place_order.return_value = None   # always fails
    broker = LiveBroker(client)
    assert broker.place_limit(_action(), strategy="weather_forecast") is None
    assert client.place_order.call_count == lb._RETRY_MAX


def test_place_with_retry_skip_class_returns_immediately():
    """SKIP errors (e.g. tick-size violation) shouldn't burn the retry budget."""
    upsert_market(Market(
        market_id="m1", slug="ev::20°C", resolution_ts=1_700_000_000,
        yes_token_id="yes-m1", no_token_id="no-m1",
    ))
    client = MagicMock()
    client.place_order.return_value = {
        "success": False, "errorMsg": "minimum tick size rule",
    }
    broker = LiveBroker(client)
    assert broker.place_limit(_action(), strategy="weather_forecast") is None
    assert client.place_order.call_count == 1   # no retries


def test_place_with_retry_halt_class_stops_immediately():
    upsert_market(Market(
        market_id="m1", slug="ev::20°C", resolution_ts=1_700_000_000,
        yes_token_id="yes-m1", no_token_id="no-m1",
    ))
    client = MagicMock()
    client.place_order.side_effect = RuntimeError("Unauthorized")
    broker = LiveBroker(client)
    assert broker.place_limit(_action(), strategy="weather_forecast") is None
    assert lb.is_halted() is True


# ---------------------------------------------------------------------------
# D.6 — Router max-notional cap
# ---------------------------------------------------------------------------


class _StubBroker(Broker):
    def __init__(self) -> None:
        self.placed: list[PlaceLimit] = []

    def place_limit(self, action, strategy):
        self.placed.append(action)
        return Order(
            order_id="o-x", client_order_id=action.client_order_id,
            market_id=action.market_id, token_side=action.token_side,
            side=action.side, price=action.price, size=action.size,
            filled=0, status="open", placed_at=0, ended_at=None,
            strategy=strategy,
        )

    def cancel(self, order_id):
        return True

    def reconcile_fills(self, event):
        return 0


def test_router_blocks_buy_above_notional_cap():
    set_enabled_strategies({"weather_forecast"})
    broker = _StubBroker()
    router = Router(broker, "weather_forecast", max_notional_usd=5.0)
    big = PlaceLimit(market_id="m1", token_id="yes", token_side="YES",
                     side="BUY", price=0.50, size=20.0,    # = $10 notional
                     client_order_id="c-big")
    n = router.execute([big])
    assert n == 0
    assert broker.placed == []


def test_router_blocks_sell_above_notional_cap():
    set_enabled_strategies({"weather_forecast"})
    broker = _StubBroker()
    router = Router(broker, "weather_forecast", max_notional_usd=5.0)
    big = PlaceLimit(market_id="m1", token_id="yes", token_side="YES",
                     side="SELL", price=0.80, size=10.0,   # = $8 notional
                     client_order_id="c-big")
    n = router.execute([big])
    assert n == 0
    assert broker.placed == []


def test_router_lets_orders_under_cap_through():
    set_enabled_strategies({"weather_forecast"})
    broker = _StubBroker()
    router = Router(broker, "weather_forecast", max_notional_usd=10.0)
    small = PlaceLimit(market_id="m1", token_id="yes", token_side="YES",
                       side="BUY", price=0.20, size=10.0,    # = $2 notional
                       client_order_id="c-small")
    n = router.execute([small])
    assert n == 1


def test_router_with_no_cap_does_not_block():
    set_enabled_strategies({"weather_forecast"})
    broker = _StubBroker()
    router = Router(broker, "weather_forecast")   # default: no cap
    big = PlaceLimit(market_id="m1", token_id="yes", token_side="YES",
                     side="BUY", price=0.50, size=1000.0,    # = $500 notional
                     client_order_id="c-huge")
    n = router.execute([big])
    assert n == 1


def test_router_cap_does_not_block_cancels():
    set_enabled_strategies({"weather_forecast"})
    broker = _StubBroker()
    router = Router(broker, "weather_forecast", max_notional_usd=5.0)
    n = router.execute([CancelOrder(order_id="x")])
    assert n == 1


# ---------------------------------------------------------------------------
# D.3 — wallet drift detection
# ---------------------------------------------------------------------------


def test_sync_wallet_balance_warns_on_drift(capsys):
    client = MagicMock()
    client.get_balance_usdc.return_value = 50.0
    # No fills/settlements → derived cash = starting_bankroll = 100.0
    # Drift = wallet (50) − derived (100) = -50 → > threshold (1.0)
    cash = sync_wallet_balance(client, starting_bankroll=100.0)
    assert cash == 50.0
    captured = capsys.readouterr()
    # structlog writes to stdout/stderr; we check the rendered output for the
    # event name we emitted.
    assert "equity_drift" in (captured.out + captured.err)


def test_sync_wallet_balance_quiet_when_aligned(capsys):
    client = MagicMock()
    client.get_balance_usdc.return_value = 100.0
    cash = sync_wallet_balance(client, starting_bankroll=100.0)
    assert cash == 100.0
    captured = capsys.readouterr()
    assert "equity_drift" not in (captured.out + captured.err)
