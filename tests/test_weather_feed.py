"""Bucket-probability + label-parsing logic in data/weather_feed."""

from __future__ import annotations

from polymarket_bot.data.weather_feed import (
    bucket_member_counts,
    bucket_probabilities,
    in_bucket,
)


def test_in_bucket_le_threshold():
    assert in_bucket(53, "53°F or below") is True
    assert in_bucket(40, "53°F or below") is True
    assert in_bucket(54, "53°F or below") is False


def test_in_bucket_ge_threshold():
    assert in_bucket(72, "72°F or higher") is True
    assert in_bucket(80, "72°F or higher") is True
    assert in_bucket(71, "72°F or higher") is False


def test_in_bucket_range_two_numbers():
    assert in_bucket(58, "58-59°F") is True
    assert in_bucket(59, "58-59°F") is True
    assert in_bucket(60, "58-59°F") is False
    assert in_bucket(57, "58-59°F") is False


def test_in_bucket_single_number():
    assert in_bucket(20, "20°C") is True
    assert in_bucket(21, "20°C") is False
    assert in_bucket(19, "20°C") is False


def test_bucket_probabilities_partition():
    members = [55, 56, 56, 58, 60, 60, 60, 62]
    labels = ["54-55°F", "56-57°F", "58-59°F", "60-61°F", "62-63°F"]
    p = bucket_probabilities(members, labels)
    assert p["54-55°F"] == 1 / 8
    assert p["56-57°F"] == 2 / 8
    assert p["58-59°F"] == 1 / 8
    assert p["60-61°F"] == 3 / 8
    assert p["62-63°F"] == 1 / 8
    assert abs(sum(p.values()) - 1.0) < 1e-9


def test_bucket_probabilities_empty_members_returns_zeros():
    p = bucket_probabilities([], ["20°C", "21°C"])
    assert p == {"20°C": 0.0, "21°C": 0.0}


def test_bucket_member_counts_matches_probabilities_times_n():
    members = [55, 56, 56, 58, 60, 60, 60, 62]
    labels = ["54-55°F", "56-57°F", "58-59°F", "60-61°F", "62-63°F"]
    counts = bucket_member_counts(members, labels)
    assert counts == {"54-55°F": 1, "56-57°F": 2, "58-59°F": 1,
                      "60-61°F": 3, "62-63°F": 1}
    # Counts should be probabilities × len(members) — sister functions.
    probs = bucket_probabilities(members, labels)
    for label in labels:
        assert counts[label] == round(probs[label] * len(members))


def test_bucket_member_counts_empty_returns_zeros():
    assert bucket_member_counts([], ["20°C"]) == {"20°C": 0}


# ---------------------------------------------------------------------------
# Rate-limit backoff cap
# ---------------------------------------------------------------------------

def test_rate_limit_backoff_capped_at_one_hour(monkeypatch):
    """Regression: hitting a 'Daily' 429 right at UTC midnight previously
    set _RATE_LIMITED_UNTIL ~24h in the future (the bot saw the response
    before the server-side quota counter had fully reset, so my code
    extended backoff to *next* midnight). Cap prevents that lockout."""
    import urllib.error
    import io
    from polymarket_bot.data import weather_feed as wf

    # Stub out time.time to land on a UTC midnight boundary.
    fixed_now = 1778371200.0   # 2026-05-09 00:00:00 UTC
    monkeypatch.setattr(wf.time, "time", lambda: fixed_now)
    # Don't actually persist to meta in this unit test.
    monkeypatch.setattr(
        "polymarket_bot.persistence.repo.set_meta",
        lambda key, value: None,
    )
    # Reset the in-process flag.
    wf._RATE_LIMITED_UNTIL = 0.0

    # Build a fake HTTPError carrying the daily-limit body.
    err = urllib.error.HTTPError(
        url="x", code=429, msg="Too Many",
        hdrs={"Retry-After": "60"},
        fp=io.BytesIO(b'{"reason":"Daily API request limit exceeded.","error":true}'),
    )

    def raise_429(*args, **kwargs):
        raise err

    monkeypatch.setattr(wf.urllib.request, "urlopen", raise_429)

    out = wf._fetch_json("https://example/x")
    assert out is None
    backoff = wf._RATE_LIMITED_UNTIL - fixed_now
    # Without the cap, this would be ~86400 (next UTC midnight). With the
    # cap, max 3600s.
    assert backoff <= wf.MAX_RATE_LIMIT_BACKOFF_SECONDS
    assert backoff > 0
