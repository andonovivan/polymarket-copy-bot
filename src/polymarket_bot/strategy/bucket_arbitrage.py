"""BucketArbitrageStrategy — model-independent structural alpha.

Each weather event has 11 mutually-exclusive YES buckets; exactly one
will resolve to $1. In a fair market the YES asks should sum to ~$1.
When they sum to materially less than $1 (slow-moving books, asymmetric
liquidity), buying every bucket guarantees a $1 payout against a
sub-$1 cost basis — risk-free arbitrage.

Per tick:
  • Compute ask_sum = Σ yes_ask across all buckets in the event.
  • If ask_sum ≤ 1 − ARBITRAGE_THRESHOLD and every bucket is liquid
    enough, place equal-share BUY YES orders on every bucket.
  • Skip if any bucket already has an open arbitrage order or held
    YES (don't double up; one arb per event lifetime).

The strategy is intentionally conservative — small thresholds, depth
checks, and a buffer for taker fees on the eventual winning bucket.
"""

from __future__ import annotations

import math
import uuid

import structlog

from polymarket_bot.strategy.base import (
    BetState,
    BettingStrategy,
    OrderAction,
    PlaceLimit,
)

logger = structlog.get_logger()

# Required edge below $1 to consider arbitrage. Polymarket charges a 5% taker
# fee on the winning bucket only, so the *effective* payout per $1 of cost is
# ~$0.95 + small slippage. A 7% gap below $1 leaves comfortable headroom.
ARBITRAGE_THRESHOLD = 0.07

# Cap a single arbitrage trade so a fat-finger book-quote bug doesn't blow
# the bankroll. Smaller than the strategy's per-bet cap because we put 11x
# of these out at once.
MAX_ARBITRAGE_PCT = 0.05

# Polymarket minimum order notional.
MIN_ORDER_NOTIONAL = 1.0


class BucketArbitrageStrategy(BettingStrategy):
    name = "bucket_arbitrage"
    display_name = "Bucket Arbitrage"

    def evaluate(self, state: BetState) -> list[OrderAction]:
        if state.seconds_to_resolution <= state.lockout_seconds:
            return []

        buckets = state.event.buckets
        # All buckets must have a usable ask, sufficient depth, and not already
        # be partly held / actively quoted by us.
        for b in buckets:
            if b.yes_ask is None or not (0.0 < b.yes_ask < 1.0):
                return []
            if b.depth_yes_ask_usd < state.min_market_depth_usd:
                return []
            if state.held_yes_shares_by_bucket.get(b.label, 0.0) > 0:
                return []
            if state.open_orders_by_bucket.get(b.label):
                return []

        ask_sum = sum(b.yes_ask for b in buckets)
        if ask_sum > 1.0 - ARBITRAGE_THRESHOLD:
            return []

        # We've found an arb. Size the budget; allocate it across all buckets
        # in proportion to their YES ask (so each bucket's fill grants the
        # same share count → uniform $1 payout regardless of which bucket
        # wins).
        bankroll = max(state.bankroll, 0.0)
        budget = bankroll * MAX_ARBITRAGE_PCT
        # Respect the global exposure cap.
        max_total = bankroll * state.max_total_exposure_pct
        budget = min(budget, max(0.0, max_total - state.total_open_exposure_usd))
        if budget < MIN_ORDER_NOTIONAL * len(buckets):
            return []

        # Floor share count so every bucket gets identical N shares. Each
        # bucket then costs `N * yes_ask` and a $1 payout returns `N` per
        # share regardless of which one wins.
        max_shares = budget / ask_sum   # if every bucket got the same N
        n_shares = math.floor(max_shares * 100) / 100
        if n_shares < 1.0:
            return []

        # Verify per-bucket notional clears the $1 minimum (Polymarket).
        if min(n_shares * b.yes_ask for b in buckets) < MIN_ORDER_NOTIONAL:
            return []

        # Risk-free profit per share = 1 − ask_sum (gross) minus the 5% fee
        # that hits the winning bucket. Net profit per share = 0.95 − ask_sum
        # if winner, but losers cost their ask. Total cost = n_shares *
        # ask_sum; total payout = 0.95 * n_shares. Margin = (0.95 − ask_sum)
        # / ask_sum. We've already enforced ask_sum ≤ 0.93.
        cost = n_shares * ask_sum
        gross_payout = n_shares
        net_payout = 0.95 * n_shares
        margin = (net_payout - cost) / cost

        actions: list[OrderAction] = []
        cid = uuid.uuid4().hex[:8]
        for b in buckets:
            actions.append(PlaceLimit(
                market_id=b.market_id,
                token_id=b.yes_token_id,
                token_side="YES",
                side="BUY",
                price=b.yes_ask,
                size=n_shares,
                client_order_id=f"arb-{state.event.slug[-26:]}-{b.label}-{cid}",
            ))

        logger.info(
            "arbitrage_detected",
            event_slug=state.event.slug,
            ask_sum=round(ask_sum, 4),
            n_buckets=len(buckets),
            n_shares=n_shares,
            cost=round(cost, 2),
            gross_payout=round(gross_payout, 2),
            net_payout=round(net_payout, 2),
            net_margin_pct=round(margin * 100, 2),
        )
        return actions
