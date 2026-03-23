"""Decides whether and how to replicate a detected trade."""

from __future__ import annotations

import time

import structlog

from polymarket_copy_bot.client import PolymarketClient
from polymarket_copy_bot.config import BotConfig
from polymarket_copy_bot.state import load_state, save_state
from polymarket_copy_bot.tracker import DetectedTrade, fetch_user_trades, BULK_LIMIT

logger = structlog.get_logger()


class TradeCopier:
    """Evaluates detected trades and places copy orders."""

    def __init__(self, config: BotConfig, client: PolymarketClient) -> None:
        self.config = config
        self.client = client
        # Load persisted state from disk (survives restarts).
        state = load_state()
        self._exposure: dict[str, float] = state["exposure"]
        self._shares_held: dict[str, float] = state["shares_held"]
        self._buy_prices: dict[str, float] = state["buy_prices"]
        self._opened_at: dict[str, int] = state.get("opened_at", {})
        self._last_running_ts: int = state["last_running_ts"]
        self._pnl: dict = state["pnl"]
        if self._shares_held:
            logger.info("state_restored", positions=len(self._shares_held))
        if self._pnl["total_trades"] > 0:
            logger.info(
                "pnl_restored",
                total_realized=round(self._pnl["total_realized"], 4),
                total_trades=self._pnl["total_trades"],
                winning=self._pnl["winning_trades"],
                losing=self._pnl["losing_trades"],
                win_rate=f"{self._pnl['winning_trades'] / self._pnl['total_trades'] * 100:.1f}%",
            )

    def _save(self) -> None:
        """Persist current state to disk."""
        save_state(
            self._shares_held,
            self._exposure,
            self.config.tracked_wallets,
            self._buy_prices,
            self._pnl,
            self._opened_at,
        )

    def _record_pnl(self, asset_id: str, buy_price: float, sell_price: float, shares: float) -> None:
        """Record a realized trade P&L."""
        now = int(time.time())
        pnl = round((sell_price - buy_price) * shares, 4)
        self._pnl["total_realized"] = round(self._pnl["total_realized"] + pnl, 4)
        self._pnl["total_trades"] += 1
        if pnl >= 0:
            self._pnl["winning_trades"] += 1
        else:
            self._pnl["losing_trades"] += 1

        opened_ts = self._opened_at.get(asset_id, 0)

        self._pnl["trade_history"].append({
            "asset_id": asset_id,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "shares": shares,
            "pnl": pnl,
            "ts": now,
            "opened_at": opened_ts,
            "closed_at": now,
        })

        total_trades = self._pnl["total_trades"]
        win_rate = self._pnl["winning_trades"] / total_trades * 100 if total_trades > 0 else 0

        logger.info(
            "pnl_update",
            trade_pnl=pnl,
            total_realized=self._pnl["total_realized"],
            total_trades=total_trades,
            win_rate=f"{win_rate:.1f}%",
        )

    def reconcile_on_startup(self) -> None:
        """
        Check all held positions on startup.

        For each position we hold:
        - Check if the tracked trader has since sold (closed) that asset.
        - If they closed AND current price > our buy price → sell for profit.
        - If they closed AND current price <= buy price → keep (not profitable).
        - If they still hold → keep.
        """
        held_assets = {aid for aid, shares in self._shares_held.items() if shares > 0}
        if not held_assets:
            return

        logger.info(
            "reconcile_start",
            positions=len(self._shares_held),
            offline_since=self._last_running_ts or "never",
        )

        # Collect asset IDs the tracked traders sold while we were offline.
        # We only care about sells on assets WE hold.
        trader_sold_assets: set[str] = set()

        for wallet in self.config.tracked_wallets:
            trades = fetch_user_trades(
                wallet,
                limit=BULK_LIMIT,
                since_ts=self._last_running_ts,
            )
            for t in trades:
                asset_id = t.get("asset", "")
                if asset_id in held_assets and t.get("side") == "SELL":
                    trader_sold_assets.add(asset_id)

        # Check each held position.
        assets_to_close: list[str] = []
        for asset_id in held_assets:
            if asset_id not in trader_sold_assets:
                logger.info("reconcile_keep", asset_id=asset_id[:16] + "...", reason="trader_still_holds")
                continue

            # Trader closed this position. Check if we'd profit.
            buy_price = self._buy_prices.get(asset_id)
            if buy_price is None:
                logger.info("reconcile_keep", asset_id=asset_id[:16] + "...", reason="no_buy_price_recorded")
                continue

            midpoint = self.client.get_midpoint(asset_id)
            if midpoint is None:
                logger.info("reconcile_keep", asset_id=asset_id[:16] + "...", reason="no_midpoint")
                continue

            if midpoint > buy_price:
                logger.info(
                    "reconcile_close_profitable",
                    asset_id=asset_id[:16] + "...",
                    buy_price=buy_price,
                    current_price=midpoint,
                    shares=self._shares_held[asset_id],
                )
                assets_to_close.append(asset_id)
            else:
                logger.info(
                    "reconcile_keep",
                    asset_id=asset_id[:16] + "...",
                    reason="not_profitable",
                    buy_price=buy_price,
                    current_price=midpoint,
                )

        # Execute the sells.
        for asset_id in assets_to_close:
            shares = self._shares_held[asset_id]
            midpoint = self.client.get_midpoint(asset_id)
            if midpoint is None or midpoint <= 0:
                continue

            result = self.client.place_order(
                token_id=asset_id,
                side="SELL",
                price=midpoint,
                size=shares,
            )
            if result is not None:
                buy_price = self._buy_prices.get(asset_id, 0.0)
                self._record_pnl(asset_id, buy_price, midpoint, shares)
                self._exposure[asset_id] = 0.0
                self._shares_held[asset_id] = 0.0
                self._buy_prices.pop(asset_id, None)
                self._opened_at.pop(asset_id, None)
                self._save()
                logger.info("reconcile_sold", asset_id=asset_id[:16] + "...", shares=shares, price=midpoint)

        logger.info("reconcile_done")

    def copy(self, trade: DetectedTrade) -> bool:
        """
        Attempt to replicate *trade*. Returns True if an order was placed.

        Skips the trade when:
        - It's a SELL and copy_sells is disabled.
        - Current price deviates too far from the original trade price.
        - We've hit the per-market exposure cap.
        """
        # --- filter sells ---
        if trade.side == "SELL" and not self.config.copy_sells:
            logger.info("skipped_sell", trade_id=trade.trade_id)
            return False

        # --- compute size ---
        if trade.price <= 0:
            logger.info("skipped_zero_price", trade_id=trade.trade_id)
            return False

        if trade.side == "SELL":
            # Sell whatever shares we hold for this asset.
            fixed_size = self._shares_held.get(trade.asset_id, 0.0)
            if fixed_size <= 0:
                logger.info("skipped_sell_no_position", trade_id=trade.trade_id)
                return False
        else:
            # Buy: fixed USDC amount converted to shares.
            fixed_size = round(self.config.fixed_amount_usdc / trade.price, 2)
            if fixed_size <= 0:
                logger.info("skipped_zero_size", trade_id=trade.trade_id)
                return False

        # --- price sanity check ---
        midpoint = self.client.get_midpoint(trade.asset_id)
        if midpoint is not None:
            deviation = abs(midpoint - trade.price)
            if deviation > self.config.price_tolerance:
                logger.warning(
                    "price_deviation_too_high",
                    trade_id=trade.trade_id,
                    original_price=trade.price,
                    midpoint=midpoint,
                    deviation=deviation,
                )
                return False

        # --- exposure cap (buys only) ---
        current_exposure = self._exposure.get(trade.asset_id, 0.0)
        order_cost = self.config.fixed_amount_usdc
        if trade.side == "BUY" and current_exposure + order_cost > self.config.max_position_usdc:
            logger.warning(
                "exposure_cap_reached",
                trade_id=trade.trade_id,
                asset_id=trade.asset_id,
                current=current_exposure,
                would_add=order_cost,
                cap=self.config.max_position_usdc,
            )
            return False

        # --- place order ---
        result = self.client.place_order(
            token_id=trade.asset_id,
            side=trade.side,
            price=trade.price,
            size=fixed_size,
        )

        if result is not None:
            if trade.side == "BUY":
                self._exposure[trade.asset_id] = current_exposure + order_cost
                old_shares = self._shares_held.get(trade.asset_id, 0.0)
                new_shares = old_shares + fixed_size
                self._shares_held[trade.asset_id] = new_shares
                # Only set opened_at on the first buy for this asset.
                if trade.asset_id not in self._opened_at or old_shares <= 0:
                    self._opened_at[trade.asset_id] = int(time.time())
                # Weighted average buy price across multiple buys.
                old_price = self._buy_prices.get(trade.asset_id, 0.0)
                if new_shares > 0:
                    self._buy_prices[trade.asset_id] = (
                        (old_price * old_shares) + (trade.price * fixed_size)
                    ) / new_shares
                else:
                    self._buy_prices[trade.asset_id] = trade.price
            else:
                buy_price = self._buy_prices.get(trade.asset_id, 0.0)
                self._record_pnl(trade.asset_id, buy_price, trade.price, fixed_size)
                sell_value = fixed_size * trade.price
                self._exposure[trade.asset_id] = max(0.0, current_exposure - sell_value)
                self._shares_held[trade.asset_id] = 0.0
                self._buy_prices.pop(trade.asset_id, None)
                self._opened_at.pop(trade.asset_id, None)

            self._save()
            logger.info(
                "trade_copied",
                trade_id=trade.trade_id,
                side=trade.side,
                price=trade.price,
                size=fixed_size,
            )
            return True

        return False
