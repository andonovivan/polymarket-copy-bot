"""Pretty-print backtest results to stdout and dump JSON."""

from __future__ import annotations

import json
from pathlib import Path

from polymarket_bot.backtest.engine import BacktestResult


def print_table(result: BacktestResult) -> None:
    m = result.metrics
    if not m or m.get("trades", 0) == 0:
        print("No trades.")
        return
    rows = [
        ("Trades",        m["trades"]),
        ("Win rate",      f"{m['win_rate']:.1%}"),
        ("Profit factor", f"{m['profit_factor']:.2f}"),
        ("Expectancy $",  f"{m['expectancy']:.4f}"),
        ("Total PnL $",   f"{m['total_pnl']:.2f}"),
        ("Sharpe",        f"{m['sharpe']:.2f}"),
        ("Max DD",        f"{m['max_dd']:.1%}"),
        ("Brier",         f"{m['brier']:.4f}"),
        ("Log loss",      f"{m['log_loss']:.4f}"),
        ("Fees $",        f"{m['total_fees']:.2f}"),
        ("Slippage $",    f"{m['total_slippage']:.2f}"),
    ]
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        print(f"  {k.ljust(width)}  {v}")


def dump_json(result: BacktestResult, path: Path) -> None:
    payload = {
        "metrics": result.metrics,
        "equity": [[ts, eq] for ts, eq in result.equity],
        "trades": [vars(t) for t in result.trades],
    }
    path.write_text(json.dumps(payload, indent=2))
