"""Bar-replay backtest engine. Same Strategy interface as live."""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from polymarket_bot.backtest.metrics import TradeRow, compute
from polymarket_bot.execution.backtest_broker import BacktestBroker
from polymarket_bot.features.pipeline import WARMUP_BARS
from polymarket_bot.persistence.repo import load_bars
from polymarket_bot.persistence.schema import get_conn
from polymarket_bot.polymarket.markets import DiscoveredMarket
from polymarket_bot.polymarket.quotes import Quote
from polymarket_bot.polymarket.settle import outcome_for_bar
from polymarket_bot.strategy.base import Strategy, StrategyContext

logger = structlog.get_logger()


@dataclass
class BacktestConfig:
    from_ts: int
    to_ts: int
    starting_bankroll: float = 100.0
    edge_threshold: float = 0.03
    kelly_fraction: float = 0.25
    max_bet_pct: float = 0.05
    min_market_depth_usd: float = 50.0
    fee_bps: float = 0.0
    slip_bps: float = 5.0
    seed: int = 42


@dataclass
class BacktestResult:
    metrics: dict
    equity: list[tuple[int, float]] = field(default_factory=list)
    trades: list[TradeRow] = field(default_factory=list)


def _quote_for_bar(market_id: str, ts: int) -> Quote | None:
    """Fetch the recorded quote at-or-before `ts` from polymarket_quotes."""
    conn = get_conn()
    row = conn.execute(
        "SELECT yes_bid, yes_ask, no_bid, no_ask, depth_yes, depth_no "
        "FROM polymarket_quotes WHERE market_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (market_id, ts),
    ).fetchone()
    if not row:
        return None
    yb, ya, nb, na, dy, dn = row
    yes_mid = ((yb + ya) / 2.0) if (yb is not None and ya is not None) else None
    no_mid = ((nb + na) / 2.0) if (nb is not None and na is not None) else None
    return Quote(
        yes_bid=yb, yes_ask=ya, yes_mid=yes_mid,
        no_bid=nb, no_ask=na, no_mid=no_mid,
        depth_yes_ask_usd=dy or 0.0, depth_no_ask_usd=dn or 0.0,
    )


def run_backtest(strategy: Strategy, cfg: BacktestConfig) -> BacktestResult:
    """Replay BTC 5-min bars in [from_ts, to_ts), letting the strategy bet.

    Each bar t is treated as a market resolving at the start of bar t+1. We
    fetch a recorded Polymarket quote at bar t's open_time; if none exists, we
    skip that bar (the bot needs paper-recorded quotes to backtest honestly).
    """
    bars = load_bars(from_ts=cfg.from_ts, to_ts=cfg.to_ts)
    if len(bars) <= WARMUP_BARS + 1:
        logger.warning("backtest_insufficient_bars", have=len(bars))
        return BacktestResult(metrics={"trades": 0})

    # Sanity check: do we have any recorded quotes covering this window?
    conn = get_conn()
    quote_count = conn.execute(
        "SELECT COUNT(*) FROM polymarket_quotes WHERE ts BETWEEN ? AND ?",
        (cfg.from_ts, cfg.to_ts),
    ).fetchone()[0]
    if quote_count == 0:
        logger.warning(
            "backtest_no_recorded_quotes",
            hint="run paper mode to record polymarket_quotes for this window first",
            from_ts=cfg.from_ts, to_ts=cfg.to_ts,
        )

    broker = BacktestBroker(base_slip_bps=cfg.slip_bps, fee_bps=cfg.fee_bps)
    bankroll = cfg.starting_bankroll
    equity: list[tuple[int, float]] = [(bars[WARMUP_BARS].open_time, bankroll)]
    trades: list[TradeRow] = []
    skipped_no_quote = 0

    for i in range(WARMUP_BARS, len(bars) - 1):
        history = bars[: i + 1]
        cur = bars[i]
        nxt = bars[i + 1]

        market = DiscoveredMarket(
            market_id=f"bt-{cur.open_time}",
            slug=f"btc-updown-5m-{cur.open_time}",
            start_ts=cur.open_time,
            resolution_ts=nxt.open_time,
            yes_token_id=f"yes-{cur.open_time}",
            no_token_id=f"no-{cur.open_time}",
        )
        quote = _quote_for_bar(market.market_id, cur.open_time)
        if quote is None:
            skipped_no_quote += 1
            continue

        ctx = StrategyContext(
            bankroll=bankroll, edge_threshold=cfg.edge_threshold,
            kelly_fraction=cfg.kelly_fraction, max_bet_pct=cfg.max_bet_pct,
            min_market_depth_usd=cfg.min_market_depth_usd,
        )
        bet = strategy.on_market(market, history, quote, ctx)
        if bet is None:
            equity.append((nxt.open_time, bankroll))
            continue

        fill = broker.submit(bet)
        if not fill.success:
            equity.append((nxt.open_time, bankroll))
            continue

        outcome = outcome_for_bar(nxt.o, nxt.c)
        outcome_up = outcome == "UP"
        won = (bet.side == "YES" and outcome_up) or (bet.side == "NO" and not outcome_up)
        payout = 1.0 if won else 0.0
        pnl = payout * fill.filled_shares - (fill.filled_shares * fill.filled_price) - fill.fees
        bankroll += pnl
        trades.append(TradeRow(
            pnl=pnl, stake=fill.filled_shares * fill.filled_price,
            predicted_p=bet.predicted_p, outcome_up=outcome_up,
            fees=fill.fees, slippage=fill.slippage,
        ))
        equity.append((nxt.open_time, bankroll))

    if skipped_no_quote and len(trades) == 0:
        logger.warning(
            "backtest_all_bars_skipped",
            skipped=skipped_no_quote,
            hint="record paper-mode quotes first so the engine has data to replay",
        )

    metrics = compute(trades, equity)
    return BacktestResult(metrics=metrics, equity=equity, trades=trades)
