"""Bot configuration loaded from environment variables."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from polymarket_copy_bot.state import load_state

load_dotenv()


class BotConfig(BaseModel):
    """All configuration for the copy-trading bot."""

    # --- Polymarket / Wallet ---
    clob_api_url: str = Field(default="https://clob.polymarket.com")
    chain_id: int = Field(default=137, description="Polygon mainnet")
    private_key: str = Field(default="")
    api_key: str = Field(default="")
    api_secret: str = Field(default="")
    api_passphrase: str = Field(default="")

    # --- Wallets to copy ---
    tracked_wallets: list[str] = Field(default_factory=list)

    # --- Trading parameters ---
    fixed_amount_usdc: float = Field(
        default=2.0,
        description="Fixed USDC amount to spend per copied trade.",
    )
    max_position_usdc: float = Field(
        default=100.0,
        description="Maximum USDC exposure per single market.",
    )
    price_tolerance: float = Field(
        default=0.03,
        description="Max price deviation from the original trade before skipping.",
    )
    copy_sells: bool = Field(
        default=True,
        description="Whether to also replicate sell / exit trades.",
    )

    # --- Polling ---
    poll_interval_seconds: int = Field(
        default=30,
        description="Seconds between each poll cycle.",
    )

    # --- Mode ---
    dry_run: bool = Field(
        default=True,
        description="When True, logs all decisions but never places real orders.",
    )

    # --- Dashboard ---
    dashboard_port: int = Field(default=8080, description="Port for the web dashboard.")

    # --- Logging ---
    log_level: str = Field(default="INFO")

    @classmethod
    def from_env(cls) -> BotConfig:
        """Build config. Tracked wallets are read from bot_state.json first, .env as fallback."""
        # Merge wallets from DB and .env (DB wallets first, then any new ones from .env).
        state = load_state()
        db_wallets = state.get("tracked_wallets", [])
        env_raw = os.getenv("TRACKED_WALLETS", "")
        env_wallets = [w.strip() for w in env_raw.split(",") if w.strip()]
        combined = db_wallets + env_wallets
        # Deduplicate while preserving order.
        wallets = list(dict.fromkeys(w.lower() for w in combined))

        return cls(
            clob_api_url=os.getenv("CLOB_API_URL", "https://clob.polymarket.com"),
            chain_id=int(os.getenv("CHAIN_ID", "137")),
            private_key=os.getenv("PRIVATE_KEY", ""),
            api_key=os.getenv("POLYMARKET_API_KEY", ""),
            api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
            api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE", ""),
            tracked_wallets=wallets,
            fixed_amount_usdc=float(os.getenv("FIXED_AMOUNT_USDC", "2")),
            max_position_usdc=float(os.getenv("MAX_POSITION_USDC", "100")),
            price_tolerance=float(os.getenv("PRICE_TOLERANCE", "0.03")),
            copy_sells=os.getenv("COPY_SELLS", "true").lower() == "true",
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "30")),
            dry_run=os.getenv("DRY_RUN", "true").lower() == "true",
            dashboard_port=int(os.getenv("DASHBOARD_PORT", "8080")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
