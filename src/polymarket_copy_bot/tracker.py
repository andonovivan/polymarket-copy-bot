"""Polls Polymarket for new trades made by tracked wallets."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    from polymarket_copy_bot.client import PolymarketClient

from polymarket_copy_bot.config import BotConfig

logger = structlog.get_logger()

DATA_API_URL = "https://data-api.polymarket.com"

# How many trades to fetch per request.
# The Data API sorts newest-first, so we only need the first page
# to catch recent activity during normal polling.
POLL_LIMIT = 100

# For bulk fetches (seeding, reconciliation), fetch up to this many.
BULK_LIMIT = 10_000


@dataclass
class DetectedTrade:
    """A trade detected from a tracked wallet."""

    trade_id: str
    wallet: str
    asset_id: str  # token_id (the "asset" field from the Data API)
    side: str  # BUY or SELL
    price: float
    size: float
    timestamp: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


def fetch_user_trades(
    wallet: str,
    limit: int = POLL_LIMIT,
    since_ts: int = 0,
) -> list[dict[str, Any]]:
    """
    Fetch trades for a wallet from the Polymarket Data API.

    The Data API has no time-filter param, so we fetch the most recent
    `limit` trades and filter client-side by `since_ts`.
    Results are returned newest-first.
    """
    try:
        resp = httpx.get(
            f"{DATA_API_URL}/trades",
            params={"user": wallet, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        trades = resp.json()

        if since_ts > 0:
            trades = [t for t in trades if t.get("timestamp", 0) > since_ts]

        return trades
    except Exception as exc:
        logger.warning("trade_fetch_failed", wallet=wallet[:10] + "...", error=str(exc))
        return []


class TradeTracker:
    """Watches tracked wallets for new trades via the Data API."""

    _MAX_SEEN_IDS = 50_000

    def __init__(self, config: BotConfig, client: PolymarketClient) -> None:
        self.config = config
        self.client = client
        self._seen_trade_ids: set[str] = set()
        self._seeded: bool = False
        self._last_poll_ts: int = 0

    def poll(self) -> list[DetectedTrade]:
        """
        Poll all tracked wallets and return only *new* trades
        that haven't been seen before.

        On the first poll, all existing trades are marked as seen
        (seeded) so only truly new trades trigger copies.
        """
        new_trades: list[DetectedTrade] = []

        for wallet in self.config.tracked_wallets:
            raw_trades = fetch_user_trades(wallet, limit=POLL_LIMIT)

            for t in raw_trades:
                trade_id = t.get("transactionHash", "")
                if not trade_id or trade_id in self._seen_trade_ids:
                    continue

                self._seen_trade_ids.add(trade_id)

                # First poll: seed seen IDs without copying anything.
                if not self._seeded:
                    continue

                detected = DetectedTrade(
                    trade_id=trade_id,
                    wallet=wallet,
                    asset_id=t.get("asset", ""),
                    side=t.get("side", "BUY"),
                    price=float(t.get("price", 0)),
                    size=float(t.get("size", 0)),
                    timestamp=int(t.get("timestamp", 0)),
                    raw=t,
                )
                new_trades.append(detected)

                logger.info(
                    "new_trade_detected",
                    wallet=wallet[:10] + "...",
                    side=detected.side,
                    price=detected.price,
                    size=detected.size,
                    asset_id=detected.asset_id[:16] + "...",
                )

        if not self._seeded:
            logger.info("tracker_seeded", seen_trade_ids=len(self._seen_trade_ids))
            self._seeded = True

        self._last_poll_ts = int(time.time())

        # Prevent unbounded memory growth.
        if len(self._seen_trade_ids) > self._MAX_SEEN_IDS:
            excess = len(self._seen_trade_ids) - self._MAX_SEEN_IDS
            for _ in range(excess):
                self._seen_trade_ids.pop()

        return new_trades
