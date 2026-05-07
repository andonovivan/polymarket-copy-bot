"""WeatherForecastStrategy logic — edge filter, sizing, lockout."""

from __future__ import annotations

from polymarket_bot.strategy.base import (
    BetState,
    Bucket,
    PlaceLimit,
    WeatherEvent,
)
from polymarket_bot.strategy.weather_forecast import WeatherForecastStrategy


def _bucket(label: str, ask: float, model_p: float, depth: float = 100.0,
            bid: float | None = None) -> Bucket:
    yes_bid = bid if bid is not None else max(0.001, ask - 0.01)
    return Bucket(
        label=label,
        market_id=f"mkt-{label}",
        yes_token_id=f"yes-{label}",
        no_token_id=f"no-{label}",
        yes_bid=yes_bid, yes_ask=ask, yes_mid=(yes_bid + ask) / 2,
        depth_yes_ask_usd=depth, model_p=model_p,
    )


def _state(buckets: list[Bucket], *, bankroll: float = 100.0,
           seconds_to_resolution: int = 3600,
           open_orders: dict | None = None,
           held_yes: dict | None = None,
           held_no: dict | None = None,
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
        held_no_shares_by_bucket=held_no or {},
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


# ---------------------------------------------------------------------------
# Profit-taking SELL behavior (#3).
# ---------------------------------------------------------------------------


def test_profit_take_sells_when_bid_well_above_threshold():
    # Bought at 0.10, model_p=0.20 → hold-EV = 0.20×0.95 = 0.19. Threshold
    # = 0.19 + 0.10 = 0.29. Bid 0.40 > 0.29 → SELL.
    bucket = _bucket("16°C", ask=0.41, model_p=0.20, bid=0.40)
    actions = WeatherForecastStrategy().evaluate(_state(
        [bucket], held_yes={"16°C": 50.0},
    ))
    assert len(actions) == 1
    a = actions[0]
    assert isinstance(a, PlaceLimit)
    assert a.side == "SELL"
    assert a.token_side == "YES"
    assert a.price == 0.40
    assert a.size == 50.0


def test_profit_take_holds_when_bid_below_threshold():
    # model_p=0.50 → threshold = 0.50×0.95 + 0.10 = 0.575. Bid 0.55 < 0.575 → HOLD.
    bucket = _bucket("16°C", ask=0.56, model_p=0.50, bid=0.55)
    actions = WeatherForecastStrategy().evaluate(_state(
        [bucket], held_yes={"16°C": 50.0},
    ))
    assert actions == []


def test_profit_take_skips_when_already_open_sell():
    from polymarket_bot.strategy.base import OpenOrder
    bucket = _bucket("16°C", ask=0.41, model_p=0.20, bid=0.40)
    open_order = OpenOrder(
        order_id="x", client_order_id="y", market_id="m",
        token_side="YES", side="SELL", price=0.40, size=50.0,
    )
    actions = WeatherForecastStrategy().evaluate(_state(
        [bucket],
        held_yes={"16°C": 50.0},
        open_orders={"16°C": [open_order]},
    ))
    assert actions == []


def test_profit_take_skips_when_below_min_notional():
    # 1 share × 0.40 = $0.40 < $1 minimum. No SELL.
    bucket = _bucket("16°C", ask=0.41, model_p=0.20, bid=0.40)
    actions = WeatherForecastStrategy().evaluate(_state(
        [bucket], held_yes={"16°C": 1.0},
    ))
    assert actions == []


def test_profit_take_does_not_buy_more_when_holding():
    # Even if there's edge, we don't double up while we already hold.
    bucket = _bucket("16°C", ask=0.10, model_p=0.50, bid=0.05)
    actions = WeatherForecastStrategy().evaluate(_state(
        [bucket], held_yes={"16°C": 50.0},
    ))
    # Held + bid=0.05 below threshold → no BUY, no SELL.
    assert actions == []


# ---------------------------------------------------------------------------
# NO-side trades (#2).
# ---------------------------------------------------------------------------


def _bucket_with_no(label: str, yes_ask: float, model_p: float,
                    no_ask: float, no_depth: float = 100.0) -> Bucket:
    """Bucket with both YES and NO quotes populated (NO opt-in path)."""
    b = _bucket(label, yes_ask, model_p)
    b.no_bid = max(0.001, no_ask - 0.01)
    b.no_ask = no_ask
    b.no_mid = (b.no_bid + no_ask) / 2
    b.depth_no_ask_usd = no_depth
    return b


def test_confidence_dampens_kelly_when_member_std_is_high():
    # Same bucket, same edge — only the event-level std differs. With std=0
    # the bet should be larger than with std=4. The 1/(1+std) multiplier
    # pulls the high-std stake to 1/5 of the low-std stake.
    # Parameters chosen so Kelly stake stays below max_bet_pct on both sides
    # (model_p=0.40, ask=0.30 → f_full≈0.143 → 0.25*0.143=3.6% <5% cap).
    bucket_low = _bucket("19°C", ask=0.30, model_p=0.40, depth=1000.0)
    state_low = _state([bucket_low], edge_threshold=0.05, bankroll=500.0)
    state_low.event.member_std = 0.0
    actions_low = WeatherForecastStrategy().evaluate(state_low)

    bucket_high = _bucket("19°C", ask=0.30, model_p=0.40, depth=1000.0)
    state_high = _state([bucket_high], edge_threshold=0.05, bankroll=500.0)
    state_high.event.member_std = 4.0
    actions_high = WeatherForecastStrategy().evaluate(state_high)

    assert len(actions_low) == 1
    assert len(actions_high) == 1
    # Stake = price * size; at the same price the share count tracks stake.
    assert isinstance(actions_low[0], PlaceLimit)
    assert isinstance(actions_high[0], PlaceLimit)
    # 1/(1+0) vs 1/(1+4) = 1.0 vs 0.2. Allow ~10% slack for floor() rounding.
    ratio = actions_high[0].size / actions_low[0].size
    assert 0.18 < ratio < 0.22


def test_warmup_gate_blocks_buys_but_not_profit_takes(monkeypatch):
    # When the city is not warmed up, BUYs are skipped but profit-take SELLs
    # on existing held positions still flow through.
    monkeypatch.setattr(
        "polymarket_bot.strategy.calibration.is_city_warmed_up",
        lambda city, n: False,
    )
    # Bucket: edge 0.30 (model_p 0.60 vs ask 0.30) — would normally fire BUY.
    # Also held 100 shares with bid rich enough to trigger profit-take.
    held_bucket = _bucket("19°C", ask=0.30, model_p=0.60, bid=0.80, depth=1000.0)
    held_bucket.yes_bid = 0.80
    held_bucket.yes_mid = 0.55
    state = _state([held_bucket], held_yes={"19°C": 100.0})
    state.warmup_min_obs = 10
    actions = WeatherForecastStrategy().evaluate(state)
    sells = [a for a in actions if isinstance(a, PlaceLimit) and a.side == "SELL"]
    buys = [a for a in actions if isinstance(a, PlaceLimit) and a.side == "BUY"]
    assert buys == []
    assert len(sells) == 1


def test_warmup_gate_off_when_min_obs_zero(monkeypatch):
    # warmup_min_obs=0 → gate disabled, BUYs proceed regardless.
    called = []
    monkeypatch.setattr(
        "polymarket_bot.strategy.calibration.is_city_warmed_up",
        lambda city, n: called.append((city, n)) or False,
    )
    bucket = _bucket("19°C", ask=0.30, model_p=0.50, depth=1000.0)
    state = _state([bucket])
    state.warmup_min_obs = 0
    actions = WeatherForecastStrategy().evaluate(state)
    # gate skipped → is_city_warmed_up never called
    assert called == []
    assert len(actions) == 1


def test_no_side_buy_when_bucket_overpriced():
    # model_p=0.20 → model_no_p=0.80. Bucket priced rich on YES (ask 0.50)
    # → NO ask should be cheap (~0.50). edge_no = 0.80 − 0.50 = 0.30, fires.
    bucket = _bucket_with_no("16°C", yes_ask=0.50, model_p=0.20, no_ask=0.50)
    actions = WeatherForecastStrategy().evaluate(_state([bucket]))
    # Should fire NO BUY only (no YES BUY since 0.20 < 0.50).
    no_actions = [a for a in actions if isinstance(a, PlaceLimit) and a.token_side == "NO"]
    yes_actions = [a for a in actions if isinstance(a, PlaceLimit) and a.token_side == "YES"]
    assert len(no_actions) == 1
    assert no_actions[0].side == "BUY"
    assert no_actions[0].price == 0.50
    assert yes_actions == []


def test_no_side_skipped_when_no_quotes_unset():
    # model_p=0.20 means NO is theoretically attractive, but with no_ask=None
    # the strategy can't act.
    bucket = _bucket("16°C", ask=0.50, model_p=0.20)
    # bucket.no_ask is None by default
    actions = WeatherForecastStrategy().evaluate(_state([bucket]))
    assert actions == []


def test_no_side_below_min_depth_skipped():
    bucket = _bucket_with_no("16°C", yes_ask=0.50, model_p=0.20,
                              no_ask=0.50, no_depth=5.0)
    actions = WeatherForecastStrategy().evaluate(_state([bucket]))
    assert actions == []


def test_no_side_no_edge_skipped():
    # model_p=0.50 → model_no_p=0.50. NO ask 0.49 → edge 0.01 < threshold 0.05.
    bucket = _bucket_with_no("16°C", yes_ask=0.51, model_p=0.50, no_ask=0.49)
    actions = WeatherForecastStrategy().evaluate(_state([bucket]))
    assert actions == []


def test_yes_and_no_edge_are_mutually_exclusive_in_tight_market():
    # YES edge: model_p=0.45 > yes_ask=0.30 → edge 0.15 fires YES
    # NO edge:  model_no_p=0.55 vs no_ask=0.69 → edge -0.14 (no fire)
    # Tight markets can have at most one side with positive edge.
    bucket = _bucket_with_no("16°C", yes_ask=0.30, model_p=0.45, no_ask=0.69)
    actions = WeatherForecastStrategy().evaluate(_state([bucket]))
    yes_buys = [a for a in actions if isinstance(a, PlaceLimit) and a.token_side == "YES" and a.side == "BUY"]
    no_buys = [a for a in actions if isinstance(a, PlaceLimit) and a.token_side == "NO" and a.side == "BUY"]
    assert len(yes_buys) == 1
    assert len(no_buys) == 0


def test_no_side_no_double_bet():
    # If we already have an open NO BUY, we don't place another.
    from polymarket_bot.strategy.base import OpenOrder
    bucket = _bucket_with_no("16°C", yes_ask=0.50, model_p=0.20, no_ask=0.50)
    open_no = OpenOrder(
        order_id="x", client_order_id="y", market_id="m",
        token_side="NO", side="BUY", price=0.50, size=10.0,
    )
    actions = WeatherForecastStrategy().evaluate(_state(
        [bucket], open_orders={"16°C": [open_no]}
    ))
    assert all(a.token_side != "NO" for a in actions if isinstance(a, PlaceLimit))


def test_no_side_no_reentry_after_fill():
    """Regression: after a NO BUY fills (no longer in open_orders), the
    held-NO check must prevent re-entry on the same edge. Without this the
    strategy would compound NO every tick the edge persists."""
    bucket = _bucket_with_no("16°C", yes_ask=0.50, model_p=0.20, no_ask=0.50)
    actions = WeatherForecastStrategy().evaluate(_state(
        [bucket],
        open_orders={},                 # filled order is no longer "open"
        held_no={"16°C": 25.0},         # but we hold the resulting position
    ))
    no_buys = [a for a in actions
               if isinstance(a, PlaceLimit) and a.token_side == "NO"
               and a.side == "BUY"]
    assert no_buys == []
