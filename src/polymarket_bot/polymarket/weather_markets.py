"""Discover Polymarket weather events (recurring daily city-temperature markets).

Each event has 11 categorical sub-markets (the buckets). We pull the event,
parse buckets + their token ids, and expose a flat structure the strategy can
consume.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

import httpx
import structlog

from polymarket_bot.data.weather_feed import CITY_REGISTRY, City
from polymarket_bot.persistence.repo import update_market_quote
from polymarket_bot.polymarket.book import fetch_book
from polymarket_bot.polymarket.markets import GAMMA_API_URL
from polymarket_bot.polymarket.quotes import parse_book
from polymarket_bot.strategy.base import Bucket, WeatherEvent

logger = structlog.get_logger()


def _detect_unit(event_json: dict) -> Literal["fahrenheit", "celsius"]:
    """Bucket labels are authoritative — descriptions mention both °F/°C boilerplate."""
    for m in event_json.get("markets", []):
        label = m.get("groupItemTitle", "")
        if "°F" in label:
            return "fahrenheit"
        if "°C" in label:
            return "celsius"
    return "fahrenheit"


def _parse_iso(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def discover_event(slug: str, city_key: str, *,
                   client: httpx.Client | None = None) -> WeatherEvent | None:
    """Fetch a single weather event by slug, or None if missing/closed."""
    own = client is None
    c = client or httpx.Client(timeout=10.0)
    try:
        resp = c.get(f"{GAMMA_API_URL}/events", params={"slug": slug})
        resp.raise_for_status()
        events = resp.json()
        if not events:
            return None
        e = events[0]
        end_ts = _parse_iso(e.get("endDate"))
        if end_ts is None:
            return None
        unit = _detect_unit(e)

        buckets: list[Bucket] = []
        for m in e.get("markets", []):
            tids = m.get("clobTokenIds")
            if isinstance(tids, str):
                tids = json.loads(tids)
            if not tids or len(tids) < 2:
                continue
            buckets.append(Bucket(
                label=m.get("groupItemTitle", ""),
                market_id=m.get("conditionId", ""),
                yes_token_id=str(tids[0]),
                no_token_id=str(tids[1]),
                yes_bid=None, yes_ask=None, yes_mid=None,
                depth_yes_ask_usd=0.0, model_p=None,
            ))
        if not buckets:
            return None

        return WeatherEvent(
            slug=slug, title=e.get("title", ""), city_key=city_key,
            end_ts=end_ts, resolution_ts=end_ts, unit=unit, buckets=buckets,
        )
    except Exception as exc:
        logger.warning("weather_event_fetch_failed", slug=slug, error=str(exc)[:200])
        return None
    finally:
        if own:
            c.close()


def upcoming_event_slugs(city: City, *, days_ahead: int = 4) -> list[str]:
    """Compute the slugs of upcoming daily weather markets for this city.

    Markets follow `<prefix><month-name>-<day>-<year>`, e.g.
    `highest-temperature-in-paris-on-may-3-2026`.
    """
    today = datetime.now(timezone.utc)
    out: list[str] = []
    for i in range(days_ahead):
        d = today.replace(hour=0, minute=0, second=0, microsecond=0)
        d = d.fromtimestamp(d.timestamp() + i * 86400, tz=timezone.utc)
        # Polymarket slugs use lowercase month names + un-padded day.
        month = d.strftime("%B").lower()
        slug = f"{city.event_slug_prefix}{month}-{d.day}-{d.year}"
        out.append(slug)
    return out


def discover_open_events(city_keys: list[str], *,
                         days_ahead: int = 4) -> list[WeatherEvent]:
    """Find all open weather events across the configured cities."""
    now = int(datetime.now(timezone.utc).timestamp())
    events: list[WeatherEvent] = []
    with httpx.Client(timeout=10.0) as c:
        for ck in city_keys:
            city = CITY_REGISTRY.get(ck)
            if city is None:
                continue
            for slug in upcoming_event_slugs(city, days_ahead=days_ahead):
                ev = discover_event(slug, ck, client=c)
                if ev is None:
                    continue
                if ev.end_ts <= now:
                    continue   # already ended
                events.append(ev)
    events.sort(key=lambda e: e.end_ts)
    return events


def _fetch_one_bucket(b, client: httpx.Client, fetch_no_book: bool) -> bool:
    """Worker for a single bucket. Returns True iff YES quotes are usable.

    Each worker writes only to its own `Bucket` instance (no contention) and
    issues one DB upsert via the thread-safe `update_market_quote`.
    """
    book = fetch_book(b.yes_token_id, client=client)
    yes_bid, yes_ask, yes_ask_usd = parse_book(book)
    b.yes_bid = yes_bid
    b.yes_ask = yes_ask
    b.depth_yes_ask_usd = yes_ask_usd
    yes_ok = yes_bid is not None and yes_ask is not None
    if yes_ok:
        b.yes_mid = (yes_bid + yes_ask) / 2.0
    update_market_quote(b.market_id, yes_bid, yes_ask)

    if fetch_no_book:
        no_book = fetch_book(b.no_token_id, client=client)
        no_bid, no_ask, no_ask_usd = parse_book(no_book)
        b.no_bid = no_bid
        b.no_ask = no_ask
        b.depth_no_ask_usd = no_ask_usd
        if no_bid is not None and no_ask is not None:
            b.no_mid = (no_bid + no_ask) / 2.0
    return yes_ok


def populate_quotes(event: WeatherEvent, *,
                    client: httpx.Client | None = None,
                    fetch_no_book: bool = False,
                    max_workers: int = 20) -> int:
    """For each bucket in `event`, fetch its YES order book and fill bid/ask/depth.

    When `fetch_no_book=True`, also fetches the NO-side book so the strategy
    can evaluate buying NO on over-priced buckets. Doubles the per-bucket HTTP
    calls — only enable when the strategy actually uses NO quotes.

    Buckets are fetched concurrently via a `ThreadPoolExecutor` capped at
    `max_workers`. The shared `httpx.Client` is thread-safe through its
    connection pool, and `update_market_quote` uses the thread-safe
    psycopg pool, so no external synchronization is needed.

    Returns the number of buckets with usable YES quotes.
    """
    own = client is None
    c = client or httpx.Client(timeout=10.0)
    try:
        buckets = list(event.buckets)
        if not buckets:
            return 0
        # Cap workers at the bucket count; spinning extra threads for an
        # event with 11 buckets is wasteful.
        workers = max(1, min(max_workers, len(buckets)))
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(
                lambda b: _fetch_one_bucket(b, c, fetch_no_book),
                buckets,
            ))
        return sum(1 for ok in results if ok)
    finally:
        if own:
            c.close()


def gamma_outcome(event: WeatherEvent, *,
                  client: httpx.Client | None = None) -> dict[str, float] | None:
    """Pull authoritative bucket-by-bucket outcomes from gamma after resolution.

    Returns a dict {bucket_label -> 1.0/0.0} or None if not yet resolved.
    A market is resolved when its `outcomePrices` is one of [1, 0] / [0, 1]
    AND any single bucket has `outcomePrices = [1, 0]` (i.e. settled YES).
    """
    own = client is None
    c = client or httpx.Client(timeout=10.0)
    try:
        resp = c.get(f"{GAMMA_API_URL}/events", params={"slug": event.slug})
        resp.raise_for_status()
        events = resp.json()
        if not events:
            return None
        out: dict[str, float] = {}
        any_settled = False
        for m in events[0].get("markets", []):
            label = m.get("groupItemTitle", "")
            op = m.get("outcomePrices")
            if isinstance(op, str):
                op = json.loads(op)
            if not op or len(op) < 2:
                return None
            yes = float(op[0])
            no = float(op[1])
            if (yes, no) in ((1.0, 0.0), (0.0, 1.0)):
                any_settled = True
            out[label] = yes
        return out if any_settled else None
    except Exception as exc:
        logger.warning("gamma_outcome_fetch_failed", slug=event.slug, error=str(exc)[:200])
        return None
    finally:
        if own:
            c.close()
