"""JSON API handlers for the MM dashboard."""

from __future__ import annotations

import time
from typing import Any

from polymarket_bot.config import BotConfig
from polymarket_bot.persistence.repo import (
    all_open_orders,
    equity_curve,
    inventory_for_market,
    latest_equity,
    list_fills,
    list_settlements,
    settlement_stats,
)
from polymarket_bot.strategy.registry import list_strategies

VERSION = "0.3.0"


def _order_dict(o) -> dict[str, Any]:
    return {k: getattr(o, k) for k in (
        "order_id", "client_order_id", "market_id", "token_side", "side",
        "price", "size", "filled", "status", "placed_at", "ended_at", "strategy",
    )}


def _fill_dict(f) -> dict[str, Any]:
    return {k: getattr(f, k) for k in (
        "id", "order_id", "market_id", "token_side", "side",
        "price", "size", "fill_ts", "strategy",
    )}


def _settlement_dict(s) -> dict[str, Any]:
    return {k: getattr(s, k) for k in (
        "market_id", "settled_at", "outcome",
        "yes_shares", "no_shares", "avg_yes_cost", "avg_no_cost",
        "payout", "cost", "pnl", "strategy",
    )}


def dispatch_get(path: str, qs: dict[str, list[str]], config: BotConfig | None) -> tuple[int, Any]:
    if path == "/api/status":
        return 200, {
            "mode": config.mode if config else "paper",
            "version": VERSION,
            "strategy": config.strategy if config else None,
            "now": int(time.time()),
        }

    if path == "/api/position":
        # Aggregate inventory across all markets that have open orders or unsettled fills.
        orders = all_open_orders()
        markets = sorted({o.market_id for o in orders})
        inventories = []
        for mid in markets:
            yes, no, avg_yes, avg_no = inventory_for_market(mid)
            inventories.append({
                "market_id": mid,
                "yes_shares": yes, "no_shares": no,
                "avg_yes_cost": avg_yes, "avg_no_cost": avg_no,
            })
        return 200, {
            "open_orders": [_order_dict(o) for o in orders],
            "inventories": inventories,
            "count": len(orders),
        }

    if path == "/api/equity-curve":
        f = int(qs.get("from", ["0"])[0]) or None
        t = int(qs.get("to", ["0"])[0]) or None
        curve = equity_curve(f, t)
        return 200, {"points": [{"ts": ts, "equity": eq} for ts, eq in curve]}

    if path == "/api/stats/today":
        day_start = int(time.time()) - (int(time.time()) % 86400)
        s = settlement_stats(from_ts=day_start)
        s["latest_equity"] = latest_equity()
        return 200, s

    if path == "/api/fills":
        limit = int(qs.get("limit", ["20"])[0])
        return 200, {"fills": [_fill_dict(f) for f in list_fills(limit=limit)]}

    if path == "/api/settlements":
        limit = int(qs.get("limit", ["50"])[0])
        return 200, {"settlements": [_settlement_dict(s) for s in list_settlements(limit=limit)]}

    if path == "/api/strategies":
        return 200, {
            "strategies": [
                {"name": n, "enabled": (config.strategy == n if config else False)}
                for n in list_strategies()
            ],
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
        return 200, {"lines": []}

    return 404, {"error": "not found"}


def dispatch_post(path: str, body: dict, config: BotConfig | None) -> tuple[int, Any]:
    return 404, {"error": "not found"}
