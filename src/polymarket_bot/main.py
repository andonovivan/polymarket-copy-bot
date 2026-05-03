"""Entry-point: CLI dispatch for run / backfill / train / backtest."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog

from polymarket_bot.config import BotConfig
from polymarket_bot.dashboard.server import start_dashboard
from polymarket_bot.data.btc_feed import backfill, latest_closed_bar
from polymarket_bot.execution.broker import Broker
from polymarket_bot.execution.live_broker import LiveBroker
from polymarket_bot.execution.paper_broker import PaperBroker
from polymarket_bot.execution.router import Router
from polymarket_bot.logging import configure as configure_logging
from polymarket_bot.model.trainer import load_active_model, train_logit
from polymarket_bot.persistence.repo import (
    Market,
    append_equity,
    latest_equity,
    load_bars,
    set_meta,
    upsert_bars,
    upsert_market,
)
from polymarket_bot.persistence.schema import init_db
from polymarket_bot.polymarket.client import PolymarketClient
from polymarket_bot.polymarket.markets import DiscoveredMarket, next_market
from polymarket_bot.polymarket.quotes import fetch_quote
from polymarket_bot.polymarket.settle import settle_resolved_market
from polymarket_bot.risk.limits import cooldown_active, market_lockout
from polymarket_bot.strategy.base import StrategyContext
from polymarket_bot.strategy.registry import get_strategy_class

logger = structlog.get_logger()

_running = True


def _handle_shutdown(signum: int, _frame: object) -> None:
    global _running
    logger.info("shutdown_signal_received", signal=signum)
    _running = False


def _interruptible_sleep(seconds: int) -> None:
    """Sleep for `seconds` but wake up promptly when shutdown is signalled."""
    deadline = time.monotonic() + seconds
    while _running and time.monotonic() < deadline:
        time.sleep(min(0.5, deadline - time.monotonic()))


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------


def _make_broker(config: BotConfig, live_flag: bool) -> Broker:
    if config.mode == "live" or live_flag:
        if not (live_flag and config.live_confirm):
            logger.error(
                "live_mode_blocked",
                hint="set --live AND POLYMARKET_BOT_LIVE=1 to enable real-money trading",
            )
            sys.exit(2)
        if not config.private_key:
            logger.error("missing_private_key", hint="set PRIVATE_KEY in .env for live mode")
            sys.exit(2)
        return LiveBroker(PolymarketClient(config))
    return PaperBroker()


def cmd_run(config: BotConfig, args: argparse.Namespace) -> None:
    """Live tick loop: discover markets, evaluate strategy, place bets, settle."""
    init_db()

    if latest_equity() is None:
        append_equity(int(time.time()), config.starting_bankroll)

    broker = _make_broker(config, args.live)
    router = Router(broker)
    StrategyClass = get_strategy_class(config.strategy)
    model = load_active_model()
    if model is None:
        logger.warning("no_trained_model",
                       hint="run `polymarket-bot train` after backfill to enable bets")
    strategy = StrategyClass(model=model)

    start_dashboard(config)

    logger.info(
        "bot_starting",
        mode="live" if args.live else config.mode,
        strategy=config.strategy,
        edge_threshold=config.edge_threshold,
        kelly_fraction=config.kelly_fraction,
        bankroll=latest_equity(),
    )

    while _running:
        try:
            _tick(config, router, strategy)
        except Exception as exc:
            logger.error("tick_error", error=str(exc))
        set_meta("last_running_ts", str(int(time.time())))
        _interruptible_sleep(config.tick_seconds)

    logger.info("bot_stopped")


def _tick(config: BotConfig, router: Router, strategy) -> None:
    """One iteration of the live loop."""
    bar = latest_closed_bar()
    if bar is not None:
        upsert_bars([bar])

    market = next_market()
    if market is None:
        logger.debug("no_market")
        _settle_due_markets(config)
        return

    upsert_market(_market_to_db(market))
    _settle_due_markets(config)

    # One bet per market — refuse to double down each tick.
    if _has_open_bet_on(market.market_id):
        logger.debug("already_bet", market_id=market.market_id[:12])
        return

    if market_lockout(market.resolution_ts, config.lock_buffer_seconds):
        logger.debug("market_locked", resolution_in=market.resolution_ts - int(time.time()))
        return

    if cooldown_active(strategy.name, config.cooldown_bars):
        logger.debug("cooldown_active")
        return

    quote = fetch_quote(market.yes_token_id, market.no_token_id)
    if quote is None or quote.yes_mid is None:
        logger.debug("no_quote")
        return

    bars = load_bars(limit=300)  # warmup window
    bankroll = latest_equity() or config.starting_bankroll
    ctx = StrategyContext(
        bankroll=bankroll, edge_threshold=config.edge_threshold,
        kelly_fraction=config.kelly_fraction, max_bet_pct=config.max_bet_pct,
        min_market_depth_usd=config.min_market_depth_usd,
    )
    bet = strategy.on_market(market, bars, quote, ctx)
    if bet is None:
        logger.debug("no_bet")
        return

    logger.info(
        "bet_decision",
        side=bet.side, edge=round(bet.edge, 4),
        predicted_p=round(bet.predicted_p, 4),
        market_p=round(bet.market_p, 4),
        stake=round(bet.stake, 4),
    )
    router.execute(bet)


def _has_open_bet_on(market_id: str) -> bool:
    from polymarket_bot.persistence.schema import get_conn
    row = get_conn().execute(
        "SELECT 1 FROM bets WHERE market_id=? AND status='open' LIMIT 1",
        (market_id,),
    ).fetchone()
    return row is not None


def _market_to_db(m: DiscoveredMarket) -> Market:
    return Market(
        market_id=m.market_id, slug=m.slug, resolution_ts=m.resolution_ts,
        yes_token_id=m.yes_token_id, no_token_id=m.no_token_id,
    )


def _settle_due_markets(config: BotConfig) -> None:
    """Close out any open bets whose underlying market has resolved."""
    from polymarket_bot.persistence.schema import get_conn
    conn = get_conn()
    now = int(time.time())
    rows = conn.execute(
        "SELECT m.market_id, m.slug, m.resolution_ts, m.yes_token_id, m.no_token_id "
        "FROM markets m WHERE m.outcome IS NULL AND m.resolution_ts <= ? "
        "AND EXISTS (SELECT 1 FROM bets b WHERE b.market_id=m.market_id AND b.status='open')",
        (now,),
    ).fetchall()
    for mid, slug, res_ts, yes, no in rows:
        # The comparison bar opens at res_ts - 300 and closes at res_ts.
        bars = load_bars(from_ts=res_ts - 300, to_ts=res_ts + 1)
        if not bars:
            logger.warning("settle_skip_no_bar", market_id=mid[:12], resolution_ts=res_ts)
            continue
        bar = bars[-1]
        market = DiscoveredMarket(
            market_id=mid, slug=slug, start_ts=res_ts - 300, resolution_ts=res_ts,
            yes_token_id=yes, no_token_id=no,
        )
        bankroll = latest_equity() or config.starting_bankroll
        settle_resolved_market(market, bar.o, bar.c, bankroll_at_settle=bankroll)


def cmd_backfill(config: BotConfig, args: argparse.Namespace) -> None:
    init_db()
    new_btc = backfill(days=args.days)
    print(f"BTC: {new_btc} bars")
    if not args.no_aux:
        from polymarket_bot.data.aux_feed import backfill_all_aux
        aux = backfill_all_aux(days=args.days)
        print(f"ETH: {aux['eth']} bars | BTC perp: {aux['btc_perp']} bars | "
              f"funding: {aux['funding']} points")


def cmd_train(config: BotConfig, args: argparse.Namespace) -> None:
    init_db()
    from polymarket_bot.model.trainer import cv_metrics_for_active_model
    model = train_logit(
        window_days=args.window_days,
        cv=not args.no_cv,
        cv_train_days=args.cv_train_days,
        cv_step_days=args.cv_step_days,
    )
    if model is None:
        print("Training failed — likely insufficient bars. Run `polymarket-bot backfill` first.")
        sys.exit(1)
    print(f"\nTrained {model.version}\n")
    cv = cv_metrics_for_active_model()
    if cv and cv.get("cv_brier") is not None:
        edge_pp = (cv["cv_brier_baseline"] - cv["cv_brier"]) * 100
        sig = "***" if cv["cv_p_value"] < 0.01 else ("**" if cv["cv_p_value"] < 0.05 else (
              "*"  if cv["cv_p_value"] < 0.10 else "ns"))
        print("Walk-forward OOS performance:")
        print(f"  folds:           {cv['cv_folds']}")
        print(f"  Brier (model):   {cv['cv_brier']:.5f}")
        print(f"  Brier (no-skill): {cv['cv_brier_baseline']:.5f}")
        print(f"  edge:            {edge_pp:+.3f} pp Brier (positive = model better)")
        print(f"  95% CI on diff:  [{cv['cv_brier_ci_lo']:+.5f}, {cv['cv_brier_ci_hi']:+.5f}]")
        print(f"  p-value:         {cv['cv_p_value']:.4f}  ({sig})")
        if cv["cv_p_value"] >= 0.05:
            print("\n  ⚠️  No statistically significant edge. Don't bet real money on this.")


def cmd_backtest(config: BotConfig, args: argparse.Namespace) -> None:
    init_db()
    from polymarket_bot.backtest import BacktestConfig, run_backtest
    from polymarket_bot.backtest.report import dump_json, print_table

    StrategyClass = get_strategy_class(args.strategy)
    model = load_active_model()
    if model is None:
        print("No trained model. Run `polymarket-bot train` first.", file=sys.stderr)
        sys.exit(1)
    strategy = StrategyClass(model=model)

    fmt = "%Y-%m-%d"
    from_ts = int(datetime.strptime(args.from_, fmt).replace(tzinfo=timezone.utc).timestamp())
    to_ts = int(datetime.strptime(args.to, fmt).replace(tzinfo=timezone.utc).timestamp())
    cfg = BacktestConfig(
        from_ts=from_ts, to_ts=to_ts,
        starting_bankroll=config.starting_bankroll,
        edge_threshold=args.edge_threshold,
        kelly_fraction=args.kelly_fraction,
        max_bet_pct=config.max_bet_pct,
        min_market_depth_usd=config.min_market_depth_usd,
        fee_bps=args.fee_bps, slip_bps=args.slip_bps,
    )
    result = run_backtest(strategy, cfg)
    print_table(result)
    if args.out:
        dump_json(result, Path(args.out))
        print(f"\nWrote {args.out}")


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def main() -> None:
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    parser = argparse.ArgumentParser(prog="polymarket-bot", description="Auto-bettor on Polymarket BTC up/down 5m markets.")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="Run the bot in paper or live mode (paper by default).")
    p_run.add_argument("--live", action="store_true",
                       help="Place real orders (requires POLYMARKET_BOT_LIVE=1).")

    p_bf = sub.add_parser("backfill", help="Cache BTC + aux (ETH spot, BTC perp, funding) bars.")
    p_bf.add_argument("--days", type=int, default=365)
    p_bf.add_argument("--no-aux", action="store_true",
                      help="Skip ETH / perp / funding (BTC only).")

    p_tr = sub.add_parser("train", help="Train a strategy's probability model with walk-forward CV.")
    p_tr.add_argument("--window-days", type=int, default=365,
                      help="Total span of data used for the final fit.")
    p_tr.add_argument("--cv-train-days", type=int, default=60,
                      help="Per-fold training window for walk-forward.")
    p_tr.add_argument("--cv-step-days", type=int, default=14,
                      help="Step between folds.")
    p_tr.add_argument("--no-cv", action="store_true",
                      help="Skip walk-forward CV (faster but no honest OOS metrics).")

    p_bt = sub.add_parser("backtest", help="Replay cached bars + recorded quotes through a strategy.")
    p_bt.add_argument("--strategy", default="momentum_logit")
    p_bt.add_argument("--from", dest="from_", required=True, help="YYYY-MM-DD (UTC)")
    p_bt.add_argument("--to", required=True, help="YYYY-MM-DD (UTC)")
    p_bt.add_argument("--edge-threshold", type=float, default=0.03)
    p_bt.add_argument("--kelly-fraction", type=float, default=0.25)
    p_bt.add_argument("--fee-bps", type=float, default=0.0)
    p_bt.add_argument("--slip-bps", type=float, default=5.0)
    p_bt.add_argument("--out", default=None, help="Path to dump JSON results.")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return

    config = BotConfig.from_env()
    configure_logging(config.log_level)

    {
        "run": cmd_run,
        "backfill": cmd_backfill,
        "train": cmd_train,
        "backtest": cmd_backtest,
    }[args.cmd](config, args)


if __name__ == "__main__":
    main()
