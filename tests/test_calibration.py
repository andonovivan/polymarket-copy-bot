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
    compute_city_bias_curve,
    compute_city_calibrator,
    get_city_bias_curve,
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


def test_bias_curve_none_when_no_data(tmp_db):
    assert compute_city_bias_curve("paris") is None


def test_bias_curve_none_below_min_events(tmp_db):
    # Insert fewer than MIN_BIAS_EVENTS rows.
    for i in range(MIN_BIAS_EVENTS - 1):
        _seed_winning_obs("paris", model_day_max=20.0, label="18°C",
                          observed_at=int(time.time()) - i * 86400)
    assert compute_city_bias_curve("paris") is None


def test_bias_curve_constant_when_no_temp_variation(tmp_db):
    # All samples at the same model_temp → degenerate slope, returns the
    # mean error as a constant function regardless of input temperature.
    for i in range(MIN_BIAS_EVENTS + 1):
        _seed_winning_obs("paris", model_day_max=20.0, label="18°C",
                          observed_at=int(time.time()) - i * 86400)
    fn = compute_city_bias_curve("paris")
    assert fn is not None
    assert abs(fn(20.0) - 2.0) < 1e-9
    assert abs(fn(15.0) - 2.0) < 1e-9   # constant outside observed range


def test_bias_curve_cached_path_degenerate_slope(tmp_db):
    """Regression: get_city_bias_curve reads `.range`/`.slope`/`.intercept`
    off the returned callable to log telemetry. The degenerate constant-temp
    case must carry the same metadata so the cached path doesn't crash."""
    for i in range(MIN_BIAS_EVENTS + 1):
        _seed_winning_obs("paris", model_day_max=20.0, label="18°C",
                          observed_at=int(time.time()) - i * 86400)
    fn = get_city_bias_curve("paris")
    assert fn is not None
    # Must work regardless of which compute path produced the function.
    assert fn.range == (20.0, 20.0)
    assert fn.slope == 0.0
    assert abs(fn.intercept - 2.0) < 1e-9
    assert abs(fn(20.0) - 2.0) < 1e-9


def test_bias_curve_captures_temperature_dependence(tmp_db):
    # Synthesize a clear temperature-dependent bias:
    #   - At cold model_temp = 5°C: actual was 4°C (model over-predicts by 1)
    #   - At warm model_temp = 25°C: actual was 26°C (model under-predicts by 1)
    # A linear regression should produce slope ≈ -0.1 and intercept ≈ 1.5,
    # so the curve crosses zero around 15°C.
    now = int(time.time())
    for i in range(6):
        _seed_winning_obs("paris", model_day_max=5.0, label="4°C",
                          observed_at=now - i * 86400)
    for i in range(6):
        _seed_winning_obs("paris", model_day_max=25.0, label="26°C",
                          observed_at=now - (i + 6) * 86400)
    fn = compute_city_bias_curve("paris")
    assert fn is not None
    # At cold end: model over-predicts → bias > 0.
    assert fn(5.0) > 0.5
    # At warm end: model under-predicts → bias < 0.
    assert fn(25.0) < -0.5
    # Mid-range: smaller magnitude.
    assert abs(fn(15.0)) < abs(fn(5.0))


def test_bias_curve_clamps_extrapolation(tmp_db):
    # Observed range is [10, 20]. Inputs outside that range should clamp to
    # the boundary values, never extrapolate to absurd biases.
    now = int(time.time())
    for i in range(6):
        _seed_winning_obs("paris", model_day_max=10.0, label="9°C",
                          observed_at=now - i * 86400)
    for i in range(6):
        _seed_winning_obs("paris", model_day_max=20.0, label="19°C",
                          observed_at=now - (i + 6) * 86400)
    fn = compute_city_bias_curve("paris")
    assert fn is not None
    val_at_lo = fn(10.0)
    val_at_hi = fn(20.0)
    # Far below lower bound → same as at lower bound.
    assert abs(fn(-50.0) - val_at_lo) < 1e-9
    # Far above upper bound → same as at upper bound.
    assert abs(fn(100.0) - val_at_hi) < 1e-9


def test_apply_bias_correction_passthrough_when_none():
    members = [16, 17, 18]
    assert apply_bias_correction(members, None) is members


def test_apply_bias_correction_uses_local_bias_per_member():
    # Constant bias function: f(t) = +1.5 → each member shifts down by ~1.5,
    # rounded.
    fn = lambda _t: 1.5
    out = apply_bias_correction([16, 17, 18, 19], fn)
    assert out == [14, 16, 16, 18]   # round half to even via int(round(...))


def test_apply_bias_correction_temperature_dependent():
    # Per-member bias varying with temperature: cold members get a +1
    # correction, warm members get a -1 correction.
    fn = lambda t: 1.0 if t < 15 else -1.0
    out = apply_bias_correction([10, 12, 18, 20], fn)
    assert out == [9, 11, 19, 21]


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
