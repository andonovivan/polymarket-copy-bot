"""Pure-logic tests for the Path A backtest harness — no network calls."""

from __future__ import annotations

from polymarket_bot.backtest.weather_city_eval import (
    _kelly_fraction,
    _slug_for,
    fetch_yes_price_at,
    _PRICE_HISTORY_CACHE,
)
from datetime import datetime


def test_slug_for_uses_lowercase_month_and_unpadded_day():
    s = _slug_for("paris", datetime(2026, 4, 4))
    assert s == "highest-temperature-in-paris-on-april-4-2026"
    s = _slug_for("tokyo", datetime(2026, 12, 9))
    assert s == "highest-temperature-in-tokyo-on-december-9-2026"


def test_kelly_fraction_zero_when_no_edge():
    # p == market_p → no edge → no bet
    assert _kelly_fraction(p=0.5, market_p=0.5, kelly=0.25, max_pct=0.05) == 0.0


def test_kelly_fraction_zero_at_invalid_prices():
    assert _kelly_fraction(p=0.5, market_p=0.0, kelly=0.25, max_pct=0.05) == 0.0
    assert _kelly_fraction(p=0.5, market_p=1.0, kelly=0.25, max_pct=0.05) == 0.0
    assert _kelly_fraction(p=0.0, market_p=0.5, kelly=0.25, max_pct=0.05) == 0.0
    assert _kelly_fraction(p=1.0, market_p=0.5, kelly=0.25, max_pct=0.05) == 0.0


def test_kelly_fraction_caps_at_max_pct():
    # Massive edge — should clamp to max_pct.
    f = _kelly_fraction(p=0.99, market_p=0.10, kelly=1.0, max_pct=0.05)
    assert f == 0.05


def test_kelly_fraction_scales_by_kelly_multiplier():
    # Exact: f_full = (b·p − q)/b, b = (1−market_p)/market_p
    # p=0.6, market_p=0.5 → b=1, f_full = 1·0.6 − 0.4 = 0.2
    # kelly=0.25 → 0.05; max_pct=0.10 → returns 0.05
    f = _kelly_fraction(p=0.6, market_p=0.5, kelly=0.25, max_pct=0.10)
    assert abs(f - 0.05) < 1e-9


def test_fetch_yes_price_at_returns_latest_at_or_before():
    # Seed the cache directly so we exercise the picker without HTTP.
    token = "tok-test-1"
    _PRICE_HISTORY_CACHE[token] = [
        (1000, 0.10),
        (2000, 0.20),
        (3000, 0.30),
        (4000, 0.40),
    ]
    try:
        assert fetch_yes_price_at(token, 999) is None    # before first sample
        assert fetch_yes_price_at(token, 1000) == 0.10   # exact match
        assert fetch_yes_price_at(token, 2500) == 0.20   # between 2000 and 3000
        assert fetch_yes_price_at(token, 5000) == 0.40   # past last sample
    finally:
        _PRICE_HISTORY_CACHE.pop(token, None)


def test_fetch_yes_price_at_empty_history():
    token = "tok-empty"
    _PRICE_HISTORY_CACHE[token] = []
    try:
        assert fetch_yes_price_at(token, 1234) is None
    finally:
        _PRICE_HISTORY_CACHE.pop(token, None)
