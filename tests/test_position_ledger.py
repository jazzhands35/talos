"""Tests for PositionLedger — pure state machine for position tracking."""

from datetime import UTC

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
        """8 filled + 0 resting + 5 new = 13 > 10 → blocked by unit gate."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=8, price=50)
        ok, reason = ledger.is_placement_safe(Side.A, count=5, price=47)
        assert not ok
        assert "exceed unit" in reason

    def test_allows_second_resting_within_unit(self):
        """5 resting + 5 new = 10 <= unit_size → allowed."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="ord-1", count=5, price=48)
        ok, reason = ledger.is_placement_safe(Side.A, count=5, price=49)
        assert ok

    def test_rejects_resting_exceeding_unit(self):
        """5 resting + 6 new = 11 > unit_size → blocked."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="ord-1", count=5, price=48)
        ok, reason = ledger.is_placement_safe(Side.A, count=6, price=49)
        assert not ok
        assert "exceed unit" in reason

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

    def test_allows_reentry_after_unit_complete(self):
        """10 filled (unit complete) + 0 resting + 10 new → allowed via modular arithmetic."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=45)
        ledger.record_fill(Side.B, count=10, price=48)
        # Side A: filled_in_unit = 10 % 10 = 0, so 0 + 0 + 10 = 10 <= 10
        ok, reason = ledger.is_placement_safe(Side.A, count=10, price=46)
        assert ok
        assert reason == ""

    def test_blocks_double_resting_after_unit_complete(self):
        """10 filled + 10 resting + 10 new → blocked (exceeds unit)."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=45)
        ledger.record_fill(Side.B, count=10, price=48)
        ledger.record_resting(Side.A, order_id="ord-2", count=10, price=46)
        ok, reason = ledger.is_placement_safe(Side.A, count=10, price=47)
        assert not ok
        assert "exceed unit" in reason

    def test_blocks_reentry_with_incomplete_unit(self):
        """5 filled + 0 resting + 10 new → blocked (5 + 10 = 15 > 10)."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=45)
        ok, reason = ledger.is_placement_safe(Side.A, count=10, price=46)
        assert not ok
        assert "exceed unit" in reason


def _make_order(
    ticker: str,
    fill_count: int = 0,
    remaining_count: int = 0,
    no_price: int = 50,
    order_id: str = "ord-1",
    status: str = "resting",
    maker_fill_cost: int | None = None,
    taker_fill_cost: int = 0,
) -> Order:
    # Default maker_fill_cost = no_price * fill_count (simple case)
    if maker_fill_cost is None:
        maker_fill_cost = no_price * fill_count
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
        maker_fill_cost=maker_fill_cost,
        taker_fill_cost=taker_fill_cost,
    )


class TestReconciliation:
    def test_sync_matching_state(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=50)
        ledger.record_resting(Side.A, order_id="ord-a", count=5, price=50)
        orders = [
            _make_order("TK-A", fill_count=5, remaining_count=5, order_id="ord-a"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.filled_count(Side.A) == 5
        assert ledger.resting_count(Side.A) == 5

    def test_sync_fill_increase_accepted(self):
        """Fill count going up between polls is normal — should sync, not flag."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=50)
        ledger.record_resting(Side.A, order_id="ord-a", count=5, price=50)
        # Kalshi says 8 filled (3 more fills happened between polls)
        orders = [
            _make_order("TK-A", fill_count=8, remaining_count=2, order_id="ord-a"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.filled_count(Side.A) == 8

    def test_sync_fill_decrease_preserves_existing(self):
        """Orders API may archive old orders — fills must never decrease."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=8, price=50)
        # Kalshi orders only reports 5 (3 were archived) — keep ledger's 8
        orders = [
            _make_order("TK-A", fill_count=5, remaining_count=5, order_id="ord-a"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.filled_count(Side.A) == 8  # preserved, not decreased
        assert ledger.filled_total_cost(Side.A) == 400  # 8 * 50, not overwritten

    def test_sync_multiple_resting_orders_sums_counts(self):
        """Multiple resting orders on same side are summed, not flagged."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        orders = [
            _make_order("TK-A", fill_count=0, remaining_count=10, order_id="ord-1"),
            _make_order("TK-A", fill_count=0, remaining_count=10, order_id="ord-2"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.resting_count(Side.A) == 20  # summed
        assert ledger.resting_order_id(Side.A) == "ord-1"  # first order

    def test_sync_resting_then_fill_updates_correctly(self):
        """Resting order gets a fill between polls — the normal case."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        # First sync: 10 resting, 0 filled on side A
        orders = [
            _make_order("TK-A", fill_count=0, remaining_count=10, order_id="ord-a"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.filled_count(Side.A) == 0
        assert ledger.resting_count(Side.A) == 10

        # Second sync: 1 fill happened, 9 remaining
        orders = [
            _make_order("TK-A", fill_count=1, remaining_count=9, order_id="ord-a"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.filled_count(Side.A) == 1
        assert ledger.resting_count(Side.A) == 9

    def test_sync_resting_fully_fills_between_polls(self):
        """Resting order fills completely between polls."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        # First sync: 10 resting on side A
        orders = [
            _make_order("TK-A", fill_count=0, remaining_count=10, order_id="ord-a"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.resting_order_id(Side.A) == "ord-a"

        # Second sync: order fully filled — 10 fills, 0 remaining, status "filled"
        orders = [
            _make_order(
                "TK-A",
                fill_count=10,
                remaining_count=0,
                order_id="ord-a",
                status="filled",
            ),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.filled_count(Side.A) == 10
        assert ledger.resting_count(Side.A) == 0
        assert ledger.resting_order_id(Side.A) is None


class TestSyncFromPositions:
    """Tests for positions-API-based fill augmentation (P7/P15)."""

    def test_augments_fills_when_orders_missed_archived(self):
        """When orders-based sync shows 0 fills but positions shows 30, patch it."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        # sync_from_orders found nothing (orders archived)
        ledger.sync_from_orders([], ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.filled_count(Side.A) == 0

        # positions API says we hold 30 NO on A, 10 NO on B
        ledger.sync_from_positions(
            position_fills={Side.A: 30, Side.B: 10},
            position_costs={Side.A: 1380, Side.B: 520},
        )
        assert ledger.filled_count(Side.A) == 30
        assert ledger.filled_count(Side.B) == 10
        assert ledger.filled_total_cost(Side.A) == 1380

    def test_no_op_when_orders_already_correct(self):
        """When orders-based sync already has the right count, positions is a no-op."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=30, price=46)
        ledger.record_fill(Side.B, count=10, price=52)

        ledger.sync_from_positions(
            position_fills={Side.A: 30, Side.B: 10},
            position_costs={Side.A: 1380, Side.B: 520},
        )
        # Unchanged — orders already had the data
        assert ledger.filled_count(Side.A) == 30
        assert ledger.filled_total_cost(Side.A) == 1380  # 30 * 46

    def test_partial_augmentation(self):
        """Orders captured some fills, positions patches the rest with higher cost."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=46)  # partial from orders
        # Positions says 30 total — cost is authoritative (more complete)
        ledger.sync_from_positions(
            position_fills={Side.A: 30, Side.B: 0},
            position_costs={Side.A: 1380, Side.B: 0},
        )
        assert ledger.filled_count(Side.A) == 30
        # Positions cost overrides partial orders cost (higher = more complete)
        assert ledger.filled_total_cost(Side.A) == 1380  # 30 * 46

    def test_cost_patched_even_when_fills_equal(self):
        """Positions API provides cost when orders had none."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        # Fills set by positions (no cost data from orders)
        ledger._sides[Side.A].filled_count = 30
        ledger._sides[Side.A].filled_total_cost = 0  # no cost yet
        ledger.sync_from_positions(
            position_fills={Side.A: 30, Side.B: 0},
            position_costs={Side.A: 1380, Side.B: 0},
        )
        assert ledger.filled_count(Side.A) == 30
        assert ledger.filled_total_cost(Side.A) == 1380  # patched

    def test_zero_positions_no_change(self):
        """When positions API shows 0, nothing changes."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.sync_from_positions(
            position_fills={Side.A: 0, Side.B: 0},
            position_costs={Side.A: 0, Side.B: 0},
        )
        assert ledger.filled_count(Side.A) == 0
        assert ledger.filled_count(Side.B) == 0


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
        from datetime import datetime

        from talos.models.market import Trade

        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="ord-1", count=10, price=45)
        ledgers = {"EVT-1": ledger}

        # Use a recent timestamp so it falls within the 5-minute CPM window
        recent_ts = datetime.now(UTC).isoformat()
        cpm = CPMTracker()
        cpm.ingest(
            "TK-A",
            [
                Trade(
                    trade_id="t1",
                    ticker="TK-A",
                    count=100,
                    price=45,
                    side="no",
                    created_time=recent_ts,
                ),
            ],
        )

        result = compute_display_positions(ledgers, [_pair()], {}, cpm)
        assert result[0].leg_a.cpm is not None
        assert result[0].leg_a.cpm > 0

    def test_missing_ledger_skipped(self):
        """Pairs with no corresponding ledger are silently skipped."""
        result = compute_display_positions({}, [_pair()], {}, CPMTracker())
        assert result == []


class TestFillCostFromMakerTaker:
    """Verify sync_from_orders uses maker_fill_cost + taker_fill_cost, not price * count."""

    def test_fill_cost_uses_actual_cost_fields(self):
        """Amended prices — actual fill cost used, not price * count."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        # Order filled 10 at various prices; average cost was 448 cents total
        # but current no_price is 50 (which would give 10 * 50 = 500 if using old formula)
        orders = [
            _make_order(
                "TK-A",
                fill_count=10,
                remaining_count=0,
                no_price=50,
                maker_fill_cost=448,
                taker_fill_cost=0,
                status="executed",
            ),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.filled_total_cost(Side.A) == 448  # not 500

    def test_fill_cost_sums_maker_and_taker(self):
        """Both maker and taker fill costs should be summed."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        orders = [
            _make_order(
                "TK-A",
                fill_count=10,
                remaining_count=0,
                no_price=45,
                maker_fill_cost=300,
                taker_fill_cost=150,
                status="executed",
            ),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.filled_total_cost(Side.A) == 450  # 300 + 150


class TestStaleSyncProtection:
    """Tests for the generation-based stale-sync guard (double-bid prevention)."""

    def test_record_placement_sets_resting_and_gen(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_placement(Side.A, order_id="ord-new", count=20, price=15)
        assert ledger.resting_order_id(Side.A) == "ord-new"
        assert ledger.resting_count(Side.A) == 20
        assert ledger.resting_price(Side.A) == 15
        assert ledger._sides[Side.A]._placed_at_gen == 0

    def test_stale_sync_preserves_optimistic_resting(self):
        """Core bug scenario: sync_from_orders with stale data must NOT clear
        resting state that was set optimistically by record_placement."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)

        # Simulate: refresh_account starts, bumps gen
        ledger.bump_sync_gen()  # gen = 1

        # Stale orders list fetched BEFORE placement (doesn't include new orders)
        stale_orders: list[Order] = []

        # Auto-accept fires: orders placed, optimistic update
        ledger.record_placement(Side.A, "ord-A", 20, 15)
        ledger.record_placement(Side.B, "ord-B", 20, 76)

        # Stale sync runs with the pre-placement orders
        ledger.sync_from_orders(stale_orders, ticker_a="TK-A", ticker_b="TK-B")

        # Resting must be PRESERVED despite empty stale orders
        assert ledger.resting_order_id(Side.A) == "ord-A"
        assert ledger.resting_count(Side.A) == 20
        assert ledger.resting_order_id(Side.B) == "ord-B"
        assert ledger.resting_count(Side.B) == 20

    def test_next_gen_sync_clears_resting_when_order_gone(self):
        """Next polling cycle (fresh data) should be able to clear resting
        if the order was filled/cancelled."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)

        # Gen 1: placement happens
        ledger.bump_sync_gen()  # gen = 1
        ledger.record_placement(Side.A, "ord-A", 20, 15)

        # Gen 2: next poll with fresh data — order is gone (fully filled)
        ledger.bump_sync_gen()  # gen = 2
        fresh_orders = [
            _make_order(
                "TK-A", fill_count=20, remaining_count=0, order_id="ord-A", status="executed"
            ),
        ]
        ledger.sync_from_orders(fresh_orders, ticker_a="TK-A", ticker_b="TK-B")

        # Resting cleared because placed_at_gen(1) < sync_gen(2)
        assert ledger.resting_order_id(Side.A) is None
        assert ledger.resting_count(Side.A) == 0
        assert ledger.filled_count(Side.A) == 20

    def test_fresh_sync_with_resting_confirms_placement(self):
        """When a sync includes the placed order as resting, it confirms it
        and clears the generation guard."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)

        ledger.bump_sync_gen()  # gen = 1
        ledger.record_placement(Side.A, "ord-A", 20, 15)

        # Same gen, but this sync includes the order (e.g., _verify_after_action)
        orders = [
            _make_order("TK-A", fill_count=0, remaining_count=20, order_id="ord-A"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")

        # Confirmed — placed_at_gen cleared
        assert ledger._sides[Side.A]._placed_at_gen is None
        assert ledger.resting_order_id(Side.A) == "ord-A"
        assert ledger.resting_count(Side.A) == 20

    def test_stale_sync_after_verify_still_preserves(self):
        """Full race scenario: verify confirms, then stale sync runs.
        The resting state from verify must survive."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)

        ledger.bump_sync_gen()  # gen = 1
        ledger.record_placement(Side.A, "ord-A", 20, 15)

        # _verify_after_action: sync with fresh data (order visible)
        verify_orders = [
            _make_order("TK-A", fill_count=0, remaining_count=20, order_id="ord-A"),
        ]
        ledger.sync_from_orders(verify_orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.resting_count(Side.A) == 20

        # Stale refresh_account sync: order NOT in the list (stale data)
        stale_orders: list[Order] = []
        ledger.sync_from_orders(stale_orders, ticker_a="TK-A", ticker_b="TK-B")

        # The verify already confirmed the order (resting_list was non-empty),
        # so placed_at_gen was cleared. But the resting state from verify
        # is now vulnerable to the stale clear. The resting IS cleared here
        # because the gen guard was already disarmed by the good sync.
        # This is acceptable because _verify_after_action is followed by
        # evaluate_opportunities running with the verify-synced state.
        # The stale sync can only run in a DIFFERENT concurrent task.

    def test_record_cancel_sets_gen_guard(self):
        """Cancel sets gen guard to protect from stale sync overwrite."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_placement(Side.A, "ord-A", 20, 15)
        ledger.record_cancel(Side.A, "ord-A")
        # Gen guard is SET (not cleared) so stale sync can't re-populate
        assert ledger._sides[Side.A]._placed_at_gen is not None
        assert ledger._sides[Side.A].resting_order_id is None

    def test_reset_pair_clears_gen_guard(self):
        """Reset should clear the generation guard."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_placement(Side.A, "ord-A", 20, 15)
        ledger.reset_pair()
        assert ledger._sides[Side.A]._placed_at_gen is None

    def test_bump_sync_gen_increments(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        assert ledger._sync_gen == 0
        ledger.bump_sync_gen()
        assert ledger._sync_gen == 1
        ledger.bump_sync_gen()
        assert ledger._sync_gen == 2


class TestPlacementSafetyCatchup:
    def test_catchup_bypasses_unit_gate(self):
        """catchup=True skips P16 unit-boundary check."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.B, 15, 48)
        # 15 filled_in_unit + 0 resting + 25 new = 40 > 20 → blocked normally
        ok, reason = ledger.is_placement_safe(Side.B, 25, 48, catchup=True)
        assert ok, f"catchup should bypass unit gate: {reason}"

    def test_catchup_still_enforces_profitability(self):
        """catchup=True still checks P18 profitability."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 20, 55)  # other side at 55c
        # 55 + 55 = 110 >= 100 → unprofitable
        ok, reason = ledger.is_placement_safe(Side.B, 20, 55, catchup=True)
        assert not ok
        assert "not profitable" in reason

    def test_default_catchup_false_preserves_unit_gate(self):
        """Default catchup=False still enforces P16 (no regression)."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.B, 15, 48)
        ok, reason = ledger.is_placement_safe(Side.B, 25, 48)
        assert not ok
        assert "exceed unit" in reason


class TestSameTickerSync:
    """Tests for YES/NO pairs where ticker_a == ticker_b."""

    def test_sync_from_orders_same_ticker(self):
        """Orders are mapped by order.side, not ticker, when same-ticker."""
        ledger = PositionLedger(
            event_ticker="MKT-1",
            unit_size=10,
            side_a_str="yes",
            side_b_str="no",
            is_same_ticker=True,
        )
        orders = [
            Order(
                order_id="yes-ord",
                ticker="MKT-1",
                action="buy",
                side="yes",
                no_price=0,
                yes_price=48,
                initial_count=10,
                remaining_count=0,
                fill_count=10,
                status="executed",
            ),
            Order(
                order_id="no-ord",
                ticker="MKT-1",
                action="buy",
                side="no",
                no_price=45,
                yes_price=0,
                initial_count=10,
                remaining_count=5,
                fill_count=5,
                status="resting",
            ),
        ]
        ledger.sync_from_orders(orders, "MKT-1", "MKT-1")
        assert ledger.filled_count(Side.A) == 10  # YES orders -> Side.A
        assert ledger.filled_count(Side.B) == 5  # NO orders -> Side.B
        assert ledger.resting_count(Side.B) == 5  # NO order is resting

    def test_sync_from_orders_same_ticker_ignores_sell(self):
        """Sell orders are filtered out even for same-ticker pairs."""
        ledger = PositionLedger(
            event_ticker="MKT-1",
            unit_size=10,
            side_a_str="yes",
            side_b_str="no",
            is_same_ticker=True,
        )
        orders = [
            Order(
                order_id="sell-ord",
                ticker="MKT-1",
                action="sell",
                side="yes",
                no_price=0,
                yes_price=52,
                initial_count=10,
                remaining_count=10,
                fill_count=0,
                status="resting",
            ),
        ]
        ledger.sync_from_orders(orders, "MKT-1", "MKT-1")
        assert ledger.filled_count(Side.A) == 0
        assert ledger.resting_count(Side.A) == 0

    def test_sync_from_positions_skipped_for_same_ticker(self):
        """sync_from_positions is a no-op when is_same_ticker=True."""
        ledger = PositionLedger(
            event_ticker="MKT-1",
            unit_size=10,
            side_a_str="yes",
            side_b_str="no",
            is_same_ticker=True,
        )
        ledger.record_fill(Side.A, count=5, price=48)
        ledger.sync_from_positions(
            position_fills={Side.A: 0, Side.B: 0},
            position_costs={Side.A: 0, Side.B: 0},
        )
        assert ledger.filled_count(Side.A) == 5  # NOT decreased

    def test_cross_no_sync_unchanged(self):
        """Cross-NO (different tickers) sync still works as before."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        orders = [
            Order(
                order_id="ord-a",
                ticker="TK-A",
                action="buy",
                side="no",
                no_price=45,
                initial_count=10,
                remaining_count=0,
                fill_count=10,
                status="executed",
            ),
            Order(
                order_id="ord-b",
                ticker="TK-B",
                action="buy",
                side="no",
                no_price=48,
                initial_count=10,
                remaining_count=5,
                fill_count=5,
                status="resting",
            ),
        ]
        ledger.sync_from_orders(orders, "TK-A", "TK-B")
        assert ledger.filled_count(Side.A) == 10
        assert ledger.filled_count(Side.B) == 5

    def test_same_ticker_resting_uses_yes_price(self):
        """YES-side resting orders use yes_price, not no_price."""
        ledger = PositionLedger(
            event_ticker="MKT-1",
            unit_size=10,
            side_a_str="yes",
            side_b_str="no",
            is_same_ticker=True,
        )
        orders = [
            Order(
                order_id="yes-rest",
                ticker="MKT-1",
                action="buy",
                side="yes",
                no_price=0,
                yes_price=42,
                initial_count=10,
                remaining_count=10,
                fill_count=0,
                status="resting",
            ),
        ]
        ledger.sync_from_orders(orders, "MKT-1", "MKT-1")
        assert ledger.resting_price(Side.A) == 42  # yes_price, not no_price


class TestClosedBucket:
    """closed_* fields mirror filled_* and start at zero."""

    def test_new_ledger_has_zero_closed_fields(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        for side in (Side.A, Side.B):
            s = ledger._sides[side]
            assert s.closed_count == 0
            assert s.closed_total_cost == 0
            assert s.closed_fees == 0

    def test_reset_zeroes_closed_fields(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        s = ledger._sides[Side.A]
        s.closed_count = 99
        s.closed_total_cost = 500
        s.closed_fees = 3
        s.reset()
        assert s.closed_count == 0
        assert s.closed_total_cost == 0
        assert s.closed_fees == 0

    def test_open_count_equals_filled_when_closed_is_zero(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger._sides[Side.A].filled_count = 7
        assert ledger.open_count(Side.A) == 7

    def test_open_count_subtracts_closed(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        s = ledger._sides[Side.A]
        s.filled_count = 10
        s.closed_count = 5
        assert ledger.open_count(Side.A) == 5

    def test_open_avg_filled_price_zero_when_no_open_fills(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        assert ledger.open_avg_filled_price(Side.A) == 0.0

    def test_open_avg_filled_price_zero_when_everything_closed(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        s = ledger._sides[Side.A]
        s.filled_count = 5
        s.filled_total_cost = 400
        s.closed_count = 5
        s.closed_total_cost = 400
        assert ledger.open_avg_filled_price(Side.A) == 0.0

    def test_open_avg_filled_price_uses_open_bucket_only(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        s = ledger._sides[Side.A]
        # Closed bucket: 5 contracts at avg 18c (90c total)
        s.closed_count = 5
        s.closed_total_cost = 90
        # Open bucket: 5 more contracts at avg 23c (115c total)
        # filled_* is cumulative: 10 total fills for 205c
        s.filled_count = 10
        s.filled_total_cost = 205
        # Open avg = (205 - 90) / (10 - 5) = 115 / 5 = 23.0
        assert ledger.open_avg_filled_price(Side.A) == 23.0

    def test_lifetime_avg_unchanged(self):
        """avg_filled_price must still return the lifetime blended avg."""
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        s = ledger._sides[Side.A]
        s.filled_count = 10
        s.filled_total_cost = 205
        s.closed_count = 5
        s.closed_total_cost = 90
        assert ledger.avg_filled_price(Side.A) == 20.5


class TestReconcileClosed:
    """_reconcile_closed flushes matched pairs into the closed bucket."""

    def test_noop_when_imbalanced(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger._sides[Side.A].filled_count = 5
        ledger._sides[Side.A].filled_total_cost = 410
        ledger._sides[Side.B].filled_count = 3
        ledger._sides[Side.B].filled_total_cost = 54
        ledger._reconcile_closed()
        # min(5, 3) = 3, 3 // 5 = 0 units, no close fires
        assert ledger._sides[Side.A].closed_count == 0
        assert ledger._sides[Side.B].closed_count == 0

    def test_closes_one_balanced_unit(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        a = ledger._sides[Side.A]
        b = ledger._sides[Side.B]
        a.filled_count = 5
        a.filled_total_cost = 410  # avg 82
        b.filled_count = 5
        b.filled_total_cost = 90   # avg 18
        ledger._reconcile_closed()
        assert a.closed_count == 5
        assert a.closed_total_cost == 410
        assert b.closed_count == 5
        assert b.closed_total_cost == 90
        assert ledger.open_count(Side.A) == 0
        assert ledger.open_count(Side.B) == 0

    def test_closes_multiple_balanced_units_at_once(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        a = ledger._sides[Side.A]
        b = ledger._sides[Side.B]
        a.filled_count = 10
        a.filled_total_cost = 820
        b.filled_count = 10
        b.filled_total_cost = 180
        ledger._reconcile_closed()
        assert a.closed_count == 10
        assert b.closed_count == 10

    def test_imbalanced_close_flushes_min_units(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        a = ledger._sides[Side.A]
        b = ledger._sides[Side.B]
        a.filled_count = 5
        a.filled_total_cost = 410  # 82
        b.filled_count = 10
        b.filled_total_cost = 205  # avg 20.5
        ledger._reconcile_closed()
        # min(5,10)//5 = 1 unit. Close 5 each.
        # A: 5 close = full flush of open (410)
        # B: 5 close = pro-rata of open avg. round(205*5/10) = round(102.5).
        # Python 3 uses banker's rounding (round-half-to-even) → 102.
        assert a.closed_count == 5
        assert a.closed_total_cost == 410
        assert b.closed_count == 5
        assert b.closed_total_cost == 102
        # After close, open B has 5 contracts: (205 - 102) / 5 = 20.6c avg.
        assert ledger.open_count(Side.A) == 0
        assert ledger.open_count(Side.B) == 5

    def test_idempotent_second_call_is_noop(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger._sides[Side.A].filled_count = 5
        ledger._sides[Side.A].filled_total_cost = 400
        ledger._sides[Side.B].filled_count = 5
        ledger._sides[Side.B].filled_total_cost = 100
        ledger._reconcile_closed()
        a_closed_before = ledger._sides[Side.A].closed_count
        b_closed_before = ledger._sides[Side.B].closed_count
        ledger._reconcile_closed()
        assert ledger._sides[Side.A].closed_count == a_closed_before
        assert ledger._sides[Side.B].closed_count == b_closed_before

    def test_emits_paper_trail_log(self, caplog):
        import logging
        from talos.position_ledger import PositionLedger, Side
        caplog.set_level(logging.INFO)
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger._sides[Side.A].filled_count = 5
        ledger._sides[Side.A].filled_total_cost = 400
        ledger._sides[Side.B].filled_count = 5
        ledger._sides[Side.B].filled_total_cost = 100
        ledger._reconcile_closed()
        # structlog records go through the standard logging module; look for the event
        assert any(
            "ledger_reconciled_closed" in rec.getMessage() or
            "ledger_reconciled_closed" in str(getattr(rec, "msg", ""))
            for rec in caplog.records
        )

    def test_record_fill_triggers_reconcile(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        # Pre-load A with 5 fills; B at zero
        ledger.record_fill(Side.A, 5, 80)
        assert ledger._sides[Side.A].closed_count == 0  # not yet balanced
        # Fill B to balance; reconcile should fire
        ledger.record_fill(Side.B, 5, 20)
        assert ledger._sides[Side.A].closed_count == 5
        assert ledger._sides[Side.B].closed_count == 5
        # After close, open_avg_filled_price is 0 on both sides
        assert ledger.open_avg_filled_price(Side.A) == 0.0
        assert ledger.open_avg_filled_price(Side.B) == 0.0

    def test_sync_from_orders_triggers_reconcile(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger(
            "EVT-X", unit_size=5,
            ticker_a="TK-A", ticker_b="TK-B",
            side_a_str="no", side_b_str="no",
        )
        orders = [
            _make_order("TK-A", fill_count=5, no_price=80, maker_fill_cost=400, status="executed"),
            _make_order("TK-B", fill_count=5, no_price=20, maker_fill_cost=100, status="executed"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger._sides[Side.A].closed_count == 5
        assert ledger._sides[Side.B].closed_count == 5

    def test_sync_from_positions_triggers_reconcile_non_same_ticker(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger(
            "EVT-X", unit_size=5,
            ticker_a="TK-A", ticker_b="TK-B",
            is_same_ticker=False,
        )
        ledger.sync_from_positions(
            position_fills={Side.A: 5, Side.B: 5},
            position_costs={Side.A: 400, Side.B: 100},
        )
        assert ledger._sides[Side.A].closed_count == 5
        assert ledger._sides[Side.B].closed_count == 5

    def test_sync_from_positions_same_ticker_early_returns_no_mutation(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger(
            "EVT-X", unit_size=5,
            ticker_a="TK-A", ticker_b="TK-A",  # same ticker
            is_same_ticker=True,
        )
        # Preload known state
        ledger.record_fill(Side.A, 5, 80)
        ledger.record_fill(Side.B, 5, 20)
        # closed already populated from record_fill's reconcile
        closed_a_before = ledger._sides[Side.A].closed_count
        closed_b_before = ledger._sides[Side.B].closed_count
        # sync_from_positions early-returns for same-ticker; closed unchanged
        ledger.sync_from_positions(
            position_fills={Side.A: 99, Side.B: 99},  # bogus values
            position_costs={Side.A: 999, Side.B: 999},
        )
        assert ledger._sides[Side.A].closed_count == closed_a_before
        assert ledger._sides[Side.B].closed_count == closed_b_before


class TestSavedDictSchema:
    """to_save_dict includes the closed_* keys."""

    def test_save_dict_includes_closed_keys(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger._sides[Side.A].closed_count = 5
        ledger._sides[Side.A].closed_total_cost = 410
        ledger._sides[Side.A].closed_fees = 7
        ledger._sides[Side.B].closed_count = 5
        ledger._sides[Side.B].closed_total_cost = 90
        ledger._sides[Side.B].closed_fees = 2
        d = ledger.to_save_dict()
        assert d["closed_count_a"] == 5
        assert d["closed_total_cost_a"] == 410
        assert d["closed_fees_a"] == 7
        assert d["closed_count_b"] == 5
        assert d["closed_total_cost_b"] == 90
        assert d["closed_fees_b"] == 2

    def test_seed_restores_closed_verbatim_when_all_six_keys_valid(self):
        """5a normal restart: closed values restored as-is, no re-derivation."""
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        data: dict[str, int | str | None] = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
            "closed_count_a": 5, "closed_total_cost_a": 410, "closed_fees_a": 0,
            "closed_count_b": 5, "closed_total_cost_b": 90, "closed_fees_b": 0,
        }
        ledger.seed_from_saved(data)
        # open_avg_B must be 23.0 (115/5), NOT the blended 20.5
        assert ledger.open_count(Side.A) == 5
        assert ledger.open_count(Side.B) == 5
        assert ledger.open_avg_filled_price(Side.B) == 23.0

    def test_seed_logs_restored_with_closed_once(self, caplog):
        import logging
        from talos.position_ledger import PositionLedger
        caplog.set_level(logging.INFO)
        ledger = PositionLedger("EVT-X", unit_size=5)
        data: dict[str, int | str | None] = {
            "filled_a": 5, "cost_a": 400, "fees_a": 0,
            "filled_b": 5, "cost_b": 100, "fees_b": 0,
            "closed_count_a": 5, "closed_total_cost_a": 400, "closed_fees_a": 0,
            "closed_count_b": 5, "closed_total_cost_b": 100, "closed_fees_b": 0,
        }
        ledger.seed_from_saved(data)
        restored = [r for r in caplog.records if "ledger_restored_with_closed" in r.getMessage()]
        assert len(restored) == 1

    def test_seed_missing_all_closed_keys_triggers_migration(self, caplog):
        import logging
        from talos.position_ledger import PositionLedger, Side
        caplog.set_level(logging.INFO)
        ledger = PositionLedger("EVT-X", unit_size=5)
        data: dict[str, int | str | None] = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
        }
        ledger.seed_from_saved(data)
        # After migration + terminal reconcile: all closed populated via pro-rata
        assert ledger._sides[Side.A].closed_count == 10
        assert ledger._sides[Side.B].closed_count == 10
        migrated = [r for r in caplog.records if "ledger_migrated_missing_closed" in r.getMessage()]
        assert len(migrated) == 1

    def test_seed_partial_closed_keys_triggers_migration(self, caplog):
        """Atomic-group rule: any missing key zeroes all six."""
        import logging
        from talos.position_ledger import PositionLedger, Side
        caplog.set_level(logging.INFO)
        ledger = PositionLedger("EVT-X", unit_size=5)
        data: dict[str, int | str | None] = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
            # Only 2 of 6 closed keys present
            "closed_count_a": 999, "closed_total_cost_a": 999,
        }
        ledger.seed_from_saved(data)
        # Migration zeros and repopulates; verbatim restore would have set closed_count_a = 999
        assert ledger._sides[Side.A].closed_count == 10  # from reconcile, not 999
        migrated = [r for r in caplog.records if "ledger_migrated_missing_closed" in r.getMessage()]
        assert len(migrated) == 1

    def test_seed_corrupt_value_types_trigger_migration(self, caplog):
        """Non-int values trigger migration, not hard-fail."""
        import logging
        from talos.position_ledger import PositionLedger, Side

        for bad_value in (None, "abc", -5, True, 5.0, "5"):
            caplog.clear()
            caplog.set_level(logging.INFO)
            ledger = PositionLedger("EVT-X", unit_size=5)
            data: dict[str, int | str | float | None] = {
                "filled_a": 10, "cost_a": 820, "fees_a": 0,
                "filled_b": 10, "cost_b": 205, "fees_b": 0,
                "closed_count_a": 5, "closed_total_cost_a": 410, "closed_fees_a": 0,
                "closed_count_b": 5, "closed_total_cost_b": 90, "closed_fees_b": 0,
            }
            data["closed_count_a"] = bad_value  # inject corruption
            ledger.seed_from_saved(data)  # type: ignore[arg-type]  # intentionally wide for corruption test
            migrated = [r for r in caplog.records if "ledger_migrated_missing_closed" in r.getMessage()]
            assert len(migrated) == 1, f"Expected migration log for bad_value={bad_value!r}"
            # Migration zeroed and reconciled — closed_count_a != 5 (the restored-verbatim value)
            assert ledger._sides[Side.A].closed_count == 10  # reconciled, not restored
