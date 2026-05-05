"""Tests for per-city bias correction and isotonic calibration."""

from __future__ import annotations

import time

import pytest

from polymarket_bot.persistence.schema import init_db, get_conn
from polymarket_bot.strategy.calibration import (
    MIN_BIAS_EVENTS,
    MIN_CALIBRATION_OBS,
    apply_bias_correction,
    apply_calibration,
    bucket_temp_midpoint,
    compute_city_bias,
    compute_city_calibrator,
    reset_caches,
)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "bot_state.db"
    monkeypatch.setenv("BOT_DB_PATH", str(db_path))
    import polymarket_bot.persistence.schema as schema_mod
    schema_mod._conn = None
    init_db(db_path)
    reset_caches()
    yield db_path
    schema_mod._conn = None
    reset_caches()


# ---------------------------------------------------------------------------
# Bucket midpoint parsing
# ---------------------------------------------------------------------------

def test_bucket_midpoint_single_temp():
    assert bucket_temp_midpoint("16°C") == 16.0


def test_bucket_midpoint_or_below():
    assert bucket_temp_midpoint("7°C or below") == 7.0


def test_bucket_midpoint_or_higher():
    assert bucket_temp_midpoint("17°C or higher") == 17.0


def test_bucket_midpoint_range():
    # Hypothetical "8-9°C" → midpoint 8.5
    assert bucket_temp_midpoint("8-9°C") == 8.5


def test_bucket_midpoint_unparseable():
    assert bucket_temp_midpoint("") is None
    assert bucket_temp_midpoint("not a temp") is None


# ---------------------------------------------------------------------------
# Bias correction
# ---------------------------------------------------------------------------

def _seed_winning_obs(city_key: str, model_day_max: float, label: str,
                      observed_at: int | None = None) -> None:
    """Insert one settled (winning-bucket) row to drive `compute_city_bias`."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO weather_research_obs "
        "(city_key, target_date, slug, bucket_label, model_p, "
        " model_day_max_mean, observed_at, outcome) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (city_key, "2026-04-01", f"slug-{label}-{observed_at or 0}",
         label, 0.5, model_day_max, observed_at or int(time.time())),
    )
    conn.commit()


def test_bias_zero_when_no_data(tmp_db):
    assert compute_city_bias("paris") == 0.0


def test_bias_zero_below_min_events(tmp_db):
    # Insert fewer than MIN_BIAS_EVENTS rows.
    for i in range(MIN_BIAS_EVENTS - 1):
        _seed_winning_obs("paris", model_day_max=20.0, label="18°C",
                          observed_at=int(time.time()) - i * 86400)
    assert compute_city_bias("paris") == 0.0


def test_bias_median_signed(tmp_db):
    # Insert 11 winning observations: model predicted 20°C, actual was 18°C.
    # Median bias should be +2.0 (model overshoots by 2°C).
    for i in range(MIN_BIAS_EVENTS + 1):
        _seed_winning_obs("paris", model_day_max=20.0, label="18°C",
                          observed_at=int(time.time()) - i * 86400)
    assert compute_city_bias("paris") == 2.0


def test_bias_handles_negative_offset(tmp_db):
    # Model under-predicts by 1°C consistently → median bias = -1.0.
    for i in range(MIN_BIAS_EVENTS + 1):
        _seed_winning_obs("paris", model_day_max=15.0, label="16°C",
                          observed_at=int(time.time()) - i * 86400)
    assert compute_city_bias("paris") == -1.0


def test_apply_bias_correction_is_noop_at_zero():
    members = [16, 17, 18]
    assert apply_bias_correction(members, 0.0) is members


def test_apply_bias_correction_rounds_to_int():
    # bias = +1.4 → shift down by ~1.4, rounded → most members drop by 1.
    out = apply_bias_correction([16, 17, 18, 19], 1.4)
    assert out == [15, 16, 17, 18]


def test_apply_bias_correction_negative():
    # bias = -2 → model under-predicts; shift members UP by 2.
    out = apply_bias_correction([10, 11, 12], -2.0)
    assert out == [12, 13, 14]


# ---------------------------------------------------------------------------
# Isotonic calibration
# ---------------------------------------------------------------------------

def _seed_obs(city_key: str, model_p: float, won: int,
              observed_at: int | None = None) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO weather_research_obs "
        "(city_key, target_date, slug, bucket_label, model_p, "
        " observed_at, outcome) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (city_key, "2026-04-01", f"slug-{model_p}-{observed_at or 0}",
         "16°C", model_p, observed_at or int(time.time()), won),
    )
    conn.commit()


def test_calibrator_none_below_min_obs(tmp_db):
    for i in range(MIN_CALIBRATION_OBS - 1):
        _seed_obs("paris", 0.3, 0, observed_at=int(time.time()) - i)
    assert compute_city_calibrator("paris") is None


def test_calibrator_corrects_overconfidence(tmp_db):
    # Setup: at p=0.8 (model very confident), observed wins at only 50%.
    # At p=0.2 (model unconfident), observed wins at 30%.
    # Isotonic should map 0.8 → ~0.5 and 0.2 → ~0.3.
    for i in range(MIN_CALIBRATION_OBS // 2):
        _seed_obs("paris", 0.8, 1 if i % 2 == 0 else 0,
                  observed_at=int(time.time()) - i)
    for i in range(MIN_CALIBRATION_OBS // 2 + 1):
        # ~30% win rate at p=0.2 (3 wins per 10).
        won = 1 if i % 10 < 3 else 0
        _seed_obs("paris", 0.2, won, observed_at=int(time.time()) - i - 1000)
    cal = compute_city_calibrator("paris")
    assert cal is not None
    # The calibrator should pull 0.8 down toward the observed 0.5.
    out_high = float(cal.predict([0.8])[0])
    out_low = float(cal.predict([0.2])[0])
    assert out_high < 0.7   # was 0.8, observed 0.5
    assert out_low < 0.4    # was 0.2, observed 0.3
    # Monotone — high input still maps higher than low input.
    assert out_high > out_low


def test_apply_calibration_is_noop_when_none():
    probs = {"a": 0.1, "b": 0.5}
    assert apply_calibration(probs, None) == probs


def test_apply_calibration_returns_new_dict_with_same_keys(tmp_db):
    for i in range(MIN_CALIBRATION_OBS):
        _seed_obs("paris", 0.5, i % 2, observed_at=int(time.time()) - i)
    cal = compute_city_calibrator("paris")
    assert cal is not None
    probs = {"15°C": 0.1, "16°C": 0.5, "17°C": 0.4}
    out = apply_calibration(probs, cal)
    assert set(out.keys()) == set(probs.keys())
    for v in out.values():
        assert 0.0 <= v <= 1.0
