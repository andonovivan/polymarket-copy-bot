"""JSON API handlers for the MM dashboard."""

from __future__ import annotations

import time
from typing import Any

from polymarket_bot.config import BotConfig
from polymarket_bot.persistence.repo import (
    all_open_orders,
    equity_curve,
    get_market,
    inventory_for_market,
    latest_equity,
    list_fills,
    list_settlements,
    markets_with_unsettled_fills,
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
        # Inventory comes from FILLS (held shares awaiting settlement); open
        # orders are listed separately. Each inventory row is enriched with the
        # latest observed YES price so the dashboard can show unrealized P&L.
        orders = all_open_orders()
        order_market_ids = {o.market_id for o in orders}
        unsettled = set(markets_with_unsettled_fills())
        inv_market_ids = sorted(unsettled | order_market_ids)
        inventories = []
        total_cost = 0.0
        priced_cost = 0.0       # only positions for which we have a current mid
        priced_mtm = 0.0
        unpriced_count = 0
        for mid in inv_market_ids:
            yes, no, avg_yes, avg_no = inventory_for_market(mid)
            if yes == 0 and no == 0:
                continue
            m = get_market(mid)
            title = (m.title if m and m.title else None)
            current_yes = (m.last_yes_mid if m and m.last_yes_mid is not None else None)
            quoted_at = (m.last_quote_ts if m else None)
            cost = yes * avg_yes + no * avg_no
            mtm = (yes * current_yes if current_yes is not None else None)
            unreal = (mtm - cost) if mtm is not None else None
            total_cost += cost
            if mtm is not None:
                priced_cost += cost
                priced_mtm += mtm
            else:
                unpriced_count += 1
            inventories.append({
                "market_id": mid,
                "title": title,
                "yes_shares": yes, "no_shares": no,
                "avg_yes_cost": avg_yes, "avg_no_cost": avg_no,
                "current_yes_price": current_yes,
                "current_yes_quoted_at": quoted_at,
                "cost": cost,
                "mtm_value": mtm,
                "unrealized_pnl": unreal,
            })
        # Annotate orders with their human title.
        order_dicts = []
        for o in orders:
            d = _order_dict(o)
            mm = get_market(o.market_id)
            d["title"] = mm.title if mm and mm.title else None
            order_dicts.append(d)
        # PnL is computed only over positions we can actually mark; mixing unpriced
        # cost into the denominator would silently bias PnL low by that cost.
        unrealized = (priced_mtm - priced_cost) if priced_cost > 0 else None
        return 200, {
            "open_orders": order_dicts,
            "inventories": inventories,
            "count": len(orders) + len(inventories),
            "totals": {
                "cost": total_cost,
                "mtm_value": priced_mtm,
                "priced_cost": priced_cost,
                "unpriced_cost": total_cost - priced_cost,
                "unpriced_positions": unpriced_count,
                "unrealized_pnl": unrealized,
            },
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
        rows = list_fills(limit=limit)
        out = []
        for f in rows:
            d = _fill_dict(f)
            mm = get_market(f.market_id)
            d["title"] = mm.title if mm and mm.title else None
            out.append(d)
        return 200, {"fills": out}

    if path == "/api/settlements":
        limit = int(qs.get("limit", ["50"])[0])
        rows = list_settlements(limit=limit)
        out = []
        for s in rows:
            d = _settlement_dict(s)
            mm = get_market(s.market_id)
            d["title"] = mm.title if mm and mm.title else None
            out.append(d)
        return 200, {"settlements": out}

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
