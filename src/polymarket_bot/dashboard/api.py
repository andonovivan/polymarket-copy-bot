"""JSON API handlers for the dashboard."""

from __future__ import annotations

import time
from typing import Any

from polymarket_bot.config import BotConfig
from polymarket_bot.persistence.repo import (
    equity_curve,
    latest_equity,
    list_trades,
    open_bets,
    trade_stats,
)
from polymarket_bot.strategy.registry import list_strategies

VERSION = "0.2.0"


def _trade_dict(t) -> dict[str, Any]:
    return {k: getattr(t, k) for k in (
        "id", "market_id", "side", "shares", "entry_price", "payout",
        "pnl", "fees", "predicted_p", "market_p", "edge", "brier",
        "outcome", "strategy", "model_version", "opened_at", "settled_at",
    )}


def _bet_dict(b) -> dict[str, Any]:
    return {k: getattr(b, k) for k in (
        "id", "market_id", "side", "shares", "entry_price", "stake",
        "predicted_p", "market_p", "edge", "strategy", "model_version",
        "opened_at", "status",
    )}


# ---------------------------------------------------------------------------
# GET dispatch
# ---------------------------------------------------------------------------


def dispatch_get(path: str, qs: dict[str, list[str]], config: BotConfig | None) -> tuple[int, Any]:
    if path == "/api/status":
        return 200, {
            "mode": config.mode if config else "paper",
            "version": VERSION,
            "strategy": config.strategy if config else None,
            "now": int(time.time()),
        }
    if path == "/api/position":
        bets = [_bet_dict(b) for b in open_bets()]
        return 200, {"bets": bets, "count": len(bets)}
    if path == "/api/equity-curve":
        f = int(qs.get("from", ["0"])[0]) or None
        t = int(qs.get("to", ["0"])[0]) or None
        curve = equity_curve(f, t)
        return 200, {"points": [{"ts": ts, "equity": eq} for ts, eq in curve]}
    if path == "/api/stats/today":
        day_start = int(time.time()) - (int(time.time()) % 86400)
        s = trade_stats(from_ts=day_start)
        s["latest_equity"] = latest_equity()
        return 200, s
    if path == "/api/fills":
        limit = int(qs.get("limit", ["10"])[0])
        return 200, {"trades": [_trade_dict(t) for t in list_trades(limit=limit)]}
    if path == "/api/bets":
        limit = int(qs.get("size", ["50"])[0])
        offset = int(qs.get("page", ["0"])[0]) * limit
        side = qs.get("side", [None])[0]
        strat = qs.get("strategy", [None])[0]
        f = int(qs.get("from", ["0"])[0]) or None
        t = int(qs.get("to", ["0"])[0]) or None
        rows = list_trades(limit=limit, offset=offset, side=side, strategy=strat, from_ts=f, to_ts=t)
        return 200, {"trades": [_trade_dict(r) for r in rows], "page": offset // limit, "size": limit}
    if path == "/api/strategies":
        from polymarket_bot.model.trainer import cv_metrics_for_active_model
        return 200, {
            "strategies": [
                {"name": n, "enabled": (config.strategy == n if config else False)}
                for n in list_strategies()
            ],
            "active_model": cv_metrics_for_active_model(),
        }
    if path == "/api/settings":
        if config is None:
            return 200, {}
        masked = config.model_dump()
        for k in ("private_key", "api_key", "api_secret", "api_passphrase"):
            if masked.get(k):
                masked[k] = "***"
        return 200, masked
    if path == "/api/logs":
        # Tail not yet wired — return empty for now.
        return 200, {"lines": []}
    return 404, {"error": "not found"}


def dispatch_post(path: str, body: dict, config: BotConfig | None) -> tuple[int, Any]:
    if path == "/api/backtests":
        return 202, {"error": "backtests must be run from the CLI for now"}
    return 404, {"error": "not found"}
