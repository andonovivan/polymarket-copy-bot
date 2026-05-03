"""Train and persist probability models from cached BTC bars."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import numpy as np
import structlog

from polymarket_bot.features.pipeline import FEATURE_NAMES, WARMUP_BARS, build_features
from polymarket_bot.model.base import Model
from polymarket_bot.model.logit import LogitModel
from polymarket_bot.persistence.repo import Bar, load_bars
from polymarket_bot.persistence.schema import get_conn, lock

logger = structlog.get_logger()


def _build_xy(bars: list[Bar]) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """Iterate the cached bars and produce (X, y, ts) for supervised fitting.

    Sample at bar t: features computed from bars[..t]; label = 1 if bars[t+1] closed up.
    """
    X: list[list[float]] = []
    y: list[int] = []
    ts: list[int] = []
    for i in range(WARMUP_BARS, len(bars) - 1):
        fv = build_features(bars[: i + 1])
        if fv is None:
            continue
        next_bar = bars[i + 1]
        label = 1 if next_bar.c > next_bar.o else 0
        X.append(fv.values.tolist())
        y.append(label)
        ts.append(fv.timestamp)
    return np.array(X, dtype=float), np.array(y, dtype=int), ts


def train_logit(window_days: int = 60) -> LogitModel | None:
    """Fit a LogitModel on the most recent `window_days` of cached bars."""
    cutoff = int(time.time()) - window_days * 86400
    bars = load_bars(from_ts=cutoff)
    if len(bars) < WARMUP_BARS + 100:
        logger.warning("insufficient_bars_to_train", have=len(bars), need=WARMUP_BARS + 100)
        return None

    X, y, _ = _build_xy(bars)
    if X.shape[0] < 100:
        logger.warning("insufficient_samples_to_train", samples=X.shape[0])
        return None

    version = datetime.now(timezone.utc).strftime("logit-%Y%m%dT%H%M%SZ")
    model = LogitModel(version=version)
    model.fit(X, y)

    # Quick holdout Brier / log-loss on the most recent 20% (chronological).
    split = int(len(X) * 0.8)
    holdout_brier = None
    holdout_logloss = None
    if split < len(X):
        from sklearn.metrics import brier_score_loss, log_loss
        Xv, yv = X[split:], y[split:]
        # Predict each row through the trained pipeline.
        preds = np.array([model.predict_proba(row) for row in Xv])
        try:
            holdout_brier = float(brier_score_loss(yv, preds))
            holdout_logloss = float(log_loss(yv, np.clip(preds, 1e-6, 1 - 1e-6)))
        except Exception as exc:
            logger.warning("metric_failed", error=str(exc))

    _persist_model(model, bars[0].open_time, bars[-1].open_time, holdout_brier, holdout_logloss)
    logger.info(
        "model_trained",
        version=version, samples=int(X.shape[0]),
        features=FEATURE_NAMES, brier=holdout_brier, logloss=holdout_logloss,
    )
    return model


def _persist_model(model: Model, win_start: int, win_end: int,
                   brier: float | None, logloss: float | None) -> None:
    conn = get_conn()
    with lock():
        conn.execute(
            "INSERT OR REPLACE INTO models "
            "(version, strategy, trained_at, window_start, window_end, payload, cv_brier, cv_logloss, calib_intercept, calib_slope) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (model.version, "momentum_logit", int(time.time()), win_start, win_end,
             model.to_bytes(), brier, logloss, None, None),
        )
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('active_model_version', ?)",
                     (model.version,))
        conn.commit()


def load_active_model() -> Model | None:
    """Load the currently-active model from the DB, or None if not trained yet."""
    conn = get_conn()
    row = conn.execute("SELECT value FROM meta WHERE key='active_model_version'").fetchone()
    if not row:
        return None
    version = row[0]
    payload_row = conn.execute("SELECT payload FROM models WHERE version=?", (version,)).fetchone()
    if not payload_row:
        return None
    return LogitModel.from_bytes(payload_row[0])
