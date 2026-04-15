"""Tests for rebalance detection and execution (extracted from test_engine.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from talos.bid_adjuster import BidAdjuster
from talos.models.order import Order
from talos.models.portfolio import Position
from talos.models.proposal import ProposedRebalance
from talos.models.strategy import ArbPair, Opportunity
from talos.orderbook import OrderBookManager
from talos.position_ledger import PositionLedger, Side
from talos.rebalance import (
    compute_overcommit_reduction,
    compute_rebalance_proposal,
    compute_topup_needs,
    execute_rebalance,
)
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner

# ── Helpers ──────────────────────────────────────────────────────────


def _make_pair() -> ArbPair:
    return ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")


def _books_with_data(no_a: int = 45, no_b: int = 48) -> OrderBookManager:
    """OrderBookManager with book data so markets are considered 'open'."""
    from talos.models.ws import OrderBookSnapshot

    books = OrderBookManager()
    books.apply_snapshot(
        "TK-A",
        OrderBookSnapshot(market_ticker="TK-A", market_id="m1", yes=[], no=[[no_a, 100]]),
    )
    books.apply_snapshot(
        "TK-B",
        OrderBookSnapshot(market_ticker="TK-B", market_id="m2", yes=[], no=[[no_b, 100]]),
    )
    return books


def _make_snapshot(no_a: int = 45, no_b: int = 48) -> Opportunity:
    return Opportunity(
        event_ticker="EVT-1",
        ticker_a="TK-A",
        ticker_b="TK-B",
        no_a=no_a,
        no_b=no_b,
        qty_a=100,
        qty_b=100,
        raw_edge=100 - no_a - no_b,
        fee_edge=5.0,
        tradeable_qty=100,
        timestamp="2026-03-13T00:00:00Z",
    )


def _make_order(
    ticker: str,
    *,
    order_id: str = "ord-1",
    fill_count: int = 0,
    remaining_count: int = 0,
    no_price: int = 45,
    status: str = "resting",
) -> Order:
    return Order(
        order_id=order_id,
        ticker=ticker,
        action="buy",
        side="no",
        no_price=no_price,
        initial_count=fill_count + remaining_count,
        remaining_count=remaining_count,
        fill_count=fill_count,
        status=status,
    )


def _make_exec_context(
    *,
    no_a: int = 45,
    no_b: int = 48,
) -> tuple[ArbitrageScanner, BidAdjuster, AsyncMock]:
    """Build scanner + adjuster + mock REST for execution tests."""
    books = OrderBookManager()
    scanner = ArbitrageScanner(books)
    scanner.add_pair("EVT-1", "TK-A", "TK-B")
    pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
    adjuster = BidAdjuster(books, [pair], unit_size=10)
    rest = AsyncMock(spec=KalshiRESTClient)
    rest.create_order_group = AsyncMock(return_value="grp-test")
    return scanner, adjuster, rest


# ── Pure detection tests ─────────────────────────────────────────────


class TestComputeRebalanceProposal:
    def test_no_imbalance_no_proposal(self):
        """Balanced positions produce no rebalance proposal."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, "ord-a", 10, 45)
        ledger.record_resting(Side.B, "ord-b", 10, 47)

        result = compute_rebalance_proposal("EVT-1", ledger, pair, None, "Test", OrderBookManager())
        assert result is None

    def test_imbalance_within_unit_no_proposal(self):
        """Delta < unit_size is tolerated (normal fill asymmetry)."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, 10, 45)
        ledger.record_fill(Side.B, 10, 47)

        # delta=0 → no proposal (perfectly balanced)
        result = compute_rebalance_proposal("EVT-1", ledger, pair, None, "Test", OrderBookManager())
        assert result is None

    def test_any_committed_delta_proposes(self):
        """Any non-zero committed delta triggers rebalance (unhedged exposure)."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, 10, 45)
        ledger.record_resting(Side.A, "ord-a", 1, 45)
        ledger.record_fill(Side.B, 10, 47)

        # delta=1 → proposal (even 1 unhedged contract matters)
        result = compute_rebalance_proposal("EVT-1", ledger, pair, None, "Test", OrderBookManager())
        assert result is not None
        assert result.kind == "rebalance"

    def test_imbalance_at_exactly_unit_size_proposes(self):
        """Delta == unit_size is flagged (a full unit of imbalance)."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, 10, 45)
        ledger.record_resting(Side.A, "ord-a", 10, 45)
        ledger.record_fill(Side.B, 10, 47)

        result = compute_rebalance_proposal("EVT-1", ledger, pair, None, "Test", OrderBookManager())
        assert result is not None
        assert result.kind == "rebalance"

    def test_imbalance_exceeds_unit_proposes_rebalance(self):
        """Delta > unit_size produces a rebalance proposal."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, 50, 45)
        ledger.record_resting(Side.A, "ord-a", 60, 45)
        ledger.record_fill(Side.B, 50, 47)
        ledger.record_resting(Side.B, "ord-b", 10, 47)

        result = compute_rebalance_proposal("EVT-1", ledger, pair, None, "Test", OrderBookManager())
        assert result is not None
        assert result.kind == "rebalance"
        assert result.key.side == "A"  # over-extended side
        assert "Cancel" in result.detail  # target=over_filled always cancels all resting
        assert "110" in result.detail  # committed_A
        assert "60" in result.detail  # committed_B

    def test_fill_imbalance_no_snapshot_manual_fallback(self):
        """With open markets but no scanner snapshot, falls back to manual."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_fill(Side.B, 10, 47)

        result = compute_rebalance_proposal("EVT-1", ledger, pair, None, "Test", _books_with_data())
        assert result is not None
        assert result.kind == "rebalance"
        assert result.key.side == "A"
        assert result.rebalance is None  # no executable step

    def test_fill_imbalance_with_snapshot_proposes_catchup(self):
        """With scanner snapshot, fill imbalance proposes catch-up bid."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_fill(Side.B, 20, 48)
        snapshot = _make_snapshot(no_a=45, no_b=48)

        result = compute_rebalance_proposal(
            "EVT-1", ledger, pair, snapshot, "Test", _books_with_data()
        )
        assert result is not None
        assert result.rebalance is not None
        # No step 1 (no resting to cancel)
        assert result.rebalance.order_id is None
        # Step 2: catch-up 10 on B at current price
        assert result.rebalance.catchup_ticker == "TK-B"
        assert result.rebalance.catchup_qty == 10
        assert result.rebalance.catchup_price == 48

    def test_two_step_cancel_then_catchup(self):
        """30f+10r / 20f -> cancel A resting, then catch up on B."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_resting(Side.A, "ord-a", 10, 45)
        ledger.record_fill(Side.B, 20, 48)
        snapshot = _make_snapshot(no_a=45, no_b=48)

        result = compute_rebalance_proposal(
            "EVT-1", ledger, pair, snapshot, "Test", OrderBookManager()
        )
        assert result is not None
        assert result.rebalance is not None
        # Step 1: cancel all resting on A
        assert result.rebalance.order_id == "ord-a"
        assert result.rebalance.current_resting == 10
        assert result.rebalance.target_resting == 0
        # Step 2: catch-up 10 on B
        assert result.rebalance.catchup_ticker == "TK-B"
        assert result.rebalance.catchup_qty == 10
        assert result.rebalance.catchup_price == 48
        assert "Cancel" in result.detail
        assert "Place 10" in result.detail

    def test_reduce_only_when_under_has_resting(self):
        """If under-side already has resting, only reduce over-side."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_resting(Side.A, "ord-a", 10, 45)
        ledger.record_fill(Side.B, 20, 48)
        ledger.record_resting(Side.B, "ord-b", 10, 48)
        snapshot = _make_snapshot(no_a=45, no_b=48)

        result = compute_rebalance_proposal(
            "EVT-1", ledger, pair, snapshot, "Test", OrderBookManager()
        )
        assert result is not None
        assert result.rebalance is not None
        assert result.rebalance.order_id == "ord-a"
        assert result.rebalance.target_resting == 0
        assert result.rebalance.catchup_qty == 0

    def test_reduce_over_side_when_under_has_more_fills(self):
        """When under-side has more fills than over-filled, cancel all over resting."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_resting(Side.A, "ord-a", 20, 45)
        ledger.record_fill(Side.B, 40, 48)

        result = compute_rebalance_proposal("EVT-1", ledger, pair, None, "Test", _books_with_data())
        assert result is not None
        assert result.rebalance is not None
        assert result.rebalance.target_resting == 0
        assert result.rebalance.current_resting == 20
        assert result.rebalance.catchup_qty == 0

    def test_catchup_bridges_full_gap(self):
        """Catch-up quantity bridges the full gap in one step (no unit cap)."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, 50, 45)
        ledger.record_fill(Side.B, 20, 48)
        snapshot = _make_snapshot(no_a=45, no_b=48)

        result = compute_rebalance_proposal(
            "EVT-1", ledger, pair, snapshot, "Test", _books_with_data()
        )
        assert result is not None
        assert result.rebalance is not None
        assert result.rebalance.catchup_qty == 30

    def test_catchup_falls_back_to_profitable_price(self):
        """When snapshot price is unprofitable, catch-up uses max profitable price.

        Regression: previously catch-up was omitted entirely, leaving positions
        stuck in "Waiting" indefinitely. Now falls back to a resting bid at the
        highest price that IS profitable against historical fills.
        """
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        # Over-side (A) has fills at expensive price 55 + resting to cancel
        ledger.record_fill(Side.A, 20, 55)
        ledger.record_resting(Side.A, "ord-a", 10, 55)
        # Under-side (B) has fills at 48
        ledger.record_fill(Side.B, 15, 48)
        # Snapshot offers B at 48 — arb: fee_adj(48) + fee_adj(55) ≈ 103.87 >= 100
        snapshot = _make_snapshot(no_a=55, no_b=48)

        result = compute_rebalance_proposal(
            "EVT-1", ledger, pair, snapshot, "Test", _books_with_data(no_a=55, no_b=48)
        )
        assert result is not None
        assert result.rebalance is not None
        # Cancel step is present (reduce over-side resting)
        assert result.rebalance.current_resting == 10
        assert result.rebalance.target_resting == 0
        # Catch-up falls back to max profitable price (44¢ < snapshot 48¢)
        assert result.rebalance.catchup_qty == 5
        assert result.rebalance.catchup_ticker == "TK-B"
        assert result.rebalance.catchup_price == 44  # max profitable vs 55¢ fills

    def test_catchup_skipped_when_no_profitable_price_exists(self):
        """Catch-up is omitted when even the max profitable price is 0."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        # Over-side fills at extreme price — no profitable catch-up possible
        ledger.record_fill(Side.A, 20, 99)
        ledger.record_fill(Side.B, 10, 48)
        snapshot = _make_snapshot(no_a=99, no_b=48)

        result = compute_rebalance_proposal(
            "EVT-1", ledger, pair, snapshot, "Test", _books_with_data(no_a=99, no_b=48)
        )
        assert result is not None
        # No executable step — can't profitably catch up
        assert result.rebalance is None or result.rebalance.catchup_qty == 0

    def test_catchup_full_gap_not_capped(self):
        """Catch-up quantity bridges full gap, not capped at unit_size."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_fill(Side.B, 15, 48)
        snapshot = _make_snapshot(no_a=45, no_b=48)

        result = compute_rebalance_proposal(
            "EVT-1", ledger, pair, snapshot, "Test", _books_with_data()
        )
        assert result is not None
        assert result.rebalance is not None
        assert result.rebalance.catchup_qty == 25

    def test_target_is_over_filled_not_max(self):
        """Target = over_filled. Over-side resting is always cancelled."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_resting(Side.A, "ord-a", 20, 45)  # 50 committed
        ledger.record_fill(Side.B, 10, 48)
        snapshot = _make_snapshot(no_a=45, no_b=48)

        result = compute_rebalance_proposal(
            "EVT-1", ledger, pair, snapshot, "Test", OrderBookManager()
        )
        assert result is not None
        assert result.rebalance is not None
        assert result.rebalance.target_resting == 0
        assert result.rebalance.current_resting == 20
        assert result.rebalance.catchup_qty == 20

    def test_under_resting_reduces_effective_gap(self):
        """Existing resting on under-side reduces effective catch-up needed."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_fill(Side.B, 15, 48)
        ledger.record_resting(Side.B, "ord-b", 10, 48)  # 25 committed
        snapshot = _make_snapshot(no_a=45, no_b=48)

        result = compute_rebalance_proposal(
            "EVT-1", ledger, pair, snapshot, "Test", _books_with_data()
        )
        assert result is not None
        assert result.rebalance is not None
        # gap = target(40) - under_committed(25) = 15
        # effective_gap = 15 - under_resting(10) = 5
        assert result.rebalance.catchup_qty == 5

    def test_empty_positions_no_proposal(self):
        """Zero committed on both sides produces no proposal."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)

        result = compute_rebalance_proposal("EVT-1", ledger, pair, None, "Test", OrderBookManager())
        assert result is None

    def test_settled_balanced_no_proposal(self):
        """Equal fills with no resting produces no proposal."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, 20, 45)
        ledger.record_fill(Side.B, 20, 48)

        result = compute_rebalance_proposal("EVT-1", ledger, pair, None, "Test", OrderBookManager())
        assert result is None

    def test_settled_imbalanced_no_books_no_proposal(self):
        """Imbalanced fills + no resting + no orderbook data -> settled, skip."""
        pair = _make_pair()
        books = OrderBookManager()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_fill(Side.B, 10, 48)

        # No books registered -> best_ask returns None -> treated as settled
        result = compute_rebalance_proposal("EVT-1", ledger, pair, None, "Test", books)
        assert result is None


# ── Async execution tests ───────────────────────────────────────────


class TestExecuteRebalance:
    @pytest.mark.asyncio
    async def test_cancel_and_catchup(self):
        """Executing two-step rebalance cancels first, then places catch-up."""
        scanner, adjuster, rest = _make_exec_context()
        ledger = adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_fill(Side.B, 20, 48)

        rest.cancel_order = AsyncMock()
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))
        # Fresh sync maintains the imbalance
        rest.get_all_orders = AsyncMock(
            return_value=[
                _make_order(
                    "TK-A", order_id="ord-a-done", fill_count=30, no_price=45, status="canceled"
                ),
                _make_order(
                    "TK-B", order_id="ord-b-done", fill_count=20, no_price=48, status="canceled"
                ),
            ]
        )

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            order_id="ord-a",
            ticker="TK-A",
            current_resting=10,
            target_resting=0,
            catchup_ticker="TK-B",
            catchup_price=48,
            catchup_qty=10,
        )
        notifications: list[tuple[str, str]] = []
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: notifications.append((msg, sev)),
        )

        rest.cancel_order.assert_called_once_with("ord-a")
        rest.create_order.assert_called_once_with(
            ticker="TK-B",
            action="buy",
            side="no",
            yes_price=None,
            no_price=48,
            count=10,
            order_group_id="grp-test",
        )

    @pytest.mark.asyncio
    async def test_decrease_reduces_resting(self):
        """Partial reduce uses decrease_order (preserves queue position)."""
        scanner, adjuster, rest = _make_exec_context()

        rest.get_order = AsyncMock(
            return_value=_make_order(
                "TK-A", order_id="ord-a", fill_count=30, remaining_count=20, no_price=45
            )
        )
        rest.decrease_order = AsyncMock(
            return_value=_make_order("TK-A", order_id="ord-a", remaining_count=10)
        )

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            order_id="ord-a",
            ticker="TK-A",
            current_resting=20,
            target_resting=10,
        )
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: None,
        )

        rest.get_order.assert_called_once_with("ord-a")
        rest.decrease_order.assert_called_once_with("ord-a", reduce_to=10)

    @pytest.mark.asyncio
    async def test_already_at_target(self):
        """If remaining_count already at or below target, skip the decrease."""
        scanner, adjuster, rest = _make_exec_context()

        rest.get_order = AsyncMock(
            return_value=_make_order(
                "TK-A", order_id="ord-a", fill_count=25, remaining_count=5, no_price=45
            )
        )
        rest.decrease_order = AsyncMock()

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            order_id="ord-a",
            ticker="TK-A",
            current_resting=30,
            target_resting=5,
        )
        notifications: list[tuple[str, str]] = []
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: notifications.append((msg, sev)),
        )

        rest.decrease_order.assert_not_called()
        assert any("already at target" in msg.lower() for msg, _ in notifications)

    @pytest.mark.asyncio
    async def test_decrease_uses_target_resting(self):
        """decrease_order uses target_resting directly."""
        scanner, adjuster, rest = _make_exec_context()

        rest.get_order = AsyncMock(
            return_value=_make_order(
                "TK-A", order_id="ord-a-new", fill_count=0, remaining_count=20, no_price=45
            )
        )
        rest.decrease_order = AsyncMock(
            return_value=_make_order("TK-A", order_id="ord-a-new", remaining_count=10)
        )

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            order_id="ord-a-new",
            ticker="TK-A",
            current_resting=20,
            target_resting=10,
        )
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: None,
        )

        rest.decrease_order.assert_called_once_with("ord-a-new", reduce_to=10)

    @pytest.mark.asyncio
    async def test_skips_decrease_when_already_at_target(self):
        """If fresh order remaining <= target, skip the decrease call."""
        scanner, adjuster, rest = _make_exec_context()

        rest.get_order = AsyncMock(
            return_value=_make_order(
                "TK-A", order_id="ord-a", fill_count=45, remaining_count=5, no_price=45
            )
        )
        rest.amend_order = AsyncMock()

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            order_id="ord-a",
            ticker="TK-A",
            current_resting=20,
            target_resting=10,
        )
        notifications: list[tuple[str, str]] = []
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: notifications.append((msg, sev)),
        )

        rest.amend_order.assert_not_called()
        assert any("already at target" in msg.lower() for msg, _ in notifications)

    @pytest.mark.asyncio
    async def test_catchup_blocked_by_safety(self):
        """Catch-up with catchup=True bypasses P16 but still places when P18 passes."""
        scanner, adjuster, rest = _make_exec_context()
        ledger = adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_fill(Side.B, 20, 48)

        # Fresh sync: A=40f, B=20f+5r=25 committed
        # fresh_catchup_qty = max(0, 40-25) = 15
        # catchup=True bypasses P16; P18: 45+48=93 < 100 → passes → order placed
        rest.get_all_orders = AsyncMock(
            return_value=[
                _make_order(
                    "TK-A", order_id="ord-a-done", fill_count=40, no_price=45, status="canceled"
                ),
                _make_order(
                    "TK-B", order_id="ord-b-done", fill_count=20, no_price=48, status="canceled"
                ),
                _make_order(
                    "TK-B", order_id="ord-b-late", remaining_count=5, no_price=48, status="resting"
                ),
            ]
        )
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            catchup_ticker="TK-B",
            catchup_price=48,
            catchup_qty=10,
        )
        notifications: list[tuple[str, str]] = []
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: notifications.append((msg, sev)),
        )

        # catchup=True bypasses P16, P18 passes → order placed with recalculated qty=15
        rest.create_order.assert_called_once()
        assert rest.create_order.call_args.kwargs["count"] == 15

    @pytest.mark.asyncio
    async def test_fresh_sync_uses_get_all_orders(self):
        """Fresh sync before catch-up uses get_all_orders (not truncated get_orders)."""
        scanner, adjuster, rest = _make_exec_context()
        ledger = adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_fill(Side.B, 20, 48)

        rest.get_all_orders = AsyncMock(
            return_value=[
                _make_order("TK-A", order_id="oa", fill_count=30, no_price=45, status="canceled"),
                _make_order("TK-B", order_id="ob", fill_count=20, no_price=48, status="canceled"),
            ]
        )
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            catchup_ticker="TK-B",
            catchup_price=48,
            catchup_qty=10,
        )
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: None,
        )

        rest.get_all_orders.assert_called_once()

    @pytest.mark.asyncio
    async def test_catchup_qty_recalculated_from_fresh_sync(self):
        """Catch-up qty is recalculated from fresh ledger, not stale proposal."""
        scanner, adjuster, rest = _make_exec_context()

        # Proposal says catchup_qty=25 (stale data: A=40, B=15)
        # But fresh sync shows B caught up to 30 → real gap is 10
        rest.get_all_orders = AsyncMock(
            return_value=[
                _make_order("TK-A", order_id="oa", fill_count=40, no_price=45, status="canceled"),
                _make_order("TK-B", order_id="ob", fill_count=30, no_price=48, status="canceled"),
            ]
        )
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            catchup_ticker="TK-B",
            catchup_price=48,
            catchup_qty=25,  # stale
        )
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: None,
        )

        # Should place 10 (recalculated), not 25 (stale)
        rest.create_order.assert_called_once()
        assert rest.create_order.call_args.kwargs["count"] == 10

    @pytest.mark.asyncio
    async def test_catchup_skipped_when_recalculated_qty_zero(self):
        """If fresh sync closes the gap entirely, skip catch-up."""
        scanner, adjuster, rest = _make_exec_context()

        rest.get_all_orders = AsyncMock(
            return_value=[
                _make_order("TK-A", order_id="oa", fill_count=30, no_price=45, status="canceled"),
                _make_order("TK-B", order_id="ob", fill_count=30, no_price=48, status="canceled"),
            ]
        )
        rest.create_order = AsyncMock()

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            catchup_ticker="TK-B",
            catchup_price=48,
            catchup_qty=10,
        )
        notifications: list[tuple[str, str]] = []
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: notifications.append((msg, sev)),
        )

        rest.create_order.assert_not_called()
        assert any(
            "skipped" in msg.lower() or "balanced" in msg.lower() for msg, _ in notifications
        )

    @pytest.mark.asyncio
    async def test_duplicate_orders_cancelled_when_tracked_already_at_target(self):
        """Double-bid: 2 separate orders (each qty 1) with unit_size=1.

        The tracked order is already at target_resting=1, but a second
        duplicate order causes persistent overcommit.  The sweep should
        cancel the duplicate.
        """
        scanner, adjuster, rest = _make_exec_context()

        # Tracked order: already at qty 1 (= target)
        rest.get_order = AsyncMock(
            return_value=_make_order(
                "TK-A", order_id="ord-a", remaining_count=1, no_price=45,
            )
        )
        rest.decrease_order = AsyncMock()
        # Sweep returns both the tracked order AND the duplicate
        rest.get_all_orders = AsyncMock(
            return_value=[
                _make_order(
                    "TK-A", order_id="ord-a", remaining_count=1, no_price=45,
                ),
                _make_order(
                    "TK-A", order_id="ord-a-dup", remaining_count=1, no_price=45,
                ),
            ]
        )
        rest.cancel_order = AsyncMock()

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            order_id="ord-a",
            ticker="TK-A",
            current_resting=2,
            target_resting=1,
        )
        notifications: list[tuple[str, str]] = []
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: notifications.append((msg, sev)),
        )

        # Tracked order should NOT be decreased (already at 1)
        rest.decrease_order.assert_not_called()
        # Duplicate order SHOULD be cancelled
        rest.cancel_order.assert_called_once_with("ord-a-dup")
        assert any("duplicate" in msg.lower() for msg, _ in notifications)

    @pytest.mark.asyncio
    async def test_no_duplicate_sweep_when_no_extras(self):
        """When tracked order is the only one, sweep finds nothing to cancel."""
        scanner, adjuster, rest = _make_exec_context()

        rest.get_order = AsyncMock(
            return_value=_make_order(
                "TK-A", order_id="ord-a", remaining_count=5, no_price=45,
            )
        )
        rest.decrease_order = AsyncMock(
            return_value=_make_order("TK-A", order_id="ord-a", remaining_count=3)
        )
        # Only the tracked order exists
        rest.get_all_orders = AsyncMock(
            return_value=[
                _make_order(
                    "TK-A", order_id="ord-a", remaining_count=3, no_price=45,
                ),
            ]
        )
        rest.cancel_order = AsyncMock()

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            order_id="ord-a",
            ticker="TK-A",
            current_resting=5,
            target_resting=3,
        )
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: None,
        )

        rest.decrease_order.assert_called_once_with("ord-a", reduce_to=3)
        rest.cancel_order.assert_not_called()


# ── Fresh sync before catch-up ───────────────────────────────────────


class TestFreshSyncBeforeCatchup:
    @pytest.mark.asyncio
    async def test_catchup_skipped_when_fresh_sync_resolves_imbalance(self):
        """If fresh sync shows gap is closed (over_filled <= under_committed), skip."""
        scanner, adjuster, rest = _make_exec_context()

        # Fresh sync reveals B filled up to 25 between polls + 5 resting = 30 committed
        # fresh_over_filled=30, fresh_under_committed=30 → fresh_catchup_qty=0 → skip
        rest.get_all_orders = AsyncMock(
            return_value=[
                _make_order("TK-A", order_id="oa", fill_count=30, no_price=45, status="canceled"),
                _make_order("TK-B", order_id="ob", fill_count=25, no_price=48, status="canceled"),
                _make_order(
                    "TK-B",
                    order_id="ob-r",
                    fill_count=0,
                    remaining_count=5,
                    no_price=48,
                    status="resting",
                ),
            ]
        )
        rest.create_order = AsyncMock()

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            catchup_ticker="TK-B",
            catchup_price=48,
            catchup_qty=10,
        )
        notifications: list[tuple[str, str]] = []
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: notifications.append((msg, sev)),
        )

        rest.create_order.assert_not_called()
        assert any("skipped" in msg.lower() for msg, _ in notifications)

    @pytest.mark.asyncio
    async def test_catchup_blocked_when_fresh_sync_fails(self):
        """If fresh sync raises, catch-up is blocked."""
        scanner, adjuster, rest = _make_exec_context()

        rest.get_all_orders = AsyncMock(side_effect=RuntimeError("API timeout"))
        rest.create_order = AsyncMock()

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            catchup_ticker="TK-B",
            catchup_price=48,
            catchup_qty=10,
        )
        notifications: list[tuple[str, str]] = []
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: notifications.append((msg, sev)),
        )

        rest.create_order.assert_not_called()
        assert any("fresh sync failed" in msg.lower() for msg, _ in notifications)

    @pytest.mark.asyncio
    async def test_catchup_blocked_when_pair_not_found(self):
        """If pair is missing from scanner, catch-up is blocked."""
        scanner, adjuster, rest = _make_exec_context()

        # Remove the pair from scanner so _find_pair returns None
        scanner._pairs.clear()

        rest.create_order = AsyncMock()

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            catchup_ticker="TK-B",
            catchup_price=48,
            catchup_qty=10,
        )
        notifications: list[tuple[str, str]] = []
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: notifications.append((msg, sev)),
        )

        rest.create_order.assert_not_called()
        assert any("pair not found" in msg.lower() for msg, _ in notifications)

    @pytest.mark.asyncio
    async def test_fresh_sync_confirms_imbalance_catchup_proceeds(self):
        """When fresh sync confirms imbalance still exists, catch-up proceeds."""
        scanner, adjuster, rest = _make_exec_context()

        # Fresh sync confirms same state
        rest.get_all_orders = AsyncMock(
            return_value=[
                _make_order("TK-A", order_id="oa", fill_count=30, no_price=45, status="canceled"),
                _make_order("TK-B", order_id="ob", fill_count=20, no_price=48, status="canceled"),
            ]
        )
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            catchup_ticker="TK-B",
            catchup_price=48,
            catchup_qty=10,
        )
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: None,
        )

        rest.create_order.assert_called_once_with(
            ticker="TK-B",
            action="buy",
            side="no",
            yes_price=None,
            no_price=48,
            count=10,
            order_group_id="grp-test",
        )


    @pytest.mark.asyncio
    async def test_catchup_records_placement_in_ledger(self):
        """After successful catch-up order, ledger must record placement
        to prevent another imbalance pass from reproposing before next poll.

        Regression: without record_placement, the under-side's committed state
        is stale and another catch-up is immediately reproposed.
        """
        scanner, adjuster, rest = _make_exec_context()
        ledger = adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_fill(Side.B, 20, 48)

        rest.get_all_orders = AsyncMock(
            return_value=[
                _make_order("TK-A", order_id="oa", fill_count=30, no_price=45, status="canceled"),
                _make_order("TK-B", order_id="ob", fill_count=20, no_price=48, status="canceled"),
            ]
        )
        rest.get_all_positions = AsyncMock(return_value=[])
        created = _make_order("TK-B", order_id="catchup-1", remaining_count=10, no_price=48)
        rest.create_order = AsyncMock(return_value=created)

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            catchup_ticker="TK-B",
            catchup_price=48,
            catchup_qty=10,
        )
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: None,
        )

        # Ledger must reflect the catch-up placement immediately
        assert ledger.resting_order_id(Side.B) == "catchup-1"
        assert ledger.resting_count(Side.B) == 10

    @pytest.mark.asyncio
    async def test_fresh_sync_calls_positions_api(self):
        """Fresh sync before catch-up must augment from positions API,
        not just orders, to handle archived fills.

        Regression: orders-only sync misses archived fills, computing
        wrong catch-up quantity.
        """
        scanner, adjuster, rest = _make_exec_context()
        ledger = adjuster.get_ledger("EVT-1")

        # Orders API shows 0 fills (old orders archived)
        rest.get_all_orders = AsyncMock(return_value=[])
        # Positions API shows the real fills
        rest.get_all_positions = AsyncMock(
            return_value=[
                Position(ticker="TK-A", position=-30, total_traded=1350, fees_paid=5),
                Position(ticker="TK-B", position=-20, total_traded=960, fees_paid=4),
            ]
        )
        created = _make_order("TK-B", order_id="catchup-2", remaining_count=10, no_price=48)
        rest.create_order = AsyncMock(return_value=created)

        rebalance = ProposedRebalance(
            event_ticker="EVT-1",
            side="A",
            catchup_ticker="TK-B",
            catchup_price=48,
            catchup_qty=10,
        )
        await execute_rebalance(
            rebalance,
            rest_client=rest,
            adjuster=adjuster,
            scanner=scanner,
            notify=lambda msg, sev: None,
        )

        # Positions API must have been called during fresh sync
        rest.get_all_positions.assert_called_once()
        # Ledger fills should reflect positions data, not zero from orders
        assert ledger.filled_count(Side.A) == 30
        assert ledger.filled_count(Side.B) == 20


# ── Top-up detection tests ───────────────────────────────────────────


class TestTopUpDetection:
    def test_both_sides_need_topup(self):
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 15, 45)
        ledger.record_fill(Side.B, 12, 48)
        snapshot = _make_snapshot(no_a=45, no_b=48)
        result = compute_topup_needs(ledger, pair, snapshot)
        assert result == {Side.A: (5, 45), Side.B: (8, 48)}

    def test_one_side_has_resting_skipped(self):
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 15, 45)
        ledger.record_resting(Side.A, "ord-a", 5, 45)
        ledger.record_fill(Side.B, 15, 48)
        snapshot = _make_snapshot(no_a=45, no_b=48)
        result = compute_topup_needs(ledger, pair, snapshot)
        assert Side.A not in result
        assert result == {Side.B: (5, 48)}

    def test_complete_unit_no_topup(self):
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 20, 45)
        ledger.record_fill(Side.B, 20, 48)
        snapshot = _make_snapshot(no_a=45, no_b=48)
        result = compute_topup_needs(ledger, pair, snapshot)
        assert result == {}

    def test_no_snapshot_no_topup(self):
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 15, 45)
        ledger.record_fill(Side.B, 12, 48)
        result = compute_topup_needs(ledger, pair, None)
        assert result == {}

    def test_imbalanced_committed_no_topup(self):
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_fill(Side.B, 15, 48)
        snapshot = _make_snapshot(no_a=45, no_b=48)
        result = compute_topup_needs(ledger, pair, snapshot)
        assert result == {}

    def test_onesided_topup_blocked_when_would_create_imbalance(self):
        """One-sided top-up that would make this side over-committed is blocked.

        Regression test for cancel→top-up→cancel thrashing loop:
        State: A has 4 filled + 8 resting (committed=12), B has 12 filled + 0 resting.
        Top-up B by 8 would bring committed_b to 20 > committed_a 12,
        which rebalance would immediately cancel — infinite loop.
        """
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 4, 45)
        ledger.record_resting(Side.A, "ord-a", 8, 45)
        ledger.record_fill(Side.B, 12, 48)
        snapshot = _make_snapshot(no_a=45, no_b=48)
        result = compute_topup_needs(ledger, pair, snapshot)
        assert result == {}

    def test_zero_fills_no_topup(self):
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        snapshot = _make_snapshot(no_a=45, no_b=48)
        result = compute_topup_needs(ledger, pair, snapshot)
        assert result == {}


# ── Overcommit reduction tests ─────────────────────────────────────


class TestOvercommitReduction:
    def test_balanced_overcommit_returns_reduction(self):
        """Balanced committed counts but unit overcommit → reduce resting.

        Reproduces the Cloud9 bug: Side A 20f+3r=23, Side B 3f+20r=23.
        Delta=0 so compute_rebalance_proposal returns None.
        But Side B has filled_in_unit=3, 3+20=23 > unit=20.
        """
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 20, 45)
        ledger.record_resting(Side.A, "ord-a", 3, 45)
        ledger.record_fill(Side.B, 3, 48)
        ledger.record_resting(Side.B, "ord-b", 20, 48)

        # Verify rebalance_proposal sees no imbalance (delta=0)
        assert compute_rebalance_proposal(
            "EVT-1", ledger, pair, None, "Test", OrderBookManager()
        ) is None

        # Overcommit reduction SHOULD fire on side B
        result = compute_overcommit_reduction("EVT-1", ledger, pair, "Test")
        assert result is not None
        assert result.side == "B"
        assert result.order_id == "ord-b"
        assert result.current_resting == 20
        assert result.target_resting == 17  # 20 - 3 filled_in_unit
        assert result.catchup_qty == 0  # reduce only, no catch-up

    def test_no_overcommit_returns_none(self):
        """Within unit capacity → no reduction needed."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 10, 45)
        ledger.record_resting(Side.A, "ord-a", 10, 45)
        ledger.record_fill(Side.B, 10, 48)
        ledger.record_resting(Side.B, "ord-b", 10, 48)

        result = compute_overcommit_reduction("EVT-1", ledger, pair, "Test")
        assert result is None

    def test_overcommit_with_no_order_id_skipped(self):
        """Overcommit detected but no resting order ID → can't reduce."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.B, 5, 48)
        # Simulate resting without a tracked order_id (e.g., orphaned)
        ledger._sides[Side.B].resting_count = 20

        result = compute_overcommit_reduction("EVT-1", ledger, pair, "Test")
        assert result is None

    def test_multi_unit_overcommit(self):
        """Overcommit after crossing a unit boundary (fills % unit_size)."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_resting(Side.A, "ord-a", 3, 45)
        ledger.record_fill(Side.B, 23, 48)
        ledger.record_resting(Side.B, "ord-b", 20, 48)

        # Side B: filled_in_unit = 23 % 20 = 3, 3 + 20 = 23 > 20
        result = compute_overcommit_reduction("EVT-1", ledger, pair, "Test")
        assert result is not None
        assert result.side == "B"
        assert result.target_resting == 17

    def test_exact_unit_boundary_no_overcommit(self):
        """Exactly at unit boundary → no overcommit."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 20, 45)
        ledger.record_resting(Side.A, "ord-a", 20, 45)
        ledger.record_fill(Side.B, 20, 48)
        ledger.record_resting(Side.B, "ord-b", 20, 48)

        # filled_in_unit = 20 % 20 = 0, 0 + 20 = 20, NOT > 20
        result = compute_overcommit_reduction("EVT-1", ledger, pair, "Test")
        assert result is None


# ── YES/NO side-awareness tests ──────────────────────────────────────


class TestYesNoRebalance:
    def test_catchup_proposal_carries_side(self):
        """ProposedRebalance.catchup_side is set from the pair."""
        pair = ArbPair(
            event_ticker="MKT-1",
            ticker_a="MKT-1",
            ticker_b="MKT-1",
            side_a="yes",
            side_b="no",
        )
        ledger = PositionLedger(
            event_ticker="MKT-1",
            unit_size=10,
            side_a_str="yes",
            side_b_str="no",
            is_same_ticker=True,
        )
        # Side A (YES) over-extended with resting
        ledger.record_fill(Side.A, count=10, price=48)
        ledger.record_resting(Side.A, order_id="yes-ord", count=10, price=48)

        books = OrderBookManager()
        from talos.models.ws import OrderBookSnapshot

        books.apply_snapshot(
            "MKT-1",
            OrderBookSnapshot(
                market_ticker="MKT-1",
                market_id="m1",
                yes=[[48, 100]],
                no=[[45, 100]],
            ),
        )
        snapshot = Opportunity(
            event_ticker="MKT-1",
            ticker_a="MKT-1",
            ticker_b="MKT-1",
            no_a=48,
            no_b=45,
            qty_a=100,
            qty_b=100,
            raw_edge=7,
            tradeable_qty=100,
            timestamp="2026-01-01",
        )
        result = compute_rebalance_proposal(
            "MKT-1",
            ledger,
            pair,
            snapshot,
            "test",
            books,
        )
        assert result is not None
        rebalance = result.rebalance
        assert rebalance is not None
        assert rebalance.reduce_side == "yes"  # Over-side is YES (Side.A)
        assert rebalance.catchup_side == "no"  # Under-side is NO (Side.B)

    def test_cross_no_defaults_preserved(self):
        """Cross-NO rebalance still has catchup_side='no'."""
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=45)
        ledger.record_resting(Side.A, order_id="ord-a", count=10, price=45)

        books = _books_with_data(no_a=45, no_b=48)
        snapshot = _make_snapshot(no_a=45, no_b=48)
        result = compute_rebalance_proposal(
            "EVT-1",
            ledger,
            pair,
            snapshot,
            "test",
            books,
        )
        assert result is not None
        rebalance = result.rebalance
        assert rebalance is not None
        assert rebalance.reduce_side == "no"
        assert rebalance.catchup_side == "no"
