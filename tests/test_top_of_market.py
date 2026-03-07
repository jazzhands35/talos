"""Tests for TopOfMarketTracker."""

from __future__ import annotations

from talos.models.order import Order
from talos.models.strategy import ArbPair
from talos.models.ws import OrderBookSnapshot
from talos.orderbook import OrderBookManager
from talos.top_of_market import TopOfMarketTracker


def _snapshot(yes: list[list[int]], no: list[list[int]]) -> OrderBookSnapshot:
    return OrderBookSnapshot(market_ticker="", market_id="", yes=yes, no=no)


def _order(
    ticker: str,
    no_price: int,
    *,
    remaining: int = 5,
    filled: int = 0,
    status: str = "resting",
) -> Order:
    return Order(
        order_id=f"ord-{ticker}-{no_price}",
        ticker=ticker,
        action="buy",
        side="no",
        no_price=no_price,
        initial_count=remaining + filled,
        remaining_count=remaining,
        fill_count=filled,
        status=status,
    )


PAIR = ArbPair(event_ticker="EVT-A", ticker_a="MKT-A", ticker_b="MKT-B")


def _make_tracker() -> tuple[OrderBookManager, TopOfMarketTracker]:
    books = OrderBookManager()
    tracker = TopOfMarketTracker(books)
    return books, tracker


class TestIsAtTop:
    def test_at_top_when_price_matches_best(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 10]]))
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])
        tracker.check("MKT-A")
        assert tracker.is_at_top("MKT-A") is True

    def test_not_at_top_when_jumped(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[48, 5], [47, 10]]))
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])
        tracker.check("MKT-A")
        assert tracker.is_at_top("MKT-A") is False

    def test_none_when_no_resting_orders(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 10]]))
        tracker.check("MKT-A")
        assert tracker.is_at_top("MKT-A") is None

    def test_uses_highest_resting_price(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 10]]))
        orders = [_order("MKT-A", 45), _order("MKT-A", 47)]
        tracker.update_orders(orders, [PAIR])
        tracker.check("MKT-A")
        assert tracker.is_at_top("MKT-A") is True

    def test_partially_filled_still_tracked(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[48, 5], [47, 10]]))
        tracker.update_orders([_order("MKT-A", 47, remaining=2, filled=3)], [PAIR])
        tracker.check("MKT-A")
        assert tracker.is_at_top("MKT-A") is False

    def test_fully_filled_not_tracked(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[48, 5]]))
        tracker.update_orders(
            [_order("MKT-A", 47, remaining=0, filled=5, status="executed")],
            [PAIR],
        )
        tracker.check("MKT-A")
        assert tracker.is_at_top("MKT-A") is None

    def test_resting_price_query(self) -> None:
        books, tracker = _make_tracker()
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])
        assert tracker.resting_price("MKT-A") == 47
        assert tracker.resting_price("MKT-B") is None
