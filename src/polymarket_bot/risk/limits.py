"""Pre-trade gating that isn't position-sizing."""

from __future__ import annotations

import time

from polymarket_bot.persistence.repo import Trade, list_trades


def cooldown_active(strategy: str, cooldown_bars: int, bar_seconds: int = 300) -> bool:
    """Block re-entry for `cooldown_bars` after the most recent loss for this strategy."""
    if cooldown_bars <= 0:
        return False
    recent = list_trades(limit=1, strategy=strategy)
    if not recent:
        return False
    last: Trade = recent[0]
    if last.pnl >= 0:
        return False
    elapsed = int(time.time()) - last.settled_at
    return elapsed < (cooldown_bars * bar_seconds)


def market_lockout(resolution_ts: int, lock_buffer_seconds: int) -> bool:
    """True when the market is too close to resolution to safely place a bet."""
    return resolution_ts - int(time.time()) <= lock_buffer_seconds
