"""Dashboard service — HTTP server only, read-only on the DB.

Phase C container: hosts the dashboard SPA + JSON API on the configured
port. No tick loop. The HTTP server is the existing
`polymarket_bot.dashboard.server.start_dashboard` thread; this wrapper
keeps the foreground process alive so docker-compose treats the container
as healthy.
"""

from __future__ import annotations

import time

import structlog

from polymarket_bot.config import BotConfig
from polymarket_bot.dashboard.server import start_dashboard
from polymarket_bot.persistence.repo import set_meta
from polymarket_bot.persistence.schema import init_db

logger = structlog.get_logger()


def run_dashboard_service() -> None:
    init_db()
    config = BotConfig.from_env()
    start_dashboard(config)
    logger.info("dashboard_service_starting", port=config.dashboard_port)
    # The HTTP server runs on a daemon thread inside `start_dashboard`. Keep
    # the main thread alive and emit a heartbeat so docker-compose can see
    # liveness via the `meta` table.
    while True:
        try:
            set_meta("last_running_ts:dashboard", str(int(time.time())))
        except Exception as exc:
            logger.warning("dashboard_heartbeat_failed", error=str(exc)[:160])
        time.sleep(30)
