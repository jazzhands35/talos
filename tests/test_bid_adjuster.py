"""Tests for BidAdjuster — async orchestrator for bid adjustment."""

from talos.bid_adjuster import BidAdjuster
from talos.models.strategy import ArbPair
from talos.position_ledger import Side


class FakeBookManager:
    """Minimal fake for OrderBookManager.best_ask()."""

    def __init__(self, prices: dict[str, int]):
        self._prices = prices

    def best_ask(self, ticker: str):
        price = self._prices.get(ticker)
        if price is None:
            return None

        class Level:
            pass

        level = Level()
        level.price = price
        return level


class TestDecisionLogic:
    def setup_method(self):
        self.pair = ArbPair(
            event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B"
        )
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

    def test_jump_on_unprofitable_side_no_proposal(self):
        ledger = self.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_resting(Side.B, order_id="ord-b", count=10, price=47)
        # Top of market moved to 51 — unprofitable: 51.8575 + 50.875 > 100
        self.books._prices["TK-B"] = 51
        proposal = self.adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is None

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


class TestDualJumpTiebreaker:
    def setup_method(self):
        self.pair = ArbPair(
            event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B"
        )
        self.books = FakeBookManager({"TK-A": 48, "TK-B": 33})
        self.adjuster = BidAdjuster(
            book_manager=self.books,
            pairs=[self.pair],
            unit_size=10,
        )

    def test_most_behind_side_goes_first(self):
        ledger = self.adjuster.get_ledger("EVT-1")
        # A: 3 filled, 7 resting → needs 7 more
        # B: 6 filled, 4 resting → needs 4 more
        ledger.record_fill(Side.A, count=3, price=47)
        ledger.record_resting(Side.A, order_id="ord-a", count=7, price=47)
        ledger.record_fill(Side.B, count=6, price=32)
        ledger.record_resting(Side.B, order_id="ord-b", count=4, price=32)

        # Both jumped — A should go first (needs 7 > B's 4)
        self.adjuster.evaluate_jump("TK-A", at_top=False)
        self.adjuster.evaluate_jump("TK-B", at_top=False)

        # Only A's proposal should be active, B should be deferred
        assert self.adjuster.has_pending_proposal("EVT-1", Side.A)
        assert not self.adjuster.has_pending_proposal("EVT-1", Side.B)
        assert self.adjuster.has_deferred("EVT-1", Side.B)

    def test_deferred_side_reevaluated_on_completion(self):
        ledger = self.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=3, price=47)
        ledger.record_resting(Side.A, order_id="ord-a", count=7, price=47)
        ledger.record_fill(Side.B, count=6, price=32)
        ledger.record_resting(Side.B, order_id="ord-b", count=4, price=32)

        # Both jumped
        self.adjuster.evaluate_jump("TK-A", at_top=False)
        self.adjuster.evaluate_jump("TK-B", at_top=False)

        # Simulate A completing — clear A's resting and fill it
        ledger.record_cancel(Side.A, "ord-a")
        ledger.record_fill(Side.A, count=7, price=48)

        # Notify adjuster that A is complete — should re-evaluate B
        proposal = self.adjuster.on_side_complete("EVT-1", Side.A)
        assert proposal is not None
        assert proposal.side == "B"
