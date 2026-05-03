"""Bar history → feature vector. Used identically by live and backtest."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from polymarket_bot.features.builders import atr, log_return, tod_bucket, zscore
from polymarket_bot.persistence.repo import Bar

FEATURE_NAMES: list[str] = [
    "r_1",            # log return last bar
    "r_3",            # log return last 3 bars
    "r_12",           # log return last 12 bars (1h on 5m)
    "atr_pct",        # ATR(14) / last close
    "vol_zscore",     # volume z-score over last 50 bars
    "ret_zscore_30",  # return z-score over last 30 bars (mean reversion proxy)
    "tod",            # time-of-day bucket (0..5)
]

WARMUP_BARS = 60  # need this many bars before we can build a complete feature vector


@dataclass
class FeatureVector:
    values: np.ndarray            # shape (len(FEATURE_NAMES),)
    timestamp: int                # bar.open_time of the bar these features describe

    def is_complete(self) -> bool:
        return bool(np.all(np.isfinite(self.values)))


def build_features(bars: list[Bar]) -> FeatureVector | None:
    """Return a feature vector describing the most recent closed bar.

    Returns None if there isn't enough history to compute every feature.
    """
    if len(bars) < WARMUP_BARS:
        return None
    closes = np.array([b.c for b in bars], dtype=float)
    highs = np.array([b.h for b in bars], dtype=float)
    lows = np.array([b.l for b in bars], dtype=float)
    volumes = np.array([b.v for b in bars], dtype=float)
    rets = np.diff(np.log(np.clip(closes, 1e-12, None)))

    last = bars[-1]
    vec = np.array([
        log_return(closes, 1),
        log_return(closes, 3),
        log_return(closes, 12),
        atr(highs, lows, closes, 14) / max(closes[-1], 1e-12),
        zscore(volumes, 50),
        zscore(rets, 30),
        float(tod_bucket(last.open_time, buckets=6)),
    ], dtype=float)
    fv = FeatureVector(values=vec, timestamp=last.open_time)
    return fv if fv.is_complete() else None
