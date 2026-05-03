"""BTC 5-minute OHLC ingestion from Binance public klines (no auth required)."""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from polymarket_bot.persistence.repo import Bar, latest_bar_time, upsert_bars

logger = structlog.get_logger()

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

SYMBOL = "BTCUSDT"
INTERVAL = "5m"
INTERVAL_SECONDS = 5 * 60
PAGE_LIMIT = 1000  # Binance klines hard cap (verified empirically)


def _parse_kline(row: list[Any]) -> Bar:
    return Bar(
        open_time=int(row[0]) // 1000,
        o=float(row[1]),
        h=float(row[2]),
        l=float(row[3]),
        c=float(row[4]),
        v=float(row[5]),
    )


def fetch_klines(start_ms: int | None = None, end_ms: int | None = None,
                 limit: int = PAGE_LIMIT, *, client: httpx.Client | None = None) -> list[Bar]:
    """Fetch one page of 5-min BTCUSDT klines from Binance."""
    own = client is None
    c = client or httpx.Client(timeout=15.0)
    try:
        params: dict[str, Any] = {"symbol": SYMBOL, "interval": INTERVAL, "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        resp = c.get(BINANCE_KLINES_URL, params=params)
        resp.raise_for_status()
        return [_parse_kline(r) for r in resp.json()]
    except Exception as exc:
        logger.warning("klines_fetch_failed", error=str(exc)[:200])
        return []
    finally:
        if own:
            c.close()


def backfill(days: int = 60) -> int:
    """Backfill the local cache with the last `days` of 5-min bars.

    Skips bars already present (the table is keyed on open_time).
    Returns the number of new bars inserted.
    """
    now_s = int(time.time())
    start_s = now_s - days * 86400
    cursor_ms = max(start_s, (latest_bar_time() or 0) + INTERVAL_SECONDS) * 1000
    end_ms = now_s * 1000

    total = 0
    with httpx.Client(timeout=15.0) as c:
        while cursor_ms < end_ms:
            page = fetch_klines(start_ms=cursor_ms, end_ms=end_ms, client=c)
            if not page:
                break
            inserted = upsert_bars(page)
            total += inserted
            last_open = page[-1].open_time
            next_cursor = (last_open + INTERVAL_SECONDS) * 1000
            if next_cursor <= cursor_ms:
                break
            cursor_ms = next_cursor
            if len(page) < PAGE_LIMIT:
                break
            time.sleep(0.05)  # polite pacing
    logger.info("btc_backfill_complete", new_bars=total, days=days)
    return total


def latest_closed_bar() -> Bar | None:
    """Fetch the most recently closed 5-min bar from Binance (synchronous)."""
    bars = fetch_klines(limit=2)
    if len(bars) < 2:
        return None
    # Last entry can be the in-progress bar. The second-to-last is closed.
    return bars[-2]
