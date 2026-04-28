"""Tests for DRIP/BLIP controller behavior."""

from __future__ import annotations

import pytest

from talos.drip import BlipAction, DripConfig, DripController, NoOp, PlaceOrder


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


def test_record_fill_waits_for_matched_pair() -> None:
    ctrl = DripController(DripConfig(drip_size=1))

    actions = ctrl.record_fill("A", 100, trade_id="t1")

    assert ctrl.filled_a_fp100 == 100
    assert ctrl.filled_b_fp100 == 0
    assert ctrl.pairs_filled == 0
    assert all(not isinstance(action, PlaceOrder) for action in actions)


def test_matched_pair_replenishes_both_sides() -> None:
    ctrl = DripController(DripConfig(drip_size=1))
    ctrl.record_fill("A", 100, trade_id="t1")

    actions = ctrl.record_fill("B", 100, trade_id="t2")

    places = [action for action in actions if isinstance(action, PlaceOrder)]
    assert ctrl.pairs_filled == 1
    assert {place.side for place in places} == {"A", "B"}
    assert all(place.drip_size_fp100 == 100 for place in places)


def test_partial_fill_does_not_replenish_until_full_drip_pair() -> None:
    ctrl = DripController(DripConfig(drip_size=10))
    ctrl.record_fill("A", 500, trade_id="t1")

    actions = ctrl.record_fill("B", 500, trade_id="t2")

    assert ctrl.pairs_filled == 0
    assert all(not isinstance(action, PlaceOrder) for action in actions)


def test_duplicate_trade_id_is_ignored() -> None:
    ctrl = DripController(DripConfig())

    ctrl.record_fill("A", 100, trade_id="t1")
    actions = ctrl.record_fill("A", 100, trade_id="t1")

    assert ctrl.filled_a_fp100 == 100
    assert actions == [NoOp("duplicate_trade_id")]


def test_blip_fires_on_ahead_side_when_eta_delta_exceeds_threshold() -> None:
    ctrl = DripController(DripConfig(blip_delta_min=5.0))

    actions = ctrl.evaluate_blip(
        eta_a_min=2.0,
        eta_b_min=10.0,
        front_a_id="order-a",
        front_b_id="order-b",
    )

    assert actions == [BlipAction("A", "order-a")]


def test_blip_does_not_fire_within_threshold() -> None:
    ctrl = DripController(DripConfig(blip_delta_min=5.0))

    actions = ctrl.evaluate_blip(
        eta_a_min=2.0,
        eta_b_min=4.0,
        front_a_id="order-a",
        front_b_id="order-b",
    )

    assert actions == [NoOp("blip_below_threshold")]


def test_blip_treats_behind_none_as_infinite_eta() -> None:
    ctrl = DripController(DripConfig(blip_delta_min=5.0))

    actions = ctrl.evaluate_blip(
        eta_a_min=2.0,
        eta_b_min=None,
        front_a_id="order-a",
        front_b_id="order-b",
    )

    assert actions == [BlipAction("A", "order-a")]


def test_blip_noops_without_any_eta_signal() -> None:
    ctrl = DripController(DripConfig(blip_delta_min=5.0))

    actions = ctrl.evaluate_blip(
        eta_a_min=None,
        eta_b_min=None,
        front_a_id="order-a",
        front_b_id="order-b",
    )

    assert actions == [NoOp("no_eta_signal")]
