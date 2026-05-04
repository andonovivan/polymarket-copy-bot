"""Typed accessors over the SQLite store. Used by both live and paper brokers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

import structlog

from polymarket_bot.persistence.schema import get_conn, lock

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Domain types
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


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------


def upsert_market(m: Market) -> None:
    """Insert or update a market row, preserving cached quote columns when present."""
    conn = get_conn()
    with lock():
        conn.execute(
            "INSERT INTO markets "
            "(market_id, slug, title, resolution_ts, yes_token_id, no_token_id, "
            " outcome, bar_open, bar_close, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(market_id) DO UPDATE SET "
            "  slug=excluded.slug, "
            "  title=COALESCE(excluded.title, markets.title), "
            "  resolution_ts=excluded.resolution_ts, "
            "  yes_token_id=excluded.yes_token_id, "
            "  no_token_id=excluded.no_token_id, "
            "  outcome=COALESCE(excluded.outcome, markets.outcome), "
            "  bar_open=COALESCE(excluded.bar_open, markets.bar_open), "
            "  bar_close=COALESCE(excluded.bar_close, markets.bar_close)",
            (m.market_id, m.slug, m.title, m.resolution_ts, m.yes_token_id, m.no_token_id,
             m.outcome, m.bar_open, m.bar_close, int(time.time())),
        )
        conn.commit()


def update_market_quote(market_id: str, yes_bid: float | None, yes_ask: float | None) -> None:
    """Cache the most recent YES bid/ask/mid for MTM display."""
    yes_mid = ((yes_bid + yes_ask) / 2.0) if (yes_bid is not None and yes_ask is not None) else None
    conn = get_conn()
    with lock():
        conn.execute(
            "UPDATE markets SET last_yes_bid=?, last_yes_ask=?, last_yes_mid=?, last_quote_ts=? "
            "WHERE market_id=?",
            (yes_bid, yes_ask, yes_mid, int(time.time()), market_id),
        )
        conn.commit()


def get_market(market_id: str) -> Market | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT market_id, slug, resolution_ts, yes_token_id, no_token_id, outcome, "
        "bar_open, bar_close, title, last_yes_bid, last_yes_ask, last_yes_mid, last_quote_ts "
        "FROM markets WHERE market_id=?", (market_id,),
    ).fetchone()
    return Market(*row) if row else None


def settle_market_row(market_id: str, outcome: str, bar_open: float, bar_close: float) -> None:
    conn = get_conn()
    with lock():
        conn.execute(
            "UPDATE markets SET outcome=?, bar_open=?, bar_close=? WHERE market_id=?",
            (outcome, bar_open, bar_close, market_id),
        )
        conn.commit()


def unsettled_markets_due(now_ts: int) -> list[Market]:
    """Markets whose resolution_ts has passed but outcome is still NULL,
    AND for which we have at least one fill (i.e., we hold inventory)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT m.market_id, m.slug, m.resolution_ts, m.yes_token_id, m.no_token_id, "
        "m.outcome, m.bar_open, m.bar_close, m.title, "
        "m.last_yes_bid, m.last_yes_ask, m.last_yes_mid, m.last_quote_ts "
        "FROM markets m "
        "WHERE m.outcome IS NULL AND m.resolution_ts <= ? "
        "AND EXISTS (SELECT 1 FROM fills f WHERE f.market_id=m.market_id)",
        (now_ts,),
    ).fetchall()
    return [Market(*r) for r in rows]


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def insert_order(o: Order) -> None:
    conn = get_conn()
    with lock():
        conn.execute(
            "INSERT INTO orders (order_id, client_order_id, market_id, token_side, side, "
            "price, size, filled, status, placed_at, ended_at, strategy) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (o.order_id, o.client_order_id, o.market_id, o.token_side, o.side,
             o.price, o.size, o.filled, o.status, o.placed_at, o.ended_at, o.strategy),
        )
        conn.commit()


def open_orders_by_market(market_id: str) -> list[Order]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT order_id, client_order_id, market_id, token_side, side, price, size, "
        "filled, status, placed_at, ended_at, strategy "
        "FROM orders WHERE market_id=? AND status='open'",
        (market_id,),
    ).fetchall()
    return [Order(*r) for r in rows]


def all_open_orders() -> list[Order]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT order_id, client_order_id, market_id, token_side, side, price, size, "
        "filled, status, placed_at, ended_at, strategy "
        "FROM orders WHERE status='open' ORDER BY placed_at DESC"
    ).fetchall()
    return [Order(*r) for r in rows]


def update_order_filled(order_id: str, filled: float, status: str, ended_at: int | None) -> None:
    conn = get_conn()
    with lock():
        conn.execute(
            "UPDATE orders SET filled=?, status=?, ended_at=? WHERE order_id=?",
            (filled, status, ended_at, order_id),
        )
        conn.commit()


def cancel_order_row(order_id: str) -> None:
    conn = get_conn()
    with lock():
        conn.execute(
            "UPDATE orders SET status='cancelled', ended_at=? WHERE order_id=? AND status='open'",
            (int(time.time()), order_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------


def insert_fill(f: Fill) -> int:
    conn = get_conn()
    with lock():
        cur = conn.execute(
            "INSERT INTO fills (order_id, market_id, token_side, side, price, size, fill_ts, strategy) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f.order_id, f.market_id, f.token_side, f.side, f.price, f.size, f.fill_ts, f.strategy),
        )
        conn.commit()
        return int(cur.lastrowid)


def fills_for_market(market_id: str) -> list[Fill]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, order_id, market_id, token_side, side, price, size, fill_ts, strategy "
        "FROM fills WHERE market_id=? ORDER BY fill_ts",
        (market_id,),
    ).fetchall()
    return [Fill(*r) for r in rows]


def list_fills(limit: int = 100, offset: int = 0) -> list[Fill]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, order_id, market_id, token_side, side, price, size, fill_ts, strategy "
        "FROM fills ORDER BY fill_ts DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [Fill(*r) for r in rows]


# ---------------------------------------------------------------------------
# Inventory & PnL (computed from fills)
# ---------------------------------------------------------------------------


def markets_with_unsettled_fills() -> list[str]:
    """All market_ids that have at least one fill and no Settlement row yet."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT f.market_id FROM fills f "
        "WHERE NOT EXISTS (SELECT 1 FROM settlements s WHERE s.market_id = f.market_id) "
        "ORDER BY f.market_id"
    ).fetchall()
    return [r[0] for r in rows]


def inventory_for_market(market_id: str) -> tuple[float, float, float, float]:
    """Return (yes_shares, no_shares, avg_yes_cost, avg_no_cost) for an open market."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT token_side, side, SUM(size), SUM(size*price) "
        "FROM fills WHERE market_id=? GROUP BY token_side, side",
        (market_id,),
    ).fetchall()
    yes_buy_size = yes_buy_notional = yes_sell_size = 0.0
    no_buy_size = no_buy_notional = no_sell_size = 0.0
    for token, side, total_size, total_notional in rows:
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


# ---------------------------------------------------------------------------
# Settlements
# ---------------------------------------------------------------------------


def insert_settlement(s: Settlement) -> None:
    conn = get_conn()
    with lock():
        conn.execute(
            "INSERT OR REPLACE INTO settlements "
            "(market_id, settled_at, outcome, yes_shares, no_shares, avg_yes_cost, avg_no_cost, "
            " payout, cost, pnl, strategy) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (s.market_id, s.settled_at, s.outcome, s.yes_shares, s.no_shares,
             s.avg_yes_cost, s.avg_no_cost, s.payout, s.cost, s.pnl, s.strategy),
        )
        conn.commit()


def list_settlements(limit: int = 100) -> list[Settlement]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT market_id, settled_at, outcome, yes_shares, no_shares, "
        "avg_yes_cost, avg_no_cost, payout, cost, pnl, strategy "
        "FROM settlements ORDER BY settled_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [Settlement(*r) for r in rows]


def settlement_stats(from_ts: int | None = None, to_ts: int | None = None) -> dict[str, Any]:
    conn = get_conn()
    sql = ("SELECT COUNT(*), COALESCE(SUM(pnl),0), "
           "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) FROM settlements WHERE 1=1")
    args: list[Any] = []
    if from_ts is not None:
        sql += " AND settled_at>=?"; args.append(from_ts)
    if to_ts is not None:
        sql += " AND settled_at<?"; args.append(to_ts)
    n, pnl, wins = conn.execute(sql, args).fetchone()
    return {
        "settlements": int(n or 0),
        "pnl": float(pnl or 0.0),
        "wins": int(wins or 0),
        "win_rate": (float(wins or 0) / n) if n else 0.0,
    }


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------


def append_equity(ts: int, equity: float) -> None:
    conn = get_conn()
    with lock():
        conn.execute("INSERT OR REPLACE INTO equity_curve (ts, equity) VALUES (?, ?)", (ts, equity))
        conn.commit()


def equity_curve(from_ts: int | None = None, to_ts: int | None = None) -> list[tuple[int, float]]:
    conn = get_conn()
    sql = "SELECT ts, equity FROM equity_curve WHERE 1=1"
    args: list[Any] = []
    if from_ts is not None:
        sql += " AND ts>=?"; args.append(from_ts)
    if to_ts is not None:
        sql += " AND ts<?"; args.append(to_ts)
    sql += " ORDER BY ts"
    return [(int(r[0]), float(r[1])) for r in conn.execute(sql, args).fetchall()]


def latest_equity() -> float | None:
    conn = get_conn()
    row = conn.execute("SELECT equity FROM equity_curve ORDER BY ts DESC LIMIT 1").fetchone()
    return float(row[0]) if row else None


# ---------------------------------------------------------------------------
# Meta key-value
# ---------------------------------------------------------------------------


def get_meta(key: str) -> str | None:
    conn = get_conn()
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(key: str, value: str) -> None:
    conn = get_conn()
    with lock():
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
