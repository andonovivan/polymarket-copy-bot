"""BucketArbitrageStrategy tests — structural arb only fires when the
sum of bucket YES asks is below 1 − threshold."""

from __future__ import annotations

from polymarket_bot.strategy.base import (
    BetState,
    Bucket,
    PlaceLimit,
    WeatherEvent,
)
from polymarket_bot.strategy.bucket_arbitrage import (
    ARBITRAGE_THRESHOLD,
    BucketArbitrageStrategy,
)


def _bucket(label: str, ask: float, depth: float = 100.0) -> Bucket:
    return Bucket(
        label=label,
        market_id=f"mkt-{label}",
        yes_token_id=f"yes-{label}",
        no_token_id=f"no-{label}",
        yes_bid=max(0.001, ask - 0.01), yes_ask=ask, yes_mid=ask - 0.005,
        depth_yes_ask_usd=depth, model_p=None,
    )


def _state(buckets, *, bankroll=100.0, seconds_to_resolution=3600,
           open_orders=None, held_yes=None, total_exposure=0.0,
           lockout_seconds=600, max_total_exposure_pct=1.0):
    event = WeatherEvent(
        slug="highest-temperature-in-paris-on-may-3-2026",
        title="Highest temp in Paris", city_key="paris",
        end_ts=1_700_000_000, resolution_ts=1_700_000_000,
        unit="celsius", buckets=buckets,
    )
    return BetState(
        event=event, bankroll=bankroll,
        seconds_to_resolution=seconds_to_resolution,
        open_orders_by_bucket=open_orders or {},
        held_yes_shares_by_bucket=held_yes or {},
        total_open_exposure_usd=total_exposure,
        edge_threshold=0.05, kelly_fraction=0.25,
        max_bet_pct=0.05, max_total_exposure_pct=max_total_exposure_pct,
        min_market_depth_usd=20.0,
        lockout_seconds=lockout_seconds,
    )


def _three_buckets(asks):
    return [_bucket(f"{i}", asks[i]) for i in range(len(asks))]


def test_no_arb_when_asks_sum_to_one():
    # 0.34 + 0.33 + 0.33 = 1.00 — no edge
    actions = BucketArbitrageStrategy().evaluate(_state(_three_buckets([0.34, 0.33, 0.33])))
    assert actions == []


def test_no_arb_when_below_threshold_but_above_min_gap():
    # Sum = 0.95 → 0.05 gap, less than ARBITRAGE_THRESHOLD (0.07)
    actions = BucketArbitrageStrategy().evaluate(_state(_three_buckets([0.32, 0.32, 0.31])))
    assert actions == []


def test_arb_fires_when_sum_well_below_one():
    # Sum = 0.85 → 0.15 gap, well above the 0.07 threshold
    actions = BucketArbitrageStrategy().evaluate(_state(_three_buckets([0.30, 0.30, 0.25])))
    assert len(actions) == 3
    # All three are BUY YES at the per-bucket ask
    for a, expected_ask in zip(actions, [0.30, 0.30, 0.25]):
        assert isinstance(a, PlaceLimit)
        assert a.side == "BUY"
        assert a.token_side == "YES"
        assert a.price == expected_ask
    # Identical share counts → uniform $1 payout regardless of which wins
    assert len({a.size for a in actions}) == 1


def test_arb_skips_when_any_bucket_has_no_quote():
    buckets = _three_buckets([0.30, 0.30, 0.25])
    buckets[1].yes_ask = None
    assert BucketArbitrageStrategy().evaluate(_state(buckets)) == []


def test_arb_skips_when_any_bucket_too_thin():
    buckets = _three_buckets([0.30, 0.30, 0.25])
    buckets[2].depth_yes_ask_usd = 5.0   # below the 20.0 min
    assert BucketArbitrageStrategy().evaluate(_state(buckets)) == []


def test_arb_skips_when_already_holding_a_bucket():
    actions = BucketArbitrageStrategy().evaluate(_state(
        _three_buckets([0.30, 0.30, 0.25]),
        held_yes={"1": 5.0},
    ))
    assert actions == []


def test_arb_locks_out_near_resolution():
    actions = BucketArbitrageStrategy().evaluate(_state(
        _three_buckets([0.30, 0.30, 0.25]),
        seconds_to_resolution=300,   # below lockout_seconds (600)
    ))
    assert actions == []


def test_arb_respects_global_exposure_cap():
    actions = BucketArbitrageStrategy().evaluate(_state(
        _three_buckets([0.30, 0.30, 0.25]),
        bankroll=100.0,
        total_exposure=99.5,
        max_total_exposure_pct=1.0,
    ))
    # Only $0.50 of headroom — well below MIN_ORDER_NOTIONAL × n_buckets
    assert actions == []


def test_arb_threshold_is_meaningful():
    # Sanity: threshold should leave headroom over the 5% Polymarket fee
    assert ARBITRAGE_THRESHOLD >= 0.05
