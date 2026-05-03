"""Polymarket fee model (research item — defaults to 0; configurable per backtest)."""

from __future__ import annotations


def polymarket_fee(notional_usd: float, fee_bps: float = 0.0) -> float:
    """Per-leg fee in $.

    As of late 2025 Polymarket charges no trading fee, but USDC bridging /
    conversion costs and gas may apply for some flows. We model it as a
    configurable bps to be honest about the assumption.
    """
    return max(0.0, notional_usd * fee_bps / 10_000.0)
