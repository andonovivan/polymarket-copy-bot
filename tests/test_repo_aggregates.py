"""Tests for the bulk / aggregate repo helpers used by the dashboard."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import polymarket_bot.persistence.schema as schema
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


@pytest.fixture
def fresh_db(tmp_path):
    schema._conn = None
    schema.init_db(tmp_path / "test.db")
    yield
    schema._conn = None


def _seed_market(mid: str, title: str = "Paris · May 5 · 14°C") -> None:
    upsert_market(Market(
        market_id=mid, slug=f"slug-{mid}",
        resolution_ts=1_700_000_000, yes_token_id=f"yes-{mid}",
        no_token_id=f"no-{mid}", title=title,
    ))


def _seed_fill(mid: str, *, token: str, side: str, size: float, price: float,
               strategy: str = "weather_forecast") -> None:
    order_id = f"o-{mid}-{token}-{side}"
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

def test_markets_bulk_returns_dict_keyed_by_market_id(fresh_db):
    _seed_market("m1", "Paris · May 5 · 14°C")
    _seed_market("m2", "Madrid · May 5 · 18°C")
    out = markets_bulk(["m1", "m2"])
    assert set(out.keys()) == {"m1", "m2"}
    assert out["m1"].title == "Paris · May 5 · 14°C"
    assert out["m2"].title == "Madrid · May 5 · 18°C"


def test_markets_bulk_handles_missing_ids(fresh_db):
    _seed_market("m1")
    out = markets_bulk(["m1", "m-nope"])
    assert "m1" in out
    assert "m-nope" not in out


def test_markets_bulk_empty_input_returns_empty_dict(fresh_db):
    assert markets_bulk([]) == {}


# ---------------------------------------------------------------------------
# inventory_snapshot
# ---------------------------------------------------------------------------

def test_inventory_snapshot_matches_per_market_helper(fresh_db):
    _seed_market("m1")
    _seed_market("m2")
    _seed_fill("m1", token="YES", side="BUY", size=10, price=0.30)
    _seed_fill("m1", token="YES", side="SELL", size=2, price=0.50)
    _seed_fill("m2", token="NO", side="BUY", size=5, price=0.40)

    snap = inventory_snapshot(["m1", "m2"])
    assert snap["m1"] == inventory_for_market("m1")
    assert snap["m2"] == inventory_for_market("m2")


def test_inventory_snapshot_empty_input(fresh_db):
    assert inventory_snapshot([]) == {}


def test_inventory_snapshot_omits_markets_with_no_fills(fresh_db):
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

def test_daily_pnl_groups_settlements_by_day(fresh_db):
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


def test_daily_pnl_respects_lookback_window(fresh_db):
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

def test_strategy_pnl_groups_by_strategy(fresh_db):
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


def test_strategy_pnl_orders_descending_by_pnl(fresh_db):
    _seed_market("m1"); _seed_market("m2")
    now = int(time.time())
    _seed_settlement("m1", pnl=-3.0, settled_at=now - 100, strategy="loser")
    _seed_settlement("m2", pnl=10.0, settled_at=now - 200, strategy="winner")
    rows = strategy_pnl_summary()
    assert [r["strategy"] for r in rows] == ["winner", "loser"]
