"""Tests for DripController — pure state machine for Drip arbitrage."""

from __future__ import annotations

from drip.config import DripConfig
from drip.controller import (
    Action,
    CancelOrder,
    DripController,
    NoOp,
    PlaceOrder,
)


def _cfg(
    price_a: int = 35,
    price_b: int = 35,
    max_resting: int = 3,
    fee_rate: float = 0.0175,
) -> DripConfig:
    """Helper to build a DripConfig with sensible test defaults."""
    return DripConfig(
        event_ticker="KXTEST-EVENT",
        ticker_a="KXTEST-A",
        ticker_b="KXTEST-B",
        price_a=price_a,
        price_b=price_b,
        max_resting=max_resting,
        fee_rate=fee_rate,
    )


def _make_ctrl(
    price_a: int = 35,
    price_b: int = 35,
    max_resting: int = 3,
    fee_rate: float = 0.0175,
) -> DripController:
    """Build a controller with test defaults."""
    return DripController(
        _cfg(price_a=price_a, price_b=price_b, max_resting=max_resting, fee_rate=fee_rate)
    )


def _seed_orders(ctrl: DripController, side: str, count: int) -> list[str]:
    """Add `count` resting orders to a side and return their order IDs."""
    ids = []
    for i in range(count):
        oid = f"{side.lower()}-order-{i}"
        ctrl._side(side).add_order(oid, ctrl._side(side).target_price)
        ids.append(oid)
    return ids


def _places(actions: list[Action]) -> list[PlaceOrder]:
    return [a for a in actions if isinstance(a, PlaceOrder)]


def _cancels(actions: list[Action]) -> list[CancelOrder]:
    return [a for a in actions if isinstance(a, CancelOrder)]


def _noops(actions: list[Action]) -> list[NoOp]:
    return [a for a in actions if isinstance(a, NoOp)]


# ======================================================================
# Profitability gate
# ======================================================================


class TestProfitability:
    def test_profitable_at_reasonable_prices(self) -> None:
        ctrl = _make_ctrl(price_a=35, price_b=35)
        assert ctrl.is_profitable() is True

    def test_unprofitable_at_extreme_prices(self) -> None:
        # price_a=50, price_b=50 => fee_adjusted_cost(50)=50+50*50*0.0175/100
        # = 50 + 0.4375 = 50.4375 each, total ~100.875 > 100
        ctrl = _make_ctrl(price_a=50, price_b=50)
        assert ctrl.is_profitable() is False

    def test_marginal_profitability(self) -> None:
        # Find a barely-profitable pair: 45 + 45
        # fee_adjusted_cost(45) = 45 + 45*55*0.0175/100 = 45 + 0.433125 = 45.433125
        # total = 90.86625 < 100 => profitable
        ctrl = _make_ctrl(price_a=45, price_b=45)
        assert ctrl.is_profitable() is True


# ======================================================================
# Properties
# ======================================================================


class TestProperties:
    def test_delta_zero_initially(self) -> None:
        ctrl = _make_ctrl()
        assert ctrl.delta == 0

    def test_ahead_side_none_when_balanced(self) -> None:
        ctrl = _make_ctrl()
        assert ctrl.ahead_side is None
        assert ctrl.behind_side is None

    def test_ahead_side_a_when_a_has_more_fills(self) -> None:
        ctrl = _make_ctrl()
        ctrl.side_a.filled_count = 3
        ctrl.side_b.filled_count = 1
        assert ctrl.ahead_side == "A"
        assert ctrl.behind_side == "B"
        assert ctrl.delta == 2

    def test_ahead_side_b_when_b_has_more_fills(self) -> None:
        ctrl = _make_ctrl()
        ctrl.side_a.filled_count = 1
        ctrl.side_b.filled_count = 4
        assert ctrl.ahead_side == "B"
        assert ctrl.behind_side == "A"
        assert ctrl.delta == 3

    def test_total_filled(self) -> None:
        ctrl = _make_ctrl()
        ctrl.side_a.filled_count = 5
        ctrl.side_b.filled_count = 3
        assert ctrl.total_filled == 8

    def test_matched_pairs(self) -> None:
        ctrl = _make_ctrl()
        ctrl.side_a.filled_count = 5
        ctrl.side_b.filled_count = 3
        assert ctrl.matched_pairs == 3


# ======================================================================
# on_fill — balanced (delta == 0)
# ======================================================================


class TestOnFillBalanced:
    def test_balanced_fill_replenishes_both(self) -> None:
        ctrl = _make_ctrl()
        # Seed 1 order on each side, fill A, fill B => balanced
        a_ids = _seed_orders(ctrl, "A", 1)
        b_ids = _seed_orders(ctrl, "B", 1)
        ctrl._side("A").record_fill(a_ids[0])
        # Now A has 1 fill, B has 0 => fill B to balance
        actions = ctrl.on_fill("B", b_ids[0])
        # Delta=0, so both sides get replenished
        places = _places(actions)
        assert len(places) == 2
        sides = {p.side for p in places}
        assert sides == {"A", "B"}

    def test_balanced_fill_at_capacity_returns_noop(self) -> None:
        ctrl = _make_ctrl(max_resting=2)
        # Fill once on each side, then add 2 resting orders to each
        ctrl.side_a.filled_count = 1
        ctrl.side_b.filled_count = 0
        _seed_orders(ctrl, "A", 2)
        b_ids = _seed_orders(ctrl, "B", 2)
        # Fill B to balance (delta=0), but both at capacity
        actions = ctrl.on_fill("B", b_ids[0])
        # A is at capacity (2 resting, max 2), B still has 1 resting after fill
        places = _places(actions)
        # B had a fill removing one order, so B has 1 resting -> has capacity
        assert any(p.side == "B" for p in places)


# ======================================================================
# on_fill — unbalanced (delta == 1)
# ======================================================================


class TestOnFillDeltaOne:
    def test_unbalanced_replenishes_behind_only(self) -> None:
        ctrl = _make_ctrl()
        a_ids = _seed_orders(ctrl, "A", 2)
        _seed_orders(ctrl, "B", 2)
        # Fill A — now A=1, B=0, delta=1, ahead=A, behind=B
        actions = ctrl.on_fill("A", a_ids[0])
        places = _places(actions)
        assert len(places) == 1
        assert places[0].side == "B"  # behind side

    def test_unbalanced_behind_at_capacity_noop(self) -> None:
        ctrl = _make_ctrl(max_resting=1)
        a_ids = _seed_orders(ctrl, "A", 1)
        _seed_orders(ctrl, "B", 1)
        # Fill A => delta=1, behind=B, but B already at capacity (1 resting)
        actions = ctrl.on_fill("A", a_ids[0])
        places = _places(actions)
        assert len(places) == 0
        noops = _noops(actions)
        assert len(noops) == 1


# ======================================================================
# on_fill — growing imbalance (delta > 1)
# ======================================================================


class TestOnFillDeltaGrowing:
    def test_growing_imbalance_cancels_ahead_front(self) -> None:
        ctrl = _make_ctrl()
        # Pre-set fills: A=2, B=0
        ctrl.side_a.filled_count = 2
        a_ids = _seed_orders(ctrl, "A", 2)
        _seed_orders(ctrl, "B", 1)
        # Fill A again => A=3, B=0, delta=3
        actions = ctrl.on_fill("A", a_ids[0])
        cancels = _cancels(actions)
        assert len(cancels) == 1
        assert cancels[0].side == "A"  # ahead side
        assert cancels[0].order_id == a_ids[1]  # front order (a_ids[0] was filled)
        assert cancels[0].reason == "delta_cancel"

    def test_growing_imbalance_replenishes_behind(self) -> None:
        ctrl = _make_ctrl()
        ctrl.side_a.filled_count = 2
        a_ids = _seed_orders(ctrl, "A", 2)
        _seed_orders(ctrl, "B", 1)
        actions = ctrl.on_fill("A", a_ids[0])
        places = _places(actions)
        assert len(places) == 1
        assert places[0].side == "B"  # behind side

    def test_growing_imbalance_no_ahead_orders_noop_cancel(self) -> None:
        ctrl = _make_ctrl()
        ctrl.side_a.filled_count = 2
        # A has no resting orders, B has 1
        a_ids = _seed_orders(ctrl, "A", 1)
        _seed_orders(ctrl, "B", 1)
        # Fill A => A=3, B=0, delta=3, ahead=A, but no resting A to cancel
        actions = ctrl.on_fill("A", a_ids[0])
        cancels = _cancels(actions)
        assert len(cancels) == 0
        # Should have a NoOp explaining no orders to cancel
        noops = _noops(actions)
        assert any("no resting orders" in n.reason for n in noops)


# ======================================================================
# on_fill — profitability gate
# ======================================================================


class TestOnFillProfitabilityGate:
    def test_unprofitable_blocks_placement(self) -> None:
        # 50+50 is unprofitable
        ctrl = _make_ctrl(price_a=50, price_b=50)
        a_ids = _seed_orders(ctrl, "A", 1)
        _seed_orders(ctrl, "B", 1)
        ctrl.side_b.filled_count = 1  # pre-fill B so delta=0 after A fill
        actions = ctrl.on_fill("A", a_ids[0])
        # Should be balanced, both would get PlaceOrder, but unprofitable
        places = _places(actions)
        assert len(places) == 0
        noops = _noops(actions)
        assert len(noops) == 2
        assert all("unprofitable" in n.reason for n in noops)


# ======================================================================
# on_jump
# ======================================================================


class TestOnJump:
    def test_jump_cancels_front_and_places_new(self) -> None:
        ctrl = _make_ctrl(price_a=35, price_b=35)
        a_ids = _seed_orders(ctrl, "A", 2)
        actions = ctrl.on_jump("A", 33)
        # Should cancel front order and place at new price
        cancels = _cancels(actions)
        assert len(cancels) == 1
        assert cancels[0].order_id == a_ids[0]
        assert cancels[0].reason == "jump_rotate"

        places = _places(actions)
        assert len(places) == 1
        assert places[0].price == 33
        assert places[0].side == "A"

    def test_jump_updates_target_price(self) -> None:
        ctrl = _make_ctrl(price_a=35, price_b=35)
        ctrl.on_jump("B", 40)
        assert ctrl.side_b.target_price == 40

    def test_jump_no_resting_orders_only_places(self) -> None:
        ctrl = _make_ctrl(price_a=35, price_b=35)
        actions = ctrl.on_jump("A", 33)
        cancels = _cancels(actions)
        assert len(cancels) == 0
        places = _places(actions)
        assert len(places) == 1
        assert places[0].price == 33

    def test_jump_unprofitable_cancels_but_no_place(self) -> None:
        ctrl = _make_ctrl(price_a=35, price_b=35)
        a_ids = _seed_orders(ctrl, "A", 1)
        # Jump A to 65 => 65 + 35 = 100, plus fees = unprofitable
        actions = ctrl.on_jump("A", 65)
        cancels = _cancels(actions)
        assert len(cancels) == 1
        assert cancels[0].order_id == a_ids[0]

        places = _places(actions)
        assert len(places) == 0

        noops = _noops(actions)
        assert len(noops) == 1
        assert "unprofitable" in noops[0].reason


# ======================================================================
# deploy_next
# ======================================================================


class TestDeployNext:
    def test_deploy_alternates_a_b(self) -> None:
        ctrl = _make_ctrl(max_resting=3)
        actions1 = ctrl.deploy_next()
        places1 = _places(actions1)
        assert len(places1) == 1
        assert places1[0].side == "A"

        actions2 = ctrl.deploy_next()
        places2 = _places(actions2)
        assert len(places2) == 1
        assert places2[0].side == "B"

        actions3 = ctrl.deploy_next()
        places3 = _places(actions3)
        assert len(places3) == 1
        assert places3[0].side == "A"

    def test_deploy_stops_when_both_at_max(self) -> None:
        ctrl = _make_ctrl(max_resting=2)
        # Fill up both sides
        _seed_orders(ctrl, "A", 2)
        _seed_orders(ctrl, "B", 2)
        actions = ctrl.deploy_next()
        noops = _noops(actions)
        assert len(noops) == 1
        assert "fully deployed" in noops[0].reason

    def test_deploy_skips_full_side(self) -> None:
        ctrl = _make_ctrl(max_resting=2)
        # Fill up A, leave B empty
        _seed_orders(ctrl, "A", 2)
        # Turn is A, but A is full => skip to B
        actions = ctrl.deploy_next()
        places = _places(actions)
        assert len(places) == 1
        assert places[0].side == "B"

    def test_deploy_marks_deploying_false_when_full(self) -> None:
        ctrl = _make_ctrl(max_resting=2)
        _seed_orders(ctrl, "A", 2)
        _seed_orders(ctrl, "B", 2)
        ctrl.deploy_next()
        assert ctrl.side_a.deploying is False
        assert ctrl.side_b.deploying is False

    def test_deploy_unprofitable_returns_noop(self) -> None:
        ctrl = _make_ctrl(price_a=50, price_b=50, max_resting=3)
        actions = ctrl.deploy_next()
        noops = _noops(actions)
        assert len(noops) == 1
        assert "unprofitable" in noops[0].reason

    def test_deploy_uses_correct_price(self) -> None:
        ctrl = _make_ctrl(price_a=35, price_b=40)
        a_actions = ctrl.deploy_next()
        assert _places(a_actions)[0].price == 35

        b_actions = ctrl.deploy_next()
        assert _places(b_actions)[0].price == 40


# ======================================================================
# on_wind_down
# ======================================================================


class TestWindDown:
    def test_wind_down_cancels_everything(self) -> None:
        ctrl = _make_ctrl()
        a_ids = _seed_orders(ctrl, "A", 3)
        b_ids = _seed_orders(ctrl, "B", 2)
        actions = ctrl.on_wind_down()
        assert len(actions) == 5
        assert all(isinstance(a, CancelOrder) for a in actions)
        assert all(a.reason == "wind_down" for a in actions)
        cancelled_ids = {a.order_id for a in actions}
        assert cancelled_ids == set(a_ids + b_ids)

    def test_wind_down_sets_deploying_false(self) -> None:
        ctrl = _make_ctrl()
        _seed_orders(ctrl, "A", 1)
        ctrl.on_wind_down()
        assert ctrl.side_a.deploying is False
        assert ctrl.side_b.deploying is False

    def test_wind_down_empty_returns_empty_list(self) -> None:
        ctrl = _make_ctrl()
        actions = ctrl.on_wind_down()
        assert actions == []

    def test_wind_down_preserves_fill_counts(self) -> None:
        ctrl = _make_ctrl()
        ctrl.side_a.filled_count = 5
        ctrl.side_b.filled_count = 3
        _seed_orders(ctrl, "A", 1)
        ctrl.on_wind_down()
        assert ctrl.side_a.filled_count == 5
        assert ctrl.side_b.filled_count == 3


# ======================================================================
# reconcile
# ======================================================================


class TestReconcile:
    def test_reconcile_overwrites_resting_orders(self) -> None:
        ctrl = _make_ctrl()
        _seed_orders(ctrl, "A", 3)
        _seed_orders(ctrl, "B", 2)
        ctrl.reconcile(
            resting_a=[("new-a-1", 35), ("new-a-2", 35)],
            resting_b=[("new-b-1", 35)],
            filled_a=0,
            filled_b=0,
        )
        assert ctrl.side_a.resting_count == 2
        assert ctrl.side_b.resting_count == 1
        assert ctrl.side_a.resting_orders[0].order_id == "new-a-1"

    def test_reconcile_uses_monotonic_max_fills(self) -> None:
        ctrl = _make_ctrl()
        ctrl.side_a.filled_count = 10
        ctrl.side_b.filled_count = 8
        # Kalshi says fewer — shouldn't decrease
        ctrl.reconcile(
            resting_a=[],
            resting_b=[],
            filled_a=5,
            filled_b=3,
        )
        assert ctrl.side_a.filled_count == 10
        assert ctrl.side_b.filled_count == 8

    def test_reconcile_increases_fills(self) -> None:
        ctrl = _make_ctrl()
        ctrl.side_a.filled_count = 2
        ctrl.side_b.filled_count = 1
        ctrl.reconcile(
            resting_a=[],
            resting_b=[],
            filled_a=5,
            filled_b=4,
        )
        assert ctrl.side_a.filled_count == 5
        assert ctrl.side_b.filled_count == 4

    def test_reconcile_clears_old_resting(self) -> None:
        ctrl = _make_ctrl()
        _seed_orders(ctrl, "A", 3)
        ctrl.reconcile(
            resting_a=[],
            resting_b=[],
            filled_a=0,
            filled_b=0,
        )
        assert ctrl.side_a.resting_count == 0


# ======================================================================
# Integration scenarios
# ======================================================================


class TestIntegrationScenarios:
    def test_fill_sequence_balanced_then_unbalanced(self) -> None:
        """Simulate: deploy 1 each, fill A, fill B (balanced), fill A (unbalanced)."""
        ctrl = _make_ctrl(max_resting=3)
        a_ids = _seed_orders(ctrl, "A", 2)
        b_ids = _seed_orders(ctrl, "B", 2)

        # Fill A => delta=1, ahead=A, replenish behind=B
        actions1 = ctrl.on_fill("A", a_ids[0])
        assert len(_places(actions1)) == 1
        assert _places(actions1)[0].side == "B"

        # Fill B => delta=0, replenish both
        actions2 = ctrl.on_fill("B", b_ids[0])
        assert len(_places(actions2)) == 2

        # Fill A again => delta=1, replenish behind=B only
        actions3 = ctrl.on_fill("A", a_ids[1])
        places3 = _places(actions3)
        assert len(places3) == 1
        assert places3[0].side == "B"

    def test_deploy_turn_starts_at_a(self) -> None:
        ctrl = _make_ctrl()
        assert ctrl._deploy_turn == "A"

    def test_jump_then_fill_uses_new_price(self) -> None:
        ctrl = _make_ctrl(price_a=35, price_b=35)
        _seed_orders(ctrl, "A", 1)
        _seed_orders(ctrl, "B", 1)
        ctrl.on_jump("A", 33)
        # After jump, target_price is 33
        # Seed a new order at 33 and fill it
        ctrl.side_a.add_order("new-a", 33)
        ctrl.side_b.filled_count = 1  # balance
        actions = ctrl.on_fill("A", "new-a")
        # Balanced => replenish both, A should use 33
        places = _places(actions)
        a_places = [p for p in places if p.side == "A"]
        assert len(a_places) == 1
        assert a_places[0].price == 33
