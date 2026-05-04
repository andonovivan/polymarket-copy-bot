"""WeatherForecastStrategy logic — edge filter, sizing, lockout."""

from __future__ import annotations

from polymarket_bot.strategy.base import (
    BetState,
    Bucket,
    PlaceLimit,
    WeatherEvent,
)
from polymarket_bot.strategy.weather_forecast import WeatherForecastStrategy


def _bucket(label: str, ask: float, model_p: float, depth: float = 100.0) -> Bucket:
    return Bucket(
        label=label,
        market_id=f"mkt-{label}",
        yes_token_id=f"yes-{label}",
        no_token_id=f"no-{label}",
        yes_bid=ask - 0.01, yes_ask=ask, yes_mid=ask - 0.005,
        depth_yes_ask_usd=depth, model_p=model_p,
    )


def _state(buckets: list[Bucket], *, bankroll: float = 100.0,
           seconds_to_resolution: int = 3600,
           open_orders: dict | None = None,
           held_yes: dict | None = None,
           total_exposure: float = 0.0,
           edge_threshold: float = 0.05,
           lockout_seconds: int = 600,
           max_total_exposure_pct: float = 1.0) -> BetState:
    event = WeatherEvent(
        slug="highest-temperature-in-paris-on-may-3-2026",
        title="Highest temperature in Paris", city_key="paris",
        end_ts=1_700_000_000, resolution_ts=1_700_000_000,
        unit="celsius", buckets=buckets,
    )
    return BetState(
        event=event, bankroll=bankroll, seconds_to_resolution=seconds_to_resolution,
        open_orders_by_bucket=open_orders or {},
        held_yes_shares_by_bucket=held_yes or {},
        total_open_exposure_usd=total_exposure,
        edge_threshold=edge_threshold, kelly_fraction=0.25,
        max_bet_pct=0.05, max_total_exposure_pct=max_total_exposure_pct,
        min_market_depth_usd=20.0,
        lockout_seconds=lockout_seconds,
    )


def test_no_bet_when_edge_below_threshold():
    # market_p = 0.40, model_p = 0.43 → edge 0.03 < 0.05
    actions = WeatherForecastStrategy().evaluate(_state([_bucket("19°C", 0.40, 0.43)]))
    assert actions == []


def test_bet_when_edge_above_threshold():
    actions = WeatherForecastStrategy().evaluate(_state([_bucket("19°C", 0.30, 0.45)]))
    assert len(actions) == 1
    a = actions[0]
    assert isinstance(a, PlaceLimit)
    assert a.token_side == "YES" and a.side == "BUY"
    assert a.price == 0.30
    assert a.market_id == "mkt-19°C"


def test_no_bet_when_negative_edge():
    # market thinks 80%, model thinks 50% — would lose money buying YES
    actions = WeatherForecastStrategy().evaluate(_state([_bucket("19°C", 0.80, 0.50)]))
    assert actions == []


def test_lockout_pulls_no_bets():
    # 5 minutes to resolution, lockout = 600s → skip
    actions = WeatherForecastStrategy().evaluate(
        _state([_bucket("19°C", 0.30, 0.50)], seconds_to_resolution=300, lockout_seconds=600)
    )
    assert actions == []


def test_skip_thin_book():
    actions = WeatherForecastStrategy().evaluate(
        _state([_bucket("19°C", 0.30, 0.50, depth=5.0)])
    )
    assert actions == []


def test_no_double_bet_on_same_bucket():
    # Existing open order on this bucket → strategy should skip.
    from polymarket_bot.strategy.base import OpenOrder
    existing = OpenOrder(
        order_id="A", client_order_id="c1", market_id="mkt-19°C",
        token_side="YES", side="BUY", price=0.30, size=10,
    )
    actions = WeatherForecastStrategy().evaluate(
        _state([_bucket("19°C", 0.30, 0.50)], open_orders={"19°C": [existing]})
    )
    assert actions == []


def test_kelly_caps_at_max_bet_pct():
    # Massive edge → full Kelly would say "go big"; max_bet_pct caps at 5% of $100 = $5
    actions = WeatherForecastStrategy().evaluate(
        _state([_bucket("19°C", 0.10, 0.90)])
    )
    a = actions[0]
    notional = a.price * a.size
    assert notional <= 5.0 + 1e-6


def test_only_bets_on_edged_buckets_among_many():
    buckets = [
        _bucket("18°C", 0.05, 0.05),    # no edge
        _bucket("19°C", 0.30, 0.45),    # +0.15 edge — bet
        _bucket("20°C", 0.50, 0.30),    # negative edge — skip
        _bucket("21°C", 0.10, 0.20),    # +0.10 edge — bet
    ]
    actions = WeatherForecastStrategy().evaluate(_state(buckets))
    bet_buckets = {a.market_id for a in actions if isinstance(a, PlaceLimit)}
    assert bet_buckets == {"mkt-19°C", "mkt-21°C"}


def test_zero_bankroll_no_bets():
    actions = WeatherForecastStrategy().evaluate(
        _state([_bucket("19°C", 0.30, 0.50)], bankroll=0.0)
    )
    assert actions == []


def test_no_bet_when_already_holding_yes():
    """Don't add to a position we already filled in a previous tick."""
    actions = WeatherForecastStrategy().evaluate(
        _state([_bucket("19°C", 0.30, 0.50)], held_yes={"19°C": 100.0})
    )
    assert actions == []


def test_total_exposure_cap_blocks_further_bets():
    """When existing exposure is already at the cap, no new bets."""
    actions = WeatherForecastStrategy().evaluate(
        _state([_bucket("19°C", 0.30, 0.50)],
               total_exposure=30.0, max_total_exposure_pct=0.30)
    )
    assert actions == []  # 100 * 0.30 = 30, fully consumed


def test_partial_exposure_caps_bet_size():
    """Stake gets clipped to remaining headroom under the global cap."""
    # bankroll=100, cap=30%, already used $28 → only $2 headroom
    actions = WeatherForecastStrategy().evaluate(
        _state([_bucket("19°C", 0.30, 0.50)],
               total_exposure=28.0, max_total_exposure_pct=0.30)
    )
    if actions:
        a = actions[0]
        notional = a.price * a.size
        assert notional <= 2.0 + 1e-6
    # Or empty if the headroom was below MIN_ORDER_NOTIONAL.
