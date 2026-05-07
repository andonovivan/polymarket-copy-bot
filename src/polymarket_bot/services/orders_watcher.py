"""Orders-watcher service — owns CLOB fill polling + settlement.

Phase C container: a single instance polls all open orders across every
strategy service, reconciles fills, and settles resolved events. Strategy
services never call `reconcile_fills` or `_settle_due_events` themselves;
those are exclusively this service's responsibility.

Because reconciliation needs the (yes_token_id, no_token_id) for each
market, we pull them via the existing `markets` table — no event tree
required. This decouples the watcher from `discover_open_events`, which
is owned by the strategy services.
"""

from __future__ import annotations

import sys
import time
from collections import defaultdict

import httpx
import structlog

from polymarket_bot.config import BotConfig
from polymarket_bot.execution.broker import Broker
from polymarket_bot.execution.paper_broker import PaperBroker
from polymarket_bot.persistence.repo import (
    Market,
    all_open_orders,
    markets_bulk,
    set_meta,
)
from polymarket_bot.persistence.schema import init_db
from polymarket_bot.polymarket.weather_markets import discover_event
from polymarket_bot.strategy.base import Bucket, WeatherEvent

logger = structlog.get_logger()


def _make_broker(config: BotConfig, live: bool) -> Broker:
    if config.mode == "live" or live:
        if not config.live_confirm or not config.private_key:
            logger.error("live_mode_blocked")
            sys.exit(2)
        from polymarket_bot.execution.live_broker import LiveBroker
        from polymarket_bot.polymarket.client import PolymarketClient
        return LiveBroker(PolymarketClient(config))
    return PaperBroker()


def _synthesize_event_from_markets(slug: str, markets: list[Market]) -> WeatherEvent:
    """Build a `WeatherEvent` shell good enough for `broker.reconcile_fills`.

    The reconciler only needs `event.buckets` with their market_id + token
    ids; it doesn't read end_ts, model_p, or unit. Quote fields stay None
    — the broker fetches fresh books per call.
    """
    return WeatherEvent(
        slug=slug, title="", city_key="?",
        end_ts=0, resolution_ts=0, unit="celsius",
        buckets=[
            Bucket(
                label=m.title or m.market_id[:8],
                market_id=m.market_id,
                yes_token_id=m.yes_token_id,
                no_token_id=m.no_token_id,
                yes_bid=None, yes_ask=None, yes_mid=None,
                depth_yes_ask_usd=0.0, model_p=None,
            )
            for m in markets
        ],
    )


def run_orders_watcher_service(*, live: bool = False) -> None:
    init_db()
    config = BotConfig.from_env()
    broker = _make_broker(config, live)

    # One-time equity-curve repair on boot — same logic the legacy
    # `cmd_run` runs (version bump or ceiling-breach heuristic).
    from polymarket_bot.main import _repair_equity_curve
    _repair_equity_curve(config)

    logger.info("orders_watcher_starting",
                tick_seconds=config.tick_seconds,
                mode="live" if live else config.mode)

    while True:
        try:
            _watcher_tick(config, broker)
        except Exception as exc:
            logger.error("orders_watcher_error", error=str(exc)[:240])
        set_meta("last_running_ts:orders-watcher", str(int(time.time())))
        time.sleep(max(1, config.tick_seconds))


def _watcher_tick(config: BotConfig, broker: Broker) -> None:
    """One pass: reconcile fills on all open orders, then settle resolved events."""
    open_orders = all_open_orders()
    if open_orders:
        # Group by event-slug-prefix to make one reconcile call per event.
        # Markets store the full slug as `<event_slug>::<bucket_label>`.
        market_ids = sorted({o.market_id for o in open_orders})
        market_map = markets_bulk(market_ids)
        by_event: dict[str, list[Market]] = defaultdict(list)
        for m in market_map.values():
            ev_slug = (m.slug or "").split("::")[0]
            if ev_slug:
                by_event[ev_slug].append(m)
        for ev_slug, markets in by_event.items():
            stub = _synthesize_event_from_markets(ev_slug, markets)
            try:
                broker.reconcile_fills(stub)
            except Exception as exc:
                logger.warning("reconcile_fills_failed",
                               event_slug=ev_slug, error=str(exc)[:200])

    # Settle anything that's resolved on gamma since the last tick.
    _settle_resolved_events(config)

    # Phase C bug-fix: the equity curve was previously sampled inside
    # `main._tick` which no microservice runs. The orders-watcher is the
    # right home for it — it ticks on a single instance, runs after
    # settlements (so realized cash is fresh), and isn't strategy-specific.
    from polymarket_bot.main import _maybe_sample_equity
    _maybe_sample_equity(config)


def _settle_resolved_events(config: BotConfig) -> None:
    """Find markets whose resolution_ts has passed AND have unsettled fills,
    fetch their gamma outcome, write per-(market, strategy) settlement rows.

    Phase C.5: settlement is now decomposed by strategy via
    `polymarket.settle.settle_resolved_event`. This watcher just enumerates
    candidate events and dispatches.
    """
    from polymarket_bot.persistence.schema import get_pool
    from polymarket_bot.polymarket.settle import settle_resolved_event
    now = int(time.time())
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT m.market_id, m.slug "
            "FROM markets m WHERE m.outcome IS NULL AND m.resolution_ts <= %s "
            "AND EXISTS (SELECT 1 FROM fills f WHERE f.market_id=m.market_id) ",
            (now,),
        ).fetchall()
    if not rows:
        return
    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for r in rows:
        full_slug = r[1] or ""
        ev_slug = full_slug.split("::")[0]
        grouped[ev_slug].append((r[0], r[1]))

    with httpx.Client(timeout=10.0) as c:
        from polymarket_bot.data.weather_feed import CITY_REGISTRY
        for ev_slug in grouped:
            ev = None
            for city_key in CITY_REGISTRY:
                ev = discover_event(ev_slug, city_key, client=c)
                if ev is not None:
                    break
            if ev is None:
                logger.warning("watcher_event_not_found", slug=ev_slug)
                continue
            settle_resolved_event(ev, winning_fee_bps=config.winning_fee_bps)
