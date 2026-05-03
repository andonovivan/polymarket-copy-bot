"""Train and persist probability models from cached BTC + aux bars.

Default training is **walk-forward**: roll a sliding window through the data,
train on `[t0, t0+window]`, predict on `[t0+window, t0+window+step]`, then
slide. The OOS predictions are concatenated and used for honest Brier / log-loss
metrics and a paired-bootstrap significance test against the no-skill baseline
(constant 0.5). The final persisted model is fit on the entire range so it has
maximum recent context for live use.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import structlog

from polymarket_bot.features.pipeline import (
    FEATURE_NAMES,
    WARMUP_BARS,
    FeatureContext,
    build_features,
)
from polymarket_bot.model.base import Model
from polymarket_bot.model.logit import LogitModel
from polymarket_bot.model.significance import SignificanceResult, bootstrap_brier
from polymarket_bot.persistence.repo import Bar, load_bars
from polymarket_bot.persistence.schema import get_conn, lock
from polymarket_bot.polymarket.settle import outcome_for_bar

logger = structlog.get_logger()

DAY_SECONDS = 86400


@dataclass
class CVMetrics:
    samples: int
    brier: float
    log_loss: float
    folds: int
    significance: SignificanceResult


_FEATURE_WINDOW = WARMUP_BARS + 1   # build_features only needs the last ~60 bars


def _build_xy(bars: list[Bar], ctx: FeatureContext) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """Convert a contiguous bar window + aux context into (X, y, ts) arrays.

    Sample at bar i: features through bar i; label = UP/DOWN of bar i+1
    (Polymarket's >= rule, via outcome_for_bar). No lookahead.

    Performance: only the last `_FEATURE_WINDOW` bars are passed to
    build_features each iteration — the function never looks further back than
    the warmup window, so feeding it more is pure waste.
    """
    X: list[list[float]] = []
    y: list[int] = []
    ts: list[int] = []
    for i in range(WARMUP_BARS, len(bars) - 1):
        start = max(0, i + 1 - _FEATURE_WINDOW)
        fv = build_features(bars[start: i + 1], ctx)
        if fv is None:
            continue
        nxt = bars[i + 1]
        label = 1 if outcome_for_bar(nxt.o, nxt.c) == "UP" else 0
        X.append(fv.values.tolist())
        y.append(label)
        ts.append(fv.timestamp)
    return np.array(X, dtype=float), np.array(y, dtype=int), ts


def _walk_forward(
    bars: list[Bar],
    ctx: FeatureContext,
    train_window_days: int,
    step_days: int,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Roll a sliding window through `bars` producing OOS predictions.

    Returns (preds, actuals, n_folds) over the entire walked range.
    """
    if not bars:
        return np.array([]), np.array([]), 0
    train_window_s = train_window_days * DAY_SECONDS
    step_s = step_days * DAY_SECONDS

    bar_times = np.array([b.open_time for b in bars])
    first_t = int(bar_times[0])
    last_t = int(bar_times[-1])

    # Slide so that the *test* window of each fold ends ≤ last_t.
    fold_start = first_t
    all_preds: list[float] = []
    all_actuals: list[int] = []
    folds = 0
    while fold_start + train_window_s + step_s <= last_t + step_s:
        train_end = fold_start + train_window_s
        test_end = train_end + step_s

        train_mask = (bar_times >= fold_start) & (bar_times < train_end)
        test_mask = (bar_times >= train_end) & (bar_times < test_end)
        train_bars = [bars[i] for i in np.where(train_mask)[0]]
        test_bars = [bars[i] for i in np.where(test_mask)[0]]
        if len(train_bars) < WARMUP_BARS + 50 or len(test_bars) < 5:
            fold_start += step_s
            continue

        X_tr, y_tr, _ = _build_xy(train_bars, ctx)
        if X_tr.shape[0] < 50:
            fold_start += step_s
            continue
        model = LogitModel(version="cv-fold")
        model.fit(X_tr, y_tr)

        # For test bars, we still need WARMUP_BARS of history before each — splice
        # the tail of the train window in front so build_features has full context.
        joined = train_bars[-WARMUP_BARS:] + test_bars
        for i in range(WARMUP_BARS, len(joined) - 1):
            start = max(0, i + 1 - _FEATURE_WINDOW)
            fv = build_features(joined[start: i + 1], ctx)
            if fv is None:
                continue
            nxt = joined[i + 1]
            label = 1 if outcome_for_bar(nxt.o, nxt.c) == "UP" else 0
            p = model.predict_proba(fv.values)
            all_preds.append(p)
            all_actuals.append(label)

        folds += 1
        fold_start += step_s

    return np.array(all_preds, dtype=float), np.array(all_actuals, dtype=int), folds


def train_logit(
    window_days: int = 365,
    *,
    cv: bool = True,
    cv_train_days: int = 60,
    cv_step_days: int = 14,
    bootstrap_iter: int = 5000,
) -> LogitModel | None:
    """Fit a LogitModel on the most recent `window_days` of cached bars.

    With `cv=True`, also runs walk-forward CV (training window `cv_train_days`,
    step `cv_step_days`) and bootstraps Brier vs the no-skill baseline.
    The final persisted model is trained on ALL `window_days` of data — CV
    is used to *estimate* OOS performance, not to pick a fold.
    """
    cutoff = int(time.time()) - window_days * DAY_SECONDS
    bars = load_bars(from_ts=cutoff)
    if len(bars) < WARMUP_BARS + 100:
        logger.warning("insufficient_bars_to_train", have=len(bars), need=WARMUP_BARS + 100)
        return None

    ctx = FeatureContext.load(from_ts=cutoff)
    X, y, _ = _build_xy(bars, ctx)
    if X.shape[0] < 100:
        logger.warning("insufficient_samples_to_train", samples=X.shape[0])
        return None

    version = datetime.now(timezone.utc).strftime("logit-%Y%m%dT%H%M%SZ")
    model = LogitModel(version=version)
    model.fit(X, y)

    cv_metrics: CVMetrics | None = None
    if cv:
        preds, actuals, folds = _walk_forward(bars, ctx,
                                              train_window_days=cv_train_days,
                                              step_days=cv_step_days)
        if preds.size >= 50:
            from sklearn.metrics import brier_score_loss, log_loss
            cv_brier = float(brier_score_loss(actuals, preds))
            cv_ll = float(log_loss(actuals, np.clip(preds, 1e-6, 1 - 1e-6)))
            sig = bootstrap_brier(preds, actuals, baseline=0.5, n_iter=bootstrap_iter)
            cv_metrics = CVMetrics(samples=int(preds.size), brier=cv_brier,
                                   log_loss=cv_ll, folds=folds, significance=sig)
        else:
            logger.warning("cv_too_few_predictions", got=int(preds.size))

    _persist_model(model, bars[0].open_time, bars[-1].open_time, cv_metrics)

    log_kw: dict = {
        "version": version, "samples": int(X.shape[0]),
        "features": FEATURE_NAMES, "window_days": window_days,
    }
    if cv_metrics:
        log_kw["cv_brier"] = round(cv_metrics.brier, 5)
        log_kw["cv_brier_baseline"] = round(cv_metrics.significance.brier_baseline, 5)
        log_kw["cv_p_value"] = round(cv_metrics.significance.p_value, 4)
        log_kw["cv_folds"] = cv_metrics.folds
        log_kw["cv_n"] = cv_metrics.samples
    logger.info("model_trained", **log_kw)
    return model


def _persist_model(model: Model, win_start: int, win_end: int,
                   cv: CVMetrics | None) -> None:
    conn = get_conn()
    with lock():
        cv_brier = cv.brier if cv else None
        cv_ll = cv.log_loss if cv else None
        cv_baseline = cv.significance.brier_baseline if cv else None
        cv_p = cv.significance.p_value if cv else None
        cv_lo = cv.significance.ci_lo if cv else None
        cv_hi = cv.significance.ci_hi if cv else None
        cv_folds = cv.folds if cv else None
        conn.execute(
            "INSERT OR REPLACE INTO models "
            "(version, strategy, trained_at, window_start, window_end, payload, "
            " feature_names, cv_brier, cv_logloss, cv_brier_baseline, cv_p_value, "
            " cv_brier_ci_lo, cv_brier_ci_hi, cv_folds, calib_intercept, calib_slope) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (model.version, "momentum_logit", int(time.time()), win_start, win_end,
             model.to_bytes(), json.dumps(FEATURE_NAMES),
             cv_brier, cv_ll, cv_baseline, cv_p, cv_lo, cv_hi, cv_folds, None, None),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('active_model_version', ?)",
            (model.version,),
        )
        conn.commit()


def load_active_model() -> Model | None:
    """Load the currently-active model from the DB, validating its feature schema.

    Returns None if no model is set OR if the saved feature_names doesn't match
    the current FEATURE_NAMES (forces a clean retrain after pipeline changes).
    """
    conn = get_conn()
    row = conn.execute("SELECT value FROM meta WHERE key='active_model_version'").fetchone()
    if not row:
        return None
    version = row[0]
    pr = conn.execute(
        "SELECT payload, feature_names FROM models WHERE version=?", (version,),
    ).fetchone()
    if not pr:
        return None
    payload, fn_json = pr
    try:
        saved_names = json.loads(fn_json) if fn_json else None
    except Exception:
        saved_names = None
    if saved_names is None:
        logger.warning(
            "active_model_missing_feature_names",
            version=version,
            hint="legacy model; retrain with `polymarket-bot train` to enable predictions",
        )
        return None
    if saved_names != FEATURE_NAMES:
        logger.warning(
            "active_model_feature_mismatch",
            version=version, saved=saved_names, current=FEATURE_NAMES,
            hint="feature pipeline changed; retrain with `polymarket-bot train`",
        )
        return None
    return LogitModel.from_bytes(payload)


def cv_metrics_for_active_model() -> dict | None:
    """Read the persisted CV metrics for the active model, for the dashboard."""
    conn = get_conn()
    row = conn.execute("SELECT value FROM meta WHERE key='active_model_version'").fetchone()
    if not row:
        return None
    version = row[0]
    r = conn.execute(
        "SELECT cv_brier, cv_logloss, cv_brier_baseline, cv_p_value, "
        " cv_brier_ci_lo, cv_brier_ci_hi, cv_folds FROM models WHERE version=?", (version,),
    ).fetchone()
    if not r:
        return None
    keys = ["cv_brier", "cv_logloss", "cv_brier_baseline", "cv_p_value",
            "cv_brier_ci_lo", "cv_brier_ci_hi", "cv_folds"]
    return {k: r[i] for i, k in enumerate(keys)} | {"version": version}
