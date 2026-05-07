"""PostgreSQL schema and connection-pool management for polymarket-bot.

Phase B (May 2026) replaced the SQLite single-file/single-writer store
with a Postgres-backed pool so multiple strategy services (Phase C) can
write concurrently. The schema is purpose-built for the bot's MM-like
flow: orders + fills + markets + equity + Path-B research obs.

Connection model:
- A single `ConnectionPool` per process lives in the module-level
  `_pool`. `init_db(database_url)` instantiates it and runs DDL once.
- Every repo function does `with _pool.connection() as conn:` — the pool
  is thread-safe so the dashboard server thread and the tick-loop
  thread can use it without external locking.
"""

from __future__ import annotations

import os

import structlog
from psycopg_pool import ConnectionPool

logger = structlog.get_logger()


def _default_database_url() -> str:
    """Read DATABASE_URL from env or fall back to the docker-compose default."""
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://polymarket_bot:changeme_local_dev@postgres:5432/polymarket_bot",
    )


# Single statements (one per element). Run in order so foreign-key
# references resolve. CREATE TABLE IF NOT EXISTS keeps boots idempotent.
SCHEMA_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS markets (
        market_id      TEXT PRIMARY KEY,
        slug           TEXT NOT NULL,
        title          TEXT,
        resolution_ts  BIGINT NOT NULL,
        yes_token_id   TEXT NOT NULL,
        no_token_id    TEXT NOT NULL,
        outcome        TEXT,
        bar_open       DOUBLE PRECISION,
        bar_close      DOUBLE PRECISION,
        discovered_at  BIGINT NOT NULL DEFAULT extract(epoch from now())::bigint,
        last_yes_bid   DOUBLE PRECISION,
        last_yes_ask   DOUBLE PRECISION,
        last_yes_mid   DOUBLE PRECISION,
        last_quote_ts  BIGINT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_markets_resolution ON markets(resolution_ts)",
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_id        TEXT PRIMARY KEY,
        client_order_id TEXT NOT NULL,
        market_id       TEXT NOT NULL REFERENCES markets(market_id),
        token_side      TEXT NOT NULL,
        side            TEXT NOT NULL,
        price           DOUBLE PRECISION NOT NULL,
        size            DOUBLE PRECISION NOT NULL,
        filled          DOUBLE PRECISION NOT NULL DEFAULT 0,
        status          TEXT NOT NULL DEFAULT 'open',
        placed_at       BIGINT NOT NULL,
        ended_at        BIGINT,
        strategy        TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
    "CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_client ON orders(client_order_id)",
    """
    CREATE TABLE IF NOT EXISTS fills (
        id          BIGSERIAL PRIMARY KEY,
        order_id    TEXT NOT NULL REFERENCES orders(order_id),
        market_id   TEXT NOT NULL REFERENCES markets(market_id),
        token_side  TEXT NOT NULL,
        side        TEXT NOT NULL,
        price       DOUBLE PRECISION NOT NULL,
        size        DOUBLE PRECISION NOT NULL,
        fill_ts     BIGINT NOT NULL,
        strategy    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fills_market ON fills(market_id)",
    "CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(fill_ts)",
    """
    CREATE TABLE IF NOT EXISTS settlements (
        market_id      TEXT NOT NULL REFERENCES markets(market_id),
        settled_at     BIGINT NOT NULL,
        outcome        TEXT NOT NULL,
        yes_shares     DOUBLE PRECISION NOT NULL,
        no_shares      DOUBLE PRECISION NOT NULL,
        avg_yes_cost   DOUBLE PRECISION NOT NULL,
        avg_no_cost    DOUBLE PRECISION NOT NULL,
        payout         DOUBLE PRECISION NOT NULL,
        cost           DOUBLE PRECISION NOT NULL,
        pnl            DOUBLE PRECISION NOT NULL,
        strategy       TEXT NOT NULL,
        PRIMARY KEY (market_id, strategy)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS equity_curve (
        ts      BIGINT PRIMARY KEY,
        equity  DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS weather_research_obs (
        id                  BIGSERIAL PRIMARY KEY,
        city_key            TEXT NOT NULL,
        target_date         TEXT NOT NULL,
        slug                TEXT NOT NULL,
        bucket_label        TEXT NOT NULL,
        model_p             DOUBLE PRECISION NOT NULL,
        model_day_max_mean  DOUBLE PRECISION,
        market_yes_mid      DOUBLE PRECISION,
        market_yes_bid      DOUBLE PRECISION,
        market_yes_ask      DOUBLE PRECISION,
        observed_at         BIGINT NOT NULL,
        outcome             INTEGER,
        settled_at          BIGINT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_research_obs_lookup "
    "  ON weather_research_obs(city_key, slug, bucket_label, observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_research_obs_unsettled "
    "  ON weather_research_obs(target_date) WHERE outcome IS NULL",
    """
    CREATE TABLE IF NOT EXISTS forecast_cache (
        city_key      TEXT NOT NULL,
        target_date   TEXT NOT NULL,
        fetched_at    BIGINT NOT NULL,
        members       JSONB NOT NULL,
        PRIMARY KEY (city_key, target_date)
    )
    """,
    # ── Phase C migration: promote settlements PK from single-column
    # `(market_id)` to composite `(market_id, strategy)`. Idempotent —
    # only runs when an old-shape PK is detected. Required so two
    # strategies that bought the same market can each have their own
    # settlement row. New databases skip this entirely (the CREATE TABLE
    # above already declares the composite PK).
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM pg_constraint c
            JOIN pg_attribute a
              ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
            WHERE c.conrelid = 'settlements'::regclass
              AND c.contype = 'p'
              AND array_length(c.conkey, 1) = 1
              AND a.attname = 'market_id'
        ) THEN
            ALTER TABLE settlements DROP CONSTRAINT settlements_pkey;
            ALTER TABLE settlements ADD PRIMARY KEY (market_id, strategy);
        END IF;
    END $$
    """,
]


_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Return the singleton ConnectionPool. Initialise on first use."""
    global _pool
    if _pool is None:
        init_db()
    assert _pool is not None
    return _pool


def init_db(database_url: str | None = None) -> None:
    """Initialise the connection pool and run DDL.

    Idempotent — safe to call multiple times. The DDL uses
    `CREATE TABLE IF NOT EXISTS` so re-runs against a populated database
    are no-ops.

    Phase C: when multiple service containers boot at once they all call
    `init_db()` concurrently. Postgres' `CREATE TABLE IF NOT EXISTS` is
    *not* fully concurrent-safe — the implicit row-type registration into
    `pg_type` races and one backend trips
    `UniqueViolation: pg_type_typname_nsp_index`. We serialise the DDL
    block behind a session-scoped advisory lock so concurrent
    `init_db()` calls take turns instead of colliding.

    The lock key (`_DDL_LOCK_KEY`) is a fixed sentinel — no other code
    uses advisory locks here, so a constant is fine.
    """
    global _pool
    url = database_url or _default_database_url()
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=url, min_size=2, max_size=8, open=True,
            kwargs={"autocommit": False},
        )
    with _pool.connection() as conn:
        # Acquire advisory lock — released automatically when the
        # connection is returned to the pool at the `with` block exit.
        conn.execute("SELECT pg_advisory_lock(%s)", (_DDL_LOCK_KEY,))
        try:
            for stmt in SCHEMA_STATEMENTS:
                conn.execute(stmt)
            conn.commit()
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_DDL_LOCK_KEY,))
    logger.info("db_initialized", url=_redact(url))


# Arbitrary 64-bit sentinel for the DDL serialisation lock. Don't change
# it casually — running services hold this on boot, and rotating the key
# would let two boots run DDL concurrently again.
_DDL_LOCK_KEY = 0x504F4C59_4D4B5442  # ASCII 'POLYMKTB'


def _redact(url: str) -> str:
    """Strip credentials from a postgres URL for safe logging."""
    if "://" in url and "@" in url:
        scheme, rest = url.split("://", 1)
        userpass, host = rest.rsplit("@", 1)
        return f"{scheme}://***@{host}"
    return url


def reset_for_tests() -> None:
    """Close the pool and clear the singleton — for test fixtures only."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
