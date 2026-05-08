"""Tests for the bulk / aggregate repo helpers used by the dashboard."""

from __future__ import annotations

import time

import pytest

from polymarket_bot.persistence.repo import (
    Fill,
    Market,
    Order,
    Settlement,
    daily_pnl_summary,
    insert_fill,
    insert_order,
    insert_settlement,
    inventory_for_market,
    inventory_snapshot,
    markets_bulk,
    strategy_pnl_summary,
    upsert_market,
)


# DB lifecycle (Postgres testcontainer + per-test TRUNCATE) is handled in
# tests/conftest.py — these tests just need the autouse fixture to fire.


def _seed_market(mid: str, title: str = "Paris · May 5 · 14°C") -> None:
    upsert_market(Market(
        market_id=mid, slug=f"slug-{mid}",
        resolution_ts=1_700_000_000, yes_token_id=f"yes-{mid}",
        no_token_id=f"no-{mid}", title=title,
    ))


def _seed_fill(mid: str, *, token: str, side: str, size: float, price: float,
               strategy: str = "weather_forecast") -> None:
    # Include strategy in the order_id so a single market can carry fills
    # from multiple strategies in one test without colliding on orders_pkey.
    order_id = f"o-{mid}-{token}-{side}-{strategy}"
    insert_order(Order(
        order_id=order_id, client_order_id=order_id,
        market_id=mid, token_side=token, side=side,
        price=price, size=size, filled=size, status="filled",
        placed_at=int(time.time()) - 60, ended_at=int(time.time()) - 60,
        strategy=strategy,
    ))
    insert_fill(Fill(
        id=None, order_id=order_id,
        market_id=mid, token_side=token, side=side,
        price=price, size=size, fill_ts=int(time.time()),
        strategy=strategy,
    ))


def _seed_settlement(mid: str, *, pnl: float, settled_at: int,
                     strategy: str = "weather_forecast",
                     outcome: str | None = None) -> None:
    if outcome is None:
        outcome = "WIN" if pnl > 0 else "LOSS"
    insert_settlement(Settlement(
        market_id=mid, settled_at=settled_at, outcome=outcome,
        yes_shares=0, no_shares=0, avg_yes_cost=0, avg_no_cost=0,
        payout=max(0.0, pnl), cost=0.0, pnl=pnl, strategy=strategy,
    ))


# ---------------------------------------------------------------------------
# markets_bulk
# ---------------------------------------------------------------------------

def test_markets_bulk_returns_dict_keyed_by_market_id():
    _seed_market("m1", "Paris · May 5 · 14°C")
    _seed_market("m2", "Madrid · May 5 · 18°C")
    out = markets_bulk(["m1", "m2"])
    assert set(out.keys()) == {"m1", "m2"}
    assert out["m1"].title == "Paris · May 5 · 14°C"
    assert out["m2"].title == "Madrid · May 5 · 18°C"


def test_markets_bulk_handles_missing_ids():
    _seed_market("m1")
    out = markets_bulk(["m1", "m-nope"])
    assert "m1" in out
    assert "m-nope" not in out


def test_markets_bulk_empty_input_returns_empty_dict():
    assert markets_bulk([]) == {}


# ---------------------------------------------------------------------------
# inventory_snapshot
# ---------------------------------------------------------------------------

def test_inventory_snapshot_matches_per_market_helper():
    _seed_market("m1")
    _seed_market("m2")
    _seed_fill("m1", token="YES", side="BUY", size=10, price=0.30)
    _seed_fill("m1", token="YES", side="SELL", size=2, price=0.50)
    _seed_fill("m2", token="NO", side="BUY", size=5, price=0.40)

    snap = inventory_snapshot(["m1", "m2"])
    assert snap["m1"] == inventory_for_market("m1")
    assert snap["m2"] == inventory_for_market("m2")


def test_inventory_snapshot_empty_input():
    assert inventory_snapshot([]) == {}


def test_inventory_snapshot_omits_markets_with_no_fills():
    _seed_market("m1")
    _seed_market("m2")
    _seed_fill("m1", token="YES", side="BUY", size=10, price=0.30)
    snap = inventory_snapshot(["m1", "m2"])
    assert "m1" in snap
    # m2 has no fills → no row produced (caller defaults via .get)
    assert "m2" not in snap


# ---------------------------------------------------------------------------
# daily_pnl_summary
# ---------------------------------------------------------------------------

def test_daily_pnl_groups_settlements_by_day():
    _seed_market("m1"); _seed_market("m2"); _seed_market("m3")
    now = int(time.time())
    day = 86400
    # Two settlements yesterday — one win, one loss → net -3
    _seed_settlement("m1", pnl=10.0, settled_at=now - day)
    _seed_settlement("m2", pnl=-13.0, settled_at=now - day)
    # One settlement today — win
    _seed_settlement("m3", pnl=5.0, settled_at=now - 60)

    rows = daily_pnl_summary(days=7)
    assert len(rows) == 2

    by_day = {r["date"]: r for r in rows}
    today_row = max(by_day.values(), key=lambda r: r["date"])
    assert today_row["n_settlements"] == 1
    assert today_row["n_wins"] == 1
    assert today_row["pnl"] == pytest.approx(5.0)

    yesterday_row = min(by_day.values(), key=lambda r: r["date"])
    assert yesterday_row["n_settlements"] == 2
    assert yesterday_row["n_wins"] == 1
    assert yesterday_row["pnl"] == pytest.approx(-3.0)


def test_daily_pnl_respects_lookback_window():
    _seed_market("m1"); _seed_market("m2")
    now = int(time.time())
    _seed_settlement("m1", pnl=10.0, settled_at=now - 5 * 86400)
    _seed_settlement("m2", pnl=20.0, settled_at=now - 60 * 86400)
    rows = daily_pnl_summary(days=10)   # 60-day-old entry excluded
    assert len(rows) == 1
    assert rows[0]["pnl"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# strategy_pnl_summary
# ---------------------------------------------------------------------------

def test_strategy_pnl_groups_by_strategy():
    _seed_market("m1"); _seed_market("m2"); _seed_market("m3")
    now = int(time.time())
    _seed_settlement("m1", pnl=10.0, settled_at=now - 100, strategy="weather_forecast")
    _seed_settlement("m2", pnl=-5.0, settled_at=now - 200, strategy="weather_forecast")
    _seed_settlement("m3", pnl=2.0, settled_at=now - 300, strategy="bucket_arbitrage")

    rows = strategy_pnl_summary(days=30)
    by_name = {r["strategy"]: r for r in rows}
    assert by_name["weather_forecast"]["n_settlements"] == 2
    assert by_name["weather_forecast"]["n_wins"] == 1
    assert by_name["weather_forecast"]["pnl"] == pytest.approx(5.0)
    assert by_name["bucket_arbitrage"]["n_settlements"] == 1
    assert by_name["bucket_arbitrage"]["pnl"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Phase A.4 — server-side strategy filter on the dashboard aggregates.
# ---------------------------------------------------------------------------

def test_daily_pnl_summary_filters_by_strategy():
    """Passing `strategies=[…]` must scope the rollup to those strategies."""
    from polymarket_bot.persistence.repo import daily_pnl_summary
    _seed_market("m1"); _seed_market("m2"); _seed_market("m3")
    now = int(time.time())
    _seed_settlement("m1", pnl=10.0, settled_at=now - 60, strategy="weather_forecast")
    _seed_settlement("m2", pnl=-5.0, settled_at=now - 60, strategy="weather_forecast")
    _seed_settlement("m3", pnl=20.0, settled_at=now - 60, strategy="bucket_arbitrage")

    weather_only = daily_pnl_summary(days=7, strategies=["weather_forecast"])
    assert len(weather_only) == 1
    assert weather_only[0]["pnl"] == pytest.approx(5.0)
    assert weather_only[0]["n_settlements"] == 2

    arb_only = daily_pnl_summary(days=7, strategies=["bucket_arbitrage"])
    assert len(arb_only) == 1
    assert arb_only[0]["pnl"] == pytest.approx(20.0)


def test_settlement_stats_filters_by_strategy():
    """settlement_stats(strategies=[…]) — used by the dashboard's
    today-card so per-strategy filter shows accurate counts."""
    from polymarket_bot.persistence.repo import settlement_stats
    _seed_market("m1"); _seed_market("m2"); _seed_market("m3")
    now = int(time.time())
    _seed_settlement("m1", pnl=10.0, settled_at=now - 60, strategy="weather_forecast")
    _seed_settlement("m2", pnl=-5.0, settled_at=now - 60, strategy="weather_forecast")
    _seed_settlement("m3", pnl=20.0, settled_at=now - 60, strategy="bucket_arbitrage")

    s = settlement_stats(strategies=["weather_forecast"])
    assert s["settlements"] == 2
    assert s["pnl"] == pytest.approx(5.0)
    assert s["wins"] == 1

    s_all = settlement_stats()
    assert s_all["settlements"] == 3
    assert s_all["pnl"] == pytest.approx(25.0)


def test_strategy_pnl_orders_descending_by_pnl():
    _seed_market("m1"); _seed_market("m2")
    now = int(time.time())
    _seed_settlement("m1", pnl=-3.0, settled_at=now - 100, strategy="loser")
    _seed_settlement("m2", pnl=10.0, settled_at=now - 200, strategy="winner")
    rows = strategy_pnl_summary()
    assert [r["strategy"] for r in rows] == ["winner", "loser"]


# ---------------------------------------------------------------------------
# Per-strategy chip filter — backs the dashboard's "Show:" chips so the
# inventory / open-orders / totals cards visibly respond to selection.
# ---------------------------------------------------------------------------

def test_all_open_orders_filters_by_strategies():
    from polymarket_bot.persistence.repo import all_open_orders
    _seed_market("m1"); _seed_market("m2")
    insert_order(Order(
        order_id="oA", client_order_id="oA", market_id="m1",
        token_side="YES", side="BUY", price=0.3, size=10, filled=0,
        status="open", placed_at=int(time.time()), ended_at=None,
        strategy="weather_forecast",
    ))
    insert_order(Order(
        order_id="oB", client_order_id="oB", market_id="m2",
        token_side="YES", side="BUY", price=0.4, size=5, filled=0,
        status="open", placed_at=int(time.time()), ended_at=None,
        strategy="bucket_arbitrage",
    ))
    all_orders = all_open_orders()
    assert {o.order_id for o in all_orders} == {"oA", "oB"}

    weather_only = all_open_orders(strategies=["weather_forecast"])
    assert [o.order_id for o in weather_only] == ["oA"]

    arb_only = all_open_orders(strategies=["bucket_arbitrage"])
    assert [o.order_id for o in arb_only] == ["oB"]

    union = all_open_orders(strategies=["weather_forecast", "bucket_arbitrage"])
    assert {o.order_id for o in union} == {"oA", "oB"}


def test_markets_with_unsettled_fills_filters_by_strategies():
    from polymarket_bot.persistence.repo import markets_with_unsettled_fills
    _seed_market("m1"); _seed_market("m2"); _seed_market("m3")
    _seed_fill("m1", token="YES", side="BUY", size=10, price=0.3,
               strategy="weather_forecast")
    _seed_fill("m2", token="YES", side="BUY", size=5, price=0.4,
               strategy="bucket_arbitrage")
    # m3 has fills from BOTH strategies — should appear in either filter.
    _seed_fill("m3", token="YES", side="BUY", size=2, price=0.5,
               strategy="weather_forecast")
    _seed_fill("m3", token="NO", side="BUY", size=1, price=0.6,
               strategy="bucket_arbitrage")

    assert set(markets_with_unsettled_fills()) == {"m1", "m2", "m3"}
    assert set(markets_with_unsettled_fills(strategies=["weather_forecast"])) == {"m1", "m3"}
    assert set(markets_with_unsettled_fills(strategies=["bucket_arbitrage"])) == {"m2", "m3"}


def test_inventory_snapshot_for_strategies_sums_across_selected():
    from polymarket_bot.persistence.repo import inventory_snapshot_for_strategies
    _seed_market("m1")
    # weather buys 10 @ 0.3, bucket-arb buys 4 @ 0.5 — both YES on m1.
    _seed_fill("m1", token="YES", side="BUY", size=10, price=0.3,
               strategy="weather_forecast")
    _seed_fill("m1", token="YES", side="BUY", size=4, price=0.5,
               strategy="bucket_arbitrage")

    weather_only = inventory_snapshot_for_strategies(["weather_forecast"], ["m1"])
    assert weather_only["m1"][0] == 10  # yes_shares
    assert weather_only["m1"][2] == pytest.approx(0.3)  # avg_yes

    union = inventory_snapshot_for_strategies(
        ["weather_forecast", "bucket_arbitrage"], ["m1"],
    )
    assert union["m1"][0] == 14
    # Volume-weighted avg: (10*0.3 + 4*0.5) / 14 ≈ 0.357
    assert union["m1"][2] == pytest.approx((10 * 0.3 + 4 * 0.5) / 14)


def test_inventory_snapshot_for_strategies_empty_inputs():
    from polymarket_bot.persistence.repo import inventory_snapshot_for_strategies
    assert inventory_snapshot_for_strategies([], ["m1"]) == {}
    assert inventory_snapshot_for_strategies(["weather_forecast"], []) == {}
