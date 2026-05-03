"""Slippage model: bps + linear impact in stake / book depth."""

from __future__ import annotations


def slippage_bps(base_bps: float, stake_usd: float, depth_usd: float, k: float = 50.0) -> float:
    """Linear impact: slippage_bps = base_bps + k * (stake / depth).

    `depth_usd` is the available top-of-book USD on the side we're crossing.
    `k` controls how aggressive impact is — calibrate against real fills later.
    """
    if depth_usd <= 0:
        return base_bps + k  # very thin → cap impact at k bps
    return base_bps + k * (stake_usd / depth_usd)
