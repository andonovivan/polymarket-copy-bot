"""Logistic regression with Platt-style calibration."""

from __future__ import annotations

import pickle

import numpy as np

from polymarket_bot.model.base import Model


class LogitModel(Model):
    """sklearn LogisticRegression wrapped to fit our Model interface.

    sklearn is imported lazily so the package can be imported without it.
    """

    name = "logit"

    def __init__(self, version: str = "uninit") -> None:
        self.version = version
        self._clf = None
        self._scaler = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler

        self._scaler = StandardScaler().fit(X)
        Xs = self._scaler.transform(X)
        self._clf = LogisticRegression(max_iter=1000)
        self._clf.fit(Xs, y)

    def predict_proba(self, x: np.ndarray) -> float:
        if self._clf is None or self._scaler is None:
            return 0.5  # untrained ⇒ no edge
        xs = self._scaler.transform(x.reshape(1, -1))
        return float(self._clf.predict_proba(xs)[0, 1])

    def to_bytes(self) -> bytes:
        return pickle.dumps({"version": self.version, "clf": self._clf, "scaler": self._scaler})

    @classmethod
    def from_bytes(cls, payload: bytes) -> "LogitModel":
        data = pickle.loads(payload)
        m = cls(version=data["version"])
        m._clf = data["clf"]
        m._scaler = data["scaler"]
        return m
