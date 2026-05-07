"""Strategy service — runs one strategy in its own tick loop.

Phase C container: each strategy lives in its own process and ticks
independently. Compared to the legacy single-process `polymarket-bot run`:

  • Only ONE strategy is instantiated per service.
  • Reads `enabled_strategies` from the DB on every tick — if its own
    name is missing from the set, sleeps without emitting orders.
    Existing positions are still managed by the orders-watcher service.
  • Does NOT call `reconcile_fills` or `_settle_due_events` — those are
    owned by the orders-watcher service.
  • Reads its bankroll slice via `config.strategy_share()` so two
    strategies can co-exist without competing for the same exposure pool.
"""

from __future__ import annotations

import sys
import time

import httpx
import structlog

from polymarket_bot.config import BotConfig
from polymarket_bot.execution.broker import Broker
from polymarket_bot.execution.paper_broker import PaperBroker
from polymarket_bot.execution.router import Router
from polymarket_bot.persistence.repo import (
    inventory_snapshot_for,
    latest_equity,
    set_meta,
)
from polymarket_bot.persistence.schema import init_db
from polymarket_bot.polymarket.weather_markets import (
    discover_open_events,
    populate_quotes,
)
from polymarket_bot.strategy.base import BetState
from polymarket_bot.strategy.registry import (
    get_enabled_strategies_helper,
    get_strategy_class,
)

logger = structlog.get_logger()

_EMPTY_INV = (0.0, 0.0, 0.0, 0.0)


def _make_broker(config: BotConfig, live: bool) -> Broker:
    if config.mode == "live" or live:
        if not config.live_confirm:
            logger.error("live_mode_blocked",
                         hint="set --live AND POLYMARKET_BOT_LIVE=1")
            sys.exit(2)
        if not config.private_key:
            logger.error("missing_private_key")
            sys.exit(2)
        from polymarket_bot.execution.live_broker import LiveBroker
        from polymarket_bot.polymarket.client import PolymarketClient
        return LiveBroker(PolymarketClient(config))
    return PaperBroker()


def run_strategy_service(strategy_name: str, *, live: bool = False) -> None:
    """Single-strategy tick loop. Blocking; intended to be the main of a
    container.
    """
    init_db()
    config = BotConfig.from_env()
    broker = _make_broker(config, live)

    StrategyClass = get_strategy_class(strategy_name)
    strategy = StrategyClass()
    router = Router(broker, strategy.name,
                    max_notional_usd=config.max_order_notional_usd)
    cities = [c.strip() for c in config.weather_cities.split(",") if c.strip()]
    from polymarket_bot.data.weather_feed import CITY_REGISTRY
    cities = [c for c in cities if c in CITY_REGISTRY]
    if not cities:
        logger.error("no_valid_cities", configured=config.weather_cities)
        sys.exit(2)

    logger.info(
        "strategy_service_starting",
        strategy=strategy.name, display_name=strategy.display_name,
        share=round(config.strategy_share(strategy.name), 3),
        cities=cities,
        tick_seconds=config.tick_seconds,
        mode="live" if live else config.mode,
    )

    # Stagger multiple strategy containers so they don't burst-fetch the same
    # forecasts before the L2 forecast_cache is populated. The first to land
    # primes the cache; the second reads it for free.
    if config.startup_jitter_seconds > 0:
        import random
        jitter = random.uniform(0, config.startup_jitter_seconds)
        logger.info("strategy_startup_jitter", seconds=round(jitter, 2))
        time.sleep(jitter)

    while True:
        try:
            _strategy_tick(config, strategy, router, cities)
        except Exception as exc:
            logger.error("strategy_tick_error", strategy=strategy.name,
                         error=str(exc)[:240])
        set_meta(f"last_running_ts:{strategy.name}", str(int(time.time())))
        time.sleep(max(1, config.tick_seconds))


def _strategy_tick(config: BotConfig, strategy, router: Router,
                   cities: list[str]) -> None:
    """One pass: discover events → quote → attach probabilities → evaluate.

    No fills reconciliation, no settlements — those live in the
    orders-watcher service. The strategy runner is purely a *signal
    generator* in the Phase C architecture.
    """
    # Lazy import — avoids a circular import at module load time.
    from polymarket_bot.main import _attach_model_probabilities, _persist_event

    # Disable gate — if the user toggled this strategy off via the
    # dashboard, sleep this tick.
    enabled = get_enabled_strategies_helper()
    if strategy.name not in enabled:
        logger.debug("strategy_disabled_via_meta", strategy=strategy.name)
        return

    events = discover_open_events(cities, days_ahead=config.days_ahead)
    bankroll_total = latest_equity() or config.starting_bankroll
    share = config.strategy_share(strategy.name)
    strat_bankroll = bankroll_total * share
    all_market_ids = [b.market_id for ev in events for b in ev.buckets]

    with httpx.Client(timeout=10.0) as client:
        for event in events:
            _persist_event(event)
            n_quoted = populate_quotes(
                event, client=client, fetch_no_book=config.no_side_enabled,
                max_workers=config.clob_fetch_concurrency,
            )
            if n_quoted == 0:
                continue
            seconds_to_res = max(0, event.resolution_ts - int(time.time()))
            # Model-independent strategies (e.g. bucket_arbitrage) skip the
            # Open-Meteo fetch entirely — saves API quota and lets them keep
            # running even when the ensemble endpoint is rate-limited or down.
            if strategy.needs_model_probabilities:
                n_members = _attach_model_probabilities(
                    event, seconds_to_resolution=seconds_to_res, config=config,
                )
                if n_members == 0:
                    continue

            strat_snapshot = inventory_snapshot_for(strategy.name, all_market_ids)
            exposure_now = sum(
                yes * avg_yes for (yes, _, avg_yes, _) in strat_snapshot.values()
            )
            held_yes = {
                b.label: strat_snapshot.get(b.market_id, _EMPTY_INV)[0]
                for b in event.buckets
            }
            held_no = {
                b.label: strat_snapshot.get(b.market_id, _EMPTY_INV)[1]
                for b in event.buckets
            }
            from polymarket_bot.main import _open_orders_by_bucket
            state = BetState(
                event=event,
                bankroll=strat_bankroll,
                seconds_to_resolution=seconds_to_res,
                open_orders_by_bucket=_open_orders_by_bucket(event),
                held_yes_shares_by_bucket=held_yes,
                held_no_shares_by_bucket=held_no,
                total_open_exposure_usd=exposure_now,
                edge_threshold=config.edge_threshold,
                kelly_fraction=config.kelly_fraction,
                max_bet_pct=config.max_bet_pct,
                max_total_exposure_pct=config.max_total_exposure_pct,
                min_market_depth_usd=config.min_market_depth_usd,
                lockout_seconds=config.lock_buffer_seconds,
                warmup_min_obs=config.warmup_min_obs,
            )
            actions = strategy.evaluate(state)
            if actions:
                router.execute(actions)
