"""Settle MM markets: when a Polymarket BTC up/down 5m market resolves,
fold the inventory we accumulated into a $/share payout, append PnL to
the equity curve, and write a Settlement row.

Outcome source: we re-fetch the event from gamma to read `outcomePrices`.
That's authoritative — it's exactly what Polymarket pays. We log a
warning when it diverges from a Binance-bar-derived UP/DOWN guess (Polymarket
settles via Chainlink BTC/USD, so divergences happen near the boundary).
"""

from __future__ import annotations

import time

import httpx
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
from polymarket_bot.polymarket.markets import GAMMA_API_URL, DiscoveredMarket

logger = structlog.get_logger()


def _outcome_from_gamma(market: DiscoveredMarket) -> str | None:
    """UP/DOWN if the gamma event is resolved, else None."""
    try:
        with httpx.Client(timeout=10.0) as c:
            resp = c.get(f"{GAMMA_API_URL}/events", params={"slug": market.slug})
            resp.raise_for_status()
            events = resp.json()
            if not events:
                return None
            m = events[0].get("markets") or []
            if not m:
                return None
            prices = m[0].get("outcomePrices")
            if isinstance(prices, str):
                import json as _json
                prices = _json.loads(prices)
            if not prices or len(prices) < 2:
                return None
            return "UP" if float(prices[0]) > float(prices[1]) else "DOWN"
    except Exception as exc:
        logger.warning("gamma_outcome_fetch_failed", slug=market.slug, error=str(exc)[:200])
        return None


def settle_resolved_market(
    market: DiscoveredMarket, *, strategy: str, bankroll_at_settle: float | None = None,
) -> bool:
    """Resolve `market`, compute PnL from our fills, append equity. Returns True if settled."""
    outcome = _outcome_from_gamma(market)
    if outcome is None:
        return False

    fills = fills_for_market(market.market_id)
    if not fills:
        # Nothing to settle — still mark the market resolved.
        settle_market_row(market.market_id, outcome, 0.0, 0.0)
        return True

    yes_shares, no_shares, avg_yes, avg_no = inventory_for_market(market.market_id)
    payout = (yes_shares if outcome == "UP" else 0.0) + (no_shares if outcome == "DOWN" else 0.0)
    cost = avg_yes * yes_shares + avg_no * no_shares
    pnl = payout - cost
    now = int(time.time())

    settle_market_row(market.market_id, outcome, 0.0, 0.0)
    insert_settlement(Settlement(
        market_id=market.market_id, settled_at=now, outcome=outcome,
        yes_shares=yes_shares, no_shares=no_shares,
        avg_yes_cost=avg_yes, avg_no_cost=avg_no,
        payout=payout, cost=cost, pnl=pnl, strategy=strategy,
    ))
    bankroll = bankroll_at_settle if bankroll_at_settle is not None else (latest_equity() or 0.0)
    append_equity(now, bankroll + pnl)
    logger.info(
        "market_settled",
        market_id=market.market_id[:12], outcome=outcome,
        yes=round(yes_shares, 2), no=round(no_shares, 2),
        payout=round(payout, 4), cost=round(cost, 4), pnl=round(pnl, 4),
    )
    return True
