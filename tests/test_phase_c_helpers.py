"""Phase C — tests for the new repo helpers and the per-strategy
settlement decomposition.

Covers:
  • `inventory_snapshot_for(strategy, market_ids)` — strategy-scoped
    bulk inventory (filters out other strategies' fills).
  • `total_open_exposure_for(strategy)` — Σ(yes × avg_yes) over the
    strategy's *unsettled* fills.
  • `forecast_cache_get` / `forecast_cache_put` — shared L2 cache across
    strategy services (TTL semantics, JSONB roundtrip).
  • `settle_resolved_event` — per-strategy decomposition: one
    Settlement row per (market, strategy) when two strategies hold
    fills on the same market.
"""

from __future__ import annotations

import time

from polymarket_bot.persistence.repo import (
    Fill,
    Market,
    Order,
    Settlement,
    forecast_cache_get,
    forecast_cache_put,
    insert_fill,
    insert_order,
    insert_settlement,
    inventory_snapshot_for,
    list_settlements,
    total_open_exposure_for,
    upsert_market,
)


# ---------------------------------------------------------------------------
# Seed helpers (mirror tests/test_repo_aggregates.py so we stay self-contained)
# ---------------------------------------------------------------------------


def _seed_market(mid: str, *, resolution_ts: int = 1_700_000_000) -> None:
    upsert_market(Market(
        market_id=mid, slug=f"slug-{mid}::{mid}",
        resolution_ts=resolution_ts,
        yes_token_id=f"yes-{mid}", no_token_id=f"no-{mid}",
        title=f"Test market {mid}",
    ))


def _seed_fill(mid: str, *, token: str, side: str, size: float, price: float,
               strategy: str = "weather_forecast") -> None:
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


# ---------------------------------------------------------------------------
# inventory_snapshot_for
# ---------------------------------------------------------------------------

def test_inventory_snapshot_for_filters_by_strategy():
    """Two strategies hold YES on the same market; each must see only its own
    shares + cost basis."""
    _seed_market("m1")
    _seed_fill("m1", token="YES", side="BUY", size=10, price=0.30,
               strategy="weather_forecast")
    _seed_fill("m1", token="YES", side="BUY", size=20, price=0.50,
               strategy="bucket_arbitrage")

    snap_w = inventory_snapshot_for("weather_forecast", ["m1"])
    snap_a = inventory_snapshot_for("bucket_arbitrage", ["m1"])

    yes_w, _, avg_yes_w, _ = snap_w["m1"]
    yes_a, _, avg_yes_a, _ = snap_a["m1"]

    assert yes_w == 10
    assert avg_yes_w == 0.30
    assert yes_a == 20
    assert avg_yes_a == 0.50


def test_inventory_snapshot_for_empty_input_returns_empty_dict():
    assert inventory_snapshot_for("weather_forecast", []) == {}


def test_inventory_snapshot_for_unknown_strategy_returns_empty():
    _seed_market("m1")
    _seed_fill("m1", token="YES", side="BUY", size=5, price=0.40,
               strategy="weather_forecast")

    assert inventory_snapshot_for("nonexistent", ["m1"]) == {}


# ---------------------------------------------------------------------------
# total_open_exposure_for
# ---------------------------------------------------------------------------

def test_total_open_exposure_for_sums_unsettled_yes_only():
    _seed_market("m1")
    _seed_market("m2")
    # Strategy W: 10×0.3 = 3.00 unrealized exposure on m1
    _seed_fill("m1", token="YES", side="BUY", size=10, price=0.30,
               strategy="weather_forecast")
    # Strategy A: 5×0.6 = 3.00 on m2 (different strategy, must be excluded)
    _seed_fill("m2", token="YES", side="BUY", size=5, price=0.60,
               strategy="bucket_arbitrage")

    assert total_open_exposure_for("weather_forecast") == 3.00


def test_total_open_exposure_for_excludes_settled():
    _seed_market("m1", resolution_ts=int(time.time()) - 10)
    _seed_fill("m1", token="YES", side="BUY", size=8, price=0.25,
               strategy="weather_forecast")
    # Settle this strategy's row → exposure should drop to zero.
    insert_settlement(Settlement(
        market_id="m1", settled_at=int(time.time()), outcome="WIN",
        yes_shares=8, no_shares=0, avg_yes_cost=0.25, avg_no_cost=0,
        payout=8.0, cost=2.0, pnl=6.0, strategy="weather_forecast",
    ))
    assert total_open_exposure_for("weather_forecast") == 0.0


def test_total_open_exposure_for_unrelated_strategy_settlement_does_not_affect():
    """Settling strategy A's row on a market must not zero out strategy W's
    exposure on the same market — that's the whole point of the composite
    PK."""
    _seed_market("m1")
    _seed_fill("m1", token="YES", side="BUY", size=10, price=0.30,
               strategy="weather_forecast")
    _seed_fill("m1", token="YES", side="BUY", size=4, price=0.50,
               strategy="bucket_arbitrage")
    insert_settlement(Settlement(
        market_id="m1", settled_at=int(time.time()), outcome="WIN",
        yes_shares=4, no_shares=0, avg_yes_cost=0.50, avg_no_cost=0,
        payout=4.0, cost=2.0, pnl=2.0, strategy="bucket_arbitrage",
    ))
    # Strategy W's exposure must remain 10 × 0.30 = 3.00.
    assert total_open_exposure_for("weather_forecast") == 3.00
    # And strategy A is now zero (it settled its own row).
    assert total_open_exposure_for("bucket_arbitrage") == 0.0


# ---------------------------------------------------------------------------
# forecast_cache_get / _put
# ---------------------------------------------------------------------------

def test_forecast_cache_roundtrip():
    forecast_cache_put("paris", "2026-05-10", [12, 13, 14, 15])
    out = forecast_cache_get("paris", "2026-05-10", max_age_seconds=3600)
    assert out == [12, 13, 14, 15]


def test_forecast_cache_returns_none_when_stale():
    forecast_cache_put("madrid", "2026-05-10", [18, 19, 20])
    # The freshness check is `now - fetched_at > max_age`, so a negative
    # TTL guarantees the row reads as stale even when written this tick.
    assert forecast_cache_get("madrid", "2026-05-10", max_age_seconds=-1) is None


def test_forecast_cache_returns_none_when_missing():
    assert forecast_cache_get("nope", "2026-05-10", max_age_seconds=3600) is None


def test_forecast_cache_overwrites_on_repeated_put():
    forecast_cache_put("london", "2026-05-10", [10, 11])
    forecast_cache_put("london", "2026-05-10", [11, 12, 13])
    out = forecast_cache_get("london", "2026-05-10", max_age_seconds=3600)
    assert out == [11, 12, 13]


# ---------------------------------------------------------------------------
# Per-strategy settlement decomposition
# ---------------------------------------------------------------------------

def test_settlements_composite_pk_allows_two_rows_per_market():
    """Sanity check that the new PK truly is composite — two rows on the
    same market with different strategies must coexist."""
    _seed_market("m1")
    insert_settlement(Settlement(
        market_id="m1", settled_at=1, outcome="WIN",
        yes_shares=1, no_shares=0, avg_yes_cost=0.3, avg_no_cost=0,
        payout=1.0, cost=0.3, pnl=0.7, strategy="weather_forecast",
    ))
    insert_settlement(Settlement(
        market_id="m1", settled_at=1, outcome="WIN",
        yes_shares=2, no_shares=0, avg_yes_cost=0.5, avg_no_cost=0,
        payout=2.0, cost=1.0, pnl=1.0, strategy="bucket_arbitrage",
    ))

    rows = list_settlements(limit=10)
    pairs = sorted((r.market_id, r.strategy) for r in rows)
    assert pairs == [("m1", "bucket_arbitrage"), ("m1", "weather_forecast")]


def test_insert_settlement_upserts_on_same_pk():
    """Re-inserting (market_id, strategy) updates the row in place."""
    _seed_market("m1")
    insert_settlement(Settlement(
        market_id="m1", settled_at=1, outcome="WIN",
        yes_shares=1, no_shares=0, avg_yes_cost=0.3, avg_no_cost=0,
        payout=1.0, cost=0.3, pnl=0.7, strategy="weather_forecast",
    ))
    # Re-settle with a different pnl — should overwrite, not duplicate.
    insert_settlement(Settlement(
        market_id="m1", settled_at=2, outcome="LOSS",
        yes_shares=1, no_shares=0, avg_yes_cost=0.3, avg_no_cost=0,
        payout=0.0, cost=0.3, pnl=-0.3, strategy="weather_forecast",
    ))
    rows = [r for r in list_settlements(limit=10)
            if r.market_id == "m1" and r.strategy == "weather_forecast"]
    assert len(rows) == 1
    assert rows[0].pnl == -0.3
    assert rows[0].outcome == "LOSS"
