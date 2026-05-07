"""WeatherForecastStrategy — bet on Polymarket buckets where the multi-model
ensemble disagrees with the market by more than `edge_threshold`.

Per-bucket logic each tick:
  • compute model probability from the ensemble (already attached to bucket)
  • **profit-take exit (#3)**: if we hold shares AND the current YES bid
    materially exceeds the discounted hold-EV (95% × model_p + buffer),
    place a SELL at bid for the full position. Locks in variance reduction
    on lottery tickets that paid off.
  • **YES entry**: if edge = model_p − yes_ask exceeds the threshold,
    BUY YES at yes_ask, sized fractional-Kelly.
  • **NO entry (#2)**: symmetric — if edge_no = (1 − model_p) − no_ask
    exceeds the threshold, BUY NO at no_ask. Captures over-priced buckets
    that BUY YES would never see. Only when `no_bid`/`no_ask` are populated
    (Bucket has them when populate_quotes(fetch_no_book=True)).
  • one entry bet per (bucket, side) per event — don't double down
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
PROFIT_TAKE_BUFFER = 0.10   # bid must beat (0.95 × model_p) by at least this much
WINNING_FEE_FACTOR = 0.95   # Polymarket charges 5% on winnings (taker only)


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
    display_name = "Weather"

    def evaluate(self, state: BetState) -> list[OrderAction]:
        if state.seconds_to_resolution <= state.lockout_seconds:
            return []   # too close to resolution; market sharpens here

        actions: list[OrderAction] = []
        bankroll = max(state.bankroll, 0.0)
        running_exposure = state.total_open_exposure_usd
        max_total = bankroll * state.max_total_exposure_pct
        # Confidence multiplier: 1/(1+std). std=0 → 1.0; std=1 → 0.5; std=3 → 0.25.
        # Dampens Kelly when the ensemble disagrees so we don't overcommit on
        # high-uncertainty events.
        member_std = state.event.member_std or 0.0
        confidence_mult = 1.0 / (1.0 + member_std)
        # Per-city warmup gate: refuse new BUYs until Path B has captured
        # enough settled obs for the city. Profit-take SELLs are unaffected
        # so existing positions still wind down.
        warmed_up = True
        if state.warmup_min_obs > 0:
            from polymarket_bot.strategy.calibration import is_city_warmed_up
            warmed_up = is_city_warmed_up(state.event.city_key, state.warmup_min_obs)
            if not warmed_up:
                logger.debug("weather_skip_warmup", city=state.event.city_key,
                             min_obs=state.warmup_min_obs)

        for b in state.event.buckets:
            # Profit-take FIRST — if we hold this bucket and the bid is rich,
            # exit instead of looking for new entries.
            held = state.held_yes_shares_by_bucket.get(b.label, 0.0)
            if held > 0:
                pt = self._profit_take(b, held, state)
                if pt is not None:
                    actions.append(pt)
                continue   # don't also try to buy more of the same bucket

            if not warmed_up:
                continue
            if b.model_p is None or b.yes_ask is None:
                continue
            if not (0.0 < b.yes_ask < 1.0):
                continue
            if b.depth_yes_ask_usd < state.min_market_depth_usd:
                continue
            # One entry per bucket per event — skip if we already have an open BUY.
            existing = state.open_orders_by_bucket.get(b.label, [])
            if any(o.token_side == "YES" and o.side == "BUY" for o in existing):
                continue

            edge = b.model_p - b.yes_ask
            if edge < state.edge_threshold:
                continue

            f_full = _kelly_fraction(b.model_p, b.yes_ask)
            f_use = min(state.kelly_fraction * f_full * confidence_mult, state.max_bet_pct)
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
                event_slug=state.event.slug, bucket=b.label, side="YES",
                model_p=round(b.model_p, 3), yes_ask=round(b.yes_ask, 3),
                edge=round(edge, 3), stake=round(stake, 2), shares=shares,
            )

        # NO-side pass — symmetric to the YES block above. Skipped if Bucket
        # doesn't carry NO quotes (i.e. populate_quotes was called without
        # fetch_no_book). NO-edge cannot coexist with YES-edge in a tight
        # market, so iterating again here is cheap and rarely emits extra
        # orders.
        for b in state.event.buckets:
            if not warmed_up:
                continue
            if b.model_p is None or b.no_ask is None:
                continue
            if not (0.0 < b.no_ask < 1.0):
                continue
            if b.depth_no_ask_usd < state.min_market_depth_usd:
                continue
            existing = state.open_orders_by_bucket.get(b.label, [])
            if any(o.token_side == "NO" and o.side == "BUY" for o in existing):
                continue
            # Don't compound a NO position. After a previous NO BUY fills,
            # the order leaves `open_orders_by_bucket` (status='filled') so
            # the open-order guard above no longer catches it — we'd buy
            # again on every tick the edge persists. The held-NO check
            # closes that loophole, mirroring held_yes_shares_by_bucket.
            if state.held_no_shares_by_bucket.get(b.label, 0.0) > 0:
                continue

            no_p = 1.0 - b.model_p
            edge = no_p - b.no_ask
            if edge < state.edge_threshold:
                continue

            f_full = _kelly_fraction(no_p, b.no_ask)
            f_use = min(state.kelly_fraction * f_full * confidence_mult, state.max_bet_pct)
            stake = bankroll * f_use
            stake = min(stake, max(0.0, max_total - running_exposure))
            if stake < MIN_ORDER_NOTIONAL:
                continue
            import math
            shares = math.floor((stake / b.no_ask) * 100) / 100
            if shares < 1.0:
                continue
            running_exposure += shares * b.no_ask

            actions.append(PlaceLimit(
                market_id=b.market_id,
                token_id=b.no_token_id,
                token_side="NO",
                side="BUY",
                price=b.no_ask,
                size=shares,
                client_order_id=f"wf-no-{state.event.slug[-26:]}-{b.label}-{uuid.uuid4().hex[:6]}",
            ))
            logger.info(
                "weather_bet_decision",
                event_slug=state.event.slug, bucket=b.label, side="NO",
                model_p=round(b.model_p, 3), no_ask=round(b.no_ask, 3),
                edge=round(edge, 3), stake=round(stake, 2), shares=shares,
            )
        return actions

    def _profit_take(self, bucket, held: float, state: BetState):
        """Return a SELL action if the bid materially beats the hold-EV; else None.

        Sell threshold is `WINNING_FEE_FACTOR × model_p + PROFIT_TAKE_BUFFER`.
        The fee factor accounts for the 5% taker fee on the eventual winner;
        the buffer keeps us from churning on tiny edges.
        """
        if bucket.model_p is None:
            return None
        if bucket.yes_bid is None or not (0.0 < bucket.yes_bid < 1.0):
            return None
        # Skip if we already have an open SELL on this bucket.
        existing = state.open_orders_by_bucket.get(bucket.label, [])
        if any(o.token_side == "YES" and o.side == "SELL" for o in existing):
            return None
        threshold = bucket.model_p * WINNING_FEE_FACTOR + PROFIT_TAKE_BUFFER
        if bucket.yes_bid < threshold:
            return None
        if held * bucket.yes_bid < MIN_ORDER_NOTIONAL:
            return None
        action = PlaceLimit(
            market_id=bucket.market_id,
            token_id=bucket.yes_token_id,
            token_side="YES", side="SELL",
            price=bucket.yes_bid, size=held,
            client_order_id=f"pt-{state.event.slug[-26:]}-{bucket.label}-{uuid.uuid4().hex[:6]}",
        )
        logger.info(
            "weather_profit_take",
            event_slug=state.event.slug, bucket=bucket.label,
            model_p=round(bucket.model_p, 3),
            yes_bid=round(bucket.yes_bid, 3),
            threshold=round(threshold, 3),
            shares=held,
            sell_notional=round(held * bucket.yes_bid, 2),
        )
        return action
