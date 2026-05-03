"""SpreadOnlyMM — v1 market-maker for binary 5-min markets.

Quoting rules:
  • Compute a fair value per side: `yes_fair = quote.yes_mid`, `no_fair = quote.no_mid`.
  • Penny-jump inside the existing book if the natural spread is wider than our
    target; otherwise post AT the live best bid (join the queue).
  • Skew quotes by inventory: bias prices to attract fills on the side we're
    short on, and discourage fills on the side we're already long.
  • Inventory cap: stop posting BUYs on a side once we hold ≥ max shares.
  • Time decay: tighten quotes (smaller spread) as resolution approaches.
  • Lockout: cancel everything within `lock_buffer_seconds` of resolution and
    let settlement clear inventory.

Cancel-and-replace: for simplicity, on every tick we compute the desired quote
set; any open order whose price deviates by more than `repost_eps` from the
desired price is cancelled and replaced.
"""

from __future__ import annotations

import time
import uuid

from polymarket_bot.strategy.base import (
    CancelOrder,
    MMState,
    MMStrategy,
    OpenOrder,
    OrderAction,
    PlaceLimit,
)

REPOST_EPS = 0.005           # repost if price drift exceeds half a cent
PRICE_TICK = 0.001           # CLOB tick — Polymarket prices are 1/1000 of a dollar


def _round_tick(p: float) -> float:
    """Round to the nearest CLOB tick, kept strictly inside (0, 1)."""
    return max(PRICE_TICK, min(1 - PRICE_TICK, round(p / PRICE_TICK) * PRICE_TICK))


class SpreadOnlyMM(MMStrategy):
    name = "spread_only"

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------

    def tick(self, state: MMState) -> list[OrderAction]:
        # Lockout near resolution — flatten quotes and let settlement do the work.
        if state.seconds_to_resolution <= state.lock_buffer_seconds:
            return [CancelOrder(o.order_id) for o in state.open_orders]

        if state.quote.yes_mid is None or state.quote.no_mid is None:
            return [CancelOrder(o.order_id) for o in state.open_orders]

        desired = self._desired_quotes(state)
        actions: list[OrderAction] = []
        existing: dict[tuple[str, str], OpenOrder] = {
            (o.token_side, o.side): o for o in state.open_orders
        }

        # Cancel anything that drifted or is no longer wanted.
        wanted_keys = {(d["token_side"], d["side"]) for d in desired}
        for key, order in existing.items():
            if key not in wanted_keys:
                actions.append(CancelOrder(order.order_id))
                continue
            wanted = next(d for d in desired if (d["token_side"], d["side"]) == key)
            if abs(order.price - wanted["price"]) > REPOST_EPS or abs(order.size - wanted["size"]) > 0.5:
                actions.append(CancelOrder(order.order_id))
                # Will get replaced below.

        # Place quotes that don't have a matching live order at the right price.
        for d in desired:
            key = (d["token_side"], d["side"])
            live = existing.get(key)
            keep = (live is not None
                    and abs(live.price - d["price"]) <= REPOST_EPS
                    and abs(live.size - d["size"]) <= 0.5)
            if keep:
                continue
            actions.append(PlaceLimit(
                market_id=state.market.market_id,
                token_side=d["token_side"],
                side="BUY",
                price=d["price"],
                size=d["size"],
                client_order_id=f"mm-{int(time.time())}-{uuid.uuid4().hex[:6]}",
            ))
        return actions

    # ------------------------------------------------------------------
    # Quote construction
    # ------------------------------------------------------------------

    def _desired_quotes(self, state: MMState) -> list[dict]:
        """For v1: BUY YES and BUY NO quotes only (no shorts).

        Returns a list of dicts {token_side, side, price, size}.
        """
        q = state.quote
        inv = state.inventory
        bankroll = max(state.bankroll, 0.0)
        cap = state.max_inventory_shares

        # Time-decay multiplier: spread shrinks linearly to ~30% as we near lockout.
        # T = full window in seconds, t_to_res = remaining.
        t = max(state.seconds_to_resolution - state.lock_buffer_seconds, 1)
        T = max(300 - state.lock_buffer_seconds, 1)
        decay_mult = 0.3 + 0.7 * (t / T)
        spread = state.base_spread * decay_mult

        # Inventory skew: positive imbalance (more YES) → lower YES bid further,
        # raise NO bid (since NO mid = 1 − yes_mid the "raise" works on the NO side).
        skew = state.inventory_skew * inv.imbalance

        out: list[dict] = []

        # No-arb cap: bid_yes ≤ yes_mid − tick AND bid_no ≤ no_mid − tick. Combined
        # this guarantees bid_yes + bid_no ≤ 1 − 2·tick, so we never pay more than
        # $1 for a guaranteed $1 payout — the irreducible MM constraint.
        yes_max = max(PRICE_TICK, q.yes_mid - PRICE_TICK)
        no_max = max(PRICE_TICK, q.no_mid - PRICE_TICK)

        out: list[dict] = []

        # YES BUY quote — only if we're under the cap.
        if inv.yes_shares < cap:
            # Penny-jump: if the live best bid is below mid − spread/2, we sit at
            # mid − spread/2; if it's above (tight book), we join at best_bid + tick.
            target_yes = (q.yes_mid - spread / 2) - skew
            if q.yes_bid is not None and target_yes <= q.yes_bid:
                target_yes = q.yes_bid + PRICE_TICK
            target_yes = _round_tick(min(target_yes, yes_max))
            size = self._size_for_side(state, target_yes, side_inv=inv.yes_shares, bankroll=bankroll)
            if size > 0 and 0 < target_yes < q.yes_ask if q.yes_ask else True:
                out.append({"token_side": "YES", "side": "BUY",
                            "price": target_yes, "size": size})

        # NO BUY quote — symmetric.
        if inv.no_shares < cap:
            target_no = (q.no_mid - spread / 2) + skew
            if q.no_bid is not None and target_no <= q.no_bid:
                target_no = q.no_bid + PRICE_TICK
            target_no = _round_tick(min(target_no, no_max))
            size = self._size_for_side(state, target_no, side_inv=inv.no_shares, bankroll=bankroll)
            if size > 0 and 0 < target_no < (q.no_ask if q.no_ask else 1.0):
                out.append({"token_side": "NO", "side": "BUY",
                            "price": target_no, "size": size})

        return out

    @staticmethod
    def _size_for_side(state: MMState, price: float, side_inv: float, bankroll: float) -> float:
        """Size of one quote.

        Cap by:
        - remaining inventory headroom (cap − current holdings)
        - $-budget per quote (e.g., 5% of bankroll), so a single fill can't blow us up
        - minimum 1 share at this price (Polymarket minimum order size)
        """
        if price <= 0:
            return 0.0
        per_quote_budget = 0.05 * bankroll
        max_shares_by_budget = per_quote_budget / price
        max_shares_by_cap = max(0.0, state.max_inventory_shares - side_inv)
        size = min(max_shares_by_budget, max_shares_by_cap)
        return float(round(size, 2)) if size >= 1.0 else 0.0
