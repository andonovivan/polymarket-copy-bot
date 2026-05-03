"""SpreadOnlyMM strategy unit tests — quoting, inventory skew, lockout, sizing."""

from __future__ import annotations

from polymarket_bot.polymarket.markets import DiscoveredMarket
from polymarket_bot.polymarket.quotes import Quote
from polymarket_bot.strategy.base import (
    CancelOrder,
    Inventory,
    MMState,
    OpenOrder,
    PlaceLimit,
)
from polymarket_bot.strategy.spread_only import SpreadOnlyMM


def _market(res_ts: int = 1_777_805_400) -> DiscoveredMarket:
    return DiscoveredMarket(
        market_id="0xabc", slug=f"btc-updown-5m-{res_ts - 300}",
        start_ts=res_ts - 300, resolution_ts=res_ts,
        yes_token_id="yes-1", no_token_id="no-1",
    )


def _quote(yes_bid=0.49, yes_ask=0.51, depth=200.0) -> Quote:
    no_bid, no_ask = 1.0 - yes_ask, 1.0 - yes_bid
    return Quote(
        yes_bid=yes_bid, yes_ask=yes_ask, yes_mid=(yes_bid + yes_ask) / 2,
        no_bid=no_bid, no_ask=no_ask, no_mid=(no_bid + no_ask) / 2,
        depth_yes_ask_usd=depth, depth_no_ask_usd=depth,
    )


def _state(*, inv: Inventory | None = None, open_orders: list[OpenOrder] | None = None,
           seconds_to_resolution: int = 200, bankroll: float = 100.0) -> MMState:
    return MMState(
        market=_market(),
        quote=_quote(),
        inventory=inv or Inventory(),
        open_orders=open_orders or [],
        bankroll=bankroll,
        seconds_to_resolution=seconds_to_resolution,
        base_spread=0.02, max_inventory_shares=20.0,
        inventory_skew=0.0005, lock_buffer_seconds=30,
    )


# ---------- Lockout ----------


def test_lockout_cancels_all_open_orders_and_places_nothing():
    open_orders = [
        OpenOrder(order_id="A", client_order_id="c1", market_id="0xabc",
                  token_side="YES", side="BUY", price=0.49, size=5),
        OpenOrder(order_id="B", client_order_id="c2", market_id="0xabc",
                  token_side="NO", side="BUY", price=0.49, size=5),
    ]
    s = SpreadOnlyMM().tick(_state(open_orders=open_orders, seconds_to_resolution=20))
    cancels = [a for a in s if isinstance(a, CancelOrder)]
    places = [a for a in s if isinstance(a, PlaceLimit)]
    assert len(cancels) == 2
    assert {c.order_id for c in cancels} == {"A", "B"}
    assert places == []


def test_no_quote_data_cancels_everything():
    open_orders = [OpenOrder(order_id="A", client_order_id="c1", market_id="0xabc",
                             token_side="YES", side="BUY", price=0.49, size=5)]
    state = _state(open_orders=open_orders)
    state.quote.yes_mid = None
    state.quote.no_mid = None
    actions = SpreadOnlyMM().tick(state)
    assert all(isinstance(a, CancelOrder) for a in actions)


# ---------- Initial quoting ----------


def test_initial_quotes_buy_both_sides_inside_book():
    actions = SpreadOnlyMM().tick(_state())
    places = [a for a in actions if isinstance(a, PlaceLimit)]
    assert len(places) == 2
    by_token = {p.token_side: p for p in places}
    assert "YES" in by_token and "NO" in by_token
    assert all(p.side == "BUY" for p in places)
    # Quotes should sit strictly inside the live spread (between bid and ask)
    yes = by_token["YES"]
    assert 0.49 < yes.price < 0.51
    no_ = by_token["NO"]
    assert 0.49 < no_.price < 0.51


# ---------- Inventory cap ----------


def test_yes_cap_skips_yes_quote_only():
    inv = Inventory(yes_shares=20.0, no_shares=0.0)  # at the cap
    actions = SpreadOnlyMM().tick(_state(inv=inv))
    places = [a for a in actions if isinstance(a, PlaceLimit)]
    tokens = {p.token_side for p in places}
    assert "YES" not in tokens
    assert "NO" in tokens


def test_both_caps_no_places():
    inv = Inventory(yes_shares=20.0, no_shares=20.0)
    actions = SpreadOnlyMM().tick(_state(inv=inv))
    places = [a for a in actions if isinstance(a, PlaceLimit)]
    assert places == []


# ---------- Inventory skew ----------


def test_long_yes_inventory_lowers_yes_bid():
    base = SpreadOnlyMM().tick(_state(inv=Inventory()))
    long_yes = SpreadOnlyMM().tick(_state(inv=Inventory(yes_shares=10.0)))
    base_yes_price = next(p.price for p in base if isinstance(p, PlaceLimit) and p.token_side == "YES")
    skewed_yes_price = next(p.price for p in long_yes if isinstance(p, PlaceLimit) and p.token_side == "YES")
    # Long YES → lower YES bid (less aggressive on YES, want to attract NO fills instead)
    assert skewed_yes_price < base_yes_price


# ---------- Repost / keep ----------


def test_keeps_existing_order_at_target_price():
    # Compute the target price the strategy would post.
    actions = SpreadOnlyMM().tick(_state())
    target_yes = next(p for p in actions if isinstance(p, PlaceLimit) and p.token_side == "YES")
    # Now plug in an open order EXACTLY at that price; strategy should not repost.
    existing = OpenOrder(
        order_id="EXISTING", client_order_id="c1", market_id="0xabc",
        token_side="YES", side="BUY", price=target_yes.price, size=target_yes.size,
    )
    second = SpreadOnlyMM().tick(_state(open_orders=[existing]))
    yes_actions = [a for a in second if (
        isinstance(a, PlaceLimit) and a.token_side == "YES"
    ) or (isinstance(a, CancelOrder) and a.order_id == "EXISTING")]
    # One open order at the right price → no PlaceLimit nor Cancel for YES.
    assert yes_actions == []


def test_drifted_order_gets_cancelled_and_replaced():
    # Existing YES order far from current target → cancel + repost.
    existing = OpenOrder(
        order_id="STALE", client_order_id="c1", market_id="0xabc",
        token_side="YES", side="BUY", price=0.40, size=5,
    )
    actions = SpreadOnlyMM().tick(_state(open_orders=[existing]))
    cancels = [a for a in actions if isinstance(a, CancelOrder)]
    places = [a for a in actions if isinstance(a, PlaceLimit) and a.token_side == "YES"]
    assert any(c.order_id == "STALE" for c in cancels)
    assert len(places) == 1


# ---------- Time decay ----------


def test_spread_tightens_near_resolution():
    early = SpreadOnlyMM().tick(_state(seconds_to_resolution=270))
    late = SpreadOnlyMM().tick(_state(seconds_to_resolution=60))

    def yes_price(actions):
        return next(p.price for p in actions if isinstance(p, PlaceLimit) and p.token_side == "YES")

    early_yes = yes_price(early)
    late_yes = yes_price(late)
    # Tighter spread late ⇒ YES bid moves UP (closer to mid).
    assert late_yes >= early_yes


# ---------- Sizing ----------


def test_size_capped_by_per_quote_budget():
    # bankroll=100 ⇒ per_quote_budget=$5. At price 0.5 ⇒ 10 shares.
    actions = SpreadOnlyMM().tick(_state(bankroll=100.0))
    yes = next(p for p in actions if isinstance(p, PlaceLimit) and p.token_side == "YES")
    assert yes.size <= 10.5  # ~$5 worth at price ~0.5
    assert yes.size >= 1.0


def test_zero_bankroll_no_quotes():
    actions = SpreadOnlyMM().tick(_state(bankroll=0.0))
    assert [a for a in actions if isinstance(a, PlaceLimit)] == []
