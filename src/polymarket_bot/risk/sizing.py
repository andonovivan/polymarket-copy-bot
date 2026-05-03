"""Position sizing — fractional Kelly for binary contracts."""

from __future__ import annotations


def kelly_fraction_full(p: float, price: float) -> float:
    """
    Full Kelly fraction of bankroll for a binary contract priced at `price`
    (in $) where you believe the win probability is `p`.

    Payout per share if won = 1 - price (you get $1, paid `price`).
    Loss per share if lost = price.
    Decimal odds b (in Kelly's notation) = (1 - price) / price.
    Kelly: f = (b*p - q) / b  where q = 1 - p.

    Returns 0 if there's no edge (p <= price), so caller never bets a loser.
    """
    if not (0.0 < price < 1.0):
        return 0.0
    if not (0.0 <= p <= 1.0):
        return 0.0
    b = (1.0 - price) / price
    q = 1.0 - p
    if b <= 0:
        return 0.0
    f = (b * p - q) / b
    return max(0.0, f)


def fractional_kelly_stake(
    p_model: float,
    side: str,
    price_paid: float,
    bankroll: float,
    kelly_fraction: float = 0.25,
    max_bet_pct: float = 0.05,
) -> float:
    """
    Compute stake in dollars for a YES or NO bet on a binary contract.

    p_model: model's predicted P(BTC up over the next 5-min window)
    side: 'YES' (bet up) or 'NO' (bet down)
    price_paid: per-share entry price in $ (0 < price < 1)
    bankroll: total bankroll in $
    kelly_fraction: scale-down factor (e.g. 0.25 for quarter-Kelly)
    max_bet_pct: hard cap on stake as % of bankroll

    Returns stake in dollars (0 if no edge or invalid inputs).
    """
    if side not in ("YES", "NO"):
        raise ValueError(f"side must be YES or NO, got {side!r}")
    if bankroll <= 0:
        return 0.0

    p = p_model if side == "YES" else 1.0 - p_model
    f_full = kelly_fraction_full(p, price_paid)
    f_use = min(kelly_fraction * f_full, max_bet_pct)
    f_use = max(0.0, f_use)
    return bankroll * f_use
