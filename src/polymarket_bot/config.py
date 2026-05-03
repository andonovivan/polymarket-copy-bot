"""Bot configuration loaded from environment variables (MM build)."""

from __future__ import annotations

import os
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()


Mode = Literal["paper", "live"]


class BotConfig(BaseModel):
    """All configuration for polymarket-bot in market-making mode."""

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
    strategy: str = Field(default="spread_only")
    base_spread: float = Field(default=0.02, description="Target spread in $ around fair value")
    max_inventory_shares: float = Field(default=20.0, description="Per-side soft cap")
    inventory_skew: float = Field(default=0.0005,
                                  description="$/share quote shift per share of net inventory imbalance")
    lock_buffer_seconds: int = Field(default=30,
                                     description="Cancel quotes within N seconds of resolution")

    # --- Bankroll ---
    starting_bankroll: float = Field(default=100.0)

    # --- Tick cadence ---
    tick_seconds: int = Field(default=5,
                              description="MM cadence (5s by default — markets only live for 300s)")

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
            strategy=os.getenv("STRATEGY", "spread_only"),
            base_spread=float(os.getenv("BASE_SPREAD", "0.02")),
            max_inventory_shares=float(os.getenv("MAX_INVENTORY_SHARES", "20")),
            inventory_skew=float(os.getenv("INVENTORY_SKEW", "0.0005")),
            lock_buffer_seconds=int(os.getenv("LOCK_BUFFER_SECONDS", "30")),
            starting_bankroll=float(os.getenv("STARTING_BANKROLL", "100")),
            tick_seconds=int(os.getenv("TICK_SECONDS", "5")),
            dashboard_port=int(os.getenv("DASHBOARD_PORT", "8080")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            live_confirm=os.getenv("POLYMARKET_BOT_LIVE", "0") == "1",
        )
