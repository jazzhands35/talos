"""Tests for DRIP/BLIP free-function behavior."""

from __future__ import annotations

import pytest

from talos.drip import (
    BlipAction,
    DripConfig,
    NoOp,
    evaluate_blip,
)


def test_drip_config_defaults() -> None:
    cfg = DripConfig()

    assert cfg.drip_size == 1
    assert cfg.max_drips == 1
    assert cfg.blip_delta_min == 5.0
    assert cfg.max_ahead_per_side == 1


def test_drip_config_validates_positive_values() -> None:
    with pytest.raises(ValueError):
        DripConfig(drip_size=0)
    with pytest.raises(ValueError):
        DripConfig(max_drips=0)
    with pytest.raises(ValueError):
        DripConfig(blip_delta_min=-0.1)


# ─── Free function evaluate_blip ────────────────────────────────────────────


def test_blip_fires_on_ahead_side_when_eta_delta_exceeds_threshold() -> None:
    cfg = DripConfig(blip_delta_min=5.0)

    action = evaluate_blip(
        cfg,
        eta_a_min=2.0,
        eta_b_min=10.0,
        front_a_id="order-a",
        front_b_id="order-b",
    )

    assert action == BlipAction("A", "order-a")


def test_blip_does_not_fire_within_threshold() -> None:
    cfg = DripConfig(blip_delta_min=5.0)

    action = evaluate_blip(
        cfg,
        eta_a_min=2.0,
        eta_b_min=4.0,
        front_a_id="order-a",
        front_b_id="order-b",
    )

    assert action == NoOp("blip_below_threshold")


def test_blip_treats_behind_none_as_infinite_eta() -> None:
    cfg = DripConfig(blip_delta_min=5.0)

    action = evaluate_blip(
        cfg,
        eta_a_min=2.0,
        eta_b_min=None,
        front_a_id="order-a",
        front_b_id="order-b",
    )

    assert action == BlipAction("A", "order-a")


def test_blip_noops_without_any_eta_signal() -> None:
    cfg = DripConfig(blip_delta_min=5.0)

    action = evaluate_blip(
        cfg,
        eta_a_min=None,
        eta_b_min=None,
        front_a_id="order-a",
        front_b_id="order-b",
    )

    assert action == NoOp("no_eta_signal")


def test_blip_noops_when_front_order_missing() -> None:
    cfg = DripConfig(blip_delta_min=5.0)

    action = evaluate_blip(
        cfg,
        eta_a_min=2.0,
        eta_b_min=10.0,
        front_a_id=None,
        front_b_id="order-b",
    )

    assert action == NoOp("no_front_order")
