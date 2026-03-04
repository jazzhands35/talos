"""Tests for position computation logic."""

from __future__ import annotations

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
        # 100 - 31 - 67 = 2¢ profit per pair
        assert s.locked_profit_cents == 5 * 2
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
        assert s.locked_profit_cents == 3 * 2
        assert s.unmatched_a == 2
        assert s.unmatched_b == 0
        # Exposure: 2 unmatched A contracts × 31¢
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
        # Worst-case price should be 33 (the higher one)
        assert s.leg_a.no_price == 33
        assert s.matched_pairs == 5


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
