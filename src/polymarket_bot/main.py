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
    settlement_stats,
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


def _attach_model_probabilities(event: WeatherEvent,
                                seconds_to_resolution: int = 0,
                                config: BotConfig | None = None) -> int:
    """Fetch the ensemble forecast for this event's date and fill bucket.model_p.

    Layers applied in order (each is a no-op until conditions are met):

      1. **Bias correction** — shift members by the historical (model − actual)
         curve so bucketing reflects the city's known temperature-conditional
         bias. (calibration.py — needs ≥10 settled events.)
      2. **Bayesian fusion** — if within `bayesian_fusion_within_seconds` of
         resolution, fetch today's observed-so-far max and shift each member
         up to at least that value (monotonicity of daily max).
      3. **Bucketing** — count members per bucket.
      4. **Isotonic calibration** — map raw bucket probabilities through a
         fitted `model_p → observed_freq` curve. (calibration.py — needs ≥110
         bucket-level observations.)
    """
    from polymarket_bot.strategy.calibration import (
        apply_bias_correction, apply_calibration,
        get_city_bias_curve, get_city_calibrator,
    )

    city = CITY_REGISTRY.get(event.city_key)
    if city is None:
        return 0
    target_date = datetime.fromtimestamp(event.resolution_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    forecast = get_ensemble(city, target_date)
    if forecast is None or not forecast.members:
        return 0

    # Layer 1 — temperature-conditional bias correction.
    bias_curve = get_city_bias_curve(event.city_key)
    members = apply_bias_correction(forecast.members, bias_curve)

    # Layer 2 — Bayesian fusion with observed-so-far temperature.
    if (config is not None
            and config.bayesian_fusion_enabled
            and 0 < seconds_to_resolution <= config.bayesian_fusion_within_seconds):
        from polymarket_bot.data.observations import (
            fuse_ensemble_with_observation, get_observed_max_today,
        )
        observed = get_observed_max_today(city, target_date)
        if observed is not None:
            n_shifted = sum(1 for m in members if m < int(round(observed)))
            members = fuse_ensemble_with_observation(members, observed)
            if n_shifted > 0:
                logger.info("bayesian_fusion_applied",
                            city=event.city_key, observed=round(observed, 1),
                            n_members_shifted=n_shifted)

    # Layer 3 — bucket counts.
    labels = [b.label for b in event.buckets]
    probs = bucket_probabilities(members, labels)

    # Layer 4 — isotonic probability calibration.
    calibrator = get_city_calibrator(event.city_key)
    probs = apply_calibration(probs, calibrator)

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


def _held_no_shares_by_bucket(event: WeatherEvent) -> dict[str, float]:
    out: dict[str, float] = {}
    for b in event.buckets:
        _, no, _, _ = inventory_for_market(b.market_id)
        out[b.label] = no
    return out


def _total_open_exposure_usd() -> float:
    """Sum of (yes_shares × avg_yes_cost) across all unsettled markets."""
    total = 0.0
    for mid in markets_with_unsettled_fills():
        yes, _, avg_yes, _ = inventory_for_market(mid)
        total += yes * avg_yes
    return total


EQUITY_CURVE_VERSION = "2"


def _repair_equity_curve(config: BotConfig) -> None:
    """Drop equity points that pre-date the current MTM-clamping logic.

    Two triggers:
      1) A version bump in EQUITY_CURVE_VERSION (forces a one-time reseed when
         we change how MTM is computed).
      2) Heuristic ceiling: realized + 2× current cost + a small floor. A
         transient bad mid that sends one position 5× above its entry can blow
         past 2× cost; once we see that we know the curve is contaminated.
    """
    from polymarket_bot.persistence.repo import get_meta, set_meta
    from polymarket_bot.persistence.schema import get_conn
    conn = get_conn()
    rows = conn.execute("SELECT ts, equity FROM equity_curve ORDER BY ts").fetchall()
    stored_version = get_meta("equity_curve_version")
    version_bumped = stored_version != EQUITY_CURVE_VERSION

    if len(rows) <= 1 and not version_bumped:
        set_meta("equity_curve_version", EQUITY_CURVE_VERSION)
        return

    realized = _realized_cash(config.starting_bankroll)
    cost = sum(
        inventory_for_market(mid)[0] * inventory_for_market(mid)[2]
        for mid in markets_with_unsettled_fills()
    )
    ceiling = realized + 2.0 * cost + 25.0
    polluted = bool(rows) and max(r[1] for r in rows) > ceiling

    if not (version_bumped or polluted):
        return

    seed_ts = rows[0][0] if rows else int(time.time())
    conn.execute("DELETE FROM equity_curve WHERE ts > ?", (seed_ts,))
    conn.commit()
    set_meta("equity_curve_version", EQUITY_CURVE_VERSION)
    logger.warning(
        "equity_curve_repaired",
        removed=max(0, len(rows) - 1),
        kept_seed_ts=seed_ts,
        reason="version_bump" if version_bumped else "ceiling_breach",
        ceiling=round(ceiling, 2),
    )


def _realized_cash(starting_bankroll: float) -> float:
    """Cash equity = starting bankroll + sum of all settled trade PnLs.

    Computed from scratch each time so we never compound MTM samples into it.
    """
    s = settlement_stats()
    return float(starting_bankroll) + float(s.get("pnl", 0.0))


def _compute_mtm_equity(starting_bankroll: float) -> float:
    """MTM equity = realized cash + sum(unrealized P&L of open positions).

    The mid is clamped to [0, 1] — a Polymarket binary share can never be
    worth more than $1 or less than $0, so any out-of-range value is a bad
    quote that would otherwise inject a phantom spike into the curve.
    """
    from polymarket_bot.persistence.repo import get_market
    unrealized = 0.0
    for mid in markets_with_unsettled_fills():
        yes, _, avg_yes, _ = inventory_for_market(mid)
        if yes <= 0:
            continue
        m = get_market(mid)
        cur = m.last_yes_mid if (m and m.last_yes_mid is not None) else None
        if cur is None:
            continue
        cur = max(0.0, min(1.0, cur))
        unrealized += yes * (cur - avg_yes)
    return _realized_cash(starting_bankroll) + unrealized


_LAST_EQUITY_SAMPLE_TS = 0


def _maybe_sample_equity(config: BotConfig) -> None:
    """Append a periodic MTM-equity snapshot (throttled by config.equity_sample_seconds)."""
    global _LAST_EQUITY_SAMPLE_TS
    now = int(time.time())
    if now - _LAST_EQUITY_SAMPLE_TS < config.equity_sample_seconds:
        return
    append_equity(now, _compute_mtm_equity(config.starting_bankroll))
    _LAST_EQUITY_SAMPLE_TS = now


def cmd_run(config: BotConfig, args: argparse.Namespace) -> None:
    init_db()

    if latest_equity() is None:
        append_equity(int(time.time()), config.starting_bankroll)

    # If the equity curve was polluted by the previous double-count bug, repair
    # it on startup: clear all but the initial seed and rewrite a fresh MTM
    # snapshot. Idempotent; safe to leave permanently.
    _repair_equity_curve(config)

    broker = _make_broker(config, args.live)

    # Live mode: read actual wallet USDC balance and use it as the live bankroll
    # ground-truth. Falls back to config.starting_bankroll if the call fails.
    live_client = None
    if (config.mode == "live" or args.live) and config.private_key:
        from polymarket_bot.execution.live_broker import sync_wallet_balance
        live_client = PolymarketClient(config)
        synced = sync_wallet_balance(live_client)
        if synced is not None:
            logger.info("live_bankroll_set", usdc=round(synced, 4))
    # Multi-strategy support: each name in `config.strategy` (comma-separated)
    # is instantiated and gets its own router so per-strategy PnL attribution
    # works through the existing fills.strategy column. The first name is the
    # "primary" strategy reported on the dashboard.
    names = [n.strip() for n in config.strategy.split(",") if n.strip()]
    strategies = [get_strategy_class(n)() for n in names]
    routers = {s.name: Router(broker, s.name) for s in strategies}
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
        strategies=[s.name for s in strategies],
        cities=cities,
        edge_threshold=config.edge_threshold,
        kelly_fraction=config.kelly_fraction,
        bankroll=latest_equity(),
        tick_seconds=config.tick_seconds,
    )

    last_wallet_sync = int(time.time())

    while _running:
        # In live mode, halt the loop if a previous tick hit a HALT-class CLOB error.
        from polymarket_bot.execution.live_broker import is_halted
        if is_halted():
            logger.error("bot_halted_due_to_clob_error",
                         hint="auth/compliance failure — fix and restart")
            break

        try:
            _tick(config, cities, broker, routers, strategies)
        except Exception as exc:
            logger.error("tick_error", error=str(exc))

        # Periodic live wallet reconciliation (every 5 min).
        if live_client is not None and int(time.time()) - last_wallet_sync >= 300:
            from polymarket_bot.execution.live_broker import sync_wallet_balance
            sync_wallet_balance(live_client)
            last_wallet_sync = int(time.time())

        set_meta("last_running_ts", str(int(time.time())))
        _interruptible_sleep(config.tick_seconds)

    logger.info("bot_stopped")


def _tick(config: BotConfig, cities: list[str], broker: Broker,
          routers: dict[str, Router], strategies: list) -> None:
    events = discover_open_events(cities, days_ahead=config.days_ahead)
    if not events:
        logger.debug("no_open_events")
    bankroll = latest_equity() or config.starting_bankroll

    with httpx.Client(timeout=10.0) as client:
        for event in events:
            _persist_event(event)
            n_quoted = populate_quotes(event, client=client,
                                       fetch_no_book=config.no_side_enabled)
            if n_quoted == 0:
                continue
            seconds_to_res = max(0, event.resolution_ts - int(time.time()))
            n_members = _attach_model_probabilities(
                event, seconds_to_resolution=seconds_to_res, config=config,
            )
            if n_members == 0:
                continue
            for strategy in strategies:
                # Recompute exposure for each strategy so earlier strategies'
                # bets in this tick already count toward the cap.
                exposure_now = _total_open_exposure_usd()
                state = BetState(
                    event=event,
                    bankroll=bankroll,
                    seconds_to_resolution=seconds_to_res,
                    open_orders_by_bucket=_open_orders_by_bucket(event),
                    held_yes_shares_by_bucket=_held_yes_shares_by_bucket(event),
                    held_no_shares_by_bucket=_held_no_shares_by_bucket(event),
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
                    routers[strategy.name].execute(actions)

            broker.reconcile_fills(event)

    # Settle anything that's resolved on gamma since the last tick. The
    # primary strategy name owns the settlement rows (PnL accounting).
    primary_name = strategies[0].name if strategies else "weather_forecast"
    _settle_due_events(primary_name, winning_fee_bps=config.winning_fee_bps)

    # Read-only research capture for candidate cities (Path B).
    if config.research_enabled:
        try:
            from polymarket_bot.research.weather_capture import (
                capture_observations, update_outcomes,
            )
            capture_observations(
                window_seconds=config.research_window_seconds,
                dedupe_seconds=config.research_dedupe_seconds,
                days_ahead=config.days_ahead,
                include_candidates=config.research_capture_candidates,
            )
            update_outcomes()
        except Exception as exc:
            logger.warning("research_capture_error", error=str(exc)[:200])

    # Sample MTM equity into the curve so the chart populates between settlements.
    _maybe_sample_equity(config)


def _settle_due_events(strategy_name: str, *, winning_fee_bps: int = 500) -> None:
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
            settle_resolved_event(ev, strategy=strategy_name, winning_fee_bps=winning_fee_bps)


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

    sub.add_parser("redemptions",
                   help="List winning YES positions that still need to be redeemed for USDC.")

    p_bw = sub.add_parser(
        "backtest-weather",
        help="Rank candidate weather cities by historical model-vs-market edge.",
    )
    p_bw.add_argument("--days", type=int, default=60)
    p_bw.add_argument("--cities", default=None,
                      help="Comma-separated city slugs (default: all 36 candidates).")
    p_bw.add_argument("--edge-threshold", type=float, default=0.05)
    p_bw.add_argument("--kelly", type=float, default=0.25)
    p_bw.add_argument("--max-bet-pct", type=float, default=0.05)
    p_bw.add_argument("--bet-offset-hours", type=float, default=24.0)
    p_bw.add_argument("--request-sleep", type=float, default=0.05)

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return

    config = BotConfig.from_env()
    configure_logging(config.log_level)

    from polymarket_bot.polymarket.redeem import cmd_redemptions

    DISPATCH = {
        "run": cmd_run,
        "redemptions": _wrap_with_init(cmd_redemptions),
        "backtest-weather": _cmd_backtest_weather,
    }
    DISPATCH[args.cmd](config, args)


def _cmd_backtest_weather(config: BotConfig, args: argparse.Namespace) -> None:
    from polymarket_bot.backtest.weather_city_eval import cmd_main
    argv = [
        "--days", str(args.days),
        "--edge-threshold", str(args.edge_threshold),
        "--kelly", str(args.kelly),
        "--max-bet-pct", str(args.max_bet_pct),
        "--bet-offset-hours", str(args.bet_offset_hours),
        "--request-sleep", str(args.request_sleep),
    ]
    # Only forward --cities if explicitly set; otherwise let cmd_main use its
    # own default (the full CANDIDATES list).
    if args.cities:
        argv += ["--cities", args.cities]
    cmd_main(argv)


def _wrap_with_init(fn):
    """Helper subcommands need init_db() but not the full bot startup."""
    def wrapper(config, args):
        init_db()
        fn(config, args)
    return wrapper


if __name__ == "__main__":
    main()
