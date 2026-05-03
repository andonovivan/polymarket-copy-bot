"""Statistical significance for binary-prediction models.

Paired bootstrap on Brier score vs a constant baseline. Returns the mean Brier
difference, a 95% bootstrap CI, and a one-sided p-value (model better than
baseline ⇒ p small).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class SignificanceResult:
    brier_model: float
    brier_baseline: float
    brier_diff: float           # model − baseline; negative = model better
    ci_lo: float
    ci_hi: float
    p_value: float              # P(brier_diff >= 0 | bootstrap) — one-sided
    n_samples: int
    n_iter: int


def bootstrap_brier(
    predictions: np.ndarray,
    actuals: np.ndarray,
    *,
    baseline: float = 0.5,
    n_iter: int = 5000,
    seed: int = 42,
) -> SignificanceResult:
    """Paired bootstrap of Brier(model) − Brier(constant `baseline`).

    `predictions` and `actuals` must be 1-D arrays of equal length.
    `actuals` is 0/1.
    """
    if predictions.ndim != 1 or actuals.ndim != 1:
        raise ValueError("predictions and actuals must be 1-D")
    if predictions.size != actuals.size:
        raise ValueError("predictions and actuals must have the same length")
    n = predictions.size
    if n < 2:
        raise ValueError("need at least 2 samples for bootstrap")

    pred_loss = (predictions - actuals) ** 2
    base_loss = (np.full(n, baseline) - actuals) ** 2
    diffs = pred_loss - base_loss

    rng = np.random.default_rng(seed)
    boot_means = np.empty(n_iter, dtype=float)
    for i in range(n_iter):
        idx = rng.integers(0, n, size=n)
        boot_means[i] = float(diffs[idx].mean())

    mean_diff = float(diffs.mean())
    ci_lo, ci_hi = (float(np.quantile(boot_means, 0.025)),
                    float(np.quantile(boot_means, 0.975)))
    # One-sided p: P(model NOT better) = fraction of bootstraps where mean diff >= 0.
    p_value = float(np.mean(boot_means >= 0.0))

    return SignificanceResult(
        brier_model=float(pred_loss.mean()),
        brier_baseline=float(base_loss.mean()),
        brier_diff=mean_diff,
        ci_lo=ci_lo, ci_hi=ci_hi,
        p_value=p_value,
        n_samples=n, n_iter=n_iter,
    )
