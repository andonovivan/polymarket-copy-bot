"""Bot configuration loaded from environment variables."""

from __future__ import annotations

import os
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()


Mode = Literal["paper", "live", "backtest"]


class BotConfig(BaseModel):
    """All configuration for polymarket-bot."""

    # --- Run mode ---
    mode: Mode = Field(default="paper")

    # --- Polymarket / Wallet credentials (only required for live mode) ---
    clob_api_url: str = Field(default="https://clob.polymarket.com")
    chain_id: int = Field(default=137, description="Polygon mainnet")
    private_key: str = Field(default="")
    api_key: str = Field(default="")
    api_secret: str = Field(default="")
    api_passphrase: str = Field(default="")

    # --- Strategy ---
    strategy: str = Field(default="momentum_logit")
    edge_threshold: float = Field(default=0.03, description="|p_model - p_market| min to bet")
    kelly_fraction: float = Field(default=0.25, description="Fractional Kelly multiplier")
    max_bet_pct: float = Field(default=0.05, description="Hard cap on stake as % of bankroll")
    min_market_depth_usd: float = Field(default=50.0, description="Skip if YES book is thinner than this")
    cooldown_bars: int = Field(default=0, description="Bars to skip after a loss (0 = none)")
    lock_buffer_seconds: int = Field(default=30, description="Skip markets within N seconds of resolution")

    # --- Model ---
    model_window_days: int = Field(default=60)
    model_retrain_cron: str = Field(default="0 4 * * *", description="UTC")

    # --- Bankroll ---
    starting_bankroll: float = Field(default=100.0, description="Used in paper/backtest")

    # --- Tick cadence ---
    tick_seconds: int = Field(default=60, description="How often the live loop polls")

    # --- Dashboard ---
    dashboard_port: int = Field(default=8080)

    # --- Logging ---
    log_level: str = Field(default="INFO")

    # --- Live-mode safety ---
    live_confirm: bool = Field(
        default=False,
        description="Set true via POLYMARKET_BOT_LIVE=1 to allow real-money trading",
    )

    @classmethod
    def from_env(cls) -> BotConfig:
        return cls(
            mode=os.getenv("MODE", "paper"),  # type: ignore[arg-type]
            clob_api_url=os.getenv("CLOB_API_URL", "https://clob.polymarket.com"),
            chain_id=int(os.getenv("CHAIN_ID", "137")),
            private_key=os.getenv("PRIVATE_KEY", ""),
            api_key=os.getenv("POLYMARKET_API_KEY", ""),
            api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
            api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE", ""),
            strategy=os.getenv("STRATEGY", "momentum_logit"),
            edge_threshold=float(os.getenv("EDGE_THRESHOLD", "0.03")),
            kelly_fraction=float(os.getenv("KELLY_FRACTION", "0.25")),
            max_bet_pct=float(os.getenv("MAX_BET_PCT", "0.05")),
            min_market_depth_usd=float(os.getenv("MIN_MARKET_DEPTH_USD", "50")),
            cooldown_bars=int(os.getenv("COOLDOWN_BARS", "0")),
            lock_buffer_seconds=int(os.getenv("LOCK_BUFFER_SECONDS", "30")),
            model_window_days=int(os.getenv("MODEL_WINDOW_DAYS", "60")),
            model_retrain_cron=os.getenv("MODEL_RETRAIN_CRON", "0 4 * * *"),
            starting_bankroll=float(os.getenv("STARTING_BANKROLL", "100")),
            tick_seconds=int(os.getenv("TICK_SECONDS", "60")),
            dashboard_port=int(os.getenv("DASHBOARD_PORT", "8080")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            live_confirm=os.getenv("POLYMARKET_BOT_LIVE", "0") == "1",
        )
