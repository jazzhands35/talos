"""Tests for BidAdjuster — async orchestrator for bid adjustment."""

from unittest.mock import AsyncMock

import pytest

from talos.bid_adjuster import BidAdjuster
from talos.models.market import OrderBookLevel
from talos.models.order import Order
from talos.models.strategy import ArbPair
from talos.orderbook import OrderBookManager
from talos.position_ledger import Side


class FakeBookManager(OrderBookManager):
    """Minimal fake for OrderBookManager.best_ask()."""

    def __init__(self, prices: dict[str, int]):
        super().__init__()
        self._prices = prices

    def best_ask(self, ticker: str, side: str = "no") -> OrderBookLevel | None:
        price = self._prices.get(ticker)
        if price is None:
            return None
        return OrderBookLevel(price_bps=price * 100, quantity_fp100=10_000)


class TestDecisionLogic:
    def setup_method(self):
        self.pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        # fee_adjusted_cost(48)=48.91, fee_adjusted_cost(50)=50.875
        # sum=99.785 < 100 → profitable
        self.books = FakeBookManager({"TK-A": 50, "TK-B": 48})
        self.adjuster = BidAdjuster(
            book_manager=self.books,
            pairs=[self.pair],
            unit_size=10,
        )

    def test_jump_on_profitable_side_emits_proposal(self):
        ledger = self.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_resting(Side.B, order_id="ord-b", count=10, price=47)
        # Side B jumped from 47 to 48 — still profitable: 48.91 + 50.875 = 99.785 < 100
        proposal = self.adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is not None
        assert proposal.side == "B"
        assert proposal.new_price == 48
        assert proposal.cancel_order_id == "ord-b"
        assert proposal.new_count == 10

    def test_jump_on_unprofitable_side_returns_hold(self):
        ledger = self.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_resting(Side.B, order_id="ord-b", count=10, price=47)
        # Top of market moved to 51 — unprofitable: 51.8575 + 50.875 > 100
        self.books._prices["TK-B"] = 51
        proposal = self.adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is not None
        assert proposal.action == "hold"
        assert "not profitable" in proposal.reason

    def test_unprofitable_no_fills_returns_withdraw(self):
        """When arb is unprofitable and neither side has fills, propose withdrawal."""
        ledger = self.adjuster.get_ledger("EVT-1")
        ledger.record_resting(Side.A, order_id="ord-a", count=10, price=48)
        ledger.record_resting(Side.B, order_id="ord-b", count=10, price=47)
        # Top of market moved to 53 — unprofitable:
        # fee_adjusted_cost(53) + fee_adjusted_cost(50) >= 100
        self.books._prices["TK-B"] = 53
        proposal = self.adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is not None
        assert proposal.action == "withdraw"
        assert "no fills" in proposal.reason

    def test_unprofitable_with_fills_returns_hold(self):
        """When arb is unprofitable but has fills, hold position."""
        ledger = self.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=5, price=48)
        ledger.record_resting(Side.A, order_id="ord-a", count=5, price=48)
        ledger.record_resting(Side.B, order_id="ord-b", count=10, price=47)
        # Top of market moved to 53 — unprofitable
        self.books._prices["TK-B"] = 53
        proposal = self.adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is not None
        assert proposal.action == "hold"
        assert "not profitable" in proposal.reason

    def test_back_at_top_no_proposal(self):
        ledger = self.adjuster.get_ledger("EVT-1")
        ledger.record_resting(Side.B, order_id="ord-b", count=10, price=48)
        proposal = self.adjuster.evaluate_jump("TK-B", at_top=True)
        assert proposal is None

    def test_no_resting_order_no_proposal(self):
        # No resting order on side B — nothing to adjust
        proposal = self.adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is None

    def test_fractional_completion_proposal(self):
        ledger = self.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_fill(Side.B, count=6, price=32)
        ledger.record_resting(Side.B, order_id="ord-b", count=4, price=32)
        # Jumped to 33 — propose 4 contracts at 33
        self.books._prices["TK-B"] = 33
        proposal = self.adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is not None
        assert proposal.new_count == 4
        assert proposal.new_price == 33

    def test_unknown_ticker_no_proposal(self):
        proposal = self.adjuster.evaluate_jump("UNKNOWN", at_top=False)
        assert proposal is None


def _make_order(order_id: str, price: int, fill_count: int, remaining_count: int) -> Order:
    return Order(
        order_id=order_id,
        ticker="TK-B",
        side="no",
        action="buy",
        no_price_bps=price * 100,
        status="resting",
        remaining_count_fp100=remaining_count * 100,
        fill_count_fp100=fill_count * 100,
        initial_count_fp100=(fill_count + remaining_count) * 100,
    )


class TestAsyncExecution:
    @pytest.mark.asyncio
    async def test_execute_amends_order(self):
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        books = FakeBookManager({"TK-A": 50, "TK-B": 48})
        adjuster = BidAdjuster(book_manager=books, pairs=[pair], unit_size=10)

        ledger = adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_resting(Side.B, order_id="ord-b", count=10, price=47)

        proposal = adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is not None

        fresh_order = _make_order("ord-b", price=47, fill_count=0, remaining_count=10)
        old_order = _make_order("ord-b", price=47, fill_count=0, remaining_count=10)
        amended_order = _make_order("ord-b", price=48, fill_count=0, remaining_count=10)

        rest_client = AsyncMock()
        rest_client.get_order.return_value = fresh_order
        rest_client.amend_order.return_value = (old_order, amended_order)

        await adjuster.execute(proposal, rest_client)

        rest_client.get_order.assert_called_once_with("ord-b")
        rest_client.amend_order.assert_called_once_with(
            "ord-b",
            ticker="TK-B",
            side="no",
            action="buy",
            no_price=48,
            count=10,
        )
        # Ledger should reflect amended state
        assert ledger.resting_order_id(Side.B) == "ord-b"
        assert ledger.resting_price(Side.B) == 48
        assert ledger.resting_count(Side.B) == 10

    @pytest.mark.asyncio
    async def test_execute_amend_with_partial_fill(self):
        """Amend a partially filled order — only unfilled portion moves."""
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        books = FakeBookManager({"TK-A": 50, "TK-B": 33})
        adjuster = BidAdjuster(book_manager=books, pairs=[pair], unit_size=10)

        ledger = adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_fill(Side.B, count=6, price=32)
        ledger.record_resting(Side.B, order_id="ord-b", count=4, price=32)

        proposal = adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is not None
        assert proposal.new_count == 4

        # get_order returns the ORDER's own state (6 fills on this order)
        fresh_order = _make_order("ord-b", price=32, fill_count=6, remaining_count=4)
        old_order = _make_order("ord-b", price=32, fill_count=6, remaining_count=4)
        amended_order = _make_order("ord-b", price=33, fill_count=6, remaining_count=4)

        rest_client = AsyncMock()
        rest_client.get_order.return_value = fresh_order
        rest_client.amend_order.return_value = (old_order, amended_order)

        await adjuster.execute(proposal, rest_client)

        # count passed to amend = ORDER's fill_count + remaining_count (not ledger aggregate)
        rest_client.get_order.assert_called_once_with("ord-b")
        rest_client.amend_order.assert_called_once_with(
            "ord-b",
            ticker="TK-B",
            side="no",
            action="buy",
            no_price=33,
            count=10,  # 6 filled + 4 remaining = 10 total (from fresh order)
        )
        assert ledger.resting_price(Side.B) == 33
        assert ledger.resting_count(Side.B) == 4

    @pytest.mark.asyncio
    async def test_execute_amend_fails_halts(self):
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        books = FakeBookManager({"TK-A": 50, "TK-B": 48})
        adjuster = BidAdjuster(book_manager=books, pairs=[pair], unit_size=10)

        ledger = adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_resting(Side.B, order_id="ord-b", count=10, price=47)

        proposal = adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is not None
        rest_client = AsyncMock()
        rest_client.get_order.return_value = _make_order(
            "ord-b", price=47, fill_count=0, remaining_count=10
        )
        rest_client.amend_order.side_effect = Exception("API error")

        with pytest.raises(Exception, match="API error"):
            await adjuster.execute(proposal, rest_client)

        # Original order should still be in ledger (amend is atomic — failure = no change)
        assert ledger.resting_order_id(Side.B) == "ord-b"
        assert ledger.resting_price(Side.B) == 47

    @pytest.mark.asyncio
    async def test_amend_preserves_ws_recorded_fills(self):
        """Post-2026-04-26 (CLE-TOR fix): the amend path no longer reads fills
        from the amend response — WS ``_on_fill`` is the single writer.

        This test verifies the amend path doesn't CLOBBER fills that WS
        already recorded into the ledger during the approval window. The
        amend response carries an ``old_order`` snapshot we explicitly
        discard; only the ``amended_order``'s remaining_count drives the
        new resting state.
        """
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        books = FakeBookManager({"TK-A": 50, "TK-B": 48})
        adjuster = BidAdjuster(book_manager=books, pairs=[pair], unit_size=20)

        ledger = adjuster.get_ledger("EVT-1")
        # Side A has 20 fills (from a prior order, now archived).
        ledger.record_fill(Side.A, count=20, price=50)
        # Side B starts with 15 historical fills + 5 resting on current order.
        ledger.record_fill(Side.B, count=15, price=47)
        ledger.record_resting(Side.B, order_id="ord-b", count=5, price=47)

        proposal = adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is not None

        # Simulate WS having delivered 2 mid-approval fills via _on_fill
        # before the amend RPC returns. Use record_fill_from_ws so the
        # trade_ids land in the dedup set — proving they cannot be
        # double-applied by anything downstream.
        ledger.record_fill_from_ws(
            Side.B,
            trade_id="trade-mid-1",
            count_fp100=100,  # 1 contract
            price_bps=4700,
            fees_bps=300,
        )
        ledger.record_fill_from_ws(
            Side.B,
            trade_id="trade-mid-2",
            count_fp100=100,  # 1 contract
            price_bps=4700,
            fees_bps=300,
        )
        assert ledger.filled_count(Side.B) == 17  # 15 historical + 2 WS
        assert ledger.resting_count(Side.B) == 3  # 5 - 2 filled-from-resting

        # Amend response: old_order has 17 fills (pre-amend snapshot), new
        # order has 17 fills + 3 remaining at new price 48. The amend path
        # must NOT add another 17 fills to the ledger.
        fresh_order = _make_order("ord-b", price=47, fill_count=15, remaining_count=5)
        old_order = _make_order("ord-b", price=47, fill_count=17, remaining_count=3)
        amended_order = _make_order("ord-b", price=48, fill_count=17, remaining_count=3)

        rest_client = AsyncMock()
        rest_client.get_order.return_value = fresh_order
        rest_client.amend_order.return_value = (old_order, amended_order)

        await adjuster.execute(proposal, rest_client)

        # Filled count is unchanged from WS — amend did NOT re-apply.
        assert ledger.filled_count(Side.B) == 17
        # Resting state is updated to the amended order's snapshot.
        assert ledger.resting_count(Side.B) == 3
        assert ledger.resting_price(Side.B) == 48


class TestYesNoPairAdjuster:
    """BidAdjuster handles YES/NO pairs where ticker_a == ticker_b."""

    def test_add_event_same_ticker_no_collision(self):
        books = FakeBookManager({})
        pair = ArbPair(
            event_ticker="MKT-1",
            ticker_a="MKT-1",
            ticker_b="MKT-1",
            side_a="yes",
            side_b="no",
        )
        adj = BidAdjuster(books, [pair])
        result_a = adj.resolve_pair("MKT-1", order_side="yes")
        result_b = adj.resolve_pair("MKT-1", order_side="no")
        assert result_a is not None
        assert result_b is not None
        assert result_a[1] == Side.A
        assert result_b[1] == Side.B

    def test_cross_no_unchanged(self):
        books = FakeBookManager({})
        pair = ArbPair(event_ticker="EVT", ticker_a="TK-A", ticker_b="TK-B")
        adj = BidAdjuster(books, [pair])
        result_a = adj.resolve_pair("TK-A")
        result_b = adj.resolve_pair("TK-B")
        assert result_a is not None
        assert result_b is not None
        assert result_a[1] == Side.A
        assert result_b[1] == Side.B

    def test_resolve_event_still_works(self):
        """The existing resolve_event(ticker) -> str method still works."""
        books = FakeBookManager({})
        pair = ArbPair(event_ticker="EVT", ticker_a="TK-A", ticker_b="TK-B")
        adj = BidAdjuster(books, [pair])
        assert adj.resolve_event("TK-A") == "EVT"
        assert adj.resolve_event("TK-B") == "EVT"

    def test_resolve_event_same_ticker(self):
        """resolve_event works for same-ticker pairs."""
        books = FakeBookManager({})
        pair = ArbPair(
            event_ticker="MKT-1",
            ticker_a="MKT-1",
            ticker_b="MKT-1",
            side_a="yes",
            side_b="no",
        )
        adj = BidAdjuster(books, [pair])
        assert adj.resolve_event("MKT-1") == "MKT-1"

    def test_remove_event_same_ticker(self):
        """remove_event cleans up all entries for same-ticker pairs."""
        books = FakeBookManager({})
        pair = ArbPair(
            event_ticker="MKT-1",
            ticker_a="MKT-1",
            ticker_b="MKT-1",
            side_a="yes",
            side_b="no",
        )
        adj = BidAdjuster(books, [pair])
        adj.remove_event("MKT-1")
        assert adj.resolve_pair("MKT-1") is None
        assert adj.resolve_event("MKT-1") is None


class TestEvaluateJumpOpenScope:
    """evaluate_jump uses open-unit avg for P18, not lifetime blend."""

    def test_jump_follows_when_only_closed_units_exist(self):
        """When the open unit is empty (all prior units closed), a jump
        should be evaluated against the new price alone — not the lifetime
        blend. This is the 'sold at 83c, should follow to 18c' scenario.

        Lifetime A avg: (92+82+80+82+80)/5 = 83.2c
        With fee_rate=0.0 and old code: 18 + 83 = 101 >= 100 → hold (bug)
        With new code: open_count(A) == 0 → other_effective = 0 → follow_jump
        """
        # fee_rate=0.0 so fee_adjusted_cost(x) == x — clean integer math
        pair = ArbPair(
            event_ticker="EVT-X",
            ticker_a="TK-X-A",
            ticker_b="TK-X-B",
            fee_rate=0.0,
        )
        # B best ask at 18 — that's the jump target
        books = FakeBookManager({"TK-X-B": 18})
        adjuster = BidAdjuster(book_manager=books, pairs=[pair], unit_size=5)
        ledger = adjuster.get_ledger("EVT-X")

        # Simulate a lifetime with 5 closed units at varying prices.
        # Each (A, B) fill pair is balanced at unit_size=5 →
        # _reconcile_closed fires after each fill pair and closes 1 unit.
        for a_price, b_price in [(92, 7), (82, 18), (80, 19), (82, 23), (80, 17)]:
            ledger.record_fill(Side.A, 5, a_price)
            ledger.record_fill(Side.B, 5, b_price)

        # All units closed — open buckets must be empty
        assert ledger.open_count(Side.A) == 0
        assert ledger.open_count(Side.B) == 0

        # B has 5 resting @ 17, A has no resting
        ledger.record_resting(Side.B, "oid-b", 5, 17)

        # evaluate_jump for B side — book shows 18 (set in FakeBookManager above)
        result = adjuster.evaluate_jump("TK-X-B", at_top=False)

        assert result is not None, "Expected a proposal, got None"
        assert result.action == "follow_jump", (
            f"Expected 'follow_jump' but got '{result.action}': {result.reason}"
        )


class TestPostCancelSafetyDripCap:
    """_check_post_cancel_safety routes through per_side_max_ahead."""

    def _make_adjuster(self, drip_config_lookup=None) -> BidAdjuster:
        pair = ArbPair(event_ticker="EVT", ticker_a="TK-A", ticker_b="TK-B")
        books = FakeBookManager({"TK-A": 50, "TK-B": 48})
        return BidAdjuster(
            book_manager=books,
            pairs=[pair],
            unit_size=5,
            drip_config_lookup=drip_config_lookup,
        )

    def test_post_cancel_safety_uses_drip_cap_when_lookup_returns_config(self):
        from talos.drip import DripConfig
        from talos.position_ledger import PositionLedger

        drip_config = DripConfig(drip_size=1, max_drips=1)
        adjuster = self._make_adjuster(
            drip_config_lookup=lambda evt: drip_config if evt == "EVT" else None
        )
        ledger = PositionLedger(event_ticker="EVT", unit_size=5)

        # 2 contracts > drip cap of 1 → blocked even though within unit_size of 5.
        ok, reason = adjuster._check_post_cancel_safety(ledger, Side.A, new_count=2, new_price=50)
        assert ok is False
        assert "drip cap" in reason

    def test_post_cancel_safety_uses_unit_when_no_drip_config(self):
        from talos.position_ledger import PositionLedger

        adjuster = self._make_adjuster(drip_config_lookup=None)
        ledger = PositionLedger(event_ticker="EVT", unit_size=5)

        # 2 contracts is within unit cap of 5 → first check passes.
        # Either ok=True or ok=False with a profitability reason (NOT a unit/cap reason).
        ok, reason = adjuster._check_post_cancel_safety(ledger, Side.A, new_count=2, new_price=50)
        if not ok:
            assert "unit" not in reason and "cap" not in reason
