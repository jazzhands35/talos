"""Tests for PositionLedger — pure state machine for position tracking."""

import pytest

from talos.position_ledger import PositionLedger, Side


class TestBasicTracking:
    def test_initial_state_is_empty(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        assert ledger.filled_count(Side.A) == 0
        assert ledger.filled_count(Side.B) == 0
        assert ledger.resting_count(Side.A) == 0
        assert ledger.resting_count(Side.B) == 0
        assert ledger.resting_order_id(Side.A) is None
        assert ledger.resting_order_id(Side.B) is None

    def test_record_fill(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=50)
        assert ledger.filled_count(Side.A) == 5
        assert ledger.filled_total_cost(Side.A) == 250  # 5 * 50
        assert ledger.avg_filled_price(Side.A) == 50.0

    def test_record_multiple_fills_accumulate(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=50)
        ledger.record_fill(Side.A, count=5, price=52)
        assert ledger.filled_count(Side.A) == 10
        assert ledger.filled_total_cost(Side.A) == 510  # 250 + 260
        assert ledger.avg_filled_price(Side.A) == 51.0

    def test_record_resting(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="ord-1", count=10, price=48)
        assert ledger.resting_count(Side.A) == 10
        assert ledger.resting_order_id(Side.A) == "ord-1"
        assert ledger.resting_price(Side.A) == 48

    def test_record_cancel(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="ord-1", count=10, price=48)
        ledger.record_cancel(Side.A, order_id="ord-1")
        assert ledger.resting_count(Side.A) == 0
        assert ledger.resting_order_id(Side.A) is None

    def test_cancel_wrong_order_id_raises(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="ord-1", count=10, price=48)
        with pytest.raises(ValueError, match="order_id mismatch"):
            ledger.record_cancel(Side.A, order_id="ord-999")


class TestDerivedQueries:
    def test_total_committed(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=6, price=50)
        ledger.record_resting(Side.A, order_id="ord-1", count=4, price=48)
        assert ledger.total_committed(Side.A) == 10

    def test_current_delta(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_fill(Side.B, count=6, price=48)
        assert ledger.current_delta() == 4  # abs(10 - 6)

    def test_unit_remaining_no_fills(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        assert ledger.unit_remaining(Side.A) == 10

    def test_unit_remaining_partial_fill(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=6, price=50)
        assert ledger.unit_remaining(Side.A) == 4

    def test_is_unit_complete(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=9, price=50)
        assert not ledger.is_unit_complete(Side.A)
        ledger.record_fill(Side.A, count=1, price=51)
        assert ledger.is_unit_complete(Side.A)

    def test_both_sides_complete(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=50)
        assert not ledger.both_sides_complete()
        ledger.record_fill(Side.B, count=10, price=48)
        assert ledger.both_sides_complete()

    def test_avg_filled_price_no_fills_returns_zero(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        assert ledger.avg_filled_price(Side.A) == 0.0

    def test_reset_pair(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_fill(Side.B, count=10, price=48)
        ledger.reset_pair()
        assert ledger.filled_count(Side.A) == 0
        assert ledger.filled_count(Side.B) == 0
        assert ledger.resting_order_id(Side.A) is None
