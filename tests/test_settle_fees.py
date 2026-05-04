"""Settlement fee math: Polymarket weather markets charge 5% on winnings (taker)."""

from __future__ import annotations

import math
from pathlib import Path

import polymarket_bot.persistence.schema as schema
from polymarket_bot.persistence.repo import (
    Fill,
    Market,
    Order,
    insert_fill,
    insert_order,
    list_settlements,
    upsert_market,
)
from polymarket_bot.polymarket.settle import settle_resolved_event
from polymarket_bot.strategy.base import Bucket, WeatherEvent


def _fresh_db(tmp_path: Path) -> None:
    schema._conn = None
    schema.init_db(tmp_path / "test.db")


def _seed_market_with_yes_buy(market_id: str, slug: str, yes_token: str,
                              shares: float, price: float) -> None:
    """Set up one bucket: market + filled BUY YES order."""
    upsert_market(Market(
        market_id=market_id, slug=slug, resolution_ts=1_700_000_000,
        yes_token_id=yes_token, no_token_id=f"no-{market_id}",
    ))
    insert_order(Order(
        order_id=f"order-{market_id}", client_order_id="c1",
        market_id=market_id, token_side="YES", side="BUY",
        price=price, size=shares, filled=shares, status="filled",
        placed_at=1_700_000_000 - 60, ended_at=1_700_000_000 - 60,
        strategy="weather_forecast",
    ))
    insert_fill(Fill(
        id=None, order_id=f"order-{market_id}", market_id=market_id,
        token_side="YES", side="BUY", price=price, size=shares,
        fill_ts=1_700_000_000 - 60, strategy="weather_forecast",
    ))


def _stub_event(slug: str, buckets: list[tuple[str, str, str]]) -> WeatherEvent:
    """`buckets` = list of (label, market_id, yes_token)."""
    return WeatherEvent(
        slug=slug, title="test event", city_key="paris",
        end_ts=1_700_000_000, resolution_ts=1_700_000_000, unit="celsius",
        buckets=[
            Bucket(
                label=lab, market_id=mid, yes_token_id=yt,
                no_token_id=f"no-{mid}",
                yes_bid=None, yes_ask=None, yes_mid=None,
                depth_yes_ask_usd=0.0, model_p=None,
            )
            for (lab, mid, yt) in buckets
        ],
    )


def test_winning_bucket_pays_after_fee(monkeypatch, tmp_path: Path) -> None:
    """100 YES @ $0.10 wins → gross $100, fee 5% × $90 winnings = $4.50, net $95.50."""
    _fresh_db(tmp_path)
    _seed_market_with_yes_buy("m1", "ev::win", "yes-m1", shares=100, price=0.10)

    event = _stub_event("ev", [("win", "m1", "yes-m1")])
    monkeypatch.setattr("polymarket_bot.polymarket.settle.gamma_outcome",
                        lambda e: {"win": 1.0})

    assert settle_resolved_event(event, strategy="weather_forecast",
                                 winning_fee_bps=500) is True
    settlements = list_settlements()
    assert len(settlements) == 1
    s = settlements[0]
    assert s.outcome == "WIN"
    assert math.isclose(s.cost, 10.0, abs_tol=1e-6)
    expected_payout = 100 - 0.05 * 90        # gross 100, fee 5% on $90 winnings
    assert math.isclose(s.payout, expected_payout, abs_tol=1e-6)
    assert math.isclose(s.pnl, expected_payout - 10.0, abs_tol=1e-6)


def test_losing_bucket_no_fee(monkeypatch, tmp_path: Path) -> None:
    """Loser pays 0; fee only applies to winnings, so PnL = -cost."""
    _fresh_db(tmp_path)
    _seed_market_with_yes_buy("m1", "ev::lose", "yes-m1", shares=50, price=0.20)

    event = _stub_event("ev", [("lose", "m1", "yes-m1")])
    monkeypatch.setattr("polymarket_bot.polymarket.settle.gamma_outcome",
                        lambda e: {"lose": 0.0})

    assert settle_resolved_event(event, strategy="weather_forecast",
                                 winning_fee_bps=500) is True
    s = list_settlements()[0]
    assert s.outcome == "LOSS"
    assert math.isclose(s.cost, 10.0, abs_tol=1e-6)
    assert math.isclose(s.payout, 0.0, abs_tol=1e-6)
    assert math.isclose(s.pnl, -10.0, abs_tol=1e-6)


def test_zero_fee_recovers_unfeed_payout(monkeypatch, tmp_path: Path) -> None:
    """winning_fee_bps=0 should give the pre-fee payout."""
    _fresh_db(tmp_path)
    _seed_market_with_yes_buy("m1", "ev::win", "yes-m1", shares=100, price=0.10)
    event = _stub_event("ev", [("win", "m1", "yes-m1")])
    monkeypatch.setattr("polymarket_bot.polymarket.settle.gamma_outcome",
                        lambda e: {"win": 1.0})
    assert settle_resolved_event(event, strategy="weather_forecast",
                                 winning_fee_bps=0) is True
    s = list_settlements()[0]
    assert math.isclose(s.payout, 100.0, abs_tol=1e-6)
    assert math.isclose(s.pnl, 90.0, abs_tol=1e-6)


def test_extreme_low_price_winner(monkeypatch, tmp_path: Path) -> None:
    """Tokyo-style: 156 YES @ $0.032 → gross $156, fee on $150.99, net $151.45."""
    _fresh_db(tmp_path)
    _seed_market_with_yes_buy("m1", "ev::win", "yes-m1", shares=156.25, price=0.032)
    event = _stub_event("ev", [("win", "m1", "yes-m1")])
    monkeypatch.setattr("polymarket_bot.polymarket.settle.gamma_outcome",
                        lambda e: {"win": 1.0})
    assert settle_resolved_event(event, strategy="weather_forecast",
                                 winning_fee_bps=500) is True
    s = list_settlements()[0]
    cost = 156.25 * 0.032
    winnings = 156.25 - cost
    fee = winnings * 0.05
    expected_payout = 156.25 - fee
    assert math.isclose(s.payout, expected_payout, rel_tol=1e-6)
    assert math.isclose(s.pnl, expected_payout - cost, rel_tol=1e-6)


def test_unsettled_event_returns_false(monkeypatch, tmp_path: Path) -> None:
    """If gamma hasn't resolved yet, settle_resolved_event returns False, no rows."""
    _fresh_db(tmp_path)
    _seed_market_with_yes_buy("m1", "ev::?", "yes-m1", shares=10, price=0.50)
    event = _stub_event("ev", [("win", "m1", "yes-m1")])
    monkeypatch.setattr("polymarket_bot.polymarket.settle.gamma_outcome",
                        lambda e: None)
    assert settle_resolved_event(event, strategy="weather_forecast") is False
    assert list_settlements() == []
