"""Phase A.3 — meta-table-backed enabled-strategy storage."""

from __future__ import annotations

from polymarket_bot.persistence.repo import (
    get_enabled_strategies,
    set_enabled_strategies,
)


def test_default_all_when_no_row_set():
    out = get_enabled_strategies(default_all=["a", "b", "c"])
    assert out == {"a", "b", "c"}


def test_default_empty_when_no_row_and_no_default():
    assert get_enabled_strategies() == set()


def test_round_trip():
    set_enabled_strategies({"weather_forecast"})
    assert get_enabled_strategies(default_all=["x", "y"]) == {"weather_forecast"}


def test_set_accepts_list_or_set():
    set_enabled_strategies(["a", "b"])
    assert get_enabled_strategies() == {"a", "b"}
    set_enabled_strategies({"c"})
    assert get_enabled_strategies() == {"c"}


def test_set_empty_disables_everything():
    set_enabled_strategies({"a"})
    set_enabled_strategies(set())
    # default_all is the fallback only when the row is absent — once we've
    # written an empty list, the answer is empty (everything disabled).
    assert get_enabled_strategies(default_all=["a", "b"]) == set()


def test_set_dedupes():
    set_enabled_strategies(["a", "a", "b"])
    assert get_enabled_strategies() == {"a", "b"}


def test_corrupted_row_falls_back_to_default():
    """If meta['enabled_strategies'] somehow becomes non-JSON, behave as if
    nothing was set — i.e. default_all."""
    from polymarket_bot.persistence.repo import set_meta
    set_meta("enabled_strategies", "not-json{{")
    assert get_enabled_strategies(default_all=["x"]) == {"x"}
