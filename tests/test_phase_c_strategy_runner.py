"""Phase C — `_strategy_tick` disabled-gate test.

The strategy runner wakes every `tick_seconds`; the *first* thing it does
is consult the `enabled_strategies` meta key. If the runner's own strategy
is missing, the tick must short-circuit *before* it tries to discover
events / fetch quotes / do anything else expensive.

Mocking the rest of the tick is out of scope here — the cheap, focused
assertion is "when disabled, no DB writes / external calls / order
emission happens". We get that for free by patching
`discover_open_events` to raise: if the gate works, the patch is never
hit; if the gate is broken, the test fails loudly.
"""

from __future__ import annotations

from polymarket_bot.config import BotConfig
from polymarket_bot.execution.paper_broker import PaperBroker
from polymarket_bot.execution.router import Router
from polymarket_bot.persistence.repo import set_enabled_strategies
from polymarket_bot.services.strategy_runner import _strategy_tick
from polymarket_bot.strategy.weather_forecast import WeatherForecastStrategy


def test_strategy_tick_short_circuits_when_disabled(monkeypatch):
    """If the strategy isn't in the enabled set, the tick returns early
    without ever calling `discover_open_events`."""

    def _explode(*_args, **_kwargs):  # pragma: no cover - asserted unreachable
        raise AssertionError("discover_open_events should not be called when "
                             "the strategy is disabled")

    monkeypatch.setattr(
        "polymarket_bot.services.strategy_runner.discover_open_events",
        _explode,
    )

    set_enabled_strategies({"bucket_arbitrage"})  # weather_forecast OFF

    config = BotConfig.from_env()
    strategy = WeatherForecastStrategy()
    broker = PaperBroker()
    router = Router(broker, strategy.name,
                    max_notional_usd=config.max_order_notional_usd)

    # Should run cleanly with no exception (and no DB writes).
    _strategy_tick(config, strategy, router, cities=["paris"])


def test_strategy_tick_proceeds_when_enabled(monkeypatch):
    """Enabling the strategy must let the tick reach
    `discover_open_events`. We don't care about the rest of the pipeline
    for this test — we just confirm the gate opened by raising inside the
    discover call and asserting the exception bubbles up."""

    sentinel = RuntimeError("gate opened — discover_open_events reached")

    def _raise(*_args, **_kwargs):
        raise sentinel

    monkeypatch.setattr(
        "polymarket_bot.services.strategy_runner.discover_open_events",
        _raise,
    )

    set_enabled_strategies({"weather_forecast"})

    config = BotConfig.from_env()
    strategy = WeatherForecastStrategy()
    broker = PaperBroker()
    router = Router(broker, strategy.name,
                    max_notional_usd=config.max_order_notional_usd)

    try:
        _strategy_tick(config, strategy, router, cities=["paris"])
    except RuntimeError as exc:
        assert exc is sentinel
    else:  # pragma: no cover
        raise AssertionError("expected the patched discover_open_events to "
                             "raise, but the tick returned normally")
