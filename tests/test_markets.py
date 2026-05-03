"""Market discovery: slug parsing + boundary computation."""

from __future__ import annotations

from polymarket_bot.polymarket.markets import _parse_event, upcoming_boundaries


def _gamma_event_fixture() -> dict:
    """Trimmed real response from gamma /events?slug=btc-updown-5m-1777801800."""
    return {
        "slug": "btc-updown-5m-1777801800",
        "markets": [{
            "conditionId": "0x44bf5e983d799b76ae7cfe4bf80d148927acdd00ed88abdc9b4496bb54b23cc2",
            # gamma returns this as a JSON-encoded string in practice
            "clobTokenIds": '["114094906045460500736942574108351348839563553510725639800931548167784614745275", "82374142423796066783356232493331642705356836748928727463229706131972715638292"]',
            "endDate": "2026-05-03T09:55:00Z",
        }],
    }


def test_parse_event_extracts_market_correctly():
    m = _parse_event(_gamma_event_fixture())
    assert m is not None
    assert m.start_ts == 1777801800
    assert m.resolution_ts == 1777801800 + 300       # 5 minutes after the start
    assert m.yes_token_id.startswith("11409490")
    assert m.no_token_id.startswith("82374142")
    assert m.market_id.startswith("0x44bf")


def test_parse_event_rejects_non_btc_slug():
    bad = {"slug": "russia-ukraine-ceasefire", "markets": [{"conditionId": "0x", "clobTokenIds": "[]"}]}
    assert _parse_event(bad) is None


def test_upcoming_boundaries_aligned_to_5min():
    bs = upcoming_boundaries(now=1777800123, count=3)
    # 1777800123 / 300 = 5926000.41 → next boundary 5926001*300 = 1777800300
    assert bs == [1777800300, 1777800600, 1777800900]
