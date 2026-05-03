"""Entry-point: CLI for the MM bot. Single subcommand: `run`."""

from __future__ import annotations

import argparse
import signal
import sys
import time

import structlog

from polymarket_bot.config import BotConfig
from polymarket_bot.dashboard.server import start_dashboard
from polymarket_bot.execution.broker import Broker
from polymarket_bot.execution.live_broker import LiveMMBroker
from polymarket_bot.execution.paper_broker import PaperMMBroker
from polymarket_bot.execution.router import MMRouter
from polymarket_bot.logging import configure as configure_logging
from polymarket_bot.persistence.repo import (
    Market,
    append_equity,
    inventory_for_market,
    latest_equity,
    open_orders_by_market,
    set_meta,
    unsettled_markets_due,
    upsert_market,
)
from polymarket_bot.persistence.schema import init_db
from polymarket_bot.polymarket.client import PolymarketClient
from polymarket_bot.polymarket.markets import DiscoveredMarket, next_market
from polymarket_bot.polymarket.quotes import fetch_quote
from polymarket_bot.polymarket.settle import settle_resolved_market
from polymarket_bot.strategy.base import Inventory, MMState, OpenOrder
from polymarket_bot.strategy.registry import get_strategy_class

logger = structlog.get_logger()

_running = True


def _handle_shutdown(signum: int, _frame: object) -> None:
    global _running
    logger.info("shutdown_signal_received", signal=signum)
    _running = False


def _interruptible_sleep(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while _running and time.monotonic() < deadline:
        time.sleep(min(0.5, max(0.01, deadline - time.monotonic())))


def _make_broker(config: BotConfig, live_flag: bool) -> Broker:
    if config.mode == "live" or live_flag:
        if not (live_flag and config.live_confirm):
            logger.error("live_mode_blocked",
                         hint="set --live AND POLYMARKET_BOT_LIVE=1 to enable real-money trading")
            sys.exit(2)
        if not config.private_key:
            logger.error("missing_private_key", hint="set PRIVATE_KEY in .env for live mode")
            sys.exit(2)
        return LiveMMBroker(PolymarketClient(config))
    return PaperMMBroker()


def _market_to_db(m: DiscoveredMarket) -> Market:
    return Market(
        market_id=m.market_id, slug=m.slug, resolution_ts=m.resolution_ts,
        yes_token_id=m.yes_token_id, no_token_id=m.no_token_id,
    )


def _db_market_to_discovered(m: Market) -> DiscoveredMarket:
    return DiscoveredMarket(
        market_id=m.market_id, slug=m.slug,
        start_ts=m.resolution_ts - 300, resolution_ts=m.resolution_ts,
        yes_token_id=m.yes_token_id, no_token_id=m.no_token_id,
    )


def _build_inventory(market_id: str) -> Inventory:
    yes, no, avg_yes, avg_no = inventory_for_market(market_id)
    return Inventory(yes_shares=yes, no_shares=no,
                     yes_cost_basis=avg_yes, no_cost_basis=avg_no)


def _build_open_orders(market_id: str) -> list[OpenOrder]:
    rows = open_orders_by_market(market_id)
    return [
        OpenOrder(
            order_id=r.order_id, client_order_id=r.client_order_id,
            market_id=r.market_id, token_side=r.token_side, side=r.side,
            price=r.price, size=r.size, filled=r.filled, placed_at=r.placed_at,
        )
        for r in rows
    ]


def cmd_run(config: BotConfig, args: argparse.Namespace) -> None:
    init_db()

    if latest_equity() is None:
        append_equity(int(time.time()), config.starting_bankroll)

    broker = _make_broker(config, args.live)
    StrategyClass = get_strategy_class(config.strategy)
    strategy = StrategyClass()
    router = MMRouter(broker, strategy.name)

    start_dashboard(config)

    logger.info(
        "bot_starting",
        mode="live" if args.live else config.mode,
        strategy=strategy.name,
        base_spread=config.base_spread,
        max_inventory_shares=config.max_inventory_shares,
        inventory_skew=config.inventory_skew,
        bankroll=latest_equity(),
        tick_seconds=config.tick_seconds,
    )

    while _running:
        try:
            _tick(config, broker, router, strategy)
        except Exception as exc:
            logger.error("tick_error", error=str(exc))
        set_meta("last_running_ts", str(int(time.time())))
        _interruptible_sleep(config.tick_seconds)

    logger.info("bot_stopped")


def _tick(config: BotConfig, broker: Broker, router: MMRouter, strategy) -> None:
    """One MM tick: settle due markets, then quote the next live market."""
    # 1) Settle anything that's resolved since the last tick.
    now = int(time.time())
    bankroll = latest_equity() or config.starting_bankroll
    for db_market in unsettled_markets_due(now):
        m = _db_market_to_discovered(db_market)
        if settle_resolved_market(m, strategy=strategy.name, bankroll_at_settle=bankroll):
            bankroll = latest_equity() or bankroll

    # 2) Pick the next live market to MM.
    market = next_market()
    if market is None:
        logger.debug("no_market")
        return
    upsert_market(_market_to_db(market))

    # 3) Pull the live YES/NO order book.
    quote = fetch_quote(market.yes_token_id, market.no_token_id)
    if quote is None or quote.yes_mid is None:
        logger.debug("no_quote")
        return

    # 4) Reconcile fills since the last tick (paper: book-cross; live: order status).
    broker.reconcile_fills(market, quote)

    # 5) Build state, ask the strategy what to do, dispatch.
    inventory = _build_inventory(market.market_id)
    open_orders = _build_open_orders(market.market_id)
    state = MMState(
        market=market,
        quote=quote,
        inventory=inventory,
        open_orders=open_orders,
        bankroll=bankroll,
        seconds_to_resolution=max(0, market.resolution_ts - now),
        base_spread=config.base_spread,
        max_inventory_shares=config.max_inventory_shares,
        inventory_skew=config.inventory_skew,
        lock_buffer_seconds=config.lock_buffer_seconds,
    )
    actions = strategy.tick(state)
    if actions:
        n = router.execute(actions, market)
        logger.debug("tick_dispatched",
                     market_id=market.market_id[:12],
                     yes_inv=round(inventory.yes_shares, 2),
                     no_inv=round(inventory.no_shares, 2),
                     actions=len(actions), executed=n)


def main() -> None:
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    parser = argparse.ArgumentParser(
        prog="polymarket-bot",
        description="Market-making bot on Polymarket BTC up/down 5m markets.",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="Run the MM bot in paper or live mode (paper by default).")
    p_run.add_argument("--live", action="store_true",
                       help="Place real orders (requires POLYMARKET_BOT_LIVE=1).")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return

    config = BotConfig.from_env()
    configure_logging(config.log_level)

    {"run": cmd_run}[args.cmd](config, args)


if __name__ == "__main__":
    main()
