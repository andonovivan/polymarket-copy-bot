"""Tests for Path B helpers — DB-backed but no network calls."""

from __future__ import annotations

import time

from polymarket_bot.persistence.schema import get_pool
from polymarket_bot.research.weather_capture import (
    _record_obs,
    _recent_obs_exists,
)


def test_record_obs_then_recent_exists_within_window():
    _record_obs(
        city_key="taipei",
        target_date="2026-05-10",
        slug="highest-temperature-in-taipei-on-may-10-2026",
        bucket_label="22°C",
        model_p=0.4,
        model_day_max_mean=22.5,
        mid=0.35, bid=0.34, ask=0.36,
    )
    assert _recent_obs_exists(
        "taipei",
        "highest-temperature-in-taipei-on-may-10-2026",
        "22°C",
        within_seconds=600,
    ) is True


def test_recent_exists_false_when_no_match():
    assert _recent_obs_exists("taipei", "any-slug", "22°C", within_seconds=600) is False


def test_recent_exists_respects_within_seconds():
    """An obs older than within_seconds should not satisfy the recency check."""
    old_ts = int(time.time()) - 3600   # 1 hour ago
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO weather_research_obs "
            "(city_key, target_date, slug, bucket_label, model_p, observed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("moscow", "2026-05-10", "slug-m", "20°C", 0.5, old_ts),
        )
    assert _recent_obs_exists("moscow", "slug-m", "20°C", within_seconds=60) is False
    assert _recent_obs_exists("moscow", "slug-m", "20°C", within_seconds=7200) is True


def test_record_obs_handles_null_market_quotes():
    """Empty order books → mid/bid/ask all None; the row should still write."""
    _record_obs(
        city_key="helsinki",
        target_date="2026-05-10",
        slug="slug-h",
        bucket_label="15°C",
        model_p=0.3,
        model_day_max_mean=None,
        mid=None, bid=None, ask=None,
    )
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT model_p, market_yes_mid, market_yes_bid, market_yes_ask "
            "FROM weather_research_obs WHERE city_key='helsinki'"
        ).fetchone()
    assert row == (0.3, None, None, None)
