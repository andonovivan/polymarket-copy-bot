"""Read live YES/NO quotes from the Polymarket CLOB (unauthenticated).

CLOB book conventions (verified empirically against the live endpoint):
  - bids: sorted ASCENDING by price → best (highest) bid is `bids[-1]`
  - asks: sorted DESCENDING by price → best (lowest) ask is `asks[-1]`
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from polymarket_bot.polymarket.book import fetch_book


@dataclass
class Quote:
    yes_bid: float | None
    yes_ask: float | None
    yes_mid: float | None
    no_bid: float | None
    no_ask: float | None
    no_mid: float | None
    depth_yes_ask_usd: float    # USD on the YES ask side (what we cross to BUY YES)
    depth_no_ask_usd: float     # USD on the NO ask side (what we cross to BUY NO)


def parse_book(book: dict | None) -> tuple[float | None, float | None, float]:
    """Return (best_bid, best_ask, ask_side_usd_depth) for one token's book."""
    if not book:
        return None, None, 0.0
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = float(bids[-1]["price"]) if bids else None
    best_ask = float(asks[-1]["price"]) if asks else None
    ask_usd = sum(float(l["price"]) * float(l["size"]) for l in asks)
    return best_bid, best_ask, ask_usd


def fetch_quote(yes_token_id: str, no_token_id: str,
                *, client: httpx.Client | None = None) -> Quote | None:
    """Fetch a snapshot of the YES + NO order books for a binary market."""
    own = client is None
    c = client or httpx.Client(timeout=8.0)
    try:
        yes_book = fetch_book(yes_token_id, client=c)
        no_book = fetch_book(no_token_id, client=c)
        if yes_book is None and no_book is None:
            return None
        yes_bid, yes_ask, yes_ask_usd = parse_book(yes_book)
        no_bid, no_ask, no_ask_usd = parse_book(no_book)
        yes_mid = ((yes_bid + yes_ask) / 2.0) if (yes_bid is not None and yes_ask is not None) else None
        no_mid = ((no_bid + no_ask) / 2.0) if (no_bid is not None and no_ask is not None) else None
        return Quote(
            yes_bid=yes_bid, yes_ask=yes_ask, yes_mid=yes_mid,
            no_bid=no_bid, no_ask=no_ask, no_mid=no_mid,
            depth_yes_ask_usd=yes_ask_usd, depth_no_ask_usd=no_ask_usd,
        )
    finally:
        if own:
            c.close()
