"""Entry-point: poll → detect → copy loop."""

from __future__ import annotations

import logging
import signal
import sys
import time

import structlog

from polymarket_copy_bot.client import PolymarketClient
from polymarket_copy_bot.config import BotConfig
from polymarket_copy_bot.copier import TradeCopier
from polymarket_copy_bot.dashboard import start_dashboard
from polymarket_copy_bot.state import update_last_running_ts
from polymarket_copy_bot.tracker import TradeTracker

logger = structlog.get_logger()

_running = True


def _handle_shutdown(signum: int, _frame: object) -> None:
    global _running
    logger.info("shutdown_signal_received", signal=signum)
    _running = False


def main() -> None:
    """Boot up the copy-trading bot."""
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    # --- Load config ---
    config = BotConfig.from_env()

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(config.log_level),
        ),
    )

    if not config.tracked_wallets:
        logger.error("no_tracked_wallets", hint="Set TRACKED_WALLETS in .env")
        sys.exit(1)

    if not config.private_key:
        logger.error("no_private_key", hint="Set PRIVATE_KEY in .env")
        sys.exit(1)

    logger.info(
        "bot_starting",
        dry_run=config.dry_run,
        tracked_wallets=len(config.tracked_wallets),
        fixed_amount_usdc=config.fixed_amount_usdc,
        poll_interval=config.poll_interval_seconds,
        max_position_usdc=config.max_position_usdc,
    )

    if config.dry_run:
        logger.warning("DRY_RUN_MODE — no real orders will be placed")

    # --- Initialise components ---
    client = PolymarketClient(config)
    tracker = TradeTracker(config, client)
    copier = TradeCopier(config, client)

    # Persist tracked wallets to state file on startup.
    copier._save()

    # Reconcile positions: close profitable trades the tracked trader exited while we were offline.
    copier.reconcile_on_startup()

    # Start web dashboard.
    start_dashboard(copier, client, port=config.dashboard_port)

    # --- Main loop ---
    low_balance_logged = False

    while _running:
        try:
            logger.debug("poll_cycle_start")
            new_trades = tracker.poll()
            logger.debug("poll_cycle_result", new_trades=len(new_trades))

            if new_trades:
                # Check balance once per cycle, not per trade.
                # In dry-run mode, always assume sufficient balance.
                if config.dry_run:
                    has_balance = True
                else:
                    balance = client.get_balance_usdc()
                    has_balance = balance is None or balance >= config.fixed_amount_usdc

                if has_balance:
                    low_balance_logged = False
                elif not low_balance_logged:
                    logger.warning(
                        "insufficient_balance",
                        balance=balance,
                        required=config.fixed_amount_usdc,
                        hint="Buys paused until balance is topped up. Sells still execute.",
                    )
                    low_balance_logged = True

                for trade in new_trades:
                    # Always allow sells (they free up capital). Skip buys when broke.
                    if trade.side == "BUY" and not has_balance:
                        logger.debug("skipped_buy_low_balance", trade_id=trade.trade_id)
                        continue
                    copier.copy(trade)

                logger.info("poll_cycle_done", new_trades=len(new_trades))

        except Exception as exc:
            logger.error("poll_cycle_error", error=str(exc))

        # Record that we're alive so reconciliation knows when we were last running.
        update_last_running_ts(int(time.time()))
        time.sleep(config.poll_interval_seconds)

    # Final timestamp update on shutdown.
    update_last_running_ts(int(time.time()))
    logger.info("bot_stopped")


if __name__ == "__main__":
    main()
