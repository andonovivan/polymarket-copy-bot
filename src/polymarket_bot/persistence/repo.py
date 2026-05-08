"""Typed accessors over the PostgreSQL store. Used by both live and paper brokers.

Phase B (May 2026) replaced the SQLite singleton + threading-lock pattern
with a `psycopg_pool.ConnectionPool`. Every public function below is a
short `with get_pool().connection() as conn:` block — the pool is
thread-safe so the dashboard server thread and the tick-loop thread can
hit it concurrently without external locking.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal

import structlog

from polymarket_bot.persistence.schema import get_pool

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Domain types — column order matches the SELECT order used everywhere below,
# so `dataclass(*row)` works without keyword unpacking.
# ---------------------------------------------------------------------------


@dataclass
class Market:
    market_id: str
    slug: str
    resolution_ts: int
    yes_token_id: str
    no_token_id: str
    outcome: str | None = None
    bar_open: float | None = None
    bar_close: float | None = None
    title: str | None = None
    last_yes_bid: float | None = None
    last_yes_ask: float | None = None
    last_yes_mid: float | None = None
    last_quote_ts: int | None = None


@dataclass
class Order:
    order_id: str
    client_order_id: str
    market_id: str
    token_side: Literal["YES", "NO"]
    side: Literal["BUY", "SELL"]
    price: float
    size: float
    filled: float
    status: str
    placed_at: int
    ended_at: int | None
    strategy: str


@dataclass
class Fill:
    id: int | None
    order_id: str
    market_id: str
    token_side: Literal["YES", "NO"]
    side: Literal["BUY", "SELL"]
    price: float
    size: float
    fill_ts: int
    strategy: str


@dataclass
class Settlement:
    market_id: str
    settled_at: int
    outcome: str
    yes_shares: float
    no_shares: float
    avg_yes_cost: float
    avg_no_cost: float
    payout: float
    cost: float
    pnl: float
    strategy: str


_MARKET_COLS = (
    "market_id, slug, resolution_ts, yes_token_id, no_token_id, "
    "outcome, bar_open, bar_close, title, "
    "last_yes_bid, last_yes_ask, last_yes_mid, last_quote_ts"
)


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------


def upsert_market(m: Market) -> None:
    """Insert or update a market row, preserving cached quote columns when present."""
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO markets "
            "(market_id, slug, title, resolution_ts, yes_token_id, no_token_id, "
            " outcome, bar_open, bar_close, discovered_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (market_id) DO UPDATE SET "
            "  slug = EXCLUDED.slug, "
            "  title = COALESCE(EXCLUDED.title, markets.title), "
            "  resolution_ts = EXCLUDED.resolution_ts, "
            "  yes_token_id = EXCLUDED.yes_token_id, "
            "  no_token_id = EXCLUDED.no_token_id, "
            "  outcome = COALESCE(EXCLUDED.outcome, markets.outcome), "
            "  bar_open = COALESCE(EXCLUDED.bar_open, markets.bar_open), "
            "  bar_close = COALESCE(EXCLUDED.bar_close, markets.bar_close)",
            (m.market_id, m.slug, m.title, m.resolution_ts, m.yes_token_id,
             m.no_token_id, m.outcome, m.bar_open, m.bar_close, int(time.time())),
        )


def update_market_quote(market_id: str, yes_bid: float | None,
                        yes_ask: float | None) -> None:
    """Cache the most recent YES bid/ask/mid for MTM display.

    Mid policy:
      - both sides present → average
      - only one side      → that side
      - neither side       → keep previous mid (don't NULL it out)
    `last_quote_ts` only advances when this poll produced *some* usable price.
    """
    if yes_bid is not None and yes_ask is not None:
        yes_mid = (yes_bid + yes_ask) / 2.0
    elif yes_bid is not None:
        yes_mid = yes_bid
    elif yes_ask is not None:
        yes_mid = yes_ask
    else:
        yes_mid = None
    with get_pool().connection() as conn:
        if yes_mid is None:
            conn.execute(
                "UPDATE markets SET last_yes_bid=%s, last_yes_ask=%s WHERE market_id=%s",
                (yes_bid, yes_ask, market_id),
            )
        else:
            conn.execute(
                "UPDATE markets SET last_yes_bid=%s, last_yes_ask=%s, "
                "last_yes_mid=%s, last_quote_ts=%s WHERE market_id=%s",
                (yes_bid, yes_ask, yes_mid, int(time.time()), market_id),
            )


def get_market(market_id: str) -> Market | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            f"SELECT {_MARKET_COLS} FROM markets WHERE market_id=%s",
            (market_id,),
        ).fetchone()
    return Market(*row) if row else None


def markets_bulk(market_ids: list[str]) -> dict[str, Market]:
    """Single SELECT for many market_ids — replaces N+1 `get_market` loops."""
    if not market_ids:
        return {}
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"SELECT {_MARKET_COLS} FROM markets WHERE market_id = ANY(%s)",
            (list(market_ids),),
        ).fetchall()
    return {r[0]: Market(*r) for r in rows}


def settle_market_row(market_id: str, outcome: str,
                      bar_open: float, bar_close: float) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE markets SET outcome=%s, bar_open=%s, bar_close=%s WHERE market_id=%s",
            (outcome, bar_open, bar_close, market_id),
        )


def unsettled_markets_due(now_ts: int) -> list[Market]:
    """Markets whose resolution_ts has passed but outcome is still NULL,
    AND for which we have at least one fill (i.e., we hold inventory)."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"SELECT {_MARKET_COLS} FROM markets m "
            "WHERE m.outcome IS NULL AND m.resolution_ts <= %s "
            "AND EXISTS (SELECT 1 FROM fills f WHERE f.market_id = m.market_id)",
            (now_ts,),
        ).fetchall()
    return [Market(*r) for r in rows]


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def insert_order(o: Order) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO orders (order_id, client_order_id, market_id, token_side, side, "
            "price, size, filled, status, placed_at, ended_at, strategy) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (o.order_id, o.client_order_id, o.market_id, o.token_side, o.side,
             o.price, o.size, o.filled, o.status, o.placed_at, o.ended_at, o.strategy),
        )


_ORDER_COLS = (
    "order_id, client_order_id, market_id, token_side, side, price, size, "
    "filled, status, placed_at, ended_at, strategy"
)


def open_orders_by_market(market_id: str) -> list[Order]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"SELECT {_ORDER_COLS} FROM orders WHERE market_id=%s AND status='open'",
            (market_id,),
        ).fetchall()
    return [Order(*r) for r in rows]


def all_open_orders(strategies: list[str] | None = None) -> list[Order]:
    """All open orders, optionally narrowed to a set of strategies.

    Pass `strategies=None` (default) for the cross-strategy view used by
    /api/dashboard's unfiltered render and the orders-watcher reconciliation
    pass. Pass a non-empty list to filter — e.g. when the dashboard chip
    filter narrows to a single strategy.
    """
    sql = f"SELECT {_ORDER_COLS} FROM orders WHERE status='open'"
    params: tuple[Any, ...] = ()
    if strategies:
        sql += " AND strategy = ANY(%s)"
        params = (list(strategies),)
    sql += " ORDER BY placed_at DESC"
    with get_pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [Order(*r) for r in rows]


def update_order_filled(order_id: str, filled: float, status: str,
                        ended_at: int | None) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE orders SET filled=%s, status=%s, ended_at=%s WHERE order_id=%s",
            (filled, status, ended_at, order_id),
        )


def cancel_order_row(order_id: str) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE orders SET status='cancelled', ended_at=%s "
            "WHERE order_id=%s AND status='open'",
            (int(time.time()), order_id),
        )


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------


_FILL_COLS = "id, order_id, market_id, token_side, side, price, size, fill_ts, strategy"


def insert_fill(f: Fill) -> int:
    with get_pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO fills (order_id, market_id, token_side, side, price, size, "
            "fill_ts, strategy) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (f.order_id, f.market_id, f.token_side, f.side, f.price, f.size,
             f.fill_ts, f.strategy),
        ).fetchone()
    return int(row[0])


def fills_for_market(market_id: str) -> list[Fill]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"SELECT {_FILL_COLS} FROM fills WHERE market_id=%s ORDER BY fill_ts",
            (market_id,),
        ).fetchall()
    return [Fill(*r) for r in rows]


def fills_for_order(order_id: str) -> list[Fill]:
    """All fills recorded against one CLOB order, ordered by fill_ts.

    Used by `LiveBroker._estimate_new_chunk_price` to subtract previously-
    recorded paid amount from the cumulative weighted-avg of CLOB trades.
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"SELECT {_FILL_COLS} FROM fills WHERE order_id=%s ORDER BY fill_ts",
            (order_id,),
        ).fetchall()
    return [Fill(*r) for r in rows]


def list_fills(limit: int = 100, offset: int = 0) -> list[Fill]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"SELECT {_FILL_COLS} FROM fills ORDER BY fill_ts DESC LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()
    return [Fill(*r) for r in rows]


# ---------------------------------------------------------------------------
# Inventory & PnL (computed from fills)
# ---------------------------------------------------------------------------


def markets_with_unsettled_fills(strategies: list[str] | None = None) -> list[str]:
    """Market_ids with at least one fill and no Settlement row yet.

    `strategies=None` returns the cross-strategy union (default). Passing a
    list narrows to fills tagged with those strategies — used by the
    dashboard's per-strategy chip filter so an inventory list only shows
    positions the selected strategies actually opened.
    """
    sql = (
        "SELECT DISTINCT f.market_id FROM fills f "
        "WHERE NOT EXISTS (SELECT 1 FROM settlements s WHERE s.market_id = f.market_id)"
    )
    params: tuple[Any, ...] = ()
    if strategies:
        sql += " AND f.strategy = ANY(%s)"
        params = (list(strategies),)
    sql += " ORDER BY f.market_id"
    with get_pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [r[0] for r in rows]


def inventory_for_market(market_id: str) -> tuple[float, float, float, float]:
    """Return (yes_shares, no_shares, avg_yes_cost, avg_no_cost) for an open market."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT token_side, side, SUM(size), SUM(size*price) "
            "FROM fills WHERE market_id=%s GROUP BY token_side, side",
            (market_id,),
        ).fetchall()
    return _aggregate_inventory_rows(rows)


def _aggregate_inventory_rows(rows) -> tuple[float, float, float, float]:
    """Reduce a fills aggregation to (yes_shares, no_shares, avg_yes, avg_no)."""
    yes_buy_size = yes_buy_notional = yes_sell_size = 0.0
    no_buy_size = no_buy_notional = no_sell_size = 0.0
    for row in rows:
        token, side, total_size, total_notional = row[-4:]
        if token == "YES" and side == "BUY":
            yes_buy_size, yes_buy_notional = float(total_size), float(total_notional)
        elif token == "YES" and side == "SELL":
            yes_sell_size = float(total_size)
        elif token == "NO" and side == "BUY":
            no_buy_size, no_buy_notional = float(total_size), float(total_notional)
        elif token == "NO" and side == "SELL":
            no_sell_size = float(total_size)
    yes_shares = yes_buy_size - yes_sell_size
    no_shares = no_buy_size - no_sell_size
    avg_yes = (yes_buy_notional / yes_buy_size) if yes_buy_size > 0 else 0.0
    avg_no = (no_buy_notional / no_buy_size) if no_buy_size > 0 else 0.0
    return yes_shares, no_shares, avg_yes, avg_no


def inventory_snapshot(
    market_ids: list[str],
) -> dict[str, tuple[float, float, float, float]]:
    """Bulk inventory for many markets in a single SQL pass.

    Returns dict {market_id → (yes_shares, no_shares, avg_yes, avg_no)}. Empty
    list → empty dict. Missing markets are absent (no zero-fill); callers
    should default with `dict.get(mid, (0, 0, 0, 0))`.
    """
    if not market_ids:
        return {}
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT market_id, token_side, side, SUM(size), SUM(size*price) "
            "FROM fills WHERE market_id = ANY(%s) "
            "GROUP BY market_id, token_side, side",
            (list(market_ids),),
        ).fetchall()
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(r[0], []).append(r)
    return {mid: _aggregate_inventory_rows(rs) for mid, rs in grouped.items()}


def inventory_snapshot_for(
    strategy: str, market_ids: list[str],
) -> dict[str, tuple[float, float, float, float]]:
    """Per-strategy version of `inventory_snapshot` (Phase C).

    With multiple strategies sharing the DB, each strategy needs to see only
    its own positions when sizing. Filters by `fills.strategy`.
    """
    if not market_ids:
        return {}
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT market_id, token_side, side, SUM(size), SUM(size*price) "
            "FROM fills WHERE strategy=%s AND market_id = ANY(%s) "
            "GROUP BY market_id, token_side, side",
            (strategy, list(market_ids)),
        ).fetchall()
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(r[0], []).append(r)
    return {mid: _aggregate_inventory_rows(rs) for mid, rs in grouped.items()}


def inventory_snapshot_for_strategies(
    strategies: list[str], market_ids: list[str],
) -> dict[str, tuple[float, float, float, float]]:
    """Inventory aggregated across the given strategies.

    Used by the dashboard's chip filter so the position cards / inventory
    table reflect only the user-selected strategies. Differs from
    `inventory_snapshot_for` (single strategy) by accepting a list and
    summing across them in one SQL pass.
    """
    if not market_ids or not strategies:
        return {}
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT market_id, token_side, side, SUM(size), SUM(size*price) "
            "FROM fills WHERE strategy = ANY(%s) AND market_id = ANY(%s) "
            "GROUP BY market_id, token_side, side",
            (list(strategies), list(market_ids)),
        ).fetchall()
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(r[0], []).append(r)
    return {mid: _aggregate_inventory_rows(rs) for mid, rs in grouped.items()}


def total_open_exposure_for(strategy: str) -> float:
    """Sum (yes_shares × avg_yes_cost) across the strategy's unsettled fills.

    Mirrors `_total_open_exposure_usd` in main.py but scoped to one
    strategy. Used by the per-strategy bankroll cap (Phase C.4).
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT market_id, token_side, side, SUM(size), SUM(size*price) "
            "FROM fills f "
            "WHERE strategy=%s AND NOT EXISTS ("
            "  SELECT 1 FROM settlements s "
            "  WHERE s.market_id=f.market_id AND s.strategy=f.strategy) "
            "GROUP BY market_id, token_side, side",
            (strategy,),
        ).fetchall()
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(r[0], []).append(r)
    total = 0.0
    for rs in grouped.values():
        yes, _, avg_yes, _ = _aggregate_inventory_rows(rs)
        total += yes * avg_yes
    return total


# ---------------------------------------------------------------------------
# Settlements
# ---------------------------------------------------------------------------


_SETTLEMENT_COLS = (
    "market_id, settled_at, outcome, yes_shares, no_shares, "
    "avg_yes_cost, avg_no_cost, payout, cost, pnl, strategy"
)


def insert_settlement(s: Settlement) -> None:
    """Upsert a settlement row.

    Phase C: the PK is the composite (market_id, strategy), so a single
    market can have one row per strategy that took fills on it. The
    `strategy` column is therefore omitted from the SET clause — it's
    part of the conflict target.
    """
    with get_pool().connection() as conn:
        conn.execute(
            f"INSERT INTO settlements ({_SETTLEMENT_COLS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (market_id, strategy) DO UPDATE SET "
            "  settled_at = EXCLUDED.settled_at, "
            "  outcome = EXCLUDED.outcome, "
            "  yes_shares = EXCLUDED.yes_shares, "
            "  no_shares = EXCLUDED.no_shares, "
            "  avg_yes_cost = EXCLUDED.avg_yes_cost, "
            "  avg_no_cost = EXCLUDED.avg_no_cost, "
            "  payout = EXCLUDED.payout, "
            "  cost = EXCLUDED.cost, "
            "  pnl = EXCLUDED.pnl",
            (s.market_id, s.settled_at, s.outcome, s.yes_shares, s.no_shares,
             s.avg_yes_cost, s.avg_no_cost, s.payout, s.cost, s.pnl, s.strategy),
        )


def list_settlements(limit: int = 100, offset: int = 0) -> list[Settlement]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"SELECT {_SETTLEMENT_COLS} FROM settlements "
            "ORDER BY settled_at DESC LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()
    return [Settlement(*r) for r in rows]


def settlement_stats(from_ts: int | None = None,
                     to_ts: int | None = None,
                     strategies: list[str] | set[str] | None = None) -> dict[str, Any]:
    """Aggregate settlement stats. Optional `strategies` filters by
    `settlements.strategy` (Phase A.4 — the dashboard's chip filter)."""
    sql = ("SELECT COUNT(*), COALESCE(SUM(pnl),0), "
           "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) FROM settlements WHERE 1=1")
    args: list[Any] = []
    if from_ts is not None:
        sql += " AND settled_at>=%s"; args.append(from_ts)
    if to_ts is not None:
        sql += " AND settled_at<%s"; args.append(to_ts)
    if strategies:
        sql += " AND strategy = ANY(%s)"
        args.append(list(strategies))
    with get_pool().connection() as conn:
        n, pnl, wins = conn.execute(sql, args).fetchone()
    return {
        "settlements": int(n or 0),
        "pnl": float(pnl or 0.0),
        "wins": int(wins or 0),
        "win_rate": (float(wins or 0) / n) if n else 0.0,
    }


def daily_pnl_summary(days: int = 30,
                      strategies: list[str] | set[str] | None = None) -> list[dict[str, Any]]:
    """Per-day rollup of settlements over the last `days` days.

    Used by the dashboard's Daily PnL bar chart and the Win-rate sparkline.
    Days with zero settlements simply don't appear in the result.

    Optional `strategies` filters by `settlements.strategy` (Phase A.4 —
    the dashboard's chip filter).
    """
    floor_ts = int(time.time()) - days * 86400
    sql = (
        "SELECT to_char(to_timestamp(settled_at), 'YYYY-MM-DD') AS day, "
        "       COUNT(*), COALESCE(SUM(pnl), 0), "
        "       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) "
        "FROM settlements WHERE settled_at >= %s "
    )
    args: list[Any] = [floor_ts]
    if strategies:
        sql += "AND strategy = ANY(%s) "
        args.append(list(strategies))
    sql += "GROUP BY day ORDER BY day"
    with get_pool().connection() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [
        {"date": r[0], "n_settlements": int(r[1] or 0),
         "pnl": float(r[2] or 0.0), "n_wins": int(r[3] or 0)}
        for r in rows
    ]


def strategy_pnl_summary(days: int = 30) -> list[dict[str, Any]]:
    """Per-strategy rollup of settlements over the last `days` days."""
    floor_ts = int(time.time()) - days * 86400
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT strategy, COUNT(*), COALESCE(SUM(pnl), 0), "
            "       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) "
            "FROM settlements WHERE settled_at >= %s "
            "GROUP BY strategy ORDER BY SUM(pnl) DESC",
            (floor_ts,),
        ).fetchall()
    return [
        {"strategy": r[0], "n_settlements": int(r[1] or 0),
         "pnl": float(r[2] or 0.0), "n_wins": int(r[3] or 0)}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------


def append_equity(ts: int, equity: float) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO equity_curve (ts, equity) VALUES (%s, %s) "
            "ON CONFLICT (ts) DO UPDATE SET equity = EXCLUDED.equity",
            (ts, equity),
        )


def equity_curve(from_ts: int | None = None,
                 to_ts: int | None = None) -> list[tuple[int, float]]:
    sql = "SELECT ts, equity FROM equity_curve WHERE 1=1"
    args: list[Any] = []
    if from_ts is not None:
        sql += " AND ts>=%s"; args.append(from_ts)
    if to_ts is not None:
        sql += " AND ts<%s"; args.append(to_ts)
    sql += " ORDER BY ts"
    with get_pool().connection() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]


def latest_equity() -> float | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT equity FROM equity_curve ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    return float(row[0]) if row else None


# ---------------------------------------------------------------------------
# Meta key-value
# ---------------------------------------------------------------------------


def get_meta(key: str) -> str | None:
    with get_pool().connection() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=%s", (key,)).fetchone()
    return row[0] if row else None


def set_meta(key: str, value: str) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )


def meta_keys_with_prefix(prefix: str) -> dict[str, str]:
    """Return all meta rows whose key starts with `prefix`. Used by the
    dashboard to discover all `last_running_ts:*` heartbeats at once."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE %s",
            (prefix + "%",),
        ).fetchall()
    return {k: v for k, v in rows}


# ---------------------------------------------------------------------------
# Per-strategy enabled flags (Phase A.3) — persisted in the meta table.
# Stored as a JSON list under `meta['enabled_strategies']`. When the row is
# absent (fresh DB), all strategies are considered enabled — callers wanting
# the "real" set should pass `default_all` from the registry.
# ---------------------------------------------------------------------------

_ENABLED_KEY = "enabled_strategies"


def get_enabled_strategies(default_all: list[str] | None = None) -> set[str]:
    raw = get_meta(_ENABLED_KEY)
    if raw is None:
        return set(default_all or [])
    try:
        names = json.loads(raw)
        if isinstance(names, list):
            return {str(n) for n in names}
    except (ValueError, TypeError):
        pass
    return set(default_all or [])


def set_enabled_strategies(names: set[str] | list[str]) -> None:
    set_meta(_ENABLED_KEY, json.dumps(sorted(set(names))))


# ---------------------------------------------------------------------------
# Forecast cache (Phase C.5) — shared across strategy services so each one
# doesn't independently hit Open-Meteo for the same (city, target_date).
#
# `members` is stored as a JSONB array of integer day-max temps. TTL-style
# eviction lives in the read path (`forecast_cache_get` returns None if the
# row is older than `max_age_seconds`); writes ALWAYS upsert.
# ---------------------------------------------------------------------------


def forecast_cache_get(city_key: str, target_date: str,
                       max_age_seconds: int) -> list[int] | None:
    """Return cached members iff the row is fresh, else None.

    The freshness check happens here (not on the writer) so any service
    can decide what TTL to honour. `weather_feed.CACHE_TTL_SECONDS` is the
    canonical default but tests / preflight may want a tighter window.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT fetched_at, members FROM forecast_cache "
            "WHERE city_key=%s AND target_date=%s",
            (city_key, target_date),
        ).fetchone()
    if row is None:
        return None
    fetched_at, members = row
    if int(time.time()) - int(fetched_at) > max_age_seconds:
        return None
    if not isinstance(members, list):
        return None
    return [int(m) for m in members]


def forecast_cache_put(city_key: str, target_date: str,
                       members: list[int]) -> None:
    """Upsert the (city, date) → members row. Caller picks `fetched_at`."""
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO forecast_cache (city_key, target_date, fetched_at, members) "
            "VALUES (%s, %s, %s, %s::jsonb) "
            "ON CONFLICT (city_key, target_date) DO UPDATE SET "
            "  fetched_at = EXCLUDED.fetched_at, "
            "  members = EXCLUDED.members",
            (city_key, target_date, int(time.time()),
             json.dumps([int(m) for m in members])),
        )


def count_settled_obs_for_city(city_key: str) -> int:
    """How many settled rows in weather_research_obs for this city.

    Powers the per-city warmup gate: the strategy refuses new BUYs until
    Path B has captured `warmup_min_obs` settled observations, ensuring the
    bias-correction calibrator has data to fit before any betting.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM weather_research_obs "
            "WHERE city_key=%s AND outcome IS NOT NULL",
            (city_key,),
        ).fetchone()
    return int(row[0]) if row else 0
