"""Regression test: SpreadOnlyMM bids must never cross the no-arb line.

If bid_yes + bid_no > $1 we'd guarantee a loss on settlement (paying more
than the $1 binary payout). This test recreates the live conditions that
exposed the bug.
"""

from __future__ import annotations

from polymarket_bot.polymarket.markets import DiscoveredMarket
from polymarket_bot.polymarket.quotes import Quote
from polymarket_bot.strategy.base import Inventory, MMState, PlaceLimit
from polymarket_bot.strategy.spread_only import SpreadOnlyMM


def _state(quote: Quote) -> MMState:
    return MMState(
        market=DiscoveredMarket(market_id="0xabc", slug="x",
                                start_ts=0, resolution_ts=300,
                                yes_token_id="y", no_token_id="n"),
        quote=quote,
        inventory=Inventory(),
        open_orders=[],
        bankroll=100.0,
        seconds_to_resolution=200,
        base_spread=0.02, max_inventory_shares=20.0,
        inventory_skew=0.0005, lock_buffer_seconds=30,
    )


def test_tight_book_does_not_break_no_arb():
    """yes_bid==no_bid==0.50 is the live setup that caused 0.501+0.501 fills."""
    q = Quote(yes_bid=0.50, yes_ask=0.50, yes_mid=0.50,
              no_bid=0.50, no_ask=0.50, no_mid=0.50,
              depth_yes_ask_usd=200, depth_no_ask_usd=200)
    actions = SpreadOnlyMM().tick(_state(q))
    yes = next((a for a in actions if isinstance(a, PlaceLimit) and a.token_side == "YES"), None)
    no_ = next((a for a in actions if isinstance(a, PlaceLimit) and a.token_side == "NO"), None)
    if yes and no_:
        assert yes.price + no_.price <= 1.0 - 1e-6, (
            f"bid_yes ({yes.price}) + bid_no ({no_.price}) crossed no-arb"
        )


def test_quotes_never_above_mid():
    """For any reasonable book, posted bids must stay strictly below their mid."""
    cases = [
        Quote(0.50, 0.50, 0.50, 0.50, 0.50, 0.50, 200, 200),      # zero spread
        Quote(0.49, 0.51, 0.50, 0.49, 0.51, 0.50, 200, 200),      # 1¢ spread
        Quote(0.30, 0.32, 0.31, 0.68, 0.70, 0.69, 200, 200),      # asymmetric
    ]
    s = SpreadOnlyMM()
    for q in cases:
        for a in s.tick(_state(q)):
            if not isinstance(a, PlaceLimit):
                continue
            mid = q.yes_mid if a.token_side == "YES" else q.no_mid
            assert a.price < mid, f"{a.token_side} bid {a.price} not below mid {mid} on book {q}"
