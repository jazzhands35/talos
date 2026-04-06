"""Tests for DripApp v2 — WS-first architecture.

Tests cover:
- Hydration reconstructs controller state correctly
- Fill WS drives controller and action execution
- User-order WS confirms lifecycle transitions
- Orderbook WS drives jump detection
- Executor gates actions when not LIVE
- Reconnect forces rehydration
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from drip.config import DripConfig
from drip.controller import CancelOrder, DripController, PlaceOrder
from drip.runtime_state import RuntimeState, SyncState
from talos.models.order import Order
from talos.models.ws import (
    FillMessage,
    OrderBookDelta,
    OrderBookSnapshot,
    UserOrderMessage,
)


def _cfg(
    price_a: int = 35,
    price_b: int = 35,
    max_resting: int = 3,
) -> DripConfig:
    return DripConfig(
        event_ticker="KXTEST-EVENT",
        ticker_a="KXTEST-A",
        ticker_b="KXTEST-B",
        price_a=price_a,
        price_b=price_b,
        max_resting=max_resting,
    )


def _make_order(
    order_id: str = "ord-1",
    ticker: str = "KXTEST-A",
    status: str = "resting",
    side: str = "no",
    action: str = "buy",
    no_price: int = 35,
    fill_count: int = 0,
) -> Order:
    """Build a minimal Order model for test mocking."""
    return Order(
        order_id=order_id,
        ticker=ticker,
        status=status,
        side=side,
        action=action,
        no_price=no_price,
        yes_price=100 - no_price,
        fill_count=fill_count,
        remaining_count=1 - fill_count,
        initial_count=1,
        created_time="2026-01-01T00:00:00Z",
        taker_fees=0,
        maker_fees=0,
        maker_fill_cost=0,
        taker_fill_cost=0,
        queue_position=None,
    )


# ---------------------------------------------------------------------------
# Test ticker-to-side mapping
# ---------------------------------------------------------------------------


class TestTickerToSide:
    """Verify the ticker-to-side mapping helper used by all WS handlers."""

    def test_maps_ticker_a(self) -> None:
        # Import at function level to avoid Textual import issues in tests
        from drip.ui.app import DripApp

        app = DripApp.__new__(DripApp)
        app._config = _cfg()
        assert app._ticker_to_side("KXTEST-A") == "A"

    def test_maps_ticker_b(self) -> None:
        from drip.ui.app import DripApp

        app = DripApp.__new__(DripApp)
        app._config = _cfg()
        assert app._ticker_to_side("KXTEST-B") == "B"

    def test_unknown_ticker_returns_none(self) -> None:
        from drip.ui.app import DripApp

        app = DripApp.__new__(DripApp)
        app._config = _cfg()
        assert app._ticker_to_side("KXTEST-UNKNOWN") is None


# ---------------------------------------------------------------------------
# Test fill handler logic
# ---------------------------------------------------------------------------


class TestFillHandler:
    """Verify fill WS messages drive the controller and enqueue actions."""

    @pytest.fixture()
    def _setup(self) -> Any:
        """Build a partially-initialized DripApp with mocked internals."""
        from drip.ui.app import DripApp

        app = DripApp.__new__(DripApp)
        app._config = _cfg()
        app._controller = DripController(app._config)
        app._runtime = RuntimeState()
        app._runtime.sync_state = SyncState.LIVE
        app._action_queue = asyncio.Queue()
        app._winding_down = False
        app._log = MagicMock()  # suppress UI logging
        return app

    @pytest.mark.asyncio()
    async def test_fill_enqueues_actions(self, _setup: Any) -> None:
        app = _setup
        # Seed an order on side A so controller.on_fill has something to fill
        app._controller.side_a.add_order("ord-A1", 35)
        app._controller.side_a.deploying = False
        app._controller.side_b.deploying = False

        msg = FillMessage(
            trade_id="trade-1",
            order_id="ord-A1",
            market_ticker="KXTEST-A",
            no_price=35,
        )
        await app._on_fill(msg)

        # Should have enqueued actions from controller.on_fill
        assert not app._action_queue.empty()

    @pytest.mark.asyncio()
    async def test_fill_unknown_ticker_ignored(self, _setup: Any) -> None:
        app = _setup
        msg = FillMessage(
            trade_id="trade-1",
            order_id="ord-X",
            market_ticker="KXTEST-UNKNOWN",
            no_price=35,
        )
        await app._on_fill(msg)
        assert app._action_queue.empty()

    @pytest.mark.asyncio()
    async def test_fill_updates_ws_timestamp(self, _setup: Any) -> None:
        app = _setup
        app._controller.side_a.add_order("ord-A1", 35)
        assert app._runtime.last_ws_at is None

        msg = FillMessage(
            trade_id="trade-1",
            order_id="ord-A1",
            market_ticker="KXTEST-A",
            no_price=35,
        )
        await app._on_fill(msg)
        assert app._runtime.last_ws_at is not None


# ---------------------------------------------------------------------------
# Test user_order handler
# ---------------------------------------------------------------------------


class TestUserOrderHandler:
    """Verify user_orders WS is the real lifecycle confirmation path."""

    @pytest.fixture()
    def _app(self) -> Any:
        from drip.ui.app import DripApp

        app = DripApp.__new__(DripApp)
        app._config = _cfg()
        app._controller = DripController(app._config)
        app._runtime = RuntimeState()
        app._runtime.sync_state = SyncState.LIVE
        app._log = MagicMock()
        return app

    @pytest.mark.asyncio()
    async def test_resting_confirms_placement(self, _app: Any) -> None:
        """user_orders status=resting must add order to controller."""
        app = _app
        # Simulate pending placement from executor
        app._runtime.side_a.pending_placements["ord-A1"] = 35

        msg = UserOrderMessage(
            order_id="ord-A1",
            ticker="KXTEST-A",
            status="resting",
        )
        await app._on_user_order(msg)

        # Controller should now have the order
        assert app._controller.side_a.resting_count == 1
        assert app._controller.side_a.resting_orders[0].order_id == "ord-A1"
        # Pending should be cleared
        assert "ord-A1" not in app._runtime.side_a.pending_placements

    @pytest.mark.asyncio()
    async def test_canceled_confirms_removal(self, _app: Any) -> None:
        """user_orders status=canceled must remove order from controller."""
        app = _app
        # Order is in controller and pending cancel
        app._controller.side_a.add_order("ord-A1", 35)
        app._runtime.side_a.pending_cancel_ids.add("ord-A1")

        msg = UserOrderMessage(
            order_id="ord-A1",
            ticker="KXTEST-A",
            status="canceled",
        )
        await app._on_user_order(msg)

        assert app._controller.side_a.resting_count == 0
        assert "ord-A1" not in app._runtime.side_a.pending_cancel_ids

    @pytest.mark.asyncio()
    async def test_executed_clears_pending(self, _app: Any) -> None:
        """user_orders status=executed must clear pending state."""
        app = _app
        app._runtime.side_a.pending_placements["ord-A1"] = 35
        app._runtime.side_a.pending_cancel_ids.add("ord-A1")

        msg = UserOrderMessage(
            order_id="ord-A1",
            ticker="KXTEST-A",
            status="executed",
        )
        await app._on_user_order(msg)

        assert "ord-A1" not in app._runtime.side_a.pending_placements
        assert "ord-A1" not in app._runtime.side_a.pending_cancel_ids

    @pytest.mark.asyncio()
    async def test_ignores_unknown_ticker(self, _app: Any) -> None:
        app = _app
        msg = UserOrderMessage(
            order_id="ord-X",
            ticker="UNKNOWN",
            status="resting",
        )
        await app._on_user_order(msg)


# ---------------------------------------------------------------------------
# Test out-of-order lifecycle scenarios
# ---------------------------------------------------------------------------


class TestOutOfOrderLifecycle:
    """Prove that race conditions between REST, fill WS, and user_orders WS
    are handled safely without controller state corruption.
    """

    @pytest.fixture()
    def _app(self) -> Any:
        from drip.ui.app import DripApp

        app = DripApp.__new__(DripApp)
        app._config = _cfg()
        app._controller = DripController(app._config)
        app._runtime = RuntimeState()
        app._runtime.sync_state = SyncState.LIVE
        app._action_queue = asyncio.Queue()
        app._winding_down = False
        app._rest = AsyncMock()
        app._log = MagicMock()
        return app

    @pytest.mark.asyncio()
    async def test_place_then_delayed_confirm(self, _app: Any) -> None:
        """Place succeeds via REST, controller not mutated until user_orders."""
        app = _app
        app._rest.create_order.return_value = _make_order(order_id="ord-new", ticker="KXTEST-A")

        # Step 1: executor places order
        await app._execute_single(PlaceOrder(side="A", price=35))

        # Controller must NOT have it yet
        assert app._controller.side_a.resting_count == 0
        assert "ord-new" in app._runtime.side_a.pending_placements

        # Step 2: user_orders confirms resting
        await app._on_user_order(
            UserOrderMessage(order_id="ord-new", ticker="KXTEST-A", status="resting")
        )

        # NOW controller has it
        assert app._controller.side_a.resting_count == 1
        assert "ord-new" not in app._runtime.side_a.pending_placements

    @pytest.mark.asyncio()
    async def test_cancel_then_fill_before_confirm(self, _app: Any) -> None:
        """Cancel requested, fill arrives before cancel confirm.

        Real-world scenario: Drip cancels order X, but X gets filled in
        the milliseconds before the cancel takes effect.  The fill WS
        arrives first, then user_orders says "executed" (not "canceled").
        """
        app = _app

        # Setup: order X is resting in controller
        app._controller.side_a.add_order("ord-X", 35)
        app._controller.side_a.deploying = False
        app._controller.side_b.deploying = False
        assert app._controller.side_a.resting_count == 1
        assert app._controller.side_a.filled_count == 0

        # Step 1: executor sends cancel
        app._rest.cancel_order.return_value = _make_order(order_id="ord-X", status="canceled")
        await app._execute_single(CancelOrder(side="A", order_id="ord-X", reason="delta_cancel"))

        # Order still in controller (cancel is pending, not confirmed)
        assert app._controller.side_a.resting_count == 1
        assert "ord-X" in app._runtime.side_a.pending_cancel_ids

        # Step 2: fill arrives BEFORE cancel confirm (fill won the race)
        await app._on_fill(
            FillMessage(
                trade_id="t-1",
                order_id="ord-X",
                market_ticker="KXTEST-A",
                no_price=35,
            )
        )

        # Fill handler called on_fill → record_fill → removed order + incremented count
        assert app._controller.side_a.resting_count == 0
        assert app._controller.side_a.filled_count == 1

        # Step 3: user_orders says "executed" (fill won, cancel lost)
        await app._on_user_order(
            UserOrderMessage(order_id="ord-X", ticker="KXTEST-A", status="executed")
        )

        # Pending cancel cleaned up, fill count unchanged
        assert "ord-X" not in app._runtime.side_a.pending_cancel_ids
        assert app._controller.side_a.filled_count == 1

    @pytest.mark.asyncio()
    async def test_fill_before_resting_confirm(self, _app: Any) -> None:
        """Order placed, fill arrives before user_orders resting confirm.

        Edge case: REST accepts placement, the order immediately fills
        before we even see a resting confirmation.
        """
        app = _app
        app._rest.create_order.return_value = _make_order(order_id="ord-fast", ticker="KXTEST-A")
        app._controller.side_a.deploying = False
        app._controller.side_b.deploying = False

        # Step 1: executor places
        await app._execute_single(PlaceOrder(side="A", price=35))
        assert app._controller.side_a.resting_count == 0  # not confirmed
        assert "ord-fast" in app._runtime.side_a.pending_placements

        # Step 2: fill arrives before resting confirm
        await app._on_fill(
            FillMessage(
                trade_id="t-2",
                order_id="ord-fast",
                market_ticker="KXTEST-A",
                no_price=35,
            )
        )

        # on_fill calls controller.on_fill → record_fill → remove_order returns None
        # (order was never added), but filled_count increments correctly
        assert app._controller.side_a.filled_count == 1

        # Step 3: user_orders says "executed" (not "resting")
        await app._on_user_order(
            UserOrderMessage(order_id="ord-fast", ticker="KXTEST-A", status="executed")
        )

        # Pending placement cleaned up, order never added to resting (correct!)
        assert "ord-fast" not in app._runtime.side_a.pending_placements
        assert app._controller.side_a.resting_count == 0
        assert app._controller.side_a.filled_count == 1


# ---------------------------------------------------------------------------
# Test orderbook jump detection
# ---------------------------------------------------------------------------


class TestOrderbookJumpDetection:
    @pytest.fixture()
    def _setup(self) -> Any:
        from drip.ui.app import DripApp

        app = DripApp.__new__(DripApp)
        app._config = _cfg()
        app._controller = DripController(app._config)
        app._runtime = RuntimeState()
        app._runtime.sync_state = SyncState.LIVE
        app._action_queue = asyncio.Queue()
        app._winding_down = False
        app._log = MagicMock()
        return app

    @pytest.mark.asyncio()
    async def test_snapshot_sets_best_price(self, _setup: Any) -> None:
        app = _setup
        msg = OrderBookSnapshot(
            market_ticker="KXTEST-A",
            market_id="mid-1",
            no=[[35, 10], [40, 5]],
        )
        await app._on_orderbook_snapshot(msg)
        assert app._runtime.side_a.last_best_no == 40

    @pytest.mark.asyncio()
    async def test_delta_triggers_jump(self, _setup: Any) -> None:
        app = _setup
        # First, initialize with a snapshot
        snap = OrderBookSnapshot(
            market_ticker="KXTEST-A",
            market_id="mid-1",
            no=[[35, 10]],
        )
        await app._on_orderbook_snapshot(snap)
        assert app._runtime.side_a.last_best_no == 35

        # Seed an order so controller.on_jump has something to cancel
        app._controller.side_a.add_order("ord-A1", 35)

        # Delta adds a better price level
        delta = OrderBookDelta(
            market_ticker="KXTEST-A",
            market_id="mid-1",
            price=40,
            delta=5,
            side="no",
            ts="123",
        )
        await app._on_orderbook_delta(delta)
        assert app._runtime.side_a.last_best_no == 40
        # on_jump should have enqueued actions
        assert not app._action_queue.empty()

    @pytest.mark.asyncio()
    async def test_yes_side_delta_ignored(self, _setup: Any) -> None:
        app = _setup
        delta = OrderBookDelta(
            market_ticker="KXTEST-A",
            market_id="mid-1",
            price=60,
            delta=5,
            side="yes",
            ts="123",
        )
        await app._on_orderbook_delta(delta)
        # No change to NO-side book
        assert app._runtime.side_a.book.best_price is None

    @pytest.mark.asyncio()
    async def test_no_jump_during_wind_down(self, _setup: Any) -> None:
        app = _setup
        app._winding_down = True
        snap = OrderBookSnapshot(
            market_ticker="KXTEST-A",
            market_id="mid-1",
            no=[[35, 10]],
        )
        await app._on_orderbook_snapshot(snap)
        # Even with a price set, no actions should be enqueued during wind-down
        assert app._action_queue.empty()


# ---------------------------------------------------------------------------
# Test executor gating
# ---------------------------------------------------------------------------


class TestExecutor:
    """Verify the executor creates pending state (not controller mutations)."""

    @pytest.mark.asyncio()
    async def test_place_creates_pending_not_controller(self) -> None:
        """REST success must NOT add to controller — only to pending_placements."""
        from drip.ui.app import DripApp

        app = DripApp.__new__(DripApp)
        app._config = _cfg()
        app._controller = DripController(app._config)
        app._runtime = RuntimeState()
        app._runtime.sync_state = SyncState.LIVE
        app._rest = AsyncMock()
        app._rest.create_order.return_value = _make_order(order_id="new-ord", ticker="KXTEST-A")
        app._log = MagicMock()

        action = PlaceOrder(side="A", price=35)
        await app._execute_single(action)

        app._rest.create_order.assert_called_once_with(
            ticker="KXTEST-A",
            action="buy",
            side="no",
            no_price=35,
            count=1,
        )
        # Controller must NOT have the order yet
        assert app._controller.side_a.resting_count == 0
        # It should be in pending_placements instead
        assert "new-ord" in app._runtime.side_a.pending_placements
        assert app._runtime.side_a.pending_placements["new-ord"] == 35

    @pytest.mark.asyncio()
    async def test_cancel_creates_pending_not_controller(self) -> None:
        """REST cancel success must NOT remove from controller — only mark pending."""
        from drip.ui.app import DripApp

        app = DripApp.__new__(DripApp)
        app._config = _cfg()
        app._controller = DripController(app._config)
        app._runtime = RuntimeState()
        app._runtime.sync_state = SyncState.LIVE
        app._rest = AsyncMock()
        app._rest.cancel_order.return_value = _make_order(order_id="ord-A1", status="canceled")
        app._log = MagicMock()

        # Seed an order in controller
        app._controller.side_a.add_order("ord-A1", 35)

        action = CancelOrder(side="A", order_id="ord-A1", reason="test")
        await app._execute_single(action)

        app._rest.cancel_order.assert_called_once_with("ord-A1")
        # Controller must still have the order
        assert app._controller.side_a.resting_count == 1
        # It should be in pending_cancel_ids
        assert "ord-A1" in app._runtime.side_a.pending_cancel_ids

    @pytest.mark.asyncio()
    async def test_cancel_rest_failure_clears_pending(self) -> None:
        """If REST cancel fails, pending_cancel_ids must be cleared for retry."""
        from drip.ui.app import DripApp

        app = DripApp.__new__(DripApp)
        app._config = _cfg()
        app._controller = DripController(app._config)
        app._runtime = RuntimeState()
        app._rest = AsyncMock()
        app._rest.cancel_order.side_effect = Exception("network error")
        app._log = MagicMock()

        app._controller.side_a.add_order("ord-A1", 35)

        action = CancelOrder(side="A", order_id="ord-A1", reason="test")
        await app._execute_single(action)

        # pending_cancel_ids must be cleared so a retry can happen
        assert "ord-A1" not in app._runtime.side_a.pending_cancel_ids
        # Controller order is still there
        assert app._controller.side_a.resting_count == 1

    @pytest.mark.asyncio()
    async def test_duplicate_cancel_skipped(self) -> None:
        from drip.ui.app import DripApp

        app = DripApp.__new__(DripApp)
        app._config = _cfg()
        app._controller = DripController(app._config)
        app._runtime = RuntimeState()
        app._rest = AsyncMock()
        app._log = MagicMock()

        # Mark cancel as already pending
        app._runtime.side_a.pending_cancel_ids.add("ord-A1")

        action = CancelOrder(side="A", order_id="ord-A1", reason="test")
        await app._execute_single(action)

        # REST should NOT have been called
        app._rest.cancel_order.assert_not_called()


# ---------------------------------------------------------------------------
# Test WS connect / disconnect lifecycle
# ---------------------------------------------------------------------------


class TestWSLifecycle:
    @pytest.mark.asyncio()
    async def test_on_ws_connect_sets_live(self) -> None:
        from drip.ui.app import DripApp

        app = DripApp.__new__(DripApp)
        app._config = _cfg()
        app._controller = DripController(app._config)
        app._runtime = RuntimeState()
        app._rest = AsyncMock()
        app._rest.get_all_orders.return_value = []
        app._log = MagicMock()

        await app._on_ws_connect()
        assert app._runtime.sync_state == SyncState.LIVE

    @pytest.mark.asyncio()
    async def test_on_ws_disconnect_sets_reconnecting(self) -> None:
        from drip.ui.app import DripApp

        app = DripApp.__new__(DripApp)
        app._config = _cfg()
        app._runtime = RuntimeState()
        app._runtime.sync_state = SyncState.LIVE
        app._log = MagicMock()

        await app._on_ws_disconnect()
        assert app._runtime.sync_state == SyncState.RECONNECTING

    @pytest.mark.asyncio()
    async def test_on_ws_connect_hydrates(self) -> None:
        from drip.ui.app import DripApp

        app = DripApp.__new__(DripApp)
        app._config = _cfg()
        app._controller = DripController(app._config)
        app._runtime = RuntimeState()
        app._rest = AsyncMock()
        app._rest.get_all_orders.return_value = [
            _make_order("ord-A1", "KXTEST-A", "resting", fill_count=0),
            _make_order("ord-B1", "KXTEST-B", "resting", fill_count=0),
            _make_order("ord-A2", "KXTEST-A", "filled", fill_count=1, no_price=35),
        ]
        app._log = MagicMock()

        await app._on_ws_connect()

        # Hydration should have reconstructed state
        assert app._controller.side_a.resting_count == 1
        assert app._controller.side_b.resting_count == 1
        assert app._controller.side_a.filled_count == 1
