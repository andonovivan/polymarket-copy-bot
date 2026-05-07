"""JSON API handlers for the MM dashboard."""

from __future__ import annotations

import time
from typing import Any

from polymarket_bot.config import BotConfig
from polymarket_bot.persistence.repo import (
    all_open_orders,
    daily_pnl_summary,
    equity_curve,
    get_market,
    inventory_snapshot,
    latest_equity,
    list_fills,
    list_settlements,
    markets_bulk,
    markets_with_unsettled_fills,
    settlement_stats,
    strategy_pnl_summary,
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


def _build_position_payload() -> dict[str, Any]:
    """Build the open-orders + inventories + totals block.

    Replaces the old N+1 pattern (one `inventory_for_market` and one
    `get_market` call per row) with two bulk queries: `inventory_snapshot`
    and `markets_bulk`.
    """
    orders = all_open_orders()
    order_market_ids = {o.market_id for o in orders}
    unsettled = set(markets_with_unsettled_fills())
    inv_market_ids = sorted(unsettled | order_market_ids)

    inv_map = inventory_snapshot(inv_market_ids)
    market_map = markets_bulk(sorted(set(inv_market_ids) | order_market_ids))

    inventories: list[dict[str, Any]] = []
    total_cost = 0.0
    priced_cost = 0.0       # only positions for which we have a current mid
    priced_mtm = 0.0
    unpriced_count = 0
    for mid in inv_market_ids:
        yes, no, avg_yes, avg_no = inv_map.get(mid, (0.0, 0.0, 0.0, 0.0))
        if yes == 0 and no == 0:
            continue
        m = market_map.get(mid)
        title = m.title if (m and m.title) else None
        current_yes = m.last_yes_mid if (m and m.last_yes_mid is not None) else None
        quoted_at = m.last_quote_ts if m else None
        cost = yes * avg_yes + no * avg_no
        mtm = (yes * current_yes) if current_yes is not None else None
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

    order_dicts: list[dict[str, Any]] = []
    for o in orders:
        d = _order_dict(o)
        mm = market_map.get(o.market_id)
        d["title"] = mm.title if (mm and mm.title) else None
        order_dicts.append(d)

    # PnL is computed only over positions we can actually mark; mixing unpriced
    # cost into the denominator would silently bias PnL low by that cost.
    unrealized = (priced_mtm - priced_cost) if priced_cost > 0 else None
    return {
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


def dispatch_get(path: str, qs: dict[str, list[str]], config: BotConfig | None) -> tuple[int, Any]:
    if path == "/api/status":
        return 200, {
            "mode": config.mode if config else "paper",
            "version": VERSION,
            "strategy": config.strategy if config else None,
            "now": int(time.time()),
        }

    if path == "/api/position":
        return 200, _build_position_payload()

    if path == "/api/dashboard":
        # Bundled endpoint that drives the entire Dashboard route in a single
        # request. Replaces the previous 4-parallel-fetch pattern (`/api/status`
        # subset + /api/stats/today + /api/equity-curve + /api/position +
        # /api/fills?limit=15) and adds the daily/strategy aggregates that the
        # new charts need.
        position = _build_position_payload()
        day_start = int(time.time()) - (int(time.time()) % 86400)
        stats = settlement_stats(from_ts=day_start)
        stats["latest_equity"] = latest_equity()
        days = int(qs.get("days", ["30"])[0])
        return 200, {
            "now": int(time.time()),
            "version": VERSION,
            "mode": config.mode if config else "paper",
            "strategy": config.strategy if config else None,
            "stats_today": stats,
            "totals": position["totals"],
            "inventories": position["inventories"],
            "open_orders": position["open_orders"],
            "equity_curve": [
                {"ts": ts, "equity": eq} for ts, eq in equity_curve()
            ],
            "daily_pnl": daily_pnl_summary(days=days),
            "strategy_pnl": strategy_pnl_summary(days=days),
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
        limit = int(qs.get("limit", ["50"])[0])
        offset = int(qs.get("offset", ["0"])[0])
        # Fetch one extra row to detect has_more without a COUNT(*) query.
        rows = list_fills(limit=limit + 1, offset=offset)
        has_more = len(rows) > limit
        rows = rows[:limit]
        out = []
        for f in rows:
            d = _fill_dict(f)
            mm = get_market(f.market_id)
            d["title"] = mm.title if mm and mm.title else None
            out.append(d)
        return 200, {"fills": out, "offset": offset, "has_more": has_more}

    if path == "/api/settlements":
        limit = int(qs.get("limit", ["50"])[0])
        offset = int(qs.get("offset", ["0"])[0])
        rows = list_settlements(limit=limit + 1, offset=offset)
        has_more = len(rows) > limit
        rows = rows[:limit]
        out = []
        for s in rows:
            d = _settlement_dict(s)
            mm = get_market(s.market_id)
            d["title"] = mm.title if mm and mm.title else None
            out.append(d)
        return 200, {"settlements": out, "offset": offset, "has_more": has_more}

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
