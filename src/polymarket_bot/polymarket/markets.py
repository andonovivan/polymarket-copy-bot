"""Discover Polymarket BTC up/down 5-minute markets via the gamma events API.

The slug pattern `btc-updown-5m-<unix_start_ts>` is deterministic — every 5-minute
boundary creates a new event with that slug. Rather than scraping `/markets` (whose
filter API doesn't actually filter — `slug_prefix` is silently ignored), we compute
the upcoming boundaries from local time and look each one up directly via
`/events?slug=…`.

Verified empirically: the slug suffix is the *start* of the comparison window;
resolution_ts = start_ts + 300s. The market resolves UP if close >= open of the
window's BTC bar.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

GAMMA_API_URL = "https://gamma-api.polymarket.com"

SLUG_PREFIX = "btc-updown-5m-"
WINDOW_SECONDS = 300


@dataclass
class DiscoveredMarket:
    market_id: str          # Polymarket condition id used by the CLOB
    slug: str               # event slug
    start_ts: int           # unix; comparison-window open
    resolution_ts: int      # unix; comparison-window close (settlement time)
    yes_token_id: str
    no_token_id: str


def _parse_event(event: dict[str, Any]) -> DiscoveredMarket | None:
    """Extract our shape from a gamma `events` record."""
    try:
        slug = event.get("slug", "")
        if not slug.startswith(SLUG_PREFIX):
            return None
        markets = event.get("markets") or []
        if not markets:
            return None
        m = markets[0]
        condition_id = m.get("conditionId") or m.get("condition_id")
        tokens = m.get("clobTokenIds") or m.get("clob_token_ids") or []
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        if not condition_id or len(tokens) < 2:
            return None
        try:
            start_ts = int(slug.removeprefix(SLUG_PREFIX))
        except ValueError:
            return None
        return DiscoveredMarket(
            market_id=str(condition_id),
            slug=slug,
            start_ts=start_ts,
            resolution_ts=start_ts + WINDOW_SECONDS,
            yes_token_id=str(tokens[0]),
            no_token_id=str(tokens[1]),
        )
    except Exception as exc:
        logger.warning("event_parse_failed", slug=event.get("slug"), error=str(exc))
        return None


def lookup_market(start_ts: int, *, client: httpx.Client | None = None) -> DiscoveredMarket | None:
    """Look up a single BTC up/down 5m market by its window start timestamp."""
    own = client is None
    c = client or httpx.Client(timeout=10.0)
    try:
        resp = c.get(f"{GAMMA_API_URL}/events", params={"slug": f"{SLUG_PREFIX}{start_ts}"})
        resp.raise_for_status()
        events = resp.json()
        if not events:
            return None
        return _parse_event(events[0])
    except Exception as exc:
        logger.warning("market_lookup_failed", start_ts=start_ts, error=str(exc)[:200])
        return None
    finally:
        if own:
            c.close()


def upcoming_boundaries(now: int | None = None, count: int = 3) -> list[int]:
    """Return the next `count` 5-minute boundary timestamps strictly after `now`."""
    now = now if now is not None else int(time.time())
    next_start = ((now // WINDOW_SECONDS) + 1) * WINDOW_SECONDS
    return [next_start + i * WINDOW_SECONDS for i in range(count)]


def next_market(now: int | None = None, *, lookahead: int = 3) -> DiscoveredMarket | None:
    """Return the soonest open BTC up/down 5m market we can find.

    We try the next `lookahead` boundaries and return the first one whose event
    exists in gamma. Polymarket lists upcoming markets a few minutes in advance.
    """
    with httpx.Client(timeout=10.0) as c:
        for start in upcoming_boundaries(now, count=lookahead):
            m = lookup_market(start, client=c)
            if m is not None:
                return m
    return None
