"""Public, unauthenticated reads of the Polymarket CLOB orderbook."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

CLOB_API_URL = "https://clob.polymarket.com"


def fetch_book(token_id: str, *, client: httpx.Client | None = None) -> dict[str, Any] | None:
    """Fetch one side's full orderbook (bids+asks). No private key required."""
    own = client is None
    c = client or httpx.Client(timeout=8.0)
    try:
        resp = c.get(f"{CLOB_API_URL}/book", params={"token_id": token_id})
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:  # the API returns 200 with an error field
            return None
        return data
    except Exception as exc:
        logger.warning("book_fetch_failed", token_id=token_id[:14] + "…", error=str(exc)[:200])
        return None
    finally:
        if own:
            c.close()
