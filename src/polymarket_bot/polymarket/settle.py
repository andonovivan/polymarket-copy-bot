"""Settle open bets when their underlying market resolves."""

from __future__ import annotations

import time

import structlog

from polymarket_bot.persistence.repo import (
    Bet,
    Trade,
    append_equity,
    insert_trade,
    latest_equity,
    mark_bet_settled,
    open_bets,
    settle_market,
)
from polymarket_bot.polymarket.markets import DiscoveredMarket

logger = structlog.get_logger()


def outcome_for_bar(bar_open: float, bar_close: float) -> str:
    """Polymarket BTC up/down 5m settlement rule (per market description):

    'resolves to Up if the price at the end is greater than OR EQUAL TO the
    price at the beginning.'  Ties resolve UP.

    Caveat: Polymarket settles via Chainlink BTC/USD; this codebase uses Binance
    BTCUSDT klines. They typically agree to a couple of bps but can diverge near
    the 5-min boundary; surface as backtest noise.
    """
    return "UP" if bar_close >= bar_open else "DOWN"


def settle_resolved_market(
    market: DiscoveredMarket,
    bar_open: float,
    bar_close: float,
    bankroll_at_settle: float | None = None,
) -> int:
    """Mark a market resolved and settle every open bet against it.

    Returns the number of bets settled.
    """
    outcome = outcome_for_bar(bar_open, bar_close)
    settle_market(market.market_id, outcome, bar_open, bar_close)

    settled = 0
    now = int(time.time())
    bankroll = bankroll_at_settle if bankroll_at_settle is not None else (latest_equity() or 0.0)

    for bet in open_bets():
        if bet.market_id != market.market_id:
            continue
        won = (bet.side == "YES" and outcome == "UP") or (bet.side == "NO" and outcome == "DOWN")
        payout = 1.0 if won else 0.0
        # PnL = payout per share × shares − stake (= entry_price × shares).
        pnl = payout * bet.shares - bet.stake
        # Brier per bet uses the model probability for the realized direction (UP).
        realized_up = 1.0 if outcome == "UP" else 0.0
        brier = (bet.predicted_p - realized_up) ** 2
        insert_trade(Trade(
            id=None, market_id=bet.market_id, side=bet.side, shares=bet.shares,
            entry_price=bet.entry_price, payout=payout, pnl=pnl,
            fees=0.0, slippage=0.0,
            predicted_p=bet.predicted_p, market_p=bet.market_p, edge=bet.edge,
            brier=brier, outcome=outcome, strategy=bet.strategy,
            model_version=bet.model_version,
            opened_at=bet.opened_at, settled_at=now,
        ))
        if bet.id is not None:
            mark_bet_settled(bet.id)
        bankroll += pnl
        settled += 1
        logger.info("bet_settled", market_id=market.market_id[:12], side=bet.side,
                    outcome=outcome, pnl=round(pnl, 4))

    if settled:
        append_equity(now, bankroll)
    return settled
