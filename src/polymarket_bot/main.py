"""Entry-point: weather-betting tick loop.

Per tick:
  1) Settle any events whose resolution has finalized on gamma.
  2) For each allowlisted city, discover its upcoming weather events.
  3) For each open event:
     - Populate live YES bid/ask + ask-side depth per bucket.
     - Pull (or read from cache) the ensemble forecast → model_p per bucket.
     - Build BetState, run strategy.evaluate(), dispatch new orders.
     - Reconcile any fills since last tick.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone

import httpx
import structlog

from polymarket_bot.config import BotConfig
from polymarket_bot.dashboard.server import start_dashboard
from polymarket_bot.data.weather_feed import (
    CITY_REGISTRY,
    bucket_probabilities,
    get_ensemble,
)
from polymarket_bot.execution.broker import Broker
from polymarket_bot.execution.live_broker import LiveBroker
from polymarket_bot.execution.paper_broker import PaperBroker
from polymarket_bot.execution.router import Router
from polymarket_bot.logging import configure as configure_logging
from polymarket_bot.persistence.repo import (
    Market,
    append_equity,
    inventory_for_market,
    latest_equity,
    markets_with_unsettled_fills,
    open_orders_by_market,
    set_meta,
    upsert_market,
)
from polymarket_bot.persistence.schema import init_db
from polymarket_bot.polymarket.client import PolymarketClient
from polymarket_bot.polymarket.settle import settle_resolved_event
from polymarket_bot.polymarket.weather_markets import (
    discover_open_events,
    populate_quotes,
)
from polymarket_bot.strategy.base import BetState, OpenOrder, WeatherEvent
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
        return LiveBroker(PolymarketClient(config))
    return PaperBroker()


def _persist_event(event: WeatherEvent) -> None:
    """One DB row per bucket so foreign keys + per-market settlements work.

    Sets a human-readable `title` like "Paris · May 4 · 16°C" so the dashboard
    can show something useful instead of a 64-char hex condition id.
    """
    date_str = datetime.fromtimestamp(event.resolution_ts, tz=timezone.utc).strftime("%b %-d")
    city_pretty = event.city_key.replace("-", " ").title()
    for b in event.buckets:
        title = f"{city_pretty} · {date_str} · {b.label}"
        upsert_market(Market(
            market_id=b.market_id, slug=f"{event.slug}::{b.label}", title=title,
            resolution_ts=event.resolution_ts,
            yes_token_id=b.yes_token_id, no_token_id=b.no_token_id,
        ))


def _attach_model_probabilities(event: WeatherEvent) -> int:
    """Fetch the ensemble forecast for this event's date and fill bucket.model_p."""
    city = CITY_REGISTRY.get(event.city_key)
    if city is None:
        return 0
    # The market resolves at end_ts; the date that "owns" it is end_ts in UTC.
    # Polymarket's slug embeds the calendar day; we trust that.
    target_date = datetime.fromtimestamp(event.resolution_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    forecast = get_ensemble(city, target_date)
    if forecast is None or not forecast.members:
        return 0
    labels = [b.label for b in event.buckets]
    probs = bucket_probabilities(forecast.members, labels)
    for b in event.buckets:
        b.model_p = probs.get(b.label, 0.0)
    return len(forecast.members)


def _open_orders_by_bucket(event: WeatherEvent) -> dict[str, list[OpenOrder]]:
    out: dict[str, list[OpenOrder]] = {}
    for b in event.buckets:
        rows = open_orders_by_market(b.market_id)
        out[b.label] = [
            OpenOrder(
                order_id=r.order_id, client_order_id=r.client_order_id,
                market_id=r.market_id, token_side=r.token_side, side=r.side,
                price=r.price, size=r.size, filled=r.filled, placed_at=r.placed_at,
            )
            for r in rows
        ]
    return out


def _held_yes_shares_by_bucket(event: WeatherEvent) -> dict[str, float]:
    out: dict[str, float] = {}
    for b in event.buckets:
        yes, _, _, _ = inventory_for_market(b.market_id)
        out[b.label] = yes
    return out


def _total_open_exposure_usd() -> float:
    """Sum of (yes_shares × avg_yes_cost) across all unsettled markets."""
    total = 0.0
    for mid in markets_with_unsettled_fills():
        yes, _, avg_yes, _ = inventory_for_market(mid)
        total += yes * avg_yes
    return total


def cmd_run(config: BotConfig, args: argparse.Namespace) -> None:
    init_db()

    if latest_equity() is None:
        append_equity(int(time.time()), config.starting_bankroll)

    broker = _make_broker(config, args.live)
    StrategyClass = get_strategy_class(config.strategy)
    strategy = StrategyClass()
    router = Router(broker, strategy.name)
    cities = [c.strip() for c in config.weather_cities.split(",") if c.strip()]
    cities = [c for c in cities if c in CITY_REGISTRY]
    if not cities:
        logger.error("no_valid_cities", configured=config.weather_cities,
                     known=list(CITY_REGISTRY))
        sys.exit(2)

    start_dashboard(config)

    logger.info(
        "bot_starting",
        mode="live" if args.live else config.mode,
        strategy=strategy.name,
        cities=cities,
        edge_threshold=config.edge_threshold,
        kelly_fraction=config.kelly_fraction,
        bankroll=latest_equity(),
        tick_seconds=config.tick_seconds,
    )

    while _running:
        try:
            _tick(config, cities, broker, router, strategy)
        except Exception as exc:
            logger.error("tick_error", error=str(exc))
        set_meta("last_running_ts", str(int(time.time())))
        _interruptible_sleep(config.tick_seconds)

    logger.info("bot_stopped")


def _tick(config: BotConfig, cities: list[str], broker: Broker, router: Router,
          strategy) -> None:
    events = discover_open_events(cities, days_ahead=config.days_ahead)
    if not events:
        logger.debug("no_open_events")
    bankroll = latest_equity() or config.starting_bankroll

    with httpx.Client(timeout=10.0) as client:
        for event in events:
            _persist_event(event)
            n_quoted = populate_quotes(event, client=client)
            if n_quoted == 0:
                continue
            n_members = _attach_model_probabilities(event)
            if n_members == 0:
                continue

            seconds_to_res = max(0, event.resolution_ts - int(time.time()))
            # Recompute exposure each event so this tick's earlier bets are honoured.
            exposure_now = _total_open_exposure_usd()
            state = BetState(
                event=event,
                bankroll=bankroll,
                seconds_to_resolution=seconds_to_res,
                open_orders_by_bucket=_open_orders_by_bucket(event),
                held_yes_shares_by_bucket=_held_yes_shares_by_bucket(event),
                total_open_exposure_usd=exposure_now,
                edge_threshold=config.edge_threshold,
                kelly_fraction=config.kelly_fraction,
                max_bet_pct=config.max_bet_pct,
                max_total_exposure_pct=config.max_total_exposure_pct,
                min_market_depth_usd=config.min_market_depth_usd,
                lockout_seconds=config.lock_buffer_seconds,
            )
            actions = strategy.evaluate(state)
            if actions:
                router.execute(actions)

            broker.reconcile_fills(event)

    # Settle anything that's resolved on gamma since the last tick.
    # Re-query open events that have ended (end_ts already in the past).
    now = int(time.time())
    settled_events = discover_open_events(cities, days_ahead=0)  # 0 includes today
    # discover_open_events filters out events whose end_ts already passed.
    # We need a direct "find events I've bet on that are past end_ts" query — use DB.
    _settle_due_events(strategy.name)


def _settle_due_events(strategy_name: str) -> None:
    """Find DB markets whose resolution has passed and that we have fills on,
    group by event slug, ask gamma for the outcome, settle all buckets in one pass."""
    from polymarket_bot.persistence.schema import get_conn
    conn = get_conn()
    now = int(time.time())
    rows = conn.execute(
        "SELECT m.market_id, m.slug, m.resolution_ts, m.yes_token_id, m.no_token_id "
        "FROM markets m WHERE m.outcome IS NULL AND m.resolution_ts <= ? "
        "AND EXISTS (SELECT 1 FROM fills f WHERE f.market_id=m.market_id) ",
        (now,),
    ).fetchall()
    if not rows:
        return
    # Markets are stored per bucket; the slug carries `<event_slug>::<bucket_label>`.
    # Group by event_slug.
    from collections import defaultdict
    grouped: dict[str, list[tuple[str, str, int, str, str]]] = defaultdict(list)
    for r in rows:
        full_slug = r[1] or ""
        ev_slug = full_slug.split("::")[0]
        grouped[ev_slug].append(r)

    from polymarket_bot.polymarket.weather_markets import discover_event
    with httpx.Client(timeout=10.0) as c:
        for ev_slug, _ in grouped.items():
            # Try every city until one returns the event.
            ev = None
            for city_key in CITY_REGISTRY:
                ev = discover_event(ev_slug, city_key, client=c)
                if ev is not None:
                    break
            if ev is None:
                logger.warning("settle_event_not_found", slug=ev_slug)
                continue
            settle_resolved_event(ev, strategy=strategy_name)


def main() -> None:
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    parser = argparse.ArgumentParser(
        prog="polymarket-bot",
        description="Weather-forecast betting bot for Polymarket recurring markets.",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="Run the bot (paper by default).")
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
