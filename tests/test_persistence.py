"""Schema initialization + round-trip CRUD against an in-memory DB."""

from __future__ import annotations

from pathlib import Path

import polymarket_bot.persistence.schema as schema
from polymarket_bot.persistence.repo import (
    Bar,
    Bet,
    Market,
    append_equity,
    insert_bet,
    latest_bar_time,
    latest_equity,
    open_bets,
    upsert_bars,
    upsert_market,
)


def _fresh_db(tmp_path: Path) -> None:
    """Reset the singleton connection to a temp file."""
    schema._conn = None
    schema.init_db(tmp_path / "test.db")


def test_init_creates_tables(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    conn = schema.get_conn(tmp_path / "test.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"markets", "bets", "trades", "equity_curve", "btc_bars",
            "polymarket_quotes", "models", "meta"}.issubset(tables)


def test_market_and_bet_roundtrip(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    upsert_market(Market(
        market_id="0xabc", slug="btc-updown-5m-1700000000",
        resolution_ts=1700000000, yes_token_id="yes-1", no_token_id="no-1",
    ))
    bet_id = insert_bet(Bet(
        id=None, market_id="0xabc", side="YES", shares=10.0,
        entry_price=0.55, stake=5.5,
        predicted_p=0.62, market_p=0.55, edge=0.07,
        strategy="momentum_logit", model_version="logit-test",
        opened_at=1700000000,
    ))
    assert bet_id > 0
    open_ = open_bets()
    assert len(open_) == 1
    assert open_[0].market_id == "0xabc"


def test_bars_and_equity(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    bars = [Bar(open_time=1700000000 + 300 * i, o=100, h=101, l=99, c=100.5, v=10) for i in range(5)]
    assert upsert_bars(bars) == 5
    assert latest_bar_time() == 1700000000 + 300 * 4
    append_equity(1700000000, 100.0)
    assert latest_equity() == 100.0
