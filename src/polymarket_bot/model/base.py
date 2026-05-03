"""Probability-model interface. Implementations: logit (v1), …"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class Model(ABC):
    """A binary-outcome probability model: features → P(class=1)."""

    name: str = "abstract"
    version: str = "uninit"

    @abstractmethod
    def fit(self, X: np.ndarray, y: np.ndarray) -> None: ...

    @abstractmethod
    def predict_proba(self, x: np.ndarray) -> float:
        """Return P(class=1) for a single feature vector."""

    @abstractmethod
    def to_bytes(self) -> bytes: ...

    @classmethod
    @abstractmethod
    def from_bytes(cls, payload: bytes) -> "Model": ...
