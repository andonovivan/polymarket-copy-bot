"""Quote parser sanity checks against the real Polymarket book shape.

Verified empirically (see commit history) that bids are sorted ascending and
asks descending in the live feed; best on each side is `levels[-1]`.
"""

from __future__ import annotations

import math

from polymarket_bot.polymarket.quotes import parse_book


def _sample_book() -> dict:
    return {
        "bids": [
            {"price": "0.01", "size": "1000"},
            {"price": "0.40", "size": "200"},
            {"price": "0.52", "size": "100"},   # best bid
        ],
        "asks": [
            {"price": "0.99", "size": "1000"},
            {"price": "0.60", "size": "300"},
            {"price": "0.53", "size": "50"},    # best ask
        ],
    }


def test_best_bid_is_last_level():
    bid, _, _ = parse_book(_sample_book())
    assert math.isclose(bid, 0.52)


def test_best_ask_is_last_level():
    _, ask, _ = parse_book(_sample_book())
    assert math.isclose(ask, 0.53)


def test_ask_side_depth_sums_price_times_size():
    _, _, ask_usd = parse_book(_sample_book())
    expected = 0.99 * 1000 + 0.60 * 300 + 0.53 * 50
    assert math.isclose(ask_usd, expected, rel_tol=1e-9)


def test_empty_book():
    bid, ask, depth = parse_book({"bids": [], "asks": []})
    assert bid is None and ask is None and depth == 0.0


def test_none_book():
    bid, ask, depth = parse_book(None)
    assert bid is None and ask is None and depth == 0.0


def test_crossed_book_drops_both_sides():
    # Best bid > best ask is a glitch — feed it as no-quote rather than into MTM.
    bid, ask, _ = parse_book({
        "bids": [{"price": "0.55", "size": "10"}],
        "asks": [{"price": "0.50", "size": "10"}],
    })
    assert bid is None and ask is None


def test_out_of_range_prices_dropped():
    bid, ask, depth = parse_book({
        "bids": [{"price": "1.01", "size": "10"}],   # invalid
        "asks": [{"price": "0.0",  "size": "10"}],   # invalid (== 0)
    })
    assert bid is None and ask is None
    assert depth == 0.0   # the 0-priced ask contributes nothing anyway


def test_one_sided_book_returns_one_side():
    bid, ask, _ = parse_book({"bids": [{"price": "0.4", "size": "1"}], "asks": []})
    assert bid == 0.4 and ask is None
