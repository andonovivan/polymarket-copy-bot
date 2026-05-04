"""Settle a resolved weather event into per-bucket Settlement rows + equity update.

Outcomes come from gamma's `outcomePrices` per sub-market: [1, 0] for the
winning bucket, [0, 1] for losers. For each bucket we hold YES tokens on, the
payout is (1 if won else 0) per share.
"""

from __future__ import annotations

import time

import structlog

from polymarket_bot.persistence.repo import (
    Settlement,
    append_equity,
    fills_for_market,
    insert_settlement,
    inventory_for_market,
    latest_equity,
    settle_market_row,
)
from polymarket_bot.polymarket.weather_markets import gamma_outcome
from polymarket_bot.strategy.base import WeatherEvent

logger = structlog.get_logger()


def settle_resolved_event(event: WeatherEvent, *, strategy: str) -> bool:
    """If `event` is resolved on gamma, write per-bucket Settlements + update equity.

    Returns True if settlement happened, False if not yet resolved or already done.
    """
    outcomes = gamma_outcome(event)
    if outcomes is None:
        return False

    now = int(time.time())
    bankroll = latest_equity() or 0.0
    settled_any = False

    for b in event.buckets:
        # Skip buckets we never touched.
        fills = fills_for_market(b.market_id)
        if not fills:
            settle_market_row(b.market_id, "WIN" if outcomes.get(b.label, 0) >= 0.5 else "LOSS",
                              0.0, 0.0)
            continue

        won = outcomes.get(b.label, 0) >= 0.5
        outcome_label = "WIN" if won else "LOSS"

        yes_shares, no_shares, avg_yes, avg_no = inventory_for_market(b.market_id)
        # We only ever BUY YES in WeatherForecast — yes_shares is the position;
        # no_shares should be 0. Guard the math anyway.
        payout = (yes_shares if won else 0.0) + (0.0 if won else no_shares)
        cost = avg_yes * yes_shares + avg_no * no_shares
        pnl = payout - cost
        bankroll += pnl
        settled_any = True

        settle_market_row(b.market_id, outcome_label, 0.0, 0.0)
        insert_settlement(Settlement(
            market_id=b.market_id, settled_at=now, outcome=outcome_label,
            yes_shares=yes_shares, no_shares=no_shares,
            avg_yes_cost=avg_yes, avg_no_cost=avg_no,
            payout=payout, cost=cost, pnl=pnl, strategy=strategy,
        ))
        logger.info(
            "weather_bucket_settled",
            event_slug=event.slug, bucket=b.label, outcome=outcome_label,
            yes_shares=round(yes_shares, 2), avg_cost=round(avg_yes, 4),
            payout=round(payout, 4), pnl=round(pnl, 4),
        )

    if settled_any:
        append_equity(now, bankroll)
    return settled_any
