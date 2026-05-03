"""Feature-builder sanity checks."""

from __future__ import annotations

import math

import numpy as np

from polymarket_bot.features.builders import atr, ema, log_return, tod_bucket, zscore


def test_log_return_basic():
    closes = np.array([100.0, 110.0])
    assert math.isclose(log_return(closes, 1), math.log(110 / 100), rel_tol=1e-9)


def test_log_return_insufficient_history_is_nan():
    assert math.isnan(log_return(np.array([100.0]), 1))


def test_atr_constant_range():
    # If true range is constant N, ATR = N.
    n = 50
    highs = np.linspace(100, 100, n)
    lows = np.linspace(99, 99, n)
    closes = np.linspace(99.5, 99.5, n)
    # All true ranges equal 1 (high-low). ATR(14) = 1.0.
    assert math.isclose(atr(highs, lows, closes, period=14), 1.0, rel_tol=1e-9)


def test_zscore_zero_for_constant_series():
    vals = np.array([1.0] * 50 + [1.0])
    assert zscore(vals, lookback=30) == 0.0


def test_zscore_positive_for_above_mean_value():
    rng = np.random.default_rng(0)
    vals = np.concatenate([rng.normal(0, 1, size=30), np.array([5.0])])
    z = zscore(vals, lookback=30)
    assert z > 0


def test_ema_collapses_to_input_when_constant():
    vals = np.array([2.0] * 30)
    assert math.isclose(ema(vals, period=10), 2.0, rel_tol=1e-9)


def test_tod_bucket_in_range():
    # 24h split into 6 buckets = 4h each; midnight UTC ⇒ bucket 0.
    assert tod_bucket(0, buckets=6) == 0
    # 23:59 UTC of the same day ⇒ last bucket.
    assert tod_bucket(86340, buckets=6) == 5
