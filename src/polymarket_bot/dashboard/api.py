"""JSON API handlers for the MM dashboard."""

from __future__ import annotations

import time
from typing import Any

from polymarket_bot.config import BotConfig
from polymarket_bot.persistence.repo import (
    all_open_orders,
    daily_pnl_summary,
    equity_curve,
    get_enabled_strategies,
    get_market,
    get_meta,
    inventory_snapshot,
    inventory_snapshot_for_strategies,
    latest_equity,
    list_fills,
    list_settlements,
    markets_bulk,
    markets_with_unsettled_fills,
    meta_keys_with_prefix,
    set_enabled_strategies,
    settlement_stats,
    strategy_pnl_summary,
)
from polymarket_bot.strategy.registry import get_display_name, list_strategies

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


def _build_position_payload(strategies: list[str] | None = None) -> dict[str, Any]:
    """Build the open-orders + inventories + totals block.

    Replaces the old N+1 pattern (one `inventory_for_market` and one
    `get_market` call per row) with two bulk queries: `inventory_snapshot`
    and `markets_bulk`.

    `strategies=None` (default) keeps the cross-strategy view used by the
    bare /api/position endpoint and the dashboard when no chip filter is
    active. When the chip filter narrows the view, the dashboard caller
    passes the selected strategies and inventories / open orders / totals
    are sliced accordingly — so the cards visibly respond to the filter.
    """
    orders = all_open_orders(strategies=strategies)
    order_market_ids = {o.market_id for o in orders}
    unsettled = set(markets_with_unsettled_fills(strategies=strategies))
    inv_market_ids = sorted(unsettled | order_market_ids)

    if strategies:
        inv_map = inventory_snapshot_for_strategies(strategies, inv_market_ids)
    else:
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


STALE_SERVICE_THRESHOLD_SECONDS = 300


def _int_or_none(s: str | None) -> int | None:
    if s is None:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def build_health_payload(now: int) -> dict[str, Any]:
    """Surface bot-wide alerts: HALT, forecast rate-limit, stale services.

    Each meta key is best-effort — missing keys mean "never tripped". The
    dashboard renders a banner when any field is non-null/non-empty.
    """
    halted_at = _int_or_none(get_meta("halted_at"))
    rl_until = _int_or_none(get_meta("forecast_rate_limited_until"))
    # `forecast_rate_limited_until` only matters while it's still in the
    # future; surface as None once the backoff window has passed.
    if rl_until is not None and rl_until <= now:
        rl_until = None
    heartbeats = meta_keys_with_prefix("last_running_ts:")
    stale: list[dict[str, Any]] = []
    for key, value in heartbeats.items():
        ts = _int_or_none(value)
        if ts is None:
            continue
        age = now - ts
        if age > STALE_SERVICE_THRESHOLD_SECONDS:
            stale.append({
                "service": key.removeprefix("last_running_ts:"),
                "age_seconds": age,
                "last_seen_ts": ts,
            })
    stale.sort(key=lambda r: r["age_seconds"], reverse=True)
    return {
        "halted_at": halted_at,
        "halt_reason": get_meta("halt_reason"),
        "forecast_rate_limited_until": rl_until,
        "stale_services": stale,
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
        day_start = int(time.time()) - (int(time.time()) % 86400)
        days = int(qs.get("days", ["30"])[0])

        # Optional ?strategies=A,B filter. Default: all enabled strategies.
        # When the user narrows the chip filter, slice the WHOLE payload —
        # totals, inventories, open orders, settlement aggregates — so the
        # cards visibly respond. Equity curve stays bot-wide (it's the
        # bot's MTM, not strategy-attributable in a clean way).
        all_names = list_strategies()
        enabled = get_enabled_strategies(default_all=all_names)
        filter_param = qs.get("strategies", [""])[0].strip()
        filter_set = (
            {n.strip() for n in filter_param.split(",") if n.strip()}
            if filter_param else enabled
        )
        # Treat "all enabled" the same as "no filter" — empty filter list
        # means the helpers skip the AND clause entirely (faster).
        filter_arg = sorted(filter_set) if filter_set != set(all_names) else None

        position = _build_position_payload(strategies=filter_arg)

        stats = settlement_stats(from_ts=day_start, strategies=filter_arg)
        stats["latest_equity"] = latest_equity()

        all_strategy_pnl = strategy_pnl_summary(days=days)
        strategy_pnl_filtered = [
            {**row, "display_name": get_display_name(row["strategy"])}
            for row in all_strategy_pnl
            if not filter_set or row["strategy"] in filter_set
        ]
        now_ts = int(time.time())
        return 200, {
            "now": now_ts,
            "version": VERSION,
            "mode": config.mode if config else "paper",
            "strategy": config.strategy if config else None,
            "filtered_strategies": sorted(filter_set),
            "stats_today": stats,
            "totals": position["totals"],
            "inventories": position["inventories"],
            "open_orders": position["open_orders"],
            "equity_curve": [
                {"ts": ts, "equity": eq} for ts, eq in equity_curve()
            ],
            "daily_pnl": daily_pnl_summary(days=days, strategies=filter_arg),
            "strategy_pnl": strategy_pnl_filtered,
            "health": build_health_payload(now_ts),
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
        all_names = list_strategies()
        enabled = get_enabled_strategies(default_all=all_names)
        return 200, {
            "strategies": [
                {
                    "name": n,
                    "display_name": get_display_name(n),
                    "enabled": n in enabled,
                }
                for n in all_names
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
    if path == "/api/strategies/enabled":
        # Body: {"names": ["weather_forecast", "bucket_arbitrage"]}.
        # Persists the enabled set; the Router consults it on every
        # `execute()` so the change takes effect on the next tick.
        all_names = set(list_strategies())
        raw = body.get("names")
        if not isinstance(raw, list):
            return 400, {"error": "names must be a list"}
        names = {str(n) for n in raw}
        unknown = names - all_names
        if unknown:
            return 400, {"error": f"unknown strategies: {sorted(unknown)}"}
        set_enabled_strategies(names)
        return 200, {"enabled": sorted(names)}

    return 404, {"error": "not found"}
