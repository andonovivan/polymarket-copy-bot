"""Tests for the Bayesian-fusion helper (no network)."""

from __future__ import annotations

from polymarket_bot.data.observations import fuse_ensemble_with_observation


def test_fusion_passthrough_when_no_observation():
    members = [16, 17, 18]
    assert fuse_ensemble_with_observation(members, None) == members


def test_fusion_passthrough_when_observation_below_all_members():
    # Members [20, 21, 22] all already exceed observed 18°C
    out = fuse_ensemble_with_observation([20, 21, 22], 18.0)
    assert out == [20, 21, 22]


def test_fusion_shifts_members_below_observed():
    # Observed 22°C; members at 18, 20, 22, 24 → shift first two up to 22
    out = fuse_ensemble_with_observation([18, 20, 22, 24], 22.0)
    assert out == [22, 22, 22, 24]


def test_fusion_rounds_observation_to_int():
    # Observed 21.6 rounds to 22 (banker's rounding pushes 0.5→even, but 21.6→22)
    out = fuse_ensemble_with_observation([18, 19, 20, 23], 21.6)
    assert out == [22, 22, 22, 23]


def test_fusion_empty_members():
    assert fuse_ensemble_with_observation([], 22.0) == []


def test_fusion_handles_negative_observation():
    # Below-zero temperatures (e.g. winter Helsinki) — algorithm should still work
    out = fuse_ensemble_with_observation([-5, -3, 0, 2], -2.0)
    assert out == [-2, -2, 0, 2]
