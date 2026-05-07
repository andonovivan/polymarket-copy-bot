"""Path B: live capture of (model_p, model_day_max_mean, market_p, outcome).

Read-only — never places orders. Each tick, for every city the bot knows
about (production CITY_REGISTRY + candidate list), snapshots:

  * `model_p`              — bucket probability from the live ensemble
  * `model_day_max_mean`   — ensemble mean day-max (for bias correction)
  * `market_yes_mid/bid/ask` — current order-book snapshot

into `weather_research_obs`. After each event resolves, `update_outcomes()`
fills in the won/lost flag.

The accumulated dataset serves two purposes:

  1. **City promotion** — same model, same data sources, same methodology
     as live trading; no leakage. Use to confirm Path A's directional
     signal before promoting a city to CITY_REGISTRY.
  2. **Strategy calibration** — `strategy/calibration.py` reads back from
     this table to compute per-city bias corrections (#1) and isotonic
     probability calibrators (#2).
"""

from __future__ import annotations

import dataclasses
import time
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from polymarket_bot.backtest.weather_city_eval import CANDIDATES, geocode
from polymarket_bot.data.weather_feed import (
    CITY_REGISTRY, City, bucket_probabilities, get_ensemble,
)
from polymarket_bot.persistence.schema import get_pool
from polymarket_bot.polymarket.weather_markets import (
    discover_event, gamma_outcome, populate_quotes,
)

logger = structlog.get_logger()


# Registry cache. Built lazily on first capture call, persists for process
# lifetime. Includes candidates only when `include_candidates=True`.
_CAPTURE_REGISTRY: dict[str, City] = {}
_REGISTRY_INCLUDED_CANDIDATES: bool = False


def _capture_registry(include_candidates: bool = False) -> dict[str, City]:
    """Cities to capture.

    Always includes production `CITY_REGISTRY` (we want bias + calibration
    derived from the cities we actually trade). Optionally also includes the
    lazily-geocoded candidate set, for future city promotion.

    Capturing production cities feeds the calibration layer with the SAME
    model that's actively trading — no leakage, no API mismatch with the
    backtest harness.
    """
    global _REGISTRY_INCLUDED_CANDIDATES
    if _CAPTURE_REGISTRY and _REGISTRY_INCLUDED_CANDIDATES == include_candidates:
        return _CAPTURE_REGISTRY
    # Rebuild — flag may have flipped on restart.
    _CAPTURE_REGISTRY.clear()
    for key, city in CITY_REGISTRY.items():
        _CAPTURE_REGISTRY[key] = city
    if include_candidates:
        # Geocode lazily; default to celsius (per-event detection in the
        # capture loop adapts if a market shows up in °F).
        for slug in CANDIDATES:
            if slug in _CAPTURE_REGISTRY:
                continue
            geo = geocode(slug)
            if geo is None:
                logger.warning("research_geocode_failed", city=slug)
                continue
            _CAPTURE_REGISTRY[slug] = City(
                key=slug, lat=geo.lat, lon=geo.lon, tz=geo.tz,
                unit="celsius",
                event_slug_prefix=f"highest-temperature-in-{slug}-on-",
            )
    _REGISTRY_INCLUDED_CANDIDATES = include_candidates
    if _CAPTURE_REGISTRY:
        logger.info("research_registry_loaded",
                    count=len(_CAPTURE_REGISTRY),
                    include_candidates=include_candidates,
                    cities=sorted(_CAPTURE_REGISTRY.keys()))
    return _CAPTURE_REGISTRY


def _recent_obs_exists(city_key: str, slug: str, bucket_label: str,
                       within_seconds: int) -> bool:
    cutoff = int(time.time()) - within_seconds
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM weather_research_obs "
            "WHERE city_key=%s AND slug=%s AND bucket_label=%s AND observed_at >= %s "
            "LIMIT 1",
            (city_key, slug, bucket_label, cutoff),
        ).fetchone()
    return row is not None


def _record_obs(city_key: str, target_date: str, slug: str, bucket_label: str,
                model_p: float, model_day_max_mean: float | None,
                mid: float | None, bid: float | None,
                ask: float | None) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO weather_research_obs "
            "(city_key, target_date, slug, bucket_label, model_p, "
            " model_day_max_mean, market_yes_mid, market_yes_bid, "
            " market_yes_ask, observed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (city_key, target_date, slug, bucket_label, model_p,
             model_day_max_mean, mid, bid, ask, int(time.time())),
        )


# Polymarket weather events resolve around noon UTC on their target date
# (verified empirically: e.g. paris-april-4 → endDate 2026-04-04T12:00:00Z).
# Used to pre-filter slugs so we don't gamma-fetch events that obviously
# can't be in our capture window. ±1 day margin absorbs any wobble.
_EXPECTED_END_HOUR_UTC = 12
_PREFILTER_MARGIN_SECONDS = 86400


def _expected_end_ts(slug_date: datetime) -> int:
    return int(slug_date.replace(
        hour=_EXPECTED_END_HOUR_UTC, minute=0, second=0, microsecond=0,
    ).timestamp())


def capture_observations(*, window_seconds: int = 3600,
                         dedupe_seconds: int = 600,
                         days_ahead: int = 4,
                         include_candidates: bool = False) -> int:
    """Snapshot capture-registry events settling within the next `window_seconds`.

    Returns the number of new obs rows written.
    """
    registry = _capture_registry(include_candidates=include_candidates)
    if not registry:
        return 0
    now = int(datetime.now(timezone.utc).timestamp())
    horizon = now + window_seconds
    today_utc = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    n_written = 0

    with httpx.Client(timeout=10.0) as c:
        for ck, city in registry.items():
            for i in range(days_ahead):
                slug_date = today_utc + timedelta(days=i)
                expected_end = _expected_end_ts(slug_date)
                # Cheap pre-filter: skip slugs whose expected resolution is
                # well outside the capture window. Saves a gamma fetch per
                # candidate-city per day-out-of-window.
                if expected_end < now - _PREFILTER_MARGIN_SECONDS:
                    continue
                if expected_end > horizon + _PREFILTER_MARGIN_SECONDS:
                    continue
                month = slug_date.strftime("%B").lower()
                slug = f"{city.event_slug_prefix}{month}-{slug_date.day}-{slug_date.year}"
                ev = discover_event(slug, ck, client=c)
                if ev is None or ev.end_ts <= now or ev.end_ts > horizon:
                    continue
                populate_quotes(ev, client=c)
                target_date = datetime.fromtimestamp(
                    ev.resolution_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                # Use the event's actual unit (parsed from bucket labels) so we
                # never feed a °C ensemble into °F bucket comparisons.
                ensemble_city = (city if ev.unit == city.unit
                                 else dataclasses.replace(city, unit=ev.unit))
                if ev.unit != city.unit:
                    logger.warning("research_unit_mismatch",
                                   city=ck, registry_unit=city.unit,
                                   event_unit=ev.unit)
                forecast = get_ensemble(ensemble_city, target_date)
                if forecast is None or not forecast.members:
                    continue
                labels = [b.label for b in ev.buckets]
                probs = bucket_probabilities(forecast.members, labels)
                day_max_mean = (sum(forecast.members) / len(forecast.members)
                                if forecast.members else None)
                for b in ev.buckets:
                    if _recent_obs_exists(ck, ev.slug, b.label, dedupe_seconds):
                        continue
                    _record_obs(
                        ck, target_date, ev.slug, b.label,
                        probs.get(b.label, 0.0), day_max_mean,
                        b.yes_mid, b.yes_bid, b.yes_ask,
                    )
                    n_written += 1

    if n_written:
        logger.info("research_captured", obs_written=n_written)
    return n_written


UNRESOLVED_GIVE_UP_DAYS = 30


def update_outcomes() -> int:
    """Backfill `outcome` on rows whose target_date has passed.

    Skips events older than `UNRESOLVED_GIVE_UP_DAYS` to avoid re-fetching
    cancelled / delisted markets indefinitely.

    Returns the number of rows updated.
    """
    now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime("%Y-%m-%d")
    floor = (now_utc - timedelta(days=UNRESOLVED_GIVE_UP_DAYS)).strftime("%Y-%m-%d")
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT city_key, slug FROM weather_research_obs "
            "WHERE outcome IS NULL AND target_date < %s AND target_date >= %s",
            (today, floor),
        ).fetchall()
    if not rows:
        return 0

    n_updated = 0
    now = int(time.time())
    with httpx.Client(timeout=10.0) as c:
        for city_key, slug in rows:
            ev = discover_event(slug, city_key, client=c)
            if ev is None:
                continue
            outcomes = gamma_outcome(ev, client=c)
            if outcomes is None:
                continue
            with get_pool().connection() as conn:
                for label, yes in outcomes.items():
                    won = 1 if yes == 1.0 else 0
                    cur = conn.execute(
                        "UPDATE weather_research_obs "
                        "SET outcome=%s, settled_at=%s "
                        "WHERE slug=%s AND bucket_label=%s AND outcome IS NULL",
                        (won, now, slug, label),
                    )
                    n_updated += cur.rowcount
    if n_updated:
        logger.info("research_outcomes_updated", rows=n_updated)
    return n_updated
