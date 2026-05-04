"""Classify Polymarket CLOB error responses for the live broker.

Reference: https://docs.polymarket.com/resources/error-codes

Three actionable buckets:
  HALT    — auth / compliance / banned. Stop the bot, alert the operator.
  SKIP    — bad input that no retry can fix (tick size, min order, expiry).
            Strategy should drop this bet and try a different bucket.
  RETRY   — transient (rate limit, matching-engine restart, service paused,
            internal 5xx). Caller should backoff and try later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Action = Literal["HALT", "SKIP", "RETRY"]


@dataclass
class ClobError:
    action: Action
    http_status: int | None
    message: str


_HALT_SUBSTRINGS = (
    "address banned",
    "closed only mode",
    "Invalid api key",
    "Invalid L1 Request headers",
    "Unauthorized",
    "owner has to be",
    "signer address has to be",
)


_SKIP_SUBSTRINGS = (
    "minimum tick size rule",
    "Size lower than the minimum",
    "invalid expiration",
    "Invalid order payload",
    "Invalid token id",
    "Invalid orderID",
    "Duplicated",
    "post-only order: order crosses book",
    "FOK orders are filled or killed",
    "no orders found to match with FAK",
    "FOK orders",
    "Payload exceeds the limit",
    "no orderbook exists",
    "not enough balance / allowance",
    "balance / allowance",                 # variant
    "price discrepancy greater than allowed",
)


_RETRY_SUBSTRINGS = (
    "Too Many Requests",
    "Trading is currently disabled",
    "Trading is currently cancel-only",
    "Matching engine is restarting",
    "match delayed",
    "market is not yet ready",
    "rounding issues",
    "Internal server error",
    "could not insert order",
    "context canceled",
)


def classify(http_status: int | None, message: str | None) -> ClobError:
    msg = (message or "").strip()
    code = http_status if http_status is not None else 0

    # Hard auth / compliance failures: halt regardless of message specifics.
    if code in (401,):
        return ClobError("HALT", code, msg or "unauthorized")
    if code == 425 or code in (429, 500, 502, 503, 504):
        return ClobError("RETRY", code, msg or f"http {code}")

    # Otherwise route by message string when available.
    lower = msg.lower()
    for s in _HALT_SUBSTRINGS:
        if s.lower() in lower:
            return ClobError("HALT", code, msg)
    for s in _RETRY_SUBSTRINGS:
        if s.lower() in lower:
            return ClobError("RETRY", code, msg)
    for s in _SKIP_SUBSTRINGS:
        if s.lower() in lower:
            return ClobError("SKIP", code, msg)

    # Conservative default for unknown 4xx → SKIP (don't infinite-retry).
    if 400 <= code < 500:
        return ClobError("SKIP", code, msg or f"http {code}")
    # Unknown otherwise → RETRY (network blip etc).
    return ClobError("RETRY", code, msg or "unknown")
