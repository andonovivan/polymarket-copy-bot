"""Tests for Path B helpers — DB-backed but no network calls."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from polymarket_bot.persistence.schema import init_db, get_conn
from polymarket_bot.research.weather_capture import (
    _record_obs,
    _recent_obs_exists,
)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh sqlite database for each test, isolated via BOT_DB_PATH."""
    db_path = tmp_path / "bot_state.db"
    monkeypatch.setenv("BOT_DB_PATH", str(db_path))
    # The schema module caches a singleton connection; reset between tests.
    import polymarket_bot.persistence.schema as schema_mod
    schema_mod._conn = None
    init_db(db_path)
    yield db_path
    schema_mod._conn = None


def test_record_obs_then_recent_exists_within_window(tmp_db):
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


def test_recent_exists_false_when_no_match(tmp_db):
    assert _recent_obs_exists("taipei", "any-slug", "22°C", within_seconds=600) is False


def test_recent_exists_respects_within_seconds(tmp_db):
    """An obs older than within_seconds should not satisfy the recency check."""
    conn = get_conn()
    old_ts = int(time.time()) - 3600   # 1 hour ago
    conn.execute(
        "INSERT INTO weather_research_obs "
        "(city_key, target_date, slug, bucket_label, model_p, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("moscow", "2026-05-10", "slug-m", "20°C", 0.5, old_ts),
    )
    conn.commit()
    assert _recent_obs_exists("moscow", "slug-m", "20°C", within_seconds=60) is False
    assert _recent_obs_exists("moscow", "slug-m", "20°C", within_seconds=7200) is True


def test_record_obs_handles_null_market_quotes(tmp_db):
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
    conn = get_conn()
    row = conn.execute(
        "SELECT model_p, market_yes_mid, market_yes_bid, market_yes_ask "
        "FROM weather_research_obs WHERE city_key='helsinki'"
    ).fetchone()
    assert row == (0.3, None, None, None)
