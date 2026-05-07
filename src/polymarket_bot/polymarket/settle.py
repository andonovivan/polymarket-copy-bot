"""Settle a resolved weather event into per-(bucket, strategy) Settlement rows.

Outcomes come from gamma's `outcomePrices` per sub-market: [1, 0] for the
winning bucket, [0, 1] for losers. For each bucket we hold YES tokens on,
the payout is (1 if won else 0) per share.

Phase C.5 changes the unit of attribution from the *whole bucket* to a
**(bucket, strategy)** pair. With multiple strategies sharing markets,
each one's fills get their own Settlement row so per-strategy PnL is
accurate. The settlements table's PK is now `(market_id, strategy)`.
"""

from __future__ import annotations

import time
from collections import defaultdict

import structlog

from polymarket_bot.persistence.repo import (
    Settlement,
    fills_for_market,
    insert_settlement,
    settle_market_row,
)
from polymarket_bot.polymarket.weather_markets import gamma_outcome
from polymarket_bot.strategy.base import WeatherEvent

logger = structlog.get_logger()


def _aggregate_fills_by_strategy(fills: list) -> dict[str, dict[str, float]]:
    """Group fills by strategy → {yes_shares, no_shares, yes_paid, no_paid}.

    Mirrors `_aggregate_inventory_rows` in repo.py but stays scoped to one
    market's fills so we can decompose per-strategy at settlement time.
    """
    out: dict[str, dict[str, float]] = defaultdict(
        lambda: {"yes_buy_size": 0.0, "yes_buy_paid": 0.0, "yes_sell_size": 0.0,
                 "no_buy_size": 0.0, "no_buy_paid": 0.0, "no_sell_size": 0.0})
    for f in fills:
        bucket = out[f.strategy]
        if f.token_side == "YES" and f.side == "BUY":
            bucket["yes_buy_size"] += float(f.size)
            bucket["yes_buy_paid"] += float(f.size) * float(f.price)
        elif f.token_side == "YES" and f.side == "SELL":
            bucket["yes_sell_size"] += float(f.size)
        elif f.token_side == "NO" and f.side == "BUY":
            bucket["no_buy_size"] += float(f.size)
            bucket["no_buy_paid"] += float(f.size) * float(f.price)
        elif f.token_side == "NO" and f.side == "SELL":
            bucket["no_sell_size"] += float(f.size)
    return out


def settle_resolved_event(  # noqa: D401 — clarity over brevity
    event: WeatherEvent, *, strategy: str = "weather_forecast",
    winning_fee_bps: int = 500,
) -> bool:
    """If `event` is resolved on gamma, write Settlement rows + update markets.

    Phase C.5: writes one Settlement row per (market_id, strategy) — every
    strategy that placed fills against this event gets its own settlement
    with proper PnL attribution. The `strategy` kwarg is kept for backwards
    compatibility with single-strategy callers but is *unused* — every
    strategy that touched a fill on this event gets a row.

    Returns True if settlement happened, False if not yet resolved or already done.
    """
    del strategy  # historical param; PoC kept for backwards compat
    outcomes = gamma_outcome(event)
    if outcomes is None:
        return False

    now = int(time.time())
    settled_any = False
    fee_rate = max(0, winning_fee_bps) / 10_000.0

    for b in event.buckets:
        fills = fills_for_market(b.market_id)
        if not fills:
            settle_market_row(
                b.market_id,
                "WIN" if outcomes.get(b.label, 0) >= 0.5 else "LOSS",
                0.0, 0.0,
            )
            continue

        won = outcomes.get(b.label, 0) >= 0.5
        outcome_label = "WIN" if won else "LOSS"
        settle_market_row(b.market_id, outcome_label, 0.0, 0.0)

        # Decompose fills per strategy so each gets its own Settlement row.
        by_strategy = _aggregate_fills_by_strategy(fills)
        for strat, agg in by_strategy.items():
            yes_shares = agg["yes_buy_size"] - agg["yes_sell_size"]
            no_shares = agg["no_buy_size"] - agg["no_sell_size"]
            avg_yes = (agg["yes_buy_paid"] / agg["yes_buy_size"]
                       if agg["yes_buy_size"] > 0 else 0.0)
            avg_no = (agg["no_buy_paid"] / agg["no_buy_size"]
                      if agg["no_buy_size"] > 0 else 0.0)
            gross_payout = (yes_shares if won else 0.0) + (
                0.0 if won else no_shares)
            cost = avg_yes * yes_shares + avg_no * no_shares
            winnings = max(0.0, gross_payout - cost)
            fee = winnings * fee_rate
            payout = gross_payout - fee
            pnl = payout - cost
            settled_any = True

            insert_settlement(Settlement(
                market_id=b.market_id, settled_at=now, outcome=outcome_label,
                yes_shares=yes_shares, no_shares=no_shares,
                avg_yes_cost=avg_yes, avg_no_cost=avg_no,
                payout=payout, cost=cost, pnl=pnl, strategy=strat,
            ))
            logger.info(
                "weather_bucket_settled",
                event_slug=event.slug, bucket=b.label, outcome=outcome_label,
                strategy=strat,
                yes_shares=round(yes_shares, 2), avg_cost=round(avg_yes, 4),
                gross_payout=round(gross_payout, 4), fee=round(fee, 4),
                payout=round(payout, 4), pnl=round(pnl, 4),
            )

    # Equity curve is updated by main._maybe_sample_equity on the next tick,
    # which recomputes realized cash from settlements (no double-counting).
    return settled_any
