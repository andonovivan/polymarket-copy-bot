"""Betting-strategy interface + shared types.

A `BettingStrategy.evaluate(state) -> list[OrderAction]` is a pure function:
state in, list of place/cancel actions out. The router/broker turns those into
real or simulated orders on the Polymarket CLOB.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Union


# ---------------------------------------------------------------------------
# Order actions emitted by strategies.
# ---------------------------------------------------------------------------


@dataclass
class PlaceLimit:
    """Place a BUY/SELL limit order on a specific token (YES or NO)."""
    market_id: str                       # Polymarket conditionId of the bucket
    token_id: str                        # YES or NO token id (the side we're buying)
    token_side: Literal["YES", "NO"]
    side: Literal["BUY", "SELL"]
    price: float                         # 0 < price < 1
    size: float                          # shares
    client_order_id: str                 # strategy-assigned id


@dataclass
class CancelOrder:
    order_id: str


OrderAction = Union[PlaceLimit, CancelOrder]


# ---------------------------------------------------------------------------
# Bucket / event state visible to a strategy.
# ---------------------------------------------------------------------------


@dataclass
class Bucket:
    """One categorical sub-market of a weather event (e.g. '60-61°F')."""
    label: str                           # e.g. "60-61°F"
    market_id: str                       # Polymarket conditionId
    yes_token_id: str
    no_token_id: str
    yes_bid: float | None
    yes_ask: float | None                # what we'd pay to BUY YES
    yes_mid: float | None
    depth_yes_ask_usd: float             # YES-ask-side liquidity we'd cross
    model_p: float | None = None         # filled by the strategy ctx (None if no forecast)
    # NO-side quotes (for the over-priced-bucket case — sell YES is rare; we
    # buy NO instead, which has the same payoff structure). Populated by
    # populate_quotes from the CLOB NO order book.
    no_bid: float | None = None
    no_ask: float | None = None
    no_mid: float | None = None
    depth_no_ask_usd: float = 0.0


@dataclass
class WeatherEvent:
    """A categorical weather market — a bundle of mutually-exclusive buckets."""
    slug: str
    title: str
    city_key: str                        # registry key (e.g. "paris")
    end_ts: int                          # market close
    resolution_ts: int                   # when actual outcome finalizes (= end_ts)
    unit: Literal["fahrenheit", "celsius"]
    buckets: list[Bucket] = field(default_factory=list)


@dataclass
class OpenOrder:
    order_id: str
    client_order_id: str
    market_id: str
    token_side: Literal["YES", "NO"]
    side: Literal["BUY", "SELL"]
    price: float
    size: float
    filled: float = 0.0
    placed_at: int = 0


@dataclass
class BetState:
    """Read-only snapshot the strategy gets each tick for one event."""
    event: WeatherEvent
    bankroll: float
    seconds_to_resolution: int
    # bucket label → list of our open orders on that bucket's YES token
    open_orders_by_bucket: dict[str, list[OpenOrder]]
    # bucket label → already-held YES shares (from prior fills, awaiting settlement)
    held_yes_shares_by_bucket: dict[str, float]
    # $ exposure already locked in across all events (cost basis of held YES shares)
    total_open_exposure_usd: float
    # Strategy parameters from BotConfig:
    edge_threshold: float                # min |model_p - market_p| to bet
    kelly_fraction: float                # fractional Kelly multiplier
    max_bet_pct: float                   # hard cap as % of bankroll
    max_total_exposure_pct: float        # cap on aggregate bankroll committed
    min_market_depth_usd: float          # skip buckets thinner than this
    lockout_seconds: int                 # don't bet within N seconds of resolution
    # bucket label → already-held NO shares (symmetric to held_yes; prevents
    # NO double-entry after a NO BUY fills and is no longer in
    # `open_orders_by_bucket`). Defaulted so existing test fixtures still work.
    held_no_shares_by_bucket: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Strategy ABC.
# ---------------------------------------------------------------------------


class BettingStrategy(ABC):
    """Stateless: state in, actions out. One call per event per tick."""

    name: str = "abstract"           # registry key, never user-facing
    display_name: str = "Abstract"   # human-readable label for the dashboard
    # Set False on strategies that don't read bucket.model_p — the runner can
    # then skip the Open-Meteo fetch entirely, halving API traffic and
    # letting model-independent strategies (e.g. bucket_arbitrage) keep
    # running through Open-Meteo outages / rate-limit windows.
    needs_model_probabilities: bool = True

    @abstractmethod
    def evaluate(self, state: BetState) -> list[OrderAction]: ...
