"""LiveBroker — fixed-math fill reconciliation, success/errorMsg routing, cancel name."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import polymarket_bot.execution.live_broker as lb
import polymarket_bot.persistence.schema as schema
from polymarket_bot.execution.live_broker import DECIMALS, LiveBroker, _fm6
from polymarket_bot.persistence.repo import (
    Market,
    fills_for_market,
    open_orders_by_market,
    upsert_market,
)
from polymarket_bot.strategy.base import Bucket, PlaceLimit, WeatherEvent


def _fresh_db(tmp_path: Path) -> None:
    schema._conn = None
    schema.init_db(tmp_path / "test.db")
    lb.reset_halt_for_test()


def _stub_event(market_id: str, yes_token: str) -> WeatherEvent:
    return WeatherEvent(
        slug="ev", title="t", city_key="paris",
        end_ts=1_700_000_000, resolution_ts=1_700_000_000, unit="celsius",
        buckets=[Bucket(
            label="20°C", market_id=market_id, yes_token_id=yes_token,
            no_token_id=f"no-{market_id}",
            yes_bid=None, yes_ask=None, yes_mid=None,
            depth_yes_ask_usd=0.0, model_p=None,
        )],
    )


def _stub_market(market_id: str) -> None:
    upsert_market(Market(
        market_id=market_id, slug="ev::20°C", resolution_ts=1_700_000_000,
        yes_token_id=f"yes-{market_id}", no_token_id=f"no-{market_id}",
    ))


def _action(market_id: str, yes_token: str) -> PlaceLimit:
    return PlaceLimit(
        market_id=market_id, token_id=yes_token,
        token_side="YES", side="BUY",
        price=0.30, size=10.0, client_order_id="c-1",
    )


# ----------------------------------------------------------------------------
# fixed-math conversion (BUG 2)
# ----------------------------------------------------------------------------


def test_fm6_basic():
    assert _fm6("1500000") == 1.5
    assert _fm6("100000000") == 100.0
    assert _fm6(5_000_000) == 5.0


def test_fm6_handles_garbage():
    assert _fm6(None) == 0.0
    assert _fm6("not a number") == 0.0
    assert _fm6("") == 0.0
    assert _fm6("garbage", default=42.0) == 42.0


def test_decimals_constant_is_1e6():
    """If Polymarket ever changes decimals, this test fires."""
    assert DECIMALS == 1_000_000.0


# ----------------------------------------------------------------------------
# reconcile_fills — uses 6-decimal conversion (was the BUG 2 site)
# ----------------------------------------------------------------------------


def test_reconcile_fills_uses_real_share_units(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    _stub_market("m1")

    client = MagicMock()
    # Simulate place_order returning a successful order with id "o1".
    client.place_order.return_value = {
        "success": True, "orderID": "o1", "status": "live", "errorMsg": "",
    }
    # Simulate get_order returning size_matched in 6-decimal fixed-math:
    # "5000000" = 5.0 real shares (not 5,000,000!)
    client.clob.get_order.return_value = {
        "id": "o1", "status": "ORDER_STATUS_LIVE",
        "size_matched": "5000000", "original_size": "10000000",
        "price": "0.3",
    }

    broker = LiveBroker(client)
    order = broker.place_limit(_action("m1", "yes-m1"), strategy="weather_forecast")
    assert order is not None
    assert open_orders_by_market("m1")[0].size == 10.0

    # The reconcile must convert "5000000" → 5.0 shares, not 5,000,000.
    n = broker.reconcile_fills(_stub_event("m1", "yes-m1"))
    assert n == 1
    fills = fills_for_market("m1")
    assert len(fills) == 1
    assert fills[0].size == 5.0
    assert fills[0].size != 5_000_000   # explicit guard against the original bug


# ----------------------------------------------------------------------------
# success / errorMsg handling (post_order response)
# ----------------------------------------------------------------------------


def test_place_limit_rejects_when_success_false(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    _stub_market("m1")
    client = MagicMock()
    client.place_order.return_value = {
        "success": False, "orderID": "", "status": "",
        "errorMsg": "not enough balance / allowance",
    }
    broker = LiveBroker(client)
    assert broker.place_limit(_action("m1", "yes-m1"), strategy="x") is None
    assert open_orders_by_market("m1") == []
    # Known SKIP message → classifier reports SKIP, not HALT.
    assert lb.is_halted() is False


def test_place_limit_unknown_rejection_does_not_set_halt(tmp_path: Path) -> None:
    """Server-side rejection with unrecognised errorMsg should be SKIP, not RETRY/HALT."""
    _fresh_db(tmp_path)
    _stub_market("m1")
    client = MagicMock()
    client.place_order.return_value = {
        "success": False, "orderID": "", "status": "",
        "errorMsg": "some new error message we've never seen",
    }
    broker = LiveBroker(client)
    assert broker.place_limit(_action("m1", "yes-m1"), strategy="x") is None
    # Unknown rejection must NOT halt the bot.
    assert lb.is_halted() is False
    # And must NOT have persisted an order.
    assert open_orders_by_market("m1") == []


def test_place_limit_rejects_when_no_response(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    _stub_market("m1")
    client = MagicMock()
    client.place_order.return_value = None
    broker = LiveBroker(client)
    assert broker.place_limit(_action("m1", "yes-m1"), strategy="x") is None


def test_place_limit_rejects_when_orderid_missing(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    _stub_market("m1")
    client = MagicMock()
    client.place_order.return_value = {
        "success": True, "orderID": "", "status": "live", "errorMsg": "",
    }
    broker = LiveBroker(client)
    assert broker.place_limit(_action("m1", "yes-m1"), strategy="x") is None


# ----------------------------------------------------------------------------
# cancel — must call client.clob.cancel (NOT cancel_order, that was BUG 1)
# ----------------------------------------------------------------------------


def test_cancel_calls_correct_method(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    _stub_market("m1")
    client = MagicMock()
    client.place_order.return_value = {
        "success": True, "orderID": "o1", "status": "live", "errorMsg": "",
    }
    client.clob.cancel.return_value = {"canceled": ["o1"], "not_canceled": {}}

    broker = LiveBroker(client)
    broker.place_limit(_action("m1", "yes-m1"), strategy="x")

    assert broker.cancel("o1") is True
    client.clob.cancel.assert_called_once_with("o1")
    # Make sure we did NOT call the wrong method name from before BUG 1 fix.
    assert not client.clob.cancel_order.called


def test_cancel_failure_keeps_order_open(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    _stub_market("m1")
    client = MagicMock()
    client.place_order.return_value = {
        "success": True, "orderID": "o1", "status": "live", "errorMsg": "",
    }
    client.clob.cancel.return_value = {
        "canceled": [], "not_canceled": {"o1": "Invalid orderID"},
    }
    broker = LiveBroker(client)
    broker.place_limit(_action("m1", "yes-m1"), strategy="x")
    assert broker.cancel("o1") is False
    # Order row still 'open' — we only mark cancelled on success.
    assert len(open_orders_by_market("m1")) == 1


# ----------------------------------------------------------------------------
# halt-on-error sets the global flag
# ----------------------------------------------------------------------------


def test_compliance_failure_sets_halt(tmp_path: Path) -> None:
    _fresh_db(tmp_path)
    _stub_market("m1")
    client = MagicMock()
    client.place_order.return_value = {
        "success": False, "orderID": "", "status": "",
        "errorMsg": "'0xabc...' address banned",
    }
    broker = LiveBroker(client)
    assert broker.place_limit(_action("m1", "yes-m1"), strategy="x") is None
    assert lb.is_halted() is True
    # Subsequent calls should refuse to place.
    client.place_order.return_value = {
        "success": True, "orderID": "o2", "status": "live", "errorMsg": "",
    }
    assert broker.place_limit(_action("m1", "yes-m1"), strategy="x") is None
