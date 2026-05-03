"""Typed accessors over the SQLite store. Used by both live and backtest."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

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


@dataclass
class Bet:
    id: int | None
    market_id: str
    side: str  # 'YES' | 'NO'
    shares: float
    entry_price: float
    stake: float
    predicted_p: float
    market_p: float
    edge: float
    strategy: str
    model_version: str
    opened_at: int
    status: str = "open"


@dataclass
class Trade:
    id: int | None
    market_id: str
    side: str
    shares: float
    entry_price: float
    payout: float
    pnl: float
    fees: float
    slippage: float
    predicted_p: float
    market_p: float
    edge: float
    brier: float
    outcome: str
    strategy: str
    model_version: str
    opened_at: int
    settled_at: int


@dataclass
class Bar:
    open_time: int
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass
class FundingPoint:
    funding_ts: int
    rate: float


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------


def upsert_market(m: Market) -> None:
    conn = get_conn()
    with lock():
        conn.execute(
            "INSERT OR REPLACE INTO markets "
            "(market_id, slug, resolution_ts, yes_token_id, no_token_id, outcome, bar_open, bar_close, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT discovered_at FROM markets WHERE market_id=?), ?))",
            (m.market_id, m.slug, m.resolution_ts, m.yes_token_id, m.no_token_id,
             m.outcome, m.bar_open, m.bar_close, m.market_id, int(time.time())),
        )
        conn.commit()


def get_market(market_id: str) -> Market | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT market_id, slug, resolution_ts, yes_token_id, no_token_id, outcome, bar_open, bar_close "
        "FROM markets WHERE market_id=?",
        (market_id,),
    ).fetchone()
    return Market(*row) if row else None


def settle_market(market_id: str, outcome: str, bar_open: float, bar_close: float) -> None:
    conn = get_conn()
    with lock():
        conn.execute(
            "UPDATE markets SET outcome=?, bar_open=?, bar_close=? WHERE market_id=?",
            (outcome, bar_open, bar_close, market_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Bets / Trades
# ---------------------------------------------------------------------------


def insert_bet(b: Bet) -> int:
    conn = get_conn()
    with lock():
        cur = conn.execute(
            "INSERT INTO bets (market_id, side, shares, entry_price, stake, predicted_p, market_p, edge, strategy, model_version, opened_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (b.market_id, b.side, b.shares, b.entry_price, b.stake, b.predicted_p,
             b.market_p, b.edge, b.strategy, b.model_version, b.opened_at, b.status),
        )
        conn.commit()
        return int(cur.lastrowid)


def open_bets() -> list[Bet]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, market_id, side, shares, entry_price, stake, predicted_p, market_p, edge, "
        "strategy, model_version, opened_at, status FROM bets WHERE status='open'"
    ).fetchall()
    return [Bet(*r) for r in rows]


def mark_bet_settled(bet_id: int) -> None:
    conn = get_conn()
    with lock():
        conn.execute("UPDATE bets SET status='settled' WHERE id=?", (bet_id,))
        conn.commit()


def insert_trade(t: Trade) -> int:
    conn = get_conn()
    with lock():
        cur = conn.execute(
            "INSERT INTO trades (market_id, side, shares, entry_price, payout, pnl, fees, slippage, "
            "predicted_p, market_p, edge, brier, outcome, strategy, model_version, opened_at, settled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (t.market_id, t.side, t.shares, t.entry_price, t.payout, t.pnl, t.fees, t.slippage,
             t.predicted_p, t.market_p, t.edge, t.brier, t.outcome, t.strategy, t.model_version,
             t.opened_at, t.settled_at),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_trades(
    limit: int = 100, offset: int = 0,
    side: str | None = None, strategy: str | None = None,
    from_ts: int | None = None, to_ts: int | None = None,
) -> list[Trade]:
    conn = get_conn()
    sql = ("SELECT id, market_id, side, shares, entry_price, payout, pnl, fees, slippage, "
           "predicted_p, market_p, edge, brier, outcome, strategy, model_version, opened_at, settled_at "
           "FROM trades WHERE 1=1")
    args: list[Any] = []
    if side:
        sql += " AND side=?"; args.append(side)
    if strategy:
        sql += " AND strategy=?"; args.append(strategy)
    if from_ts is not None:
        sql += " AND settled_at>=?"; args.append(from_ts)
    if to_ts is not None:
        sql += " AND settled_at<?"; args.append(to_ts)
    sql += " ORDER BY settled_at DESC LIMIT ? OFFSET ?"
    args.extend([limit, offset])
    return [Trade(*r) for r in conn.execute(sql, args).fetchall()]


def trade_stats(from_ts: int | None = None, to_ts: int | None = None) -> dict[str, Any]:
    conn = get_conn()
    sql = "SELECT COUNT(*), COALESCE(SUM(pnl),0), COALESCE(AVG(brier),0), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) FROM trades WHERE 1=1"
    args: list[Any] = []
    if from_ts is not None:
        sql += " AND settled_at>=?"; args.append(from_ts)
    if to_ts is not None:
        sql += " AND settled_at<?"; args.append(to_ts)
    n, pnl, brier, wins = conn.execute(sql, args).fetchone()
    return {
        "trades": int(n or 0),
        "pnl": float(pnl or 0.0),
        "brier": float(brier or 0.0),
        "wins": int(wins or 0),
        "win_rate": float(wins or 0) / n if n else 0.0,
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
# BTC bars cache
# ---------------------------------------------------------------------------


def upsert_bars(bars: list[Bar]) -> int:
    if not bars:
        return 0
    conn = get_conn()
    with lock():
        conn.executemany(
            "INSERT OR REPLACE INTO btc_bars (open_time, o, h, l, c, v) VALUES (?, ?, ?, ?, ?, ?)",
            [(b.open_time, b.o, b.h, b.l, b.c, b.v) for b in bars],
        )
        conn.commit()
    return len(bars)


def latest_bar_time() -> int | None:
    conn = get_conn()
    row = conn.execute("SELECT MAX(open_time) FROM btc_bars").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def load_bars(from_ts: int | None = None, to_ts: int | None = None, limit: int | None = None) -> list[Bar]:
    conn = get_conn()
    sql = "SELECT open_time, o, h, l, c, v FROM btc_bars WHERE 1=1"
    args: list[Any] = []
    if from_ts is not None:
        sql += " AND open_time>=?"; args.append(from_ts)
    if to_ts is not None:
        sql += " AND open_time<?"; args.append(to_ts)
    sql += " ORDER BY open_time"
    if limit is not None:
        sql += " LIMIT ?"; args.append(limit)
    return [Bar(*r) for r in conn.execute(sql, args).fetchall()]


# ---------------------------------------------------------------------------
# Aux bars (ETH spot, BTC perp) — same shape as btc_bars, different tables
# ---------------------------------------------------------------------------


def _upsert_into(table: str, bars: list[Bar]) -> int:
    if not bars:
        return 0
    conn = get_conn()
    with lock():
        conn.executemany(
            f"INSERT OR REPLACE INTO {table} (open_time, o, h, l, c, v) VALUES (?, ?, ?, ?, ?, ?)",
            [(b.open_time, b.o, b.h, b.l, b.c, b.v) for b in bars],
        )
        conn.commit()
    return len(bars)


def _load_from(table: str, from_ts: int | None = None, to_ts: int | None = None,
               limit: int | None = None) -> list[Bar]:
    conn = get_conn()
    sql = f"SELECT open_time, o, h, l, c, v FROM {table} WHERE 1=1"
    args: list[Any] = []
    if from_ts is not None:
        sql += " AND open_time>=?"; args.append(from_ts)
    if to_ts is not None:
        sql += " AND open_time<?"; args.append(to_ts)
    sql += " ORDER BY open_time"
    if limit is not None:
        sql += " LIMIT ?"; args.append(limit)
    return [Bar(*r) for r in conn.execute(sql, args).fetchall()]


def _latest_open_time(table: str) -> int | None:
    conn = get_conn()
    row = conn.execute(f"SELECT MAX(open_time) FROM {table}").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def upsert_eth_bars(bars: list[Bar]) -> int:
    return _upsert_into("eth_bars", bars)


def load_eth_bars(from_ts: int | None = None, to_ts: int | None = None) -> list[Bar]:
    return _load_from("eth_bars", from_ts, to_ts)


def latest_eth_bar_time() -> int | None:
    return _latest_open_time("eth_bars")


def upsert_perp_bars(bars: list[Bar]) -> int:
    return _upsert_into("btc_perp_bars", bars)


def load_perp_bars(from_ts: int | None = None, to_ts: int | None = None) -> list[Bar]:
    return _load_from("btc_perp_bars", from_ts, to_ts)


def latest_perp_bar_time() -> int | None:
    return _latest_open_time("btc_perp_bars")


# ---------------------------------------------------------------------------
# Funding rate (BTC perp, every 8h)
# ---------------------------------------------------------------------------


def upsert_funding(points: list[FundingPoint]) -> int:
    if not points:
        return 0
    conn = get_conn()
    with lock():
        conn.executemany(
            "INSERT OR REPLACE INTO btc_funding (funding_ts, rate) VALUES (?, ?)",
            [(p.funding_ts, p.rate) for p in points],
        )
        conn.commit()
    return len(points)


def load_funding(from_ts: int | None = None, to_ts: int | None = None) -> list[FundingPoint]:
    conn = get_conn()
    sql = "SELECT funding_ts, rate FROM btc_funding WHERE 1=1"
    args: list[Any] = []
    if from_ts is not None:
        sql += " AND funding_ts>=?"; args.append(from_ts)
    if to_ts is not None:
        sql += " AND funding_ts<?"; args.append(to_ts)
    sql += " ORDER BY funding_ts"
    return [FundingPoint(int(r[0]), float(r[1])) for r in conn.execute(sql, args).fetchall()]


def latest_funding_ts() -> int | None:
    conn = get_conn()
    row = conn.execute("SELECT MAX(funding_ts) FROM btc_funding").fetchone()
    return int(row[0]) if row and row[0] is not None else None


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
