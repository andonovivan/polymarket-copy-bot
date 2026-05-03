"""Pure-numpy feature builders. Same code path is used live and in backtest."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np


def log_return(closes: np.ndarray, lookback: int) -> float:
    """Log return over the most recent `lookback` bars. NaN if insufficient data."""
    if closes.size <= lookback or lookback < 1:
        return float("nan")
    a = float(closes[-1])
    b = float(closes[-1 - lookback])
    if a <= 0 or b <= 0:
        return float("nan")
    return float(np.log(a / b))


def true_range(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> np.ndarray:
    """Wilder's True Range series (length N-1 vs inputs of length N)."""
    if highs.size < 2:
        return np.array([])
    h = highs[1:]
    l = lows[1:]
    pc = closes[:-1]
    return np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])


def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    """Average True Range over the last `period` bars. NaN if insufficient data."""
    tr = true_range(highs, lows, closes)
    if tr.size < period:
        return float("nan")
    return float(np.mean(tr[-period:]))


def ema(values: np.ndarray, period: int) -> float:
    """Exponential moving average evaluated at the last point. NaN if insufficient data."""
    if values.size < period or period < 1:
        return float("nan")
    alpha = 2.0 / (period + 1.0)
    e = float(values[-period])
    for v in values[-period + 1:]:
        e = alpha * float(v) + (1 - alpha) * e
    return e


def zscore(values: np.ndarray, lookback: int) -> float:
    """Z-score of the last value vs the prior `lookback` values."""
    if values.size <= lookback or lookback < 2:
        return float("nan")
    window = values[-lookback - 1:-1]
    mu = float(np.mean(window))
    sd = float(np.std(window))
    if sd == 0.0:
        return 0.0
    return (float(values[-1]) - mu) / sd


def tod_bucket(unix_ts: int, buckets: int = 6) -> int:
    """Time-of-day bucket index in [0, buckets). 24h split into N equal slices, UTC."""
    if buckets <= 0:
        raise ValueError("buckets must be positive")
    dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    minutes = dt.hour * 60 + dt.minute
    width = (24 * 60) // buckets
    return min(buckets - 1, minutes // width)
