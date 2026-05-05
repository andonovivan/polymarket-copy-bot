"""SQLite schema and connection management for polymarket-bot (MM mode).

Schema is purpose-built for market-making: orders + fills + markets + equity.
The legacy direction-prediction tables are dropped on first boot.
"""

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
    title          TEXT,                           -- human-readable, e.g. "Paris · May 4 · 16°C"
    resolution_ts  INTEGER NOT NULL,
    yes_token_id   TEXT NOT NULL,
    no_token_id    TEXT NOT NULL,
    outcome        TEXT,
    bar_open       REAL,
    bar_close      REAL,
    discovered_at  INTEGER NOT NULL,
    last_yes_bid   REAL,                           -- most recent observed YES bid
    last_yes_ask   REAL,                           -- most recent observed YES ask
    last_yes_mid   REAL,                           -- (last_yes_bid + last_yes_ask) / 2
    last_quote_ts  INTEGER                         -- when last_* were sampled
);
CREATE INDEX IF NOT EXISTS idx_markets_resolution ON markets(resolution_ts);

CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    token_side      TEXT NOT NULL,                 -- 'YES' | 'NO'
    side            TEXT NOT NULL,                 -- 'BUY' | 'SELL'
    price           REAL NOT NULL,
    size            REAL NOT NULL,
    filled          REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'filled' | 'cancelled' | 'expired'
    placed_at       INTEGER NOT NULL,
    ended_at        INTEGER,
    strategy        TEXT NOT NULL,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market_id);
CREATE INDEX IF NOT EXISTS idx_orders_client ON orders(client_order_id);

CREATE TABLE IF NOT EXISTS fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT NOT NULL,
    market_id   TEXT NOT NULL,
    token_side  TEXT NOT NULL,
    side        TEXT NOT NULL,
    price       REAL NOT NULL,
    size        REAL NOT NULL,
    fill_ts     INTEGER NOT NULL,
    strategy    TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);
CREATE INDEX IF NOT EXISTS idx_fills_market ON fills(market_id);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(fill_ts);

CREATE TABLE IF NOT EXISTS settlements (
    market_id      TEXT PRIMARY KEY,
    settled_at     INTEGER NOT NULL,
    outcome        TEXT NOT NULL,                  -- 'UP' | 'DOWN'
    yes_shares     REAL NOT NULL,
    no_shares      REAL NOT NULL,
    avg_yes_cost   REAL NOT NULL,
    avg_no_cost    REAL NOT NULL,
    payout         REAL NOT NULL,
    cost           REAL NOT NULL,
    pnl            REAL NOT NULL,
    strategy       TEXT NOT NULL,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);

CREATE TABLE IF NOT EXISTS equity_curve (
    ts      INTEGER PRIMARY KEY,
    equity  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS weather_research_obs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    city_key            TEXT NOT NULL,
    target_date         TEXT NOT NULL,                  -- YYYY-MM-DD in city tz
    slug                TEXT NOT NULL,
    bucket_label        TEXT NOT NULL,
    model_p             REAL NOT NULL,
    model_day_max_mean  REAL,                           -- ensemble mean day-max (°C or °F)
    market_yes_mid      REAL,
    market_yes_bid      REAL,
    market_yes_ask      REAL,
    observed_at         INTEGER NOT NULL,
    outcome             INTEGER,                        -- 1 won, 0 lost, NULL until settled
    settled_at          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_research_obs_lookup
    ON weather_research_obs(city_key, slug, bucket_label, observed_at);
CREATE INDEX IF NOT EXISTS idx_research_obs_unsettled
    ON weather_research_obs(target_date) WHERE outcome IS NULL;
"""


# Legacy tables from the direction-prediction era. Drop on first boot of the MM build.
_LEGACY_TABLES = [
    "bets", "trades",
    "btc_bars", "eth_bars", "btc_perp_bars", "btc_funding",
    "polymarket_quotes", "models",
]


_MARKETS_NEW_COLUMNS = [
    ("title", "TEXT"),
    ("last_yes_bid", "REAL"),
    ("last_yes_ask", "REAL"),
    ("last_yes_mid", "REAL"),
    ("last_quote_ts", "INTEGER"),
]

_RESEARCH_OBS_NEW_COLUMNS = [
    ("model_day_max_mean", "REAL"),
]


def _drop_legacy(conn: sqlite3.Connection) -> None:
    for t in _LEGACY_TABLES:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        except Exception as exc:
            logger.warning("legacy_drop_failed", table=t, error=str(exc))


def _migrate(conn: sqlite3.Connection) -> None:
    """Forward-only ALTER TABLE migrations."""
    market_cols = {row[1] for row in conn.execute("PRAGMA table_info(markets)")}
    for col, type_ in _MARKETS_NEW_COLUMNS:
        if col not in market_cols:
            conn.execute(f"ALTER TABLE markets ADD COLUMN {col} {type_}")
    obs_cols = {row[1] for row in conn.execute("PRAGMA table_info(weather_research_obs)")}
    for col, type_ in _RESEARCH_OBS_NEW_COLUMNS:
        if col not in obs_cols:
            conn.execute(f"ALTER TABLE weather_research_obs ADD COLUMN {col} {type_}")


def get_conn(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Get or create the singleton SQLite connection."""
    global _conn
    if _conn is not None:
        return _conn
    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.execute("PRAGMA foreign_keys=ON")
    _drop_legacy(_conn)
    _conn.executescript(SCHEMA_DDL)
    _migrate(_conn)
    _conn.commit()
    return _conn


def init_db(path: Path = DEFAULT_DB_PATH) -> None:
    """Initialize the database. Call once at startup."""
    get_conn(path)
    logger.info("db_initialized", path=str(path))


def lock() -> threading.Lock:
    return _lock
