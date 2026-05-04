"""WeatherForecastStrategy — bet on Polymarket buckets where the multi-model
ensemble disagrees with the market by more than `edge_threshold`.

Per-bucket logic each tick:
  • compute model probability from the ensemble (already attached to bucket)
  • edge = model_p − yes_ask
  • if edge > edge_threshold: BUY YES at yes_ask, sized fractional-Kelly
  • one bet per bucket per event (don't double down)
  • lockout near resolution (model uncertainty resolves there; market is sharper)
"""

from __future__ import annotations

import time
import uuid

import structlog

from polymarket_bot.strategy.base import (
    BetState,
    BettingStrategy,
    OrderAction,
    PlaceLimit,
)

logger = structlog.get_logger()

MIN_ORDER_NOTIONAL = 1.0   # Polymarket minimum order $1


def _kelly_fraction(p: float, price: float) -> float:
    """Full-Kelly fraction of bankroll for a YES bet at `price` with model prob `p`."""
    if not (0.0 < price < 1.0) or not (0.0 <= p <= 1.0):
        return 0.0
    b = (1.0 - price) / price
    if b <= 0:
        return 0.0
    q = 1.0 - p
    return max(0.0, (b * p - q) / b)


class WeatherForecastStrategy(BettingStrategy):
    name = "weather_forecast"

    def evaluate(self, state: BetState) -> list[OrderAction]:
        if state.seconds_to_resolution <= state.lockout_seconds:
            return []   # too close to resolution; market sharpens here

        actions: list[OrderAction] = []
        bankroll = max(state.bankroll, 0.0)
        running_exposure = state.total_open_exposure_usd
        max_total = bankroll * state.max_total_exposure_pct

        for b in state.event.buckets:
            if b.model_p is None or b.yes_ask is None:
                continue
            if not (0.0 < b.yes_ask < 1.0):
                continue
            if b.depth_yes_ask_usd < state.min_market_depth_usd:
                continue
            # One bet per bucket per event — skip if we have any open OR filled YES on it.
            existing = state.open_orders_by_bucket.get(b.label, [])
            if any(o.token_side == "YES" and o.side == "BUY" for o in existing):
                continue
            if state.held_yes_shares_by_bucket.get(b.label, 0.0) > 0:
                continue

            edge = b.model_p - b.yes_ask
            if edge < state.edge_threshold:
                continue

            f_full = _kelly_fraction(b.model_p, b.yes_ask)
            f_use = min(state.kelly_fraction * f_full, state.max_bet_pct)
            stake = bankroll * f_use
            # Global exposure cap — never commit more than max_total_exposure_pct of bankroll.
            stake = min(stake, max(0.0, max_total - running_exposure))
            if stake < MIN_ORDER_NOTIONAL:
                continue

            # Floor (not round) so the actual notional never exceeds the budgeted stake.
            import math
            shares = math.floor((stake / b.yes_ask) * 100) / 100
            if shares < 1.0:
                continue
            running_exposure += shares * b.yes_ask

            actions.append(PlaceLimit(
                market_id=b.market_id,
                token_id=b.yes_token_id,
                token_side="YES",
                side="BUY",
                price=b.yes_ask,
                size=shares,
                client_order_id=f"wf-{state.event.slug[-30:]}-{b.label}-{uuid.uuid4().hex[:6]}",
            ))
            logger.info(
                "weather_bet_decision",
                event_slug=state.event.slug, bucket=b.label,
                model_p=round(b.model_p, 3), yes_ask=round(b.yes_ask, 3),
                edge=round(edge, 3), stake=round(stake, 2), shares=shares,
            )
        return actions
