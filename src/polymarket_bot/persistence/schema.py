"""SQLite schema and connection management for polymarket-bot."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import structlog

logger = structlog.get_logger()

DEFAULT_DB_PATH = Path(os.getenv("BOT_DB_PATH", "bot_state.db"))

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS markets (
    market_id      TEXT PRIMARY KEY,
    slug           TEXT NOT NULL,
    resolution_ts  INTEGER NOT NULL,
    yes_token_id   TEXT NOT NULL,
    no_token_id    TEXT NOT NULL,
    outcome        TEXT,
    bar_open       REAL,
    bar_close      REAL,
    discovered_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markets_resolution ON markets(resolution_ts);

CREATE TABLE IF NOT EXISTS bets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id      TEXT NOT NULL,
    side           TEXT NOT NULL,
    shares         REAL NOT NULL,
    entry_price    REAL NOT NULL,
    stake          REAL NOT NULL,
    predicted_p    REAL NOT NULL,
    market_p       REAL NOT NULL,
    edge           REAL NOT NULL,
    strategy       TEXT NOT NULL,
    model_version  TEXT NOT NULL,
    opened_at      INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);
CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);
CREATE INDEX IF NOT EXISTS idx_bets_market ON bets(market_id);

CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id      TEXT NOT NULL,
    side           TEXT NOT NULL,
    shares         REAL NOT NULL,
    entry_price    REAL NOT NULL,
    payout         REAL NOT NULL,
    pnl            REAL NOT NULL,
    fees           REAL NOT NULL DEFAULT 0,
    slippage       REAL NOT NULL DEFAULT 0,
    predicted_p    REAL NOT NULL,
    market_p       REAL NOT NULL,
    edge           REAL NOT NULL,
    brier          REAL NOT NULL,
    outcome        TEXT NOT NULL,
    strategy       TEXT NOT NULL,
    model_version  TEXT NOT NULL,
    opened_at      INTEGER NOT NULL,
    settled_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_settled ON trades(settled_at);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);

CREATE TABLE IF NOT EXISTS equity_curve (
    ts      INTEGER PRIMARY KEY,
    equity  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS btc_bars (
    open_time  INTEGER PRIMARY KEY,
    o REAL NOT NULL,
    h REAL NOT NULL,
    l REAL NOT NULL,
    c REAL NOT NULL,
    v REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS polymarket_quotes (
    market_id  TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    yes_bid    REAL,
    yes_ask    REAL,
    no_bid     REAL,
    no_ask     REAL,
    depth_yes  REAL,
    depth_no   REAL,
    PRIMARY KEY (market_id, ts)
);

CREATE TABLE IF NOT EXISTS models (
    version           TEXT PRIMARY KEY,
    strategy          TEXT NOT NULL,
    trained_at        INTEGER NOT NULL,
    window_start      INTEGER NOT NULL,
    window_end        INTEGER NOT NULL,
    payload           BLOB NOT NULL,
    cv_brier          REAL,
    cv_logloss        REAL,
    calib_intercept   REAL,
    calib_slope       REAL
);
CREATE INDEX IF NOT EXISTS idx_models_strategy ON models(strategy, trained_at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_conn(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Get or create the singleton SQLite connection."""
    global _conn
    if _conn is not None:
        return _conn
    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(SCHEMA_DDL)
    _conn.commit()
    return _conn


def init_db(path: Path = DEFAULT_DB_PATH) -> None:
    """Initialize the database. Call once at startup."""
    get_conn(path)
    logger.info("db_initialized", path=str(path))


def lock() -> threading.Lock:
    return _lock
