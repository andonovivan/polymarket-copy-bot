"""Per-city probability calibration on top of the raw ensemble model.

Two layers, applied in order inside `_attach_model_probabilities`:

  1. **Temperature-conditional bias correction (#1).**
     `compute_city_bias_curve(city_key)` fits a linear regression of
     forecast error against the forecast temperature itself, over recent
     settled events:

         error(model_temp) = a + b · model_temp

     The caller evaluates this curve at each ensemble member's value and
     subtracts the local bias before bucketing. This naturally captures
     seasonal patterns (e.g. cold-month over-prediction vs warm-month
     under-prediction) without explicit seasonality buckets — when warm
     observations dominate the lookback window, the curve reflects the
     warm-regime bias; when the city transitions seasons, the curve adapts
     as new data arrives.

  2. **Isotonic probability calibration (#2).**
     `compute_city_calibrator(city_key)` fits an isotonic regression
     `(model_p, won) → calibrated_p` over recent bucket-level observations
     from the same table. Returns a callable that maps any model
     probability into a calibrated probability.

Both layers are pass-through (no-op) when there isn't enough data — they
return `None`. The bot keeps trading the raw ensemble until enough events
accumulate, at which point the corrections kick in automatically.

Recent fits are cached in-process for `CACHE_TTL_SECONDS` to keep tick-loop
overhead near zero.
"""

from __future__ import annotations

import re
import time
from typing import Callable, Optional

import structlog
from sklearn.isotonic import IsotonicRegression

from polymarket_bot.persistence.schema import get_pool

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


# Cache: key → (fitted_at, bias_curve_callable_or_None).
_BIAS_CACHE: dict[str, tuple[float, "Optional[Callable[[float], float]]"]] = {}
_CALIBRATOR_CACHE: dict[str, tuple[float, "IsotonicRegression | None"]] = {}
# Cache: city_key → (fitted_at, settled_obs_count). The DB query is cheap but
# every strategy tick on every event would still hit it; cache for the same
# TTL as the calibration fits.
_WARMUP_COUNT_CACHE: dict[str, tuple[float, int]] = {}


def is_city_warmed_up(city_key: str, min_obs: int) -> bool:
    """True iff the city has at least `min_obs` settled research observations.

    The strategy uses this to gate new BUYs on cities the calibrator hasn't
    had data to fit yet. Result is cached for `CACHE_TTL_SECONDS` to keep
    tick-loop overhead negligible.
    """
    if min_obs <= 0:
        return True
    cached = _WARMUP_COUNT_CACHE.get(city_key)
    now = time.time()
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1] >= min_obs
    from polymarket_bot.persistence.repo import count_settled_obs_for_city
    count = count_settled_obs_for_city(city_key)
    _WARMUP_COUNT_CACHE[city_key] = (now, count)
    return count >= min_obs


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

def _query_bias_samples(city_key: str,
                        lookback_days: int) -> list[tuple[float, float]]:
    """Fetch (model_day_max_mean, error) pairs per settled event.

    `error` is `model_day_max_mean − actual_day_max`, where actual is the
    midpoint of the winning bucket (bucket-resolution noise of ±0.5°C is
    averaged out across multiple events).
    """
    cutoff = int(time.time()) - lookback_days * 86400
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT slug, observed_at, model_day_max_mean, bucket_label "
            "FROM weather_research_obs "
            "WHERE city_key=%s AND outcome=1 AND model_day_max_mean IS NOT NULL "
            "  AND observed_at >= %s",
            (city_key, cutoff),
        ).fetchall()
    samples: list[tuple[float, float]] = []
    for _slug, _ts, model_mean, label in rows:
        actual = bucket_temp_midpoint(label)
        if actual is None or model_mean is None:
            continue
        samples.append((float(model_mean), float(model_mean) - actual))
    return samples


def compute_city_bias_curve(city_key: str, *,
                            lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                            min_events: int = MIN_BIAS_EVENTS,
                            ) -> "Optional[Callable[[float], float]]":
    """Fit a linear `model_temp → expected_error` regression for one city.

    Returns a callable that takes a forecast temperature and returns the
    expected `(model − actual)` bias at that temperature, or None if there
    aren't enough samples yet.

    The returned function clamps inputs to the observed temperature range so
    we never extrapolate wildly (e.g. into temperatures the city has never
    seen in our window).
    """
    samples = _query_bias_samples(city_key, lookback_days)
    if len(samples) < min_events:
        return None

    n = len(samples)
    sx = sum(t for t, _ in samples)
    sy = sum(e for _, e in samples)
    sxx = sum(t * t for t, _ in samples)
    sxy = sum(t * e for t, e in samples)
    denom = n * sxx - sx * sx
    min_t = min(t for t, _ in samples)
    max_t = max(t for t, _ in samples)
    if denom == 0:
        # All samples at identical model_temp — degenerate slope; use the
        # mean error as a constant function.
        slope = 0.0
        intercept = sy / n
    else:
        slope = (n * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / n

    def f(model_temp: float) -> float:
        clamped = max(min_t, min(max_t, model_temp))
        return intercept + slope * clamped

    # Stamp the function with metadata for the cache log + telemetry.
    f.slope = slope          # type: ignore[attr-defined]
    f.intercept = intercept  # type: ignore[attr-defined]
    f.range = (min_t, max_t) # type: ignore[attr-defined]
    return f


def get_city_bias_curve(city_key: str) -> "Optional[Callable[[float], float]]":
    """Cached `compute_city_bias_curve`. Returns None when insufficient data."""
    now = time.time()
    cached = _BIAS_CACHE.get(city_key)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    fn = compute_city_bias_curve(city_key)
    _BIAS_CACHE[city_key] = (now, fn)
    if fn is not None:
        lo, hi = fn.range  # type: ignore[attr-defined]
        logger.info(
            "city_bias_curve_fitted", city=city_key,
            slope=round(fn.slope, 3),       # type: ignore[attr-defined]
            intercept=round(fn.intercept, 2),  # type: ignore[attr-defined]
            range=f"{lo:.1f}–{hi:.1f}°",
            bias_at_lo=round(fn(lo), 2),
            bias_at_hi=round(fn(hi), 2),
        )
    return fn


def apply_bias_correction(members: list[int],
                          bias_fn: "Optional[Callable[[float], float]]",
                          ) -> list[int]:
    """Shift each ensemble member by its locally-estimated bias.

    `bias_fn(t)` returns the expected `(model − actual)` error at forecast
    temperature `t`; we subtract that from `t` to recover the implied actual.
    """
    if bias_fn is None:
        return members
    return [int(round(m - bias_fn(float(m)))) for m in members]


# ---------------------------------------------------------------------------
# #2 — Isotonic probability calibration
# ---------------------------------------------------------------------------

def _query_calibration_samples(city_key: str,
                               lookback_days: int) -> tuple[list[float], list[int]]:
    """Return (model_p, won) pairs for settled bucket observations."""
    cutoff = int(time.time()) - lookback_days * 86400
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT model_p, outcome FROM weather_research_obs "
            "WHERE city_key=%s AND outcome IS NOT NULL AND observed_at >= %s",
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
    _WARMUP_COUNT_CACHE.clear()
