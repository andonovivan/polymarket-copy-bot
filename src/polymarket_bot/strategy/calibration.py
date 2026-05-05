"""Per-city probability calibration on top of the raw ensemble model.

Two layers, applied in order inside `_attach_model_probabilities`:

  1. **Bias correction (#1).** `compute_city_bias(city_key)` returns the
     median signed error `(model_day_max_mean − actual_day_max)` over recent
     settled events from `weather_research_obs`. The caller subtracts this
     from each ensemble member before bucketing, shifting probability mass
     into the buckets that actually occur.

  2. **Isotonic calibration (#2).** `compute_city_calibrator(city_key)`
     fits an isotonic regression `(model_p, won) → calibrated_p` over recent
     bucket-level observations from the same table. Returns a callable that
     maps any model probability into a calibrated probability.

Both layers are pass-through (no-op) when there isn't enough data — they
return `0.0` bias and `None` calibrator respectively. The bot keeps trading
the raw ensemble until 30+ events accumulate, at which point the corrections
kick in automatically.

Recent fits are cached in-process for `CACHE_TTL_SECONDS` to keep tick-loop
overhead near zero.
"""

from __future__ import annotations

import re
import time
from typing import Callable

import structlog
from sklearn.isotonic import IsotonicRegression

from polymarket_bot.persistence.schema import get_conn, lock

logger = structlog.get_logger()

# How long a fitted bias / calibrator stays in memory before re-querying. The
# DB reads and isotonic fit are cheap, but doing them on every tick is waste.
CACHE_TTL_SECONDS = 3600

# Minimum settled events required before we trust a city's bias correction.
# 10 is small but keeps the median resistant to one-off swings.
MIN_BIAS_EVENTS = 10

# Minimum bucket-level observations required before we trust an isotonic
# calibrator. ~10 events × 11 buckets ≈ 110 obs.
MIN_CALIBRATION_OBS = 110

DEFAULT_LOOKBACK_DAYS = 30


_BIAS_CACHE: dict[str, tuple[float, float]] = {}            # key → (fitted_at, bias)
_CALIBRATOR_CACHE: dict[str, tuple[float, "IsotonicRegression | None"]] = {}


# ---------------------------------------------------------------------------
# Bucket label → temperature midpoint (for resolving the actual day-max from
# the winning bucket). Buckets are integer-degree windows; the midpoint of an
# "or below" or "or higher" bucket falls back to its threshold.
# ---------------------------------------------------------------------------

def bucket_temp_midpoint(label: str) -> float | None:
    """Return the representative temperature for a bucket label, or None."""
    if not label:
        return None
    nums = [int(x) for x in re.findall(r"(\d+)", label)]
    if not nums:
        return None
    if "≤" in label or "or below" in label:
        return float(nums[0])
    if "≥" in label or "or higher" in label or "or above" in label:
        return float(nums[0])
    if len(nums) == 1:
        return float(nums[0])
    return (nums[0] + nums[1]) / 2.0


# ---------------------------------------------------------------------------
# #1 — Bias correction
# ---------------------------------------------------------------------------

def _query_bias_samples(city_key: str, lookback_days: int) -> list[float]:
    """Fetch (model_day_max_mean − actual_day_max) per settled event."""
    conn = get_conn()
    cutoff = int(time.time()) - lookback_days * 86400
    with lock():
        rows = conn.execute(
            "SELECT slug, observed_at, model_day_max_mean, bucket_label "
            "FROM weather_research_obs "
            "WHERE city_key=? AND outcome=1 AND model_day_max_mean IS NOT NULL "
            "  AND observed_at >= ? ",
            (city_key, cutoff),
        ).fetchall()
    # One observation per (event, winning bucket) row. The winning bucket's
    # midpoint approximates the actual day-max with bucket-resolution noise.
    samples: list[float] = []
    for slug, _ts, model_mean, label in rows:
        actual = bucket_temp_midpoint(label)
        if actual is None or model_mean is None:
            continue
        samples.append(float(model_mean) - actual)
    return samples


def compute_city_bias(city_key: str, *,
                      lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                      min_events: int = MIN_BIAS_EVENTS) -> float:
    """Median (model − actual) day-max bias for the city, or 0.0 if insufficient."""
    samples = _query_bias_samples(city_key, lookback_days)
    if len(samples) < min_events:
        return 0.0
    samples.sort()
    n = len(samples)
    return samples[n // 2] if n % 2 else (samples[n // 2 - 1] + samples[n // 2]) / 2


def get_city_bias(city_key: str) -> float:
    """Cached `compute_city_bias`. Returns 0.0 when there isn't enough history."""
    now = time.time()
    cached = _BIAS_CACHE.get(city_key)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    bias = compute_city_bias(city_key)
    _BIAS_CACHE[city_key] = (now, bias)
    if abs(bias) > 0:
        logger.info("city_bias_fitted", city=city_key, bias=round(bias, 2))
    return bias


def apply_bias_correction(members: list[int], bias: float) -> list[int]:
    """Shift each ensemble member by `-bias` (rounded back to int)."""
    if bias == 0.0:
        return members
    return [int(round(m - bias)) for m in members]


# ---------------------------------------------------------------------------
# #2 — Isotonic probability calibration
# ---------------------------------------------------------------------------

def _query_calibration_samples(city_key: str,
                               lookback_days: int) -> tuple[list[float], list[int]]:
    """Return (model_p, won) pairs for settled bucket observations."""
    conn = get_conn()
    cutoff = int(time.time()) - lookback_days * 86400
    with lock():
        rows = conn.execute(
            "SELECT model_p, outcome FROM weather_research_obs "
            "WHERE city_key=? AND outcome IS NOT NULL AND observed_at >= ? ",
            (city_key, cutoff),
        ).fetchall()
    xs: list[float] = []
    ys: list[int] = []
    for p, won in rows:
        if p is None or won is None:
            continue
        xs.append(float(p))
        ys.append(int(won))
    return xs, ys


def compute_city_calibrator(city_key: str, *,
                            lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                            min_obs: int = MIN_CALIBRATION_OBS,
                            ) -> "IsotonicRegression | None":
    """Fit an isotonic `model_p → calibrated_p` map for one city.

    Returns None if there aren't enough labelled observations yet.
    """
    xs, ys = _query_calibration_samples(city_key, lookback_days)
    if len(xs) < min_obs:
        return None
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(xs, ys)
    return iso


def get_city_calibrator(city_key: str) -> "IsotonicRegression | None":
    """Cached `compute_city_calibrator`. Returns None when insufficient data."""
    now = time.time()
    cached = _CALIBRATOR_CACHE.get(city_key)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    cal = compute_city_calibrator(city_key)
    _CALIBRATOR_CACHE[city_key] = (now, cal)
    if cal is not None:
        logger.info("city_calibrator_fitted", city=city_key)
    return cal


def apply_calibration(probs: dict[str, float],
                      calibrator: "IsotonicRegression | None",
                      ) -> dict[str, float]:
    """Map each bucket probability through the calibrator (no-op if None)."""
    if calibrator is None or not probs:
        return probs
    labels = list(probs.keys())
    xs = [probs[k] for k in labels]
    ys = calibrator.predict(xs)
    return {labels[i]: float(ys[i]) for i in range(len(labels))}


def reset_caches() -> None:
    """Clear the in-memory caches. For tests."""
    _BIAS_CACHE.clear()
    _CALIBRATOR_CACHE.clear()
