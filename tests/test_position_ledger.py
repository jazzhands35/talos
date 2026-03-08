"""Tests for PositionLedger — pure state machine for position tracking."""

import pytest

from talos.cpm import CPMTracker
from talos.models.order import Order
from talos.models.strategy import ArbPair
from talos.position_ledger import PositionLedger, Side, compute_display_positions


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


class TestSafetyGate:
    def test_rejects_exceeding_unit(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=6, price=50)
        ledger.record_resting(Side.A, order_id="ord-1", count=4, price=48)
        ok, reason = ledger.is_placement_safe(Side.A, count=1, price=47)
        assert not ok
        assert "exceed unit" in reason

    def test_rejects_duplicate_resting(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="ord-1", count=5, price=48)
        ok, reason = ledger.is_placement_safe(Side.A, count=5, price=49)
        assert not ok
        assert "already resting" in reason

    def test_rejects_unprofitable_arb_with_fills(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=50)
        # At 50c each side: fee_adjusted_cost(50) = 50 + 50*0.0175 = 50.875
        # 50.875 + 50.875 = 101.75 >= 100 → unprofitable
        ok, reason = ledger.is_placement_safe(Side.B, count=10, price=50)
        assert not ok
        assert "not profitable" in reason

    def test_accepts_profitable_arb(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=50)
        # At 48c: fee_adjusted_cost(48) = 48 + 52*0.0175 = 48.91
        # 50.875 + 48.91 = 99.785 < 100 → profitable
        ok, reason = ledger.is_placement_safe(Side.B, count=10, price=48)
        assert ok
        assert reason == ""

    def test_allows_placement_when_other_side_empty(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ok, reason = ledger.is_placement_safe(Side.A, count=10, price=50)
        assert ok

    def test_fractional_completion_within_unit(self):
        """6 filled + 4 new = 10 = unit_size → allowed."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=50)  # other side
        ledger.record_fill(Side.B, count=6, price=48)
        # fee_adjusted_cost(47) + fee_adjusted_cost(50) = 47.9275 + 50.875 = 98.80 < 100
        ok, reason = ledger.is_placement_safe(Side.B, count=4, price=47)
        assert ok

    def test_fractional_completion_exceeds_unit(self):
        """6 filled + 5 new = 11 > unit_size → rejected."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_fill(Side.B, count=6, price=48)
        ok, reason = ledger.is_placement_safe(Side.B, count=5, price=49)
        assert not ok
        assert "exceed unit" in reason

    def test_rejects_when_discrepancy_exists(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger._discrepancy = "test mismatch"
        ok, reason = ledger.is_placement_safe(Side.A, count=10, price=50)
        assert not ok
        assert "discrepancy" in reason


def _make_order(
    ticker: str,
    fill_count: int = 0,
    remaining_count: int = 0,
    no_price: int = 50,
    order_id: str = "ord-1",
    status: str = "resting",
) -> Order:
    return Order(
        order_id=order_id,
        ticker=ticker,
        action="buy",
        side="no",
        no_price=no_price,
        fill_count=fill_count,
        remaining_count=remaining_count,
        initial_count=fill_count + remaining_count,
        status=status,
    )


class TestReconciliation:
    def test_sync_matching_state_no_discrepancy(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=50)
        ledger.record_resting(Side.A, order_id="ord-a", count=5, price=50)
        orders = [
            _make_order("TK-A", fill_count=5, remaining_count=5, order_id="ord-a"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert not ledger.has_discrepancy

    def test_sync_fill_count_mismatch_flags(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=50)
        # Kalshi says 8 filled — mismatch
        orders = [
            _make_order("TK-A", fill_count=8, remaining_count=2, order_id="ord-a"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.has_discrepancy
        assert ledger.discrepancy is not None
        assert "filled" in ledger.discrepancy

    def test_sync_multiple_resting_orders_flags(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        orders = [
            _make_order("TK-A", fill_count=0, remaining_count=10, order_id="ord-1"),
            _make_order("TK-A", fill_count=0, remaining_count=10, order_id="ord-2"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.has_discrepancy
        assert ledger.discrepancy is not None
        assert "2 resting orders" in ledger.discrepancy

    def test_discrepancy_blocks_placement(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        orders = [
            _make_order("TK-A", fill_count=0, remaining_count=10, order_id="ord-1"),
            _make_order("TK-A", fill_count=0, remaining_count=10, order_id="ord-2"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        ok, reason = ledger.is_placement_safe(Side.B, count=10, price=48)
        assert not ok
        assert "discrepancy" in reason

    def test_sync_clears_discrepancy_when_state_matches(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger._discrepancy = "old problem"
        orders = []  # no orders = no fills, no resting = matches empty ledger
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert not ledger.has_discrepancy


def _pair(event: str = "EVT-1", a: str = "TK-A", b: str = "TK-B") -> ArbPair:
    return ArbPair(event_ticker=event, ticker_a=a, ticker_b=b)


class TestComputeDisplayPositions:
    def test_empty_ledger_returns_empty(self):
        ledgers = {"EVT-1": PositionLedger(event_ticker="EVT-1")}
        result = compute_display_positions(ledgers, [_pair()], {}, CPMTracker())
        assert result == []

    def test_both_sides_filled_equally(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=45)
        ledger.record_fill(Side.B, count=5, price=47)
        ledgers = {"EVT-1": ledger}

        result = compute_display_positions(ledgers, [_pair()], {}, CPMTracker())
        assert len(result) == 1
        s = result[0]
        assert s.matched_pairs == 5
        assert s.unmatched_a == 0
        assert s.unmatched_b == 0
        assert s.locked_profit_cents > 0  # 45+47=92 < 100, profitable
        assert s.exposure_cents == 0
        assert s.leg_a.filled_count == 5
        assert s.leg_b.filled_count == 5
        assert s.leg_a.no_price == 45
        assert s.leg_b.no_price == 47

    def test_one_side_ahead(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=45)
        ledger.record_fill(Side.B, count=3, price=47)
        ledgers = {"EVT-1": ledger}

        result = compute_display_positions(ledgers, [_pair()], {}, CPMTracker())
        assert len(result) == 1
        s = result[0]
        assert s.matched_pairs == 3
        assert s.unmatched_a == 2
        assert s.unmatched_b == 0
        assert s.exposure_cents > 0  # 2 unmatched contracts on A

    def test_resting_only_shows_resting_price(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="ord-1", count=10, price=45)
        ledgers = {"EVT-1": ledger}

        result = compute_display_positions(ledgers, [_pair()], {}, CPMTracker())
        assert len(result) == 1
        assert result[0].leg_a.no_price == 45
        assert result[0].leg_a.resting_count == 10

    def test_queue_enrichment(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="ord-1", count=10, price=45)
        queue_cache = {"ord-1": 42}
        ledgers = {"EVT-1": ledger}

        result = compute_display_positions(ledgers, [_pair()], queue_cache, CPMTracker())
        assert result[0].leg_a.queue_position == 42

    def test_cpm_enrichment(self):
        from datetime import datetime, timezone

        from talos.models.market import Trade

        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="ord-1", count=10, price=45)
        ledgers = {"EVT-1": ledger}

        # Use a recent timestamp so it falls within the 5-minute CPM window
        recent_ts = datetime.now(timezone.utc).isoformat()
        cpm = CPMTracker()
        cpm.ingest("TK-A", [
            Trade(trade_id="t1", ticker="TK-A", count=100, price=45,
                  side="no", created_time=recent_ts),
        ])

        result = compute_display_positions(ledgers, [_pair()], {}, cpm)
        assert result[0].leg_a.cpm is not None
        assert result[0].leg_a.cpm > 0

    def test_missing_ledger_skipped(self):
        """Pairs with no corresponding ledger are silently skipped."""
        result = compute_display_positions({}, [_pair()], {}, CPMTracker())
        assert result == []
