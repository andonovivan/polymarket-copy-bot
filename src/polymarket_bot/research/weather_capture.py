"""Path B: live capture of (model_p, market_p, outcome) for candidate cities.

Read-only — never places orders. Each tick, for cities NOT already in the
production CITY_REGISTRY, snapshots model probabilities and market quotes
into `weather_research_obs`. After each event resolves, `update_outcomes()`
fills in the won/lost flag.

The accumulated dataset is the ground truth for promoting cities into
CITY_REGISTRY: same model, same data sources, same closing-price methodology
as live trading — no leakage, no API mismatch with the backtest harness.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
import structlog

from polymarket_bot.backtest.weather_city_eval import CANDIDATES, geocode
from polymarket_bot.data.weather_feed import (
    CITY_REGISTRY, City, bucket_probabilities, get_ensemble,
)
from polymarket_bot.persistence.schema import get_conn
from polymarket_bot.polymarket.weather_markets import (
    discover_event, gamma_outcome, populate_quotes, upcoming_event_slugs,
)

logger = structlog.get_logger()


_CANDIDATE_REGISTRY: dict[str, City] = {}


def _candidate_registry() -> dict[str, City]:
    """Lazily build a City entry for each non-CITY_REGISTRY candidate."""
    if _CANDIDATE_REGISTRY:
        return _CANDIDATE_REGISTRY
    for slug in CANDIDATES:
        if slug in CITY_REGISTRY:
            continue
        geo = geocode(slug)
        if geo is None:
            logger.warning("research_geocode_failed", city=slug)
            continue
        _CANDIDATE_REGISTRY[slug] = City(
            key=slug, lat=geo.lat, lon=geo.lon, tz=geo.tz,
            unit="celsius",
            event_slug_prefix=f"highest-temperature-in-{slug}-on-",
        )
    if _CANDIDATE_REGISTRY:
        logger.info("research_candidates_loaded",
                    count=len(_CANDIDATE_REGISTRY),
                    cities=sorted(_CANDIDATE_REGISTRY.keys()))
    return _CANDIDATE_REGISTRY


def _recent_obs_exists(city_key: str, slug: str, bucket_label: str,
                       within_seconds: int) -> bool:
    cutoff = int(time.time()) - within_seconds
    row = get_conn().execute(
        "SELECT 1 FROM weather_research_obs "
        "WHERE city_key=? AND slug=? AND bucket_label=? AND observed_at >= ? "
        "LIMIT 1",
        (city_key, slug, bucket_label, cutoff),
    ).fetchone()
    return row is not None


def _record_obs(city_key: str, target_date: str, slug: str, bucket_label: str,
                model_p: float, mid: float | None, bid: float | None,
                ask: float | None) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO weather_research_obs "
        "(city_key, target_date, slug, bucket_label, model_p, "
        " market_yes_mid, market_yes_bid, market_yes_ask, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (city_key, target_date, slug, bucket_label, model_p,
         mid, bid, ask, int(time.time())),
    )
    conn.commit()


def capture_observations(*, window_seconds: int = 3600,
                         dedupe_seconds: int = 600,
                         days_ahead: int = 4) -> int:
    """Snapshot candidate-city events settling within the next `window_seconds`.

    Returns the number of new obs rows written.
    """
    registry = _candidate_registry()
    if not registry:
        return 0
    now = int(datetime.now(timezone.utc).timestamp())
    horizon = now + window_seconds
    n_written = 0

    with httpx.Client(timeout=10.0) as c:
        for ck, city in registry.items():
            for slug in upcoming_event_slugs(city, days_ahead=days_ahead):
                ev = discover_event(slug, ck, client=c)
                if ev is None or ev.end_ts <= now or ev.end_ts > horizon:
                    continue
                populate_quotes(ev, client=c)
                target_date = datetime.fromtimestamp(
                    ev.resolution_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                forecast = get_ensemble(city, target_date)
                if forecast is None or not forecast.members:
                    continue
                labels = [b.label for b in ev.buckets]
                probs = bucket_probabilities(forecast.members, labels)
                for b in ev.buckets:
                    if _recent_obs_exists(ck, ev.slug, b.label, dedupe_seconds):
                        continue
                    _record_obs(
                        ck, target_date, ev.slug, b.label,
                        probs.get(b.label, 0.0),
                        b.yes_mid, b.yes_bid, b.yes_ask,
                    )
                    n_written += 1

    if n_written:
        logger.info("research_captured", obs_written=n_written)
    return n_written


def update_outcomes() -> int:
    """Backfill `outcome` on rows whose target_date has passed.

    Returns the number of rows updated.
    """
    conn = get_conn()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT DISTINCT city_key, slug FROM weather_research_obs "
        "WHERE outcome IS NULL AND target_date < ?",
        (today,),
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
            for label, yes in outcomes.items():
                won = 1 if yes == 1.0 else 0
                cur = conn.execute(
                    "UPDATE weather_research_obs "
                    "SET outcome=?, settled_at=? "
                    "WHERE slug=? AND bucket_label=? AND outcome IS NULL",
                    (won, now, slug, label),
                )
                n_updated += cur.rowcount
            conn.commit()
    if n_updated:
        logger.info("research_outcomes_updated", rows=n_updated)
    return n_updated
