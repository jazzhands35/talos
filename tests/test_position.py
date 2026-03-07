"""Tests for position computation logic."""

from __future__ import annotations

import pytest

from talos.fees import MAKER_FEE_RATE
from talos.models.order import Order
from talos.models.strategy import ArbPair
from talos.position import compute_event_positions


def _order(
    ticker: str,
    *,
    fill_count: int = 0,
    remaining_count: int = 0,
    no_price: int = 0,
    side: str = "no",
    action: str = "buy",
    status: str = "resting",
    queue_position: int | None = None,
) -> Order:
    return Order(
        order_id=f"ord-{ticker}-{fill_count}-{remaining_count}",
        ticker=ticker,
        action=action,
        side=side,
        no_price=no_price,
        initial_count=fill_count + remaining_count,
        remaining_count=remaining_count,
        fill_count=fill_count,
        status=status,
        queue_position=queue_position,
    )


PAIR = ArbPair(event_ticker="EVT-AB", ticker_a="MKT-A", ticker_b="MKT-B")


class TestNoOrders:
    def test_empty_orders_returns_empty(self) -> None:
        assert compute_event_positions([], [PAIR]) == []

    def test_no_pairs_returns_empty(self) -> None:
        orders = [_order("MKT-A", fill_count=3, remaining_count=2, no_price=31)]
        assert compute_event_positions(orders, []) == []


class TestBothLegsMatched:
    def test_equal_fills_compute_locked_profit(self) -> None:
        orders = [
            _order("MKT-A", fill_count=5, remaining_count=0, no_price=31),
            _order("MKT-B", fill_count=5, remaining_count=0, no_price=67),
        ]
        result = compute_event_positions(orders, [PAIR])
        assert len(result) == 1
        s = result[0]
        assert s.matched_pairs == 5
        # Raw: 100 - 31 - 67 = 2¢/pair → 10¢ total
        # Fee (worst case, B wins): (500 - 155) * 0.0175 = 6.0375
        raw = 5 * (100 - 31 - 67)
        worst_fee = (5 * 100 - 5 * 31) * MAKER_FEE_RATE
        assert s.locked_profit_cents == pytest.approx(raw - worst_fee)
        assert s.unmatched_a == 0
        assert s.unmatched_b == 0
        assert s.exposure_cents == 0


class TestOneLegAhead:
    def test_leg_a_ahead_shows_exposure(self) -> None:
        orders = [
            _order("MKT-A", fill_count=5, remaining_count=0, no_price=31),
            _order("MKT-B", fill_count=3, remaining_count=2, no_price=67),
        ]
        result = compute_event_positions(orders, [PAIR])
        s = result[0]
        assert s.matched_pairs == 3
        # 3 matched: raw = 3*2=6, worst fee = (300-93)*0.0175 = 3.6225
        raw = 3 * (100 - 31 - 67)
        worst_fee = (3 * 100 - 3 * 31) * MAKER_FEE_RATE
        assert s.locked_profit_cents == pytest.approx(raw - worst_fee)
        assert s.unmatched_a == 2
        assert s.unmatched_b == 0
        assert s.exposure_cents == 2 * 31

    def test_leg_b_ahead_shows_exposure(self) -> None:
        orders = [
            _order("MKT-A", fill_count=2, remaining_count=3, no_price=31),
            _order("MKT-B", fill_count=5, remaining_count=0, no_price=67),
        ]
        result = compute_event_positions(orders, [PAIR])
        s = result[0]
        assert s.matched_pairs == 2
        assert s.unmatched_a == 0
        assert s.unmatched_b == 3
        assert s.exposure_cents == 3 * 67


class TestMultipleOrdersAccumulate:
    def test_two_orders_same_leg_accumulate(self) -> None:
        orders = [
            _order("MKT-A", fill_count=3, remaining_count=2, no_price=31),
            _order("MKT-A", fill_count=2, remaining_count=1, no_price=33),
            _order("MKT-B", fill_count=5, remaining_count=0, no_price=67),
        ]
        result = compute_event_positions(orders, [PAIR])
        s = result[0]
        assert s.leg_a.filled_count == 5  # 3 + 2
        assert s.leg_a.resting_count == 3  # 2 + 1
        # Weighted average: (31*3 + 33*2) / 5 = 159/5 = 31
        assert s.leg_a.no_price == 31
        assert s.matched_pairs == 5
        # Raw profit: 500 - 159 - 335 = 6
        # Worst fee (B wins): (500 - 159) * 0.0175 = 5.9675
        raw = 500 - 159 - 335
        worst_fee = (500 - 159) * MAKER_FEE_RATE
        assert s.locked_profit_cents == pytest.approx(raw - worst_fee)


class TestMixedPricePnL:
    def test_mixed_prices_both_legs_correct_pnl(self) -> None:
        """Regression: max-price formula cross-multiplied worst prices, giving wrong P&L."""
        orders = [
            _order("MKT-A", fill_count=5, remaining_count=0, no_price=34),
            _order("MKT-A", fill_count=5, remaining_count=0, no_price=35),
            _order("MKT-B", fill_count=5, remaining_count=0, no_price=64),
            _order("MKT-B", fill_count=5, remaining_count=0, no_price=67),
        ]
        result = compute_event_positions(orders, [PAIR])
        s = result[0]
        assert s.matched_pairs == 10
        # Actual costs: A = 5*34 + 5*35 = 345, B = 5*64 + 5*67 = 655
        # Raw profit = 1000 - 345 - 655 = 0
        # With fees: worst case fee on winning side profit, profit is negative after fees
        assert s.locked_profit_cents < 0  # fees make break-even into a loss
        assert s.exposure_cents == 0


class TestUnrecognizedOrders:
    def test_orders_not_in_any_pair_ignored(self) -> None:
        orders = [
            _order("UNKNOWN-MKT", fill_count=10, remaining_count=5, no_price=50),
        ]
        assert compute_event_positions(orders, [PAIR]) == []

    def test_yes_side_orders_ignored(self) -> None:
        orders = [
            _order("MKT-A", fill_count=5, remaining_count=0, no_price=31, side="yes"),
        ]
        assert compute_event_positions(orders, [PAIR]) == []

    def test_sell_action_orders_ignored(self) -> None:
        orders = [
            _order("MKT-A", fill_count=5, remaining_count=0, no_price=31, action="sell"),
        ]
        assert compute_event_positions(orders, [PAIR]) == []

    def test_cancelled_orders_excluded(self) -> None:
        """Cancelled orders must not inflate fill or resting counts."""
        orders = [
            _order("MKT-A", fill_count=5, remaining_count=0, no_price=31, status="executed"),
            _order("MKT-A", fill_count=0, remaining_count=5, no_price=31, status="cancelled"),
            _order("MKT-B", fill_count=5, remaining_count=0, no_price=67, status="executed"),
        ]
        result = compute_event_positions(orders, [PAIR])
        s = result[0]
        assert s.leg_a.filled_count == 5
        assert s.leg_a.resting_count == 0  # cancelled order excluded
        assert s.matched_pairs == 5


class TestLegSummaryFields:
    def test_leg_summary_populated(self) -> None:
        orders = [
            _order("MKT-A", fill_count=3, remaining_count=2, no_price=31),
            _order("MKT-B", fill_count=3, remaining_count=2, no_price=67),
        ]
        result = compute_event_positions(orders, [PAIR])
        s = result[0]
        assert s.leg_a.ticker == "MKT-A"
        assert s.leg_a.no_price == 31
        assert s.leg_a.filled_count == 3
        assert s.leg_a.resting_count == 2
        assert s.leg_b.ticker == "MKT-B"
        assert s.leg_b.no_price == 67

    def test_total_fill_cost_propagated(self) -> None:
        """total_fill_cost on LegSummary should match sum of price*fills."""
        orders = [
            _order("MKT-A", fill_count=3, remaining_count=2, no_price=31),
            _order("MKT-B", fill_count=3, remaining_count=2, no_price=67),
        ]
        result = compute_event_positions(orders, [PAIR])
        s = result[0]
        assert s.leg_a.total_fill_cost == 3 * 31
        assert s.leg_b.total_fill_cost == 3 * 67


class TestRestingOnlyPair:
    def test_resting_only_still_shows_summary(self) -> None:
        """Resting orders with zero fills should still appear."""
        orders = [
            _order("MKT-A", fill_count=0, remaining_count=5, no_price=31),
            _order("MKT-B", fill_count=0, remaining_count=5, no_price=67),
        ]
        result = compute_event_positions(orders, [PAIR])
        assert len(result) == 1
        s = result[0]
        assert s.matched_pairs == 0
        assert s.locked_profit_cents == 0
        assert s.exposure_cents == 0


class TestQueuePosition:
    def test_best_queue_position_per_leg(self) -> None:
        """Takes the lowest queue position among resting orders."""
        orders = [
            _order("MKT-A", fill_count=0, remaining_count=3, no_price=31, queue_position=15),
            _order("MKT-A", fill_count=0, remaining_count=2, no_price=31, queue_position=8),
            _order("MKT-B", fill_count=0, remaining_count=5, no_price=67, queue_position=42),
        ]
        result = compute_event_positions(orders, [PAIR])
        assert result[0].leg_a.queue_position == 8
        assert result[0].leg_b.queue_position == 42

    def test_queue_position_none_when_no_fills_and_no_data(self) -> None:
        """Resting-only orders with no queue data → None (unknown)."""
        orders = [
            _order("MKT-A", fill_count=0, remaining_count=5, no_price=31),
            _order("MKT-B", fill_count=0, remaining_count=5, no_price=67),
        ]
        result = compute_event_positions(orders, [PAIR])
        assert result[0].leg_a.queue_position is None
        assert result[0].leg_b.queue_position is None

    def test_zero_queue_position_treated_as_no_data(self) -> None:
        """API returns 0 meaning 'no data' — should not become position 1."""
        orders = [
            _order("MKT-A", fill_count=0, remaining_count=5, no_price=31, queue_position=0),
            _order("MKT-B", fill_count=0, remaining_count=5, no_price=67, queue_position=42),
        ]
        result = compute_event_positions(orders, [PAIR])
        assert result[0].leg_a.queue_position is None
        assert result[0].leg_b.queue_position == 42

    def test_partially_filled_no_queue_data_is_none(self) -> None:
        """Partially filled order with no queue data → None (unknown)."""
        orders = [
            _order("MKT-A", fill_count=3, remaining_count=2, no_price=31),
            _order("MKT-B", fill_count=3, remaining_count=2, no_price=67),
        ]
        result = compute_event_positions(orders, [PAIR])
        assert result[0].leg_a.queue_position is None
        assert result[0].leg_b.queue_position is None

    def test_queue_position_ignored_for_filled_orders(self) -> None:
        """Fully filled orders (remaining=0) don't contribute queue position."""
        orders = [
            _order("MKT-A", fill_count=5, remaining_count=0, no_price=31, queue_position=3),
            _order("MKT-B", fill_count=5, remaining_count=0, no_price=67, queue_position=7),
        ]
        result = compute_event_positions(orders, [PAIR])
        assert result[0].leg_a.queue_position is None
        assert result[0].leg_b.queue_position is None
