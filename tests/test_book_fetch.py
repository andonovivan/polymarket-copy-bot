"""Tests for fetch_book transient-retry + dead-token semantics.

The CLOB throws bursty transport errors (SSL EOF, server-disconnected, read
timeouts). The fetch path retries those a small number of times before they
count toward the dead-token threshold; non-transient errors (404, error
field in response) escalate immediately.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from polymarket_bot.polymarket import book


@pytest.fixture(autouse=True)
def _reset_dead_state():
    book._dead_until.clear()
    book._fail_count.clear()
    yield
    book._dead_until.clear()
    book._fail_count.clear()


def _client_returning(*responses):
    """Build a fake httpx.Client whose `.get(...)` returns the given responses
    in order; pass an Exception instance to raise it instead."""
    calls = iter(responses)

    def get(*args, **kwargs):
        nxt = next(calls)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = get
    return client


def _ok_response(payload):
    r = MagicMock()
    r.status_code = 200
    r.raise_for_status = lambda: None
    r.json = lambda: payload
    return r


def _http_404():
    r = MagicMock()
    r.status_code = 404
    r.raise_for_status = lambda: None
    r.json = lambda: {}
    return r


def test_fetch_book_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr(book.time, "sleep", lambda *_: None)
    client = _client_returning(
        httpx.ReadTimeout("read timed out"),
        _ok_response({"bids": [], "asks": []}),
    )
    out = book.fetch_book("tok-A", client=client)
    assert out == {"bids": [], "asks": []}
    # Successful retry resets the fail counter — token must NOT be flagged.
    assert "tok-A" not in book._fail_count
    assert "tok-A" not in book._dead_until


def test_fetch_book_retries_ssl_eof_then_succeeds(monkeypatch):
    # OSError covers SSL `UNEXPECTED_EOF_WHILE_READING` from the underlying
    # socket layer; that's the actual error string we observed in production.
    monkeypatch.setattr(book.time, "sleep", lambda *_: None)
    client = _client_returning(
        OSError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF in violation"),
        _ok_response({"bids": [], "asks": []}),
    )
    out = book.fetch_book("tok-B", client=client)
    assert out == {"bids": [], "asks": []}


def test_fetch_book_exhausts_retries_then_fails(monkeypatch):
    monkeypatch.setattr(book.time, "sleep", lambda *_: None)
    client = _client_returning(
        httpx.ReadTimeout("retry 1"),
        httpx.ReadTimeout("retry 2"),
        httpx.ReadTimeout("retry 3"),
    )
    out = book.fetch_book("tok-C", client=client)
    assert out is None
    # All retries exhausted → counts as ONE failure (not three).
    assert book._fail_count["tok-C"] == 1


def test_fetch_book_404_short_circuits_no_retry(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(book.time, "sleep", lambda s: sleep_calls.append(s))
    client = _client_returning(_http_404())
    out = book.fetch_book("tok-D", client=client)
    assert out is None
    # 404 must not retry — that's a real "this token does not exist" signal.
    assert sleep_calls == []
    # 404 also marks dead immediately.
    assert "tok-D" in book._dead_until


def test_fetch_book_dead_short_circuits():
    # Pre-mark the token dead; client should never be touched.
    import time as _time
    book._dead_until["tok-E"] = _time.monotonic() + 60
    client = _client_returning(_ok_response({"bids": [], "asks": []}))
    out = book.fetch_book("tok-E", client=client)
    assert out is None
    client.get.assert_not_called()


def test_fetch_book_three_separate_calls_marks_dead(monkeypatch):
    """A single fetch with all retries failing counts as 1 failure. Three
    such tick-level calls hit _DEAD_BACKOFF_FAILS and mark dead."""
    monkeypatch.setattr(book.time, "sleep", lambda *_: None)
    for _ in range(3):
        client = _client_returning(
            httpx.ReadTimeout("a"),
            httpx.ReadTimeout("b"),
            httpx.ReadTimeout("c"),
        )
        book.fetch_book("tok-F", client=client)
    assert "tok-F" in book._dead_until


def test_fetch_book_success_resets_fail_counter(monkeypatch):
    monkeypatch.setattr(book.time, "sleep", lambda *_: None)
    # First call: all retries timeout → 1 failure.
    client_fail = _client_returning(
        httpx.ReadTimeout("x"), httpx.ReadTimeout("y"), httpx.ReadTimeout("z"),
    )
    book.fetch_book("tok-G", client=client_fail)
    assert book._fail_count["tok-G"] == 1
    # Second call: success on first attempt → counter cleared.
    client_ok = _client_returning(_ok_response({"bids": [], "asks": []}))
    book.fetch_book("tok-G", client=client_ok)
    assert "tok-G" not in book._fail_count
