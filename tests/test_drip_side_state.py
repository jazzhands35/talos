"""Tests for DripSide per-side order tracking state."""

from __future__ import annotations

from drip.side_state import DripSide, OrderInfo


class TestDripSideAddRemove:
    def test_add_order_appears_in_resting(self) -> None:
        side = DripSide(target_price=35)
        side.add_order("order-1", 35)
        assert side.resting_count == 1
        order = side.resting_orders[0]
        assert order.order_id == "order-1"
        assert order.price == 35

    def test_add_multiple_orders_oldest_first(self) -> None:
        side = DripSide(target_price=35)
        side.add_order("order-1", 35)
        side.add_order("order-2", 35)
        side.add_order("order-3", 35)
        assert side.resting_count == 3
        ids = [o.order_id for o in side.resting_orders]
        assert ids == ["order-1", "order-2", "order-3"]

    def test_remove_existing_order_returns_info(self) -> None:
        side = DripSide(target_price=35)
        side.add_order("order-1", 35)
        result = side.remove_order("order-1")
        assert isinstance(result, OrderInfo)
        assert result.order_id == "order-1"
        assert side.resting_count == 0

    def test_remove_nonexistent_order_returns_none(self) -> None:
        side = DripSide(target_price=35)
        side.add_order("order-1", 35)
        result = side.remove_order("order-999")
        assert result is None
        assert side.resting_count == 1  # original order untouched

    def test_remove_from_middle_preserves_order(self) -> None:
        side = DripSide(target_price=35)
        side.add_order("order-1", 35)
        side.add_order("order-2", 35)
        side.add_order("order-3", 35)
        side.remove_order("order-2")
        ids = [o.order_id for o in side.resting_orders]
        assert ids == ["order-1", "order-3"]

    def test_remove_from_empty_side_returns_none(self) -> None:
        side = DripSide(target_price=35)
        result = side.remove_order("order-999")
        assert result is None


class TestDripSideRecordFill:
    def test_record_fill_increments_count(self) -> None:
        side = DripSide(target_price=35)
        side.add_order("order-1", 35)
        side.record_fill("order-1")
        assert side.filled_count == 1

    def test_record_fill_removes_order_from_resting(self) -> None:
        side = DripSide(target_price=35)
        side.add_order("order-1", 35)
        side.record_fill("order-1")
        assert side.resting_count == 0

    def test_record_multiple_fills_accumulate(self) -> None:
        side = DripSide(target_price=35)
        side.add_order("order-1", 35)
        side.add_order("order-2", 35)
        side.record_fill("order-1")
        side.record_fill("order-2")
        assert side.filled_count == 2
        assert side.resting_count == 0

    def test_record_fill_nonexistent_still_increments(self) -> None:
        """Filling an unknown order ID still increments filled_count (idempotent-friendly)."""
        side = DripSide(target_price=35)
        side.record_fill("ghost-order")
        assert side.filled_count == 1
        assert side.resting_count == 0


class TestDripSideFrontOrder:
    def test_front_order_is_oldest(self) -> None:
        side = DripSide(target_price=35)
        side.add_order("order-1", 35)
        side.add_order("order-2", 35)
        front = side.front_order()
        assert front is not None
        assert front.order_id == "order-1"

    def test_front_order_empty_returns_none(self) -> None:
        side = DripSide(target_price=35)
        assert side.front_order() is None

    def test_front_order_after_removal_updates(self) -> None:
        side = DripSide(target_price=35)
        side.add_order("order-1", 35)
        side.add_order("order-2", 35)
        side.remove_order("order-1")
        front = side.front_order()
        assert front is not None
        assert front.order_id == "order-2"


class TestDripSideCapacity:
    def test_has_capacity_when_empty(self) -> None:
        side = DripSide(target_price=35)
        assert side.has_capacity(max_resting=20) is True

    def test_has_capacity_when_below_max(self) -> None:
        side = DripSide(target_price=35)
        for i in range(5):
            side.add_order(f"order-{i}", 35)
        assert side.has_capacity(max_resting=20) is True

    def test_no_capacity_when_at_max(self) -> None:
        side = DripSide(target_price=35)
        for i in range(20):
            side.add_order(f"order-{i}", 35)
        assert side.has_capacity(max_resting=20) is False

    def test_no_capacity_when_above_max(self) -> None:
        """Handles edge case where resting somehow exceeds max."""
        side = DripSide(target_price=35)
        for i in range(25):
            side.add_order(f"order-{i}", 35)
        assert side.has_capacity(max_resting=20) is False

    def test_capacity_with_max_1(self) -> None:
        side = DripSide(target_price=35)
        assert side.has_capacity(max_resting=1) is True
        side.add_order("order-1", 35)
        assert side.has_capacity(max_resting=1) is False


class TestDripSideInitialState:
    def test_initial_state(self) -> None:
        side = DripSide(target_price=42)
        assert side.resting_orders == []
        assert side.filled_count == 0
        assert side.target_price == 42
        assert side.deploying is True
        assert side.resting_count == 0

    def test_deploying_flag_can_be_cleared(self) -> None:
        side = DripSide(target_price=35)
        assert side.deploying is True
        side.deploying = False
        assert side.deploying is False
