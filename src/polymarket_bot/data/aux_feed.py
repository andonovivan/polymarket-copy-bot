"""Auxiliary feeds: ETH 5m klines, BTC perp 5m klines, BTC perp funding rate.

All endpoints are public + unauthenticated:
  - ETH spot klines:   https://api.binance.com/api/v3/klines       (symbol=ETHUSDT)
  - BTC perp klines:   https://fapi.binance.com/fapi/v1/klines     (symbol=BTCUSDT)
  - BTC funding rate:  https://fapi.binance.com/fapi/v1/fundingRate (symbol=BTCUSDT)

Funding is published every 8h (UTC 00:00, 08:00, 16:00); we treat the most
recent publication ≤ a given bar timestamp as that bar's funding rate.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from polymarket_bot.persistence.repo import (
    Bar,
    FundingPoint,
    upsert_eth_bars,
    upsert_funding,
    upsert_perp_bars,
)

logger = structlog.get_logger()

SPOT_KLINES_URL = "https://api.binance.com/api/v3/klines"
FUTURES_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
FUTURES_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

INTERVAL = "5m"
INTERVAL_SECONDS = 5 * 60
PAGE_LIMIT = 1000
FUNDING_PAGE_LIMIT = 1000


def _parse_kline(row: list[Any]) -> Bar:
    return Bar(
        open_time=int(row[0]) // 1000,
        o=float(row[1]),
        h=float(row[2]),
        l=float(row[3]),
        c=float(row[4]),
        v=float(row[5]),
    )


def _fetch_klines(url: str, symbol: str, start_ms: int, end_ms: int,
                  client: httpx.Client) -> list[Bar]:
    try:
        resp = client.get(url, params={
            "symbol": symbol, "interval": INTERVAL,
            "startTime": start_ms, "endTime": end_ms, "limit": PAGE_LIMIT,
        })
        resp.raise_for_status()
        return [_parse_kline(r) for r in resp.json()]
    except Exception as exc:
        logger.warning("klines_fetch_failed", url=url, symbol=symbol, error=str(exc)[:200])
        return []


def _backfill_klines(url: str, symbol: str, days: int, upserter, label: str) -> int:
    """Generic backfill loop. Pages until end_ms; idempotent (INSERT OR REPLACE)."""
    now_s = int(time.time())
    start_s = now_s - days * 86400
    cursor_ms = start_s * 1000
    end_ms = now_s * 1000
    total = 0
    with httpx.Client(timeout=15.0) as c:
        while cursor_ms < end_ms:
            page = _fetch_klines(url, symbol, cursor_ms, end_ms, c)
            if not page:
                break
            total += upserter(page)
            next_cursor = (page[-1].open_time + INTERVAL_SECONDS) * 1000
            if next_cursor <= cursor_ms:
                break
            cursor_ms = next_cursor
            if len(page) < PAGE_LIMIT:
                break
            time.sleep(0.05)
    logger.info("aux_backfill_complete", feed=label, new_bars=total, days=days)
    return total


def backfill_eth(days: int = 60) -> int:
    return _backfill_klines(SPOT_KLINES_URL, "ETHUSDT", days, upsert_eth_bars, "eth")


def backfill_btc_perp(days: int = 60) -> int:
    return _backfill_klines(FUTURES_KLINES_URL, "BTCUSDT", days, upsert_perp_bars, "btc_perp")


def _fetch_funding_page(start_ms: int, end_ms: int, client: httpx.Client) -> list[FundingPoint]:
    try:
        resp = client.get(FUTURES_FUNDING_URL, params={
            "symbol": "BTCUSDT", "startTime": start_ms, "endTime": end_ms,
            "limit": FUNDING_PAGE_LIMIT,
        })
        resp.raise_for_status()
        rows = resp.json()
        return [
            FundingPoint(funding_ts=int(r["fundingTime"]) // 1000,
                         rate=float(r["fundingRate"]))
            for r in rows
        ]
    except Exception as exc:
        logger.warning("funding_fetch_failed", error=str(exc)[:200])
        return []


def backfill_funding(days: int = 60) -> int:
    now_s = int(time.time())
    start_s = now_s - days * 86400
    cursor_ms = start_s * 1000
    end_ms = now_s * 1000
    total = 0
    with httpx.Client(timeout=15.0) as c:
        while cursor_ms < end_ms:
            page = _fetch_funding_page(cursor_ms, end_ms, c)
            if not page:
                break
            total += upsert_funding(page)
            next_cursor = (page[-1].funding_ts + 1) * 1000
            if next_cursor <= cursor_ms:
                break
            cursor_ms = next_cursor
            if len(page) < FUNDING_PAGE_LIMIT:
                break
            time.sleep(0.05)
    logger.info("funding_backfill_complete", new_points=total, days=days)
    return total


def backfill_all_aux(days: int = 60) -> dict[str, int]:
    """Backfill ETH spot bars, BTC perp bars, and BTC funding rates."""
    return {
        "eth": backfill_eth(days),
        "btc_perp": backfill_btc_perp(days),
        "funding": backfill_funding(days),
    }
