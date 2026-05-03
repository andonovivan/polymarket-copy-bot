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
