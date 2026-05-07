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
        default="paris,madrid,london,tokyo,taipei,moscow,chengdu,shanghai,chongqing,helsinki,beijing",
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
    # Drop tail buckets where only a handful of the 122 ensemble members landed
    # — those probabilities are statistical noise, not signal. Setting model_p
    # to None on these buckets short-circuits the strategy's BUY block while
    # leaving profit-take SELLs untouched.
    min_bucket_member_count: int = Field(
        default=6,
        description="Buckets with raw ensemble counts below this get model_p=None.",
    )
    # Per-city warmup gate — refuse new BUYs until Path B has captured this
    # many settled obs for the city (so calibration's bias correction can fit).
    # Default 0 = gate disabled. Doesn't gate SELL or CancelOrder.
    warmup_min_obs: int = Field(
        default=0,
        description="Min settled weather_research_obs per city before BUYs are allowed.",
    )
    # Concurrency for parallel CLOB book fetches in populate_quotes.
    clob_fetch_concurrency: int = Field(
        default=20,
        description="Max parallel HTTP calls when populating bucket quotes.",
    )
    # Random delay (uniform 0..N seconds) at strategy-runner startup, so two
    # strategy containers don't burst-fetch the same forecasts simultaneously.
    startup_jitter_seconds: float = Field(
        default=10.0,
        description="Max random sleep at strategy-runner startup. 0 disables.",
    )

    # --- Fees ---
    # Polymarket weather markets have feeSchedule.rate=0.05 (5%) on winnings, taker-only.
    # We're always takers in v1 (BUY at ask), so we always pay this fee on winning shares.
    winning_fee_bps: int = Field(default=500,
                                 description="Fee in bps applied to winnings (1 - entry_price) per winning share")

    # --- Equity sampling cadence ---
    equity_sample_seconds: int = Field(default=60,
                                       description="Append a MTM-equity snapshot every N seconds")

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

    # --- Research / live-capture (Path B for city evaluation) ---
    research_enabled: bool = Field(default=False,
                                   description="Capture (model_p, model_day_max_mean, market_p, outcome) per bucket")
    research_window_seconds: int = Field(default=3600,
                                         description="Only snapshot events settling within this many seconds")
    research_dedupe_seconds: int = Field(default=600,
                                         description="Skip if same (city, slug, bucket) was observed this recently")
    research_capture_candidates: bool = Field(
        default=False,
        description=("Also capture the non-promoted candidate cities (in addition to "
                     "production CITY_REGISTRY). Off by default — enable when API quota allows."),
    )

    # --- Bayesian fusion (#4 — observed-temp prior on ensemble forecasts) ---
    bayesian_fusion_enabled: bool = Field(
        default=True,
        description="Sharpen ensemble probabilities by fusing with observed-so-far temps "
                    "in the hours leading up to resolution.",
    )
    bayesian_fusion_within_seconds: int = Field(
        default=21600,   # 6h
        description="Only fuse when an event is this close to resolution.",
    )

    # --- NO-side trades (#2 — buy NO on over-priced buckets) ---
    no_side_enabled: bool = Field(
        default=True,
        description="Also evaluate BUY NO opportunities (doubles populate_quotes API cost).",
    )

    # --- Live-mode safety cap (Phase D.6) ---
    max_order_notional_usd: float = Field(
        default=50.0,
        description="Hard cap on a single order's price × size, enforced at the Router. "
                    "Catches fat-finger / config bugs before the order hits the CLOB.",
    )

    # --- Per-strategy bankroll slicing (Phase C.4) ---
    # Loaded as a dict from BANKROLL_SHARE_<UPPER_NAME> env vars (e.g.
    # BANKROLL_SHARE_WEATHER_FORECAST=0.7). Defaults to 1/N for the N
    # currently-registered strategies — used by `strategy_share()` below.
    bankroll_shares: dict[str, float] = Field(default_factory=dict)

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
            weather_cities=os.getenv(
                "WEATHER_CITIES",
                "paris,madrid,london,tokyo,taipei,moscow,chengdu,shanghai,chongqing,helsinki,beijing",
            ),
            days_ahead=int(os.getenv("DAYS_AHEAD", "4")),
            edge_threshold=float(os.getenv("EDGE_THRESHOLD", "0.05")),
            kelly_fraction=float(os.getenv("KELLY_FRACTION", "0.25")),
            max_bet_pct=float(os.getenv("MAX_BET_PCT", "0.05")),
            max_total_exposure_pct=float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "0.30")),
            min_market_depth_usd=float(os.getenv("MIN_MARKET_DEPTH_USD", "20")),
            lock_buffer_seconds=int(os.getenv("LOCK_BUFFER_SECONDS", "600")),
            min_bucket_member_count=int(os.getenv("MIN_BUCKET_MEMBER_COUNT", "6")),
            warmup_min_obs=int(os.getenv("WARMUP_MIN_OBS", "0")),
            clob_fetch_concurrency=int(os.getenv("CLOB_FETCH_CONCURRENCY", "20")),
            startup_jitter_seconds=float(os.getenv("STARTUP_JITTER_SECONDS", "10")),
            winning_fee_bps=int(os.getenv("WINNING_FEE_BPS", "500")),
            equity_sample_seconds=int(os.getenv("EQUITY_SAMPLE_SECONDS", "60")),
            starting_bankroll=float(os.getenv("STARTING_BANKROLL", "100")),
            tick_seconds=int(os.getenv("TICK_SECONDS", "60")),
            dashboard_port=int(os.getenv("DASHBOARD_PORT", "8080")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            live_confirm=os.getenv("POLYMARKET_BOT_LIVE", "0") == "1",
            research_enabled=os.getenv("RESEARCH_ENABLED", "0") == "1",
            research_window_seconds=int(os.getenv("RESEARCH_WINDOW_SECONDS", "3600")),
            research_dedupe_seconds=int(os.getenv("RESEARCH_DEDUPE_SECONDS", "600")),
            research_capture_candidates=os.getenv("RESEARCH_CAPTURE_CANDIDATES", "0") == "1",
            bayesian_fusion_enabled=os.getenv("BAYESIAN_FUSION_ENABLED", "1") == "1",
            bayesian_fusion_within_seconds=int(os.getenv("BAYESIAN_FUSION_WITHIN_SECONDS", "21600")),
            no_side_enabled=os.getenv("NO_SIDE_ENABLED", "1") == "1",
            max_order_notional_usd=float(os.getenv("MAX_ORDER_NOTIONAL_USD", "50.0")),
            bankroll_shares={
                k.removeprefix("BANKROLL_SHARE_").lower(): float(v)
                for k, v in os.environ.items()
                if k.startswith("BANKROLL_SHARE_")
            },
        )

    def strategy_share(self, strategy_name: str) -> float:
        """Return the bankroll fraction allocated to `strategy_name`.

        Looks up `BANKROLL_SHARE_<UPPER_NAME>` first; falls back to 1/N
        where N is the number of registered strategies. Caller multiplies
        the bot's current equity by this share before calling
        `strategy.evaluate(BetState)`.
        """
        explicit = self.bankroll_shares.get(strategy_name.lower())
        if explicit is not None:
            return max(0.0, min(1.0, explicit))
        from polymarket_bot.strategy.registry import list_strategies
        all_names = list_strategies() or [strategy_name]
        return 1.0 / len(all_names)
