"""Backtest metrics: returns, drawdown, Brier, calibration, cost diagnostics."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class TradeRow:
    pnl: float
    stake: float
    predicted_p: float
    outcome_up: bool   # True if BTC closed up
    fees: float
    slippage: float


def compute(rows: list[TradeRow], equity: list[tuple[int, float]]) -> dict[str, float | int]:
    """Compute the full metric panel from a list of trades and an equity curve."""
    n = len(rows)
    if n == 0:
        return {"trades": 0}

    pnls = np.array([r.pnl for r in rows])
    stakes = np.array([r.stake for r in rows])
    preds = np.array([r.predicted_p for r in rows])
    actuals = np.array([1.0 if r.outcome_up else 0.0 for r in rows])
    fees = np.array([r.fees for r in rows])
    slip = np.array([r.slippage for r in rows])

    wins = int((pnls > 0).sum())
    losses = int((pnls < 0).sum())
    win_rate = wins / n
    profit_factor = (pnls[pnls > 0].sum() / -pnls[pnls < 0].sum()) if (pnls < 0).any() else float("inf")
    expectancy = float(pnls.mean())
    avg_stake = float(stakes.mean())

    brier = float(np.mean((preds - actuals) ** 2))
    eps = 1e-6
    pclip = np.clip(preds, eps, 1 - eps)
    logloss = float(-np.mean(actuals * np.log(pclip) + (1 - actuals) * np.log(1 - pclip)))

    eq = np.array([e for _, e in equity], dtype=float)
    if eq.size > 1:
        rets = np.diff(eq) / np.maximum(eq[:-1], 1e-9)
        sharpe = float(np.sqrt(105_120) * rets.mean() / rets.std()) if rets.std() > 0 else 0.0
        peak = np.maximum.accumulate(eq)
        dd = (peak - eq) / np.maximum(peak, 1e-9)
        max_dd = float(dd.max())
    else:
        sharpe, max_dd = 0.0, 0.0

    total_pnl = float(pnls.sum())
    return {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "avg_stake": avg_stake,
        "total_pnl": total_pnl,
        "brier": brier,
        "log_loss": logloss,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "total_fees": float(fees.sum()),
        "total_slippage": float(slip.sum()),
    }
