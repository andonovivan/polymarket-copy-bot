"""Bootstrap significance test: recovers known signal vs no-skill, gives sane CIs."""

from __future__ import annotations

import numpy as np

from polymarket_bot.model.significance import bootstrap_brier


def test_no_skill_predictions_not_significant():
    """If predictions = baseline (always 0.5), p-value should be near 1."""
    rng = np.random.default_rng(0)
    actuals = rng.integers(0, 2, size=500)
    preds = np.full(500, 0.5)
    r = bootstrap_brier(preds, actuals, baseline=0.5, n_iter=2000)
    assert abs(r.brier_diff) < 1e-9
    assert r.p_value > 0.4  # consistent with no edge


def test_oracle_recovers_strong_significance():
    """A perfect oracle should produce a tiny p-value vs baseline."""
    rng = np.random.default_rng(1)
    actuals = rng.integers(0, 2, size=500)
    # Oracle leaks a bit of probability around the truth (Brier ≈ 0.02 vs baseline 0.25)
    preds = np.where(actuals == 1, 0.85, 0.15)
    r = bootstrap_brier(preds, actuals, baseline=0.5, n_iter=2000)
    assert r.brier_model < 0.05
    assert r.brier_diff < -0.15
    assert r.p_value < 0.01


def test_ci_brackets_mean_diff():
    """The 95% CI should bracket the point estimate."""
    rng = np.random.default_rng(2)
    actuals = rng.integers(0, 2, size=300)
    preds = np.clip(0.5 + (actuals - 0.5) * 0.2 + rng.normal(0, 0.1, size=300), 0.01, 0.99)
    r = bootstrap_brier(preds, actuals, baseline=0.5, n_iter=3000)
    assert r.ci_lo <= r.brier_diff <= r.ci_hi


def test_input_validation():
    import pytest
    with pytest.raises(ValueError):
        bootstrap_brier(np.array([0.5, 0.5]), np.array([1]), n_iter=10)
    with pytest.raises(ValueError):
        bootstrap_brier(np.array([0.5]), np.array([1]), n_iter=10)
