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
    fills_for_market,
    insert_settlement,
    inventory_for_market,
    settle_market_row,
)
from polymarket_bot.polymarket.weather_markets import gamma_outcome
from polymarket_bot.strategy.base import WeatherEvent

logger = structlog.get_logger()


def settle_resolved_event(  # noqa: D401 — clarity over brevity
    event: WeatherEvent, *, strategy: str, winning_fee_bps: int = 500,
) -> bool:
    """If `event` is resolved on gamma, write per-bucket Settlements + update equity.

    Polymarket weather markets charge a fee on the **winnings** side at resolution
    (taker-only, configurable via `winning_fee_bps`). Per gamma's feeSchedule the
    rate is 5%; we apply it on the (payout − cost) winnings of each share, so a YES
    bought at 0.10 that wins pays $1.00 − 0.05·0.90 = $0.955 net.

    Returns True if settlement happened, False if not yet resolved or already done.
    """
    outcomes = gamma_outcome(event)
    if outcomes is None:
        return False

    now = int(time.time())
    settled_any = False
    fee_rate = max(0, winning_fee_bps) / 10_000.0

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
        # YES pays $1 each if the bucket wins; NO pays $1 each if it doesn't.
        gross_payout = (yes_shares if won else 0.0) + (0.0 if won else no_shares)
        cost = avg_yes * yes_shares + avg_no * no_shares
        # Polymarket charges the 5% taker fee on net winnings, regardless of
        # which side won — so a profitable NO trade also pays the fee. The
        # `max(0, ...)` guard ensures losses don't accrue a negative fee.
        winnings = max(0.0, gross_payout - cost)
        fee = winnings * fee_rate
        payout = gross_payout - fee
        pnl = payout - cost
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
            gross_payout=round(gross_payout, 4), fee=round(fee, 4),
            payout=round(payout, 4), pnl=round(pnl, 4),
        )

    # Equity curve is updated by main._maybe_sample_equity on the next tick,
    # which recomputes realized cash from settlements (no double-counting).
    return settled_any
