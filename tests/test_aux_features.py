"""Cross-asset / basis / funding alignment in the v2 feature pipeline."""

from __future__ import annotations

import math

import numpy as np

from polymarket_bot.features.pipeline import (
    FEATURE_NAMES,
    WARMUP_BARS,
    FeatureContext,
    build_features,
)
from polymarket_bot.persistence.repo import Bar


def _btc_bars(n: int, base: float = 50_000.0, step: float = 1.0) -> list[Bar]:
    """Synthetic BTC bars with monotonic close — simple but deterministic."""
    return [
        Bar(open_time=1_700_000_000 + i * 300,
            o=base + i * step, h=base + i * step + 5,
            l=base + i * step - 5, c=base + i * step,
            v=100.0 + i)
        for i in range(n)
    ]


def _eth_bars_for(btc: list[Bar], scale: float = 0.06, step: float = 0.05) -> list[Bar]:
    """ETH bars on the same timestamps as BTC, mostly tracking BTC."""
    return [
        Bar(open_time=b.open_time,
            o=b.c * scale, h=b.c * scale + 1, l=b.c * scale - 1,
            c=b.c * scale + i * step, v=10.0 + i)
        for i, b in enumerate(btc)
    ]


def _perp_bars_for(btc: list[Bar], premium_pct: float = 0.0005) -> list[Bar]:
    """Perp bars at a constant premium over spot (simulates a +5 bps basis)."""
    return [
        Bar(open_time=b.open_time,
            o=b.o * (1 + premium_pct), h=b.h * (1 + premium_pct),
            l=b.l * (1 + premium_pct), c=b.c * (1 + premium_pct),
            v=b.v)
        for b in btc
    ]


def _ctx(eth: list[Bar], perp: list[Bar],
         funding_points: list[tuple[int, float]] | None = None) -> FeatureContext:
    funding_points = funding_points or []
    return FeatureContext(
        eth_by_time={b.open_time: b for b in eth},
        perp_by_time={b.open_time: b for b in perp},
        funding_ts=np.array([t for t, _ in funding_points], dtype=np.int64),
        funding_rates=np.array([r for _, r in funding_points], dtype=float),
    )


def test_feature_count_matches_names():
    btc = _btc_bars(WARMUP_BARS + 5)
    ctx = _ctx(_eth_bars_for(btc), _perp_bars_for(btc),
               funding_points=[(1_700_000_000 - 1, 0.0001)])
    fv = build_features(btc, ctx)
    assert fv is not None
    assert fv.values.shape == (len(FEATURE_NAMES),)
    assert "eth_r_3" in FEATURE_NAMES
    assert "basis_pct" in FEATURE_NAMES
    assert "funding_rate" in FEATURE_NAMES


def test_basis_zero_when_perp_equals_spot():
    btc = _btc_bars(WARMUP_BARS + 5)
    perp_eq = [Bar(b.open_time, b.o, b.h, b.l, b.c, b.v) for b in btc]
    ctx = _ctx(_eth_bars_for(btc), perp_eq, funding_points=[(1_700_000_000 - 1, 0.0)])
    fv = build_features(btc, ctx)
    assert fv is not None
    basis_idx = FEATURE_NAMES.index("basis_pct")
    assert math.isclose(fv.values[basis_idx], 0.0, abs_tol=1e-12)


def test_basis_positive_when_perp_above_spot():
    btc = _btc_bars(WARMUP_BARS + 5)
    ctx = _ctx(_eth_bars_for(btc), _perp_bars_for(btc, premium_pct=0.001),
               funding_points=[(1_700_000_000 - 1, 0.0)])
    fv = build_features(btc, ctx)
    basis_idx = FEATURE_NAMES.index("basis_pct")
    assert fv.values[basis_idx] > 0.0


def test_returns_none_when_eth_missing_at_bar_time():
    btc = _btc_bars(WARMUP_BARS + 5)
    ctx = _ctx(eth=[], perp=_perp_bars_for(btc), funding_points=[(1_700_000_000 - 1, 0.0)])
    assert build_features(btc, ctx) is None


def test_returns_none_when_perp_missing():
    btc = _btc_bars(WARMUP_BARS + 5)
    ctx = _ctx(eth=_eth_bars_for(btc), perp=[], funding_points=[(1_700_000_000 - 1, 0.0)])
    assert build_features(btc, ctx) is None


def test_funding_lookup_uses_most_recent_at_or_before():
    btc = _btc_bars(WARMUP_BARS + 5)
    last_t = btc[-1].open_time
    funding = [
        (last_t - 86400, 0.0002),
        (last_t - 3600, 0.0005),       # most recent before last_t — should be picked
        (last_t + 7200, 0.001),        # in the future — must be ignored
    ]
    ctx = _ctx(_eth_bars_for(btc), _perp_bars_for(btc), funding_points=funding)
    fv = build_features(btc, ctx)
    funding_idx = FEATURE_NAMES.index("funding_rate")
    assert math.isclose(fv.values[funding_idx], 0.0005, rel_tol=1e-9)


def test_funding_nan_when_only_future_points():
    btc = _btc_bars(WARMUP_BARS + 5)
    ctx = _ctx(_eth_bars_for(btc), _perp_bars_for(btc),
               funding_points=[(btc[-1].open_time + 1, 0.0001)])
    # build_features's is_complete() check rejects NaN funding — returns None.
    assert build_features(btc, ctx) is None
