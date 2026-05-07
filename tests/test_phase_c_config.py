"""Phase C — config.strategy_share() tests.

Each strategy service multiplies the global equity by its share to get
its own bankroll slice. Three behaviours we care about:

  • Default (no env overrides): even split — 1/N for N registered.
  • Explicit override via BANKROLL_SHARE_<NAME>: that value wins.
  • Out-of-band values (negative, > 1) clamp to [0, 1].
"""

from __future__ import annotations

from polymarket_bot.config import BotConfig
from polymarket_bot.strategy.registry import list_strategies


def test_strategy_share_default_is_one_over_n():
    config = BotConfig.from_env()
    n = len(list_strategies())
    assert n >= 2  # weather_forecast + bucket_arbitrage are always registered.
    for name in list_strategies():
        assert config.strategy_share(name) == 1.0 / n


def test_strategy_share_explicit_override(monkeypatch):
    monkeypatch.setenv("BANKROLL_SHARE_WEATHER_FORECAST", "0.7")
    monkeypatch.setenv("BANKROLL_SHARE_BUCKET_ARBITRAGE", "0.3")
    config = BotConfig.from_env()
    assert config.strategy_share("weather_forecast") == 0.7
    assert config.strategy_share("bucket_arbitrage") == 0.3


def test_strategy_share_clamps_to_unit_interval(monkeypatch):
    monkeypatch.setenv("BANKROLL_SHARE_WEATHER_FORECAST", "1.5")
    monkeypatch.setenv("BANKROLL_SHARE_BUCKET_ARBITRAGE", "-0.2")
    config = BotConfig.from_env()
    assert config.strategy_share("weather_forecast") == 1.0
    assert config.strategy_share("bucket_arbitrage") == 0.0


def test_strategy_share_unknown_strategy_falls_back_to_even_split(monkeypatch):
    """A name with no env override and not in the registry should still
    return a positive fraction — the runner uses this for sizing, so 0
    would silently disable trading."""
    monkeypatch.delenv("BANKROLL_SHARE_FOO", raising=False)
    config = BotConfig.from_env()
    n = len(list_strategies())
    assert config.strategy_share("foo") == 1.0 / n
