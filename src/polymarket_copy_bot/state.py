"""Persist bot state (positions, exposure) to a JSON file across restarts."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import structlog

logger = structlog.get_logger()

DEFAULT_STATE_PATH = Path("bot_state.json")

# Lock to prevent concurrent read-modify-write cycles from corrupting the file.
_file_lock = threading.Lock()


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict:
    """Load state from disk. Returns empty defaults if file doesn't exist or is corrupt."""
    empty = {
        "shares_held": {},
        "exposure": {},
        "tracked_wallets": [],
        "buy_prices": {},
        "last_running_ts": 0,
        "pnl": {
            "total_realized": 0.0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "trade_history": [],
        },
    }
    if not path.exists():
        return empty
    try:
        with _file_lock:
            data = json.loads(path.read_text())
        default_pnl = {
            "total_realized": 0.0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "trade_history": [],
        }
        pnl = data.get("pnl", default_pnl)
        # Ensure all keys exist (backwards compatibility).
        for k, v in default_pnl.items():
            pnl.setdefault(k, v)

        return {
            "shares_held": data.get("shares_held", {}),
            "exposure": data.get("exposure", {}),
            "tracked_wallets": data.get("tracked_wallets", []),
            "buy_prices": data.get("buy_prices", {}),
            "last_running_ts": data.get("last_running_ts", 0),
            "pnl": pnl,
        }
    except Exception as exc:
        logger.warning("state_load_failed", error=str(exc), path=str(path))
        return empty


def save_state(
    shares_held: dict[str, float],
    exposure: dict[str, float],
    tracked_wallets: list[str] | None = None,
    buy_prices: dict[str, float] | None = None,
    pnl: dict | None = None,
    path: Path = DEFAULT_STATE_PATH,
) -> None:
    """Write current state to disk. Merges with existing data to avoid losing fields."""
    try:
        with _file_lock:
            existing = {}
            if path.exists():
                try:
                    existing = json.loads(path.read_text())
                except Exception:
                    pass

            existing["shares_held"] = shares_held
            existing["exposure"] = exposure
            if tracked_wallets is not None:
                existing["tracked_wallets"] = tracked_wallets
            if buy_prices is not None:
                existing["buy_prices"] = buy_prices
            if pnl is not None:
                existing["pnl"] = pnl

            path.write_text(json.dumps(existing, indent=2))
    except Exception as exc:
        logger.error("state_save_failed", error=str(exc), path=str(path))


def update_last_running_ts(ts: int, path: Path = DEFAULT_STATE_PATH) -> None:
    """Update only the last_running_ts field in the state file."""
    try:
        with _file_lock:
            existing = {}
            if path.exists():
                try:
                    existing = json.loads(path.read_text())
                except Exception:
                    pass
            existing["last_running_ts"] = ts
            path.write_text(json.dumps(existing, indent=2))
    except Exception as exc:
        logger.error("timestamp_save_failed", error=str(exc))
