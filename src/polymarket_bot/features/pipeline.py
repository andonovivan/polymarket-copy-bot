"""Bar history → feature vector. Used identically by live and backtest.

v2 features add cross-asset (ETH), perp futures basis, and funding-rate context.
All features are aligned to the BTC bar's `open_time`. Aux data lookups use a
`FeatureContext` built once per training/run from the local cache.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from polymarket_bot.features.builders import atr, log_return, tod_bucket, zscore
from polymarket_bot.persistence.repo import (
    Bar,
    FundingPoint,
    load_eth_bars,
    load_funding,
    load_perp_bars,
)

INTERVAL_SECONDS = 5 * 60

# Feature ordering is part of the model contract — persisted with each model.
FEATURE_NAMES: list[str] = [
    # --- BTC-only (v1) ---
    "r_1",                # BTC log return, last bar
    "r_3",                # BTC log return, last 3 bars
    "r_12",               # BTC log return, last 12 bars (1h on 5m)
    "atr_pct",            # BTC ATR(14) / last close
    "vol_zscore",         # BTC volume z-score, last 50 bars
    "ret_zscore_30",      # BTC return z-score, last 30 bars
    "tod",                # time-of-day bucket (0..5, UTC)
    # --- v2 additions ---
    "eth_r_3",            # ETH log return, last 3 bars
    "eth_btc_diff_3",     # eth_r_3 − btc_r_3 (cross-asset divergence)
    "basis_pct",          # (perp_close − btc_close) / btc_close (futures basis)
    "funding_rate",       # most recent funding rate at-or-before bar.open_time
]

WARMUP_BARS = 60


@dataclass
class FeatureVector:
    values: np.ndarray
    timestamp: int

    def is_complete(self) -> bool:
        return bool(np.all(np.isfinite(self.values)))


@dataclass
class FeatureContext:
    """Aux data indexed for O(1) per-bar lookups during training / live."""

    eth_by_time: dict[int, Bar]
    perp_by_time: dict[int, Bar]
    funding_ts: np.ndarray            # sorted asc, int64
    funding_rates: np.ndarray         # parallel float64

    @classmethod
    def empty(cls) -> "FeatureContext":
        return cls({}, {},
                   np.array([], dtype=np.int64),
                   np.array([], dtype=float))

    @classmethod
    def load(cls, from_ts: int | None = None, to_ts: int | None = None) -> "FeatureContext":
        eth = load_eth_bars(from_ts, to_ts)
        perp = load_perp_bars(from_ts, to_ts)
        # Funding is published every 8h; pull a margin so the most-recent-before lookup
        # works at the start of the window (`from_ts` may sit between two funding events).
        funding_from = (from_ts - 86400) if from_ts is not None else None
        funding = load_funding(funding_from, to_ts)
        funding_sorted = sorted(funding, key=lambda f: f.funding_ts)
        return cls(
            eth_by_time={b.open_time: b for b in eth},
            perp_by_time={b.open_time: b for b in perp},
            funding_ts=np.array([f.funding_ts for f in funding_sorted], dtype=np.int64),
            funding_rates=np.array([f.rate for f in funding_sorted], dtype=float),
        )

    def funding_at(self, ts: int) -> float:
        """Most recent funding rate at-or-before `ts`. NaN if none."""
        if self.funding_ts.size == 0:
            return float("nan")
        idx = int(np.searchsorted(self.funding_ts, ts, side="right")) - 1
        if idx < 0:
            return float("nan")
        return float(self.funding_rates[idx])


def build_features(bars: list[Bar], ctx: FeatureContext | None = None) -> FeatureVector | None:
    """Build a feature vector describing the most recent closed BTC bar.

    Requires `ctx` to contain ETH bar @ same open_time, BTC perp bar @ same
    open_time, and a funding rate at-or-before `bar.open_time`. Returns None if
    any of those are missing or if BTC bar history is too short.
    """
    if len(bars) < WARMUP_BARS:
        return None
    if ctx is None:
        ctx = FeatureContext.empty()

    closes = np.array([b.c for b in bars], dtype=float)
    highs = np.array([b.h for b in bars], dtype=float)
    lows = np.array([b.l for b in bars], dtype=float)
    volumes = np.array([b.v for b in bars], dtype=float)
    rets = np.diff(np.log(np.clip(closes, 1e-12, None)))

    last = bars[-1]

    # --- BTC v1 features ---
    btc_r_1 = log_return(closes, 1)
    btc_r_3 = log_return(closes, 3)
    btc_r_12 = log_return(closes, 12)
    atr_pct = atr(highs, lows, closes, 14) / max(closes[-1], 1e-12)
    vol_z = zscore(volumes, 50)
    ret_z = zscore(rets, 30)
    tod = float(tod_bucket(last.open_time, buckets=6))

    # --- v2: ETH cross-asset ---
    eth_now = ctx.eth_by_time.get(last.open_time)
    eth_3 = ctx.eth_by_time.get(last.open_time - 3 * INTERVAL_SECONDS)
    if eth_now is None or eth_3 is None:
        return None
    if eth_now.c <= 0 or eth_3.c <= 0:
        return None
    eth_r_3 = math.log(eth_now.c / eth_3.c)
    eth_btc_diff_3 = eth_r_3 - (btc_r_3 if math.isfinite(btc_r_3) else 0.0)

    # --- v2: BTC perp basis ---
    perp_now = ctx.perp_by_time.get(last.open_time)
    if perp_now is None or perp_now.c <= 0:
        return None
    basis_pct = (perp_now.c - last.c) / last.c if last.c > 0 else float("nan")

    # --- v2: funding rate ---
    funding_rate = ctx.funding_at(last.open_time)

    vec = np.array([
        btc_r_1, btc_r_3, btc_r_12, atr_pct, vol_z, ret_z, tod,
        eth_r_3, eth_btc_diff_3, basis_pct, funding_rate,
    ], dtype=float)
    fv = FeatureVector(values=vec, timestamp=last.open_time)
    return fv if fv.is_complete() else None
