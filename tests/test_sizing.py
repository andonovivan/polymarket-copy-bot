"""Sanity checks for the fractional-Kelly sizing math."""

from __future__ import annotations

import math

from polymarket_bot.risk.sizing import fractional_kelly_stake, kelly_fraction_full


def test_kelly_zero_when_no_edge():
    # Fair coin priced at 0.5 → no edge → no bet.
    assert kelly_fraction_full(p=0.5, price=0.5) == 0.0


def test_kelly_zero_when_negative_edge():
    # Believing 0.4 while market is 0.6 ⇒ shouldn't bet on the YES side at all.
    assert kelly_fraction_full(p=0.4, price=0.6) == 0.0


def test_kelly_textbook_value():
    # Standard Kelly result: p=0.6 at price=0.5 ⇒ f = (1·0.6 - 0.4)/1 = 0.2
    f = kelly_fraction_full(p=0.6, price=0.5)
    assert math.isclose(f, 0.2, rel_tol=1e-9)


def test_fractional_kelly_caps_at_max_bet_pct():
    # With a huge edge, full Kelly would say "go big"; cap kicks in.
    stake = fractional_kelly_stake(
        p_model=0.95, side="YES", price_paid=0.5,
        bankroll=1000.0, kelly_fraction=1.0, max_bet_pct=0.05,
    )
    assert math.isclose(stake, 50.0)  # 5% of bankroll


def test_fractional_kelly_no_side_no_bet():
    # No edge on the NO side either when probability matches market.
    stake = fractional_kelly_stake(
        p_model=0.5, side="NO", price_paid=0.5,
        bankroll=1000.0, kelly_fraction=0.25, max_bet_pct=0.05,
    )
    assert stake == 0.0


def test_fractional_kelly_scales_by_kelly_fraction():
    full = fractional_kelly_stake(
        p_model=0.6, side="YES", price_paid=0.5,
        bankroll=1000.0, kelly_fraction=1.0, max_bet_pct=1.0,
    )
    quarter = fractional_kelly_stake(
        p_model=0.6, side="YES", price_paid=0.5,
        bankroll=1000.0, kelly_fraction=0.25, max_bet_pct=1.0,
    )
    assert math.isclose(quarter, full * 0.25)


def test_invalid_side_raises():
    import pytest
    with pytest.raises(ValueError):
        fractional_kelly_stake(p_model=0.5, side="MAYBE", price_paid=0.5,
                               bankroll=100.0, kelly_fraction=0.25, max_bet_pct=0.05)
