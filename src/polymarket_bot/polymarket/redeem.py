"""Redemption helpers for Polymarket binary markets.

Polymarket binary markets do NOT auto-redeem. After resolution, winning YES
tokens become *redeemable for $1 USDC each* via a call to the conditional
tokens framework (CTF) contract — but the user must initiate it. Redemption
is gasless via Polymarket's relayer.

This module is the bookkeeping layer:
  • `pending_redemptions()`         — list settled markets we still hold tokens on
  • `mark_redeemed(market_id, ...)` — record a successful on-chain redemption

The actual web3.py call to the NegRiskAdapter / CTF contract is **not yet
implemented**; redemption today is a manual step on polymarket.com or via the
official SDK. The CLI subcommand `polymarket-bot redemptions` prints the list
the operator needs to redeem.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from polymarket_bot.persistence.repo import (
    get_market,
    inventory_for_market,
    list_settlements,
)

logger = structlog.get_logger()


@dataclass
class PendingRedemption:
    market_id: str          # Polymarket conditionId
    title: str | None
    yes_shares: float
    avg_yes_cost: float
    payout_usdc: float      # what redemption will return (= yes_shares for the winner)
    settled_at: int


def pending_redemptions() -> list[PendingRedemption]:
    """Return settled markets where we still hold YES tokens awaiting redemption.

    A "winning" YES position has yes_shares > 0 and a settlement row with
    outcome="WIN". (A LOSS position has 0 USDC value — nothing to redeem.)
    """
    out: list[PendingRedemption] = []
    for s in list_settlements(limit=10_000):
        if s.outcome != "WIN":
            continue
        yes_shares, _, avg_yes, _ = inventory_for_market(s.market_id)
        if yes_shares <= 0:
            continue
        m = get_market(s.market_id)
        out.append(PendingRedemption(
            market_id=s.market_id,
            title=(m.title if m else None),
            yes_shares=yes_shares,
            avg_yes_cost=avg_yes,
            payout_usdc=yes_shares,    # $1/share for the winning side
            settled_at=s.settled_at,
        ))
    return out


def cmd_redemptions(_config, _args) -> None:
    """CLI: list every winning YES position that needs to be redeemed for USDC."""
    pending = pending_redemptions()
    if not pending:
        print("No pending redemptions.")
        return
    print(f"\n{len(pending)} pending redemption(s):\n")
    print(f"  {'market':<36}  {'shares':>9}  {'cost':>7}  {'payout':>8}")
    print("  " + "-" * 70)
    total_payout = 0.0
    total_cost = 0.0
    for p in pending:
        title = (p.title or p.market_id[:14] + "…")[:36]
        cost = p.yes_shares * p.avg_yes_cost
        total_cost += cost
        total_payout += p.payout_usdc
        print(f"  {title:<36}  {p.yes_shares:>9.2f}  ${cost:>5.2f}  ${p.payout_usdc:>6.2f}")
    print("  " + "-" * 70)
    print(f"  {'TOTAL':<36}  {'':>9}  ${total_cost:>5.2f}  ${total_payout:>6.2f}")
    print()
    print("To redeem (live mode):")
    print("  1) Visit https://polymarket.com/portfolio and click 'Redeem' on each market, OR")
    print("  2) Use polymarket.com CLI tools.")
    print("  Automated on-chain redemption via web3.py is on the live-readiness backlog.")
