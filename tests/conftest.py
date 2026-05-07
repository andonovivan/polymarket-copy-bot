"""Pytest fixtures for the Postgres-backed persistence layer.

A session-scoped `PostgresContainer` boots once per pytest run; an autouse
function-scoped fixture truncates all tables between tests. This replaces
the per-test SQLite-file pattern from the SQLite era.

Requires Docker on the dev machine — the container is `postgres:16-alpine`,
matching production.
"""

from __future__ import annotations

import pytest
from testcontainers.postgres import PostgresContainer

import polymarket_bot.persistence.schema as schema_mod

_SCHEMA_TABLES = (
    "weather_research_obs",
    "equity_curve",
    "settlements",
    "fills",
    "orders",
    "markets",
    "meta",
    "forecast_cache",
)


@pytest.fixture(scope="session")
def _pg_container():
    """Start a Postgres container for the whole pytest session."""
    container = PostgresContainer("postgres:16-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session", autouse=True)
def _init_db(_pg_container):
    """Initialise the schema module's connection pool once per session."""
    url = _pg_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql://", 1)
    schema_mod.reset_for_tests()
    schema_mod.init_db(url)
    yield
    schema_mod.reset_for_tests()


@pytest.fixture(autouse=True)
def _truncate_tables():
    """Per-test truncation. RESTART IDENTITY resets BIGSERIAL counters too."""
    pool = schema_mod.get_pool()
    with pool.connection() as conn:
        conn.execute(
            "TRUNCATE " + ", ".join(_SCHEMA_TABLES) + " RESTART IDENTITY CASCADE"
        )
    yield


@pytest.fixture(autouse=True)
def _fast_live_broker_retries(monkeypatch):
    """Phase D.2 introduced retries with exponential backoff (1s/2s/4s).
    Tests that exercise the LiveBroker retry path would otherwise sleep ~7s
    per failing call. Patch the base sleep to 0 so tests stay fast."""
    import polymarket_bot.execution.live_broker as lb_mod
    monkeypatch.setattr(lb_mod, "_RETRY_BASE_SLEEP", 0.0)
    yield
