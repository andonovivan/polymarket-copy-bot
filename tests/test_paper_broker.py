"""PaperMMBroker — order placement, cancellation, and cross-fill simulation."""

from __future__ import annotations

from pathlib import Path

import polymarket_bot.persistence.schema as schema
from polymarket_bot.execution.paper_broker import PaperMMBroker
from polymarket_bot.persistence.repo import (
    all_open_orders,
    fills_for_market,
    open_orders_by_market,
)
from polymarket_bot.polymarket.markets import DiscoveredMarket
from polymarket_bot.polymarket.quotes import Quote
from polymarket_bot.strategy.base import PlaceLimit


def _fresh_db(tmp_path: Path) -> None:
    schema._conn = None
    schema.init_db(tmp_path / "test.db")


def _market() -> DiscoveredMarket:
    return DiscoveredMarket(
        market_id="0xabc", slug="btc-updown-5m-1700000000",
        start_ts=1_700_000_000, resolution_ts=1_700_000_300,
        yes_token_id="yes-1", no_token_id="no-1",
    )


def _quote(yes_bid=0.49, yes_ask=0.51) -> Quote:
    return Quote(
        yes_bid=yes_bid, yes_ask=yes_ask, yes_mid=(yes_bid + yes_ask) / 2,
        no_bid=1 - yes_ask, no_ask=1 - yes_bid, no_mid=1 - (yes_bid + yes_ask) / 2,
        depth_yes_ask_usd=100, depth_no_ask_usd=100,
    )


def _place(token_side: str, side: str, price: float, size: float = 5.0) -> PlaceLimit:
    return PlaceLimit(
        market_id="0xabc", token_side=token_side, side=side,
        price=price, size=size, client_order_id=f"c-{token_side}-{side}",
    )


def _upsert_market(market: DiscoveredMarket) -> None:
    """Markets table FK requires the market to exist before we insert orders."""
    from polymarket_bot.persistence.repo import Market, upsert_market
    upsert_market(Market(
        market_id=market.market_id, slug=market.slug,
        resolution_ts=market.resolution_ts,
        yes_token_id=market.yes_token_id, no_token_id=market.no_token_id,
    ))


def test_place_persists_order(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    _upsert_market(_market())
    broker = PaperMMBroker()
    order = broker.place_limit(_place("YES", "BUY", 0.49), _market(), strategy="spread_only")
    assert order is not None
    assert order.status == "open"
    assert all_open_orders()[0].price == 0.49


def test_place_rejects_invalid_price(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    _upsert_market(_market())
    broker = PaperMMBroker()
    assert broker.place_limit(_place("YES", "BUY", 0.0), _market(), "spread_only") is None
    assert broker.place_limit(_place("YES", "BUY", 1.0), _market(), "spread_only") is None
    assert all_open_orders() == []


def test_cancel_marks_order_cancelled(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    _upsert_market(_market())
    broker = PaperMMBroker()
    order = broker.place_limit(_place("YES", "BUY", 0.49), _market(), "spread_only")
    assert broker.cancel(order.order_id) is True
    assert all_open_orders() == []


def test_buy_fills_when_best_ask_drops_to_limit(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    _upsert_market(_market())
    broker = PaperMMBroker()
    order = broker.place_limit(_place("YES", "BUY", 0.49), _market(), "spread_only")
    # Initial book: yes_ask=0.51 → no fill.
    n = broker.reconcile_fills(_market(), _quote(yes_bid=0.49, yes_ask=0.51))
    assert n == 0
    # Now yes_ask drops to our level → cross.
    n = broker.reconcile_fills(_market(), _quote(yes_bid=0.48, yes_ask=0.49))
    assert n == 1
    fills = fills_for_market("0xabc")
    assert len(fills) == 1
    assert fills[0].price == 0.49
    assert fills[0].size == 5.0
    assert open_orders_by_market("0xabc") == []


def test_buy_does_not_fill_when_book_stays_above(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    _upsert_market(_market())
    broker = PaperMMBroker()
    broker.place_limit(_place("YES", "BUY", 0.49), _market(), "spread_only")
    n = broker.reconcile_fills(_market(), _quote(yes_bid=0.50, yes_ask=0.55))
    assert n == 0
    assert fills_for_market("0xabc") == []


def test_sell_fills_when_best_bid_rises_to_limit(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    _upsert_market(_market())
    broker = PaperMMBroker()
    broker.place_limit(_place("YES", "SELL", 0.55), _market(), "spread_only")
    # Bid rises to our SELL price → cross.
    n = broker.reconcile_fills(_market(), _quote(yes_bid=0.55, yes_ask=0.56))
    assert n == 1
    fill = fills_for_market("0xabc")[0]
    assert fill.side == "SELL"
    assert fill.price == 0.55


def test_no_side_book_isolated_from_yes_side(tmp_path: Path) -> None:
    """A YES BUY shouldn't fill on NO-side movement and vice versa."""
    _fresh_db(tmp_path)
    _upsert_market(_market())
    broker = PaperMMBroker()
    broker.place_limit(_place("YES", "BUY", 0.40), _market(), "spread_only")
    broker.place_limit(_place("NO", "BUY", 0.40), _market(), "spread_only")
    # YES book moves to favor YES BUY fill but NO book doesn't.
    n = broker.reconcile_fills(
        _market(),
        Quote(
            yes_bid=0.39, yes_ask=0.40, yes_mid=0.395,
            no_bid=0.55, no_ask=0.65, no_mid=0.60,
            depth_yes_ask_usd=100, depth_no_ask_usd=100,
        ),
    )
    assert n == 1
    fills = fills_for_market("0xabc")
    assert len(fills) == 1
    assert fills[0].token_side == "YES"
