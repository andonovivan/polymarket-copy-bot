"""Bot configuration loaded from environment variables."""

from __future__ import annotations

import os
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()


Mode = Literal["paper", "live"]


class BotConfig(BaseModel):
    """All configuration for polymarket-bot (weather-forecast betting)."""

    # --- Run mode ---
    mode: Mode = Field(default="paper")

    # --- Polymarket / Wallet credentials (only required for live mode) ---
    clob_api_url: str = Field(default="https://clob.polymarket.com")
    chain_id: int = Field(default=137)
    private_key: str = Field(default="")
    api_key: str = Field(default="")
    api_secret: str = Field(default="")
    api_passphrase: str = Field(default="")

    # --- Strategy ---
    strategy: str = Field(default="weather_forecast")

    # --- Weather params ---
    weather_cities: str = Field(
        default="paris,madrid,london,tokyo",
        description="Comma-separated city keys from CITY_REGISTRY (allowlist).",
    )
    days_ahead: int = Field(default=4, description="How many upcoming days of markets to consider")
    edge_threshold: float = Field(default=0.05, description="Min model_p − yes_ask to bet")
    kelly_fraction: float = Field(default=0.25, description="Fractional-Kelly multiplier")
    max_bet_pct: float = Field(default=0.05, description="Hard cap on a single bet as % of bankroll")
    max_total_exposure_pct: float = Field(default=0.30,
                                          description="Cap on aggregate bankroll committed across all open bets")
    min_market_depth_usd: float = Field(default=20.0, description="Skip buckets thinner than this")
    lock_buffer_seconds: int = Field(default=600,
                                     description="Stop betting within N seconds of resolution")

    # --- Bankroll ---
    starting_bankroll: float = Field(default=100.0)

    # --- Tick cadence ---
    tick_seconds: int = Field(default=60, description="Polling interval — weather is slow")

    # --- Dashboard ---
    dashboard_port: int = Field(default=8080)

    # --- Logging ---
    log_level: str = Field(default="INFO")

    # --- Live-mode safety ---
    live_confirm: bool = Field(default=False,
                               description="Set true via POLYMARKET_BOT_LIVE=1 to enable real-money trading")

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
            strategy=os.getenv("STRATEGY", "weather_forecast"),
            weather_cities=os.getenv("WEATHER_CITIES", "paris,madrid,london,tokyo"),
            days_ahead=int(os.getenv("DAYS_AHEAD", "4")),
            edge_threshold=float(os.getenv("EDGE_THRESHOLD", "0.05")),
            kelly_fraction=float(os.getenv("KELLY_FRACTION", "0.25")),
            max_bet_pct=float(os.getenv("MAX_BET_PCT", "0.05")),
            max_total_exposure_pct=float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "0.30")),
            min_market_depth_usd=float(os.getenv("MIN_MARKET_DEPTH_USD", "20")),
            lock_buffer_seconds=int(os.getenv("LOCK_BUFFER_SECONDS", "600")),
            starting_bankroll=float(os.getenv("STARTING_BANKROLL", "100")),
            tick_seconds=int(os.getenv("TICK_SECONDS", "60")),
            dashboard_port=int(os.getenv("DASHBOARD_PORT", "8080")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            live_confirm=os.getenv("POLYMARKET_BOT_LIVE", "0") == "1",
        )
