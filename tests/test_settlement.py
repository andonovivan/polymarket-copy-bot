"""Settlement rule + Brier calculation."""

from __future__ import annotations

from polymarket_bot.polymarket.settle import outcome_for_bar


def test_outcome_up_when_close_above_open():
    assert outcome_for_bar(bar_open=100.0, bar_close=101.0) == "UP"


def test_outcome_down_when_close_below_open():
    assert outcome_for_bar(bar_open=100.0, bar_close=99.5) == "DOWN"


def test_outcome_up_on_flat_bar():
    # Polymarket's market description: ">= the price at the beginning". Ties → UP.
    assert outcome_for_bar(bar_open=100.0, bar_close=100.0) == "UP"
