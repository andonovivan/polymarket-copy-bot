"""CLOB error classifier — halt vs skip vs retry routing."""

from __future__ import annotations

from polymarket_bot.execution.error_codes import classify


def test_auth_failure_halts():
    err = classify(401, "Invalid api key")
    assert err.action == "HALT"


def test_l1_header_failure_halts():
    err = classify(401, "Invalid L1 Request headers")
    assert err.action == "HALT"


def test_address_banned_halts_even_on_400():
    err = classify(400, "'0xabc...' address banned")
    assert err.action == "HALT"


def test_closed_only_mode_halts():
    err = classify(400, "'0xabc...' address in closed only mode")
    assert err.action == "HALT"


def test_rate_limit_retries():
    assert classify(429, "Too Many Requests").action == "RETRY"


def test_matching_engine_restart_retries():
    assert classify(425, "Matching engine is restarting").action == "RETRY"


def test_trading_paused_retries():
    assert classify(503, "Trading is currently disabled. Check polymarket.com").action == "RETRY"


def test_internal_server_error_retries():
    assert classify(500, "could not insert order").action == "RETRY"


def test_min_tick_skips():
    err = classify(400, "Price (100) breaks minimum tick size rule: 0.1")
    assert err.action == "SKIP"


def test_min_size_skips():
    err = classify(400, "Size lower than the minimum")
    assert err.action == "SKIP"


def test_insufficient_balance_skips():
    err = classify(400, "not enough balance / allowance")
    assert err.action == "SKIP"


def test_invalid_token_skips():
    err = classify(400, "Invalid token id")
    assert err.action == "SKIP"


def test_unknown_4xx_defaults_to_skip():
    err = classify(404, "market not found")
    assert err.action == "SKIP"


def test_unknown_5xx_defaults_to_retry():
    err = classify(502, "Bad Gateway")
    assert err.action == "RETRY"


def test_unknown_no_status_defaults_to_retry():
    err = classify(None, "some weird transport error")
    assert err.action == "RETRY"
