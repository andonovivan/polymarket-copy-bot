"""SQLite-backed persistence: schema, types, accessors."""

from polymarket_bot.persistence.repo import (
    Bar,
    Bet,
    Market,
    Trade,
    append_equity,
    equity_curve,
    get_market,
    get_meta,
    insert_bet,
    insert_trade,
    latest_bar_time,
    latest_equity,
    list_trades,
    load_bars,
    mark_bet_settled,
    open_bets,
    set_meta,
    settle_market,
    trade_stats,
    upsert_bars,
    upsert_market,
)
from polymarket_bot.persistence.schema import init_db

__all__ = [
    "Bar", "Bet", "Market", "Trade",
    "init_db",
    "append_equity", "equity_curve", "latest_equity",
    "get_market", "settle_market", "upsert_market",
    "get_meta", "set_meta",
    "insert_bet", "open_bets", "mark_bet_settled",
    "insert_trade", "list_trades", "trade_stats",
    "upsert_bars", "latest_bar_time", "load_bars",
]
