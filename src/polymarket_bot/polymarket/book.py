"""Public, unauthenticated reads of the Polymarket CLOB orderbook."""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

CLOB_API_URL = "https://clob.polymarket.com"

# In-process cache of token IDs that are 404 / persistently failing. We back
# off polling them so the logs stop drowning in the same warning every tick.
_DEAD_TTL_SECONDS = 900           # 15 min before we re-probe a "dead" token
_DEAD_BACKOFF_FAILS = 3           # consecutive failures before we mark dead
_dead_lock = threading.Lock()
_dead_until: dict[str, float] = {}
_fail_count: dict[str, int] = {}


def _is_dead(token_id: str) -> bool:
    with _dead_lock:
        return _dead_until.get(token_id, 0.0) > time.monotonic()


def _record_failure(token_id: str, is_404: bool) -> None:
    with _dead_lock:
        n = _fail_count.get(token_id, 0) + 1
        _fail_count[token_id] = n
        if is_404 or n >= _DEAD_BACKOFF_FAILS:
            _dead_until[token_id] = time.monotonic() + _DEAD_TTL_SECONDS


def _record_success(token_id: str) -> None:
    with _dead_lock:
        _fail_count.pop(token_id, None)
        _dead_until.pop(token_id, None)


def fetch_book(token_id: str, *, client: httpx.Client | None = None) -> dict[str, Any] | None:
    """Fetch one side's full orderbook (bids+asks). No private key required.

    Tokens that have 404'd recently are short-circuited to None for
    `_DEAD_TTL_SECONDS` so we don't spam the CLOB or our own logs.
    """
    if _is_dead(token_id):
        return None
    own = client is None
    c = client or httpx.Client(timeout=8.0)
    try:
        resp = c.get(f"{CLOB_API_URL}/book", params={"token_id": token_id})
        is_404 = resp.status_code == 404
        if is_404:
            _record_failure(token_id, is_404=True)
            return None
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:  # the API returns 200 with an error field
            _record_failure(token_id, is_404=False)
            return None
        _record_success(token_id)
        return data
    except Exception as exc:
        _record_failure(token_id, is_404=False)
        logger.warning("book_fetch_failed", token_id=token_id[:14] + "…", error=str(exc)[:200])
        return None
    finally:
        if own:
            c.close()
