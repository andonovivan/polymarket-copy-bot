"""Persist bot state to a SQLite database. Drop-in replacement for the old JSON file."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import structlog

logger = structlog.get_logger()

DEFAULT_DB_PATH = Path("bot_state.db")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _get_conn(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Get or create a thread-safe SQLite connection and ensure tables exist."""
    global _conn
    if _conn is not None:
        return _conn

    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")  # better concurrent read/write
    _conn.execute("PRAGMA busy_timeout=5000")

    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            asset_id   TEXT PRIMARY KEY,
            shares     REAL NOT NULL DEFAULT 0,
            exposure   REAL NOT NULL DEFAULT 0,
            buy_price  REAL NOT NULL DEFAULT 0,
            opened_at  INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tracked_wallets (
            wallet TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS trade_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id   TEXT NOT NULL,
            buy_price  REAL NOT NULL,
            sell_price REAL NOT NULL,
            shares     REAL NOT NULL,
            pnl        REAL NOT NULL,
            ts         INTEGER NOT NULL,
            opened_at  INTEGER NOT NULL DEFAULT 0,
            closed_at  INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS pnl_summary (
            id              INTEGER PRIMARY KEY CHECK (id = 1),
            total_realized  REAL NOT NULL DEFAULT 0,
            total_trades    INTEGER NOT NULL DEFAULT 0,
            winning_trades  INTEGER NOT NULL DEFAULT 0,
            losing_trades   INTEGER NOT NULL DEFAULT 0
        );

        INSERT OR IGNORE INTO pnl_summary (id, total_realized, total_trades, winning_trades, losing_trades)
        VALUES (1, 0, 0, 0, 0);

        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    _conn.commit()

    # --- Schema migrations for existing databases ---
    _run_migrations(_conn)
    _conn.commit()
    return _conn


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Add columns that may be missing from older database versions."""
    # Check which columns exist and add missing ones.
    migrations = [
        ("positions", "opened_at", "INTEGER NOT NULL DEFAULT 0"),
        ("trade_history", "opened_at", "INTEGER NOT NULL DEFAULT 0"),
        ("trade_history", "closed_at", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for table, column, col_type in migrations:
        try:
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                logger.info("schema_migration", table=table, added_column=column)
        except Exception as exc:
            logger.warning("schema_migration_failed", table=table, column=column, error=str(exc))

    # Backfill closed_at from ts for existing rows that have closed_at=0.
    try:
        conn.execute("UPDATE trade_history SET closed_at = ts WHERE closed_at = 0 AND ts > 0")
    except Exception:
        pass


def _migrate_from_json(json_path: Path = Path("bot_state.json"), db_path: Path = DEFAULT_DB_PATH) -> None:
    """One-time migration: import existing bot_state.json into SQLite, then rename the file."""
    if not json_path.exists():
        return

    try:
        data = json.loads(json_path.read_text())
    except Exception:
        return

    conn = _get_conn(db_path)
    with _lock:
        try:
            # Positions
            shares = data.get("shares_held", {})
            exposure = data.get("exposure", {})
            buy_prices = data.get("buy_prices", {})
            opened_at = data.get("opened_at", {})
            all_assets = set(shares) | set(exposure) | set(buy_prices)
            for aid in all_assets:
                conn.execute(
                    "INSERT OR REPLACE INTO positions (asset_id, shares, exposure, buy_price, opened_at) VALUES (?, ?, ?, ?, ?)",
                    (aid, shares.get(aid, 0), exposure.get(aid, 0), buy_prices.get(aid, 0), opened_at.get(aid, 0)),
                )

            # Tracked wallets
            for w in data.get("tracked_wallets", []):
                conn.execute("INSERT OR IGNORE INTO tracked_wallets (wallet) VALUES (?)", (w,))

            # PnL summary
            pnl = data.get("pnl", {})
            if pnl:
                conn.execute(
                    "UPDATE pnl_summary SET total_realized=?, total_trades=?, winning_trades=?, losing_trades=? WHERE id=1",
                    (pnl.get("total_realized", 0), pnl.get("total_trades", 0),
                     pnl.get("winning_trades", 0), pnl.get("losing_trades", 0)),
                )
                for t in pnl.get("trade_history", []):
                    conn.execute(
                        "INSERT INTO trade_history (asset_id, buy_price, sell_price, shares, pnl, ts, opened_at, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (t["asset_id"], t["buy_price"], t["sell_price"], t["shares"], t["pnl"], t["ts"],
                         t.get("opened_at", 0), t.get("closed_at", t["ts"])),
                    )

            # Meta
            last_ts = data.get("last_running_ts", 0)
            if last_ts:
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_running_ts', ?)", (str(last_ts),))

            conn.commit()

            # Rename old file so migration doesn't run again.
            backup = json_path.with_suffix(".json.bak")
            json_path.rename(backup)
            logger.info("migrated_json_to_sqlite", backup=str(backup))
        except Exception as exc:
            logger.error("json_migration_failed", error=str(exc))


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Initialize the database and run migration if needed. Call once at startup."""
    _get_conn(db_path)
    _migrate_from_json(db_path=db_path)


# ---------------------------------------------------------------------------
# Public API — same signatures as the old JSON-based functions
# ---------------------------------------------------------------------------


def load_state(db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Load full state from SQLite. Returns the same dict shape as the old JSON version."""
    conn = _get_conn(db_path)
    with _lock:
        try:
            # Positions
            shares_held = {}
            exposure_map = {}
            buy_prices = {}
            opened_at = {}
            for row in conn.execute("SELECT asset_id, shares, exposure, buy_price, opened_at FROM positions"):
                aid, sh, exp, bp, oa = row
                if sh > 0 or exp > 0:
                    shares_held[aid] = sh
                    exposure_map[aid] = exp
                    buy_prices[aid] = bp
                    opened_at[aid] = oa or 0

            # Tracked wallets
            wallets = [r[0] for r in conn.execute("SELECT wallet FROM tracked_wallets ORDER BY rowid")]

            # PnL summary
            row = conn.execute(
                "SELECT total_realized, total_trades, winning_trades, losing_trades FROM pnl_summary WHERE id=1"
            ).fetchone()
            pnl_summary = {
                "total_realized": row[0] if row else 0.0,
                "total_trades": row[1] if row else 0,
                "winning_trades": row[2] if row else 0,
                "losing_trades": row[3] if row else 0,
            }

            # Trade history
            history = []
            for r in conn.execute(
                "SELECT asset_id, buy_price, sell_price, shares, pnl, ts, opened_at, closed_at FROM trade_history ORDER BY id"
            ):
                history.append({
                    "asset_id": r[0], "buy_price": r[1], "sell_price": r[2],
                    "shares": r[3], "pnl": r[4], "ts": r[5],
                    "opened_at": r[6] or 0, "closed_at": r[7] or r[5],
                })
            pnl_summary["trade_history"] = history

            # Meta
            ts_row = conn.execute("SELECT value FROM meta WHERE key='last_running_ts'").fetchone()
            last_running_ts = int(ts_row[0]) if ts_row else 0

            return {
                "shares_held": shares_held,
                "exposure": exposure_map,
                "tracked_wallets": wallets,
                "buy_prices": buy_prices,
                "opened_at": opened_at,
                "last_running_ts": last_running_ts,
                "pnl": pnl_summary,
            }
        except Exception as exc:
            logger.error("state_load_failed", error=str(exc))
            return {
                "shares_held": {},
                "exposure": {},
                "tracked_wallets": [],
                "buy_prices": {},
                "opened_at": {},
                "last_running_ts": 0,
                "pnl": {
                    "total_realized": 0.0, "total_trades": 0,
                    "winning_trades": 0, "losing_trades": 0, "trade_history": [],
                },
            }


def save_state(
    shares_held: dict[str, float],
    exposure: dict[str, float],
    tracked_wallets: list[str] | None = None,
    buy_prices: dict[str, float] | None = None,
    pnl: dict | None = None,
    opened_at: dict[str, int] | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Persist current state to SQLite."""
    conn = _get_conn(db_path)
    with _lock:
        try:
            # Upsert positions — collect all asset IDs.
            all_assets = set(shares_held) | set(exposure)
            if buy_prices:
                all_assets |= set(buy_prices)

            for aid in all_assets:
                conn.execute(
                    "INSERT OR REPLACE INTO positions (asset_id, shares, exposure, buy_price, opened_at) VALUES (?, ?, ?, ?, ?)",
                    (aid, shares_held.get(aid, 0), exposure.get(aid, 0),
                     (buy_prices or {}).get(aid, 0), (opened_at or {}).get(aid, 0)),
                )
            # Remove positions no longer held.
            if all_assets:
                placeholders = ",".join("?" * len(all_assets))
                conn.execute(f"DELETE FROM positions WHERE asset_id NOT IN ({placeholders})", list(all_assets))
            else:
                conn.execute("DELETE FROM positions")

            # Tracked wallets
            if tracked_wallets is not None:
                conn.execute("DELETE FROM tracked_wallets")
                for w in tracked_wallets:
                    conn.execute("INSERT OR IGNORE INTO tracked_wallets (wallet) VALUES (?)", (w,))

            # PnL summary + history
            if pnl is not None:
                conn.execute(
                    "UPDATE pnl_summary SET total_realized=?, total_trades=?, winning_trades=?, losing_trades=? WHERE id=1",
                    (pnl.get("total_realized", 0), pnl.get("total_trades", 0),
                     pnl.get("winning_trades", 0), pnl.get("losing_trades", 0)),
                )

                # Append only new trades (compare count).
                existing_count = conn.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0]
                history = pnl.get("trade_history", [])
                new_trades = history[existing_count:]
                for t in new_trades:
                    conn.execute(
                        "INSERT INTO trade_history (asset_id, buy_price, sell_price, shares, pnl, ts, opened_at, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (t["asset_id"], t["buy_price"], t["sell_price"], t["shares"], t["pnl"], t["ts"],
                         t.get("opened_at", 0), t.get("closed_at", t["ts"])),
                    )

            conn.commit()
        except Exception as exc:
            logger.error("state_save_failed", error=str(exc))


def update_last_running_ts(ts: int, db_path: Path = DEFAULT_DB_PATH) -> None:
    """Update the last_running_ts timestamp."""
    conn = _get_conn(db_path)
    with _lock:
        try:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_running_ts', ?)", (str(ts),))
            conn.commit()
        except Exception as exc:
            logger.error("timestamp_save_failed", error=str(exc))
