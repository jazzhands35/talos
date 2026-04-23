"""Tests for TopOfMarketTracker."""

from __future__ import annotations

from talos.models.order import Order
from talos.models.strategy import ArbPair
from talos.models.ws import OrderBookSnapshot
from talos.orderbook import OrderBookManager
from talos.scanner import ArbitrageScanner
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
        no_price_bps=no_price * 100,
        initial_count_fp100=(remaining + filled) * 100,
        remaining_count_fp100=remaining * 100,
        fill_count_fp100=filled * 100,
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


class TestCallbackTransitions:
    def test_callback_fires_on_loss(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 10]]))
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])
        tracker.check("MKT-A")  # initial state: at top

        changes: list[tuple[str, str, bool]] = []
        tracker.on_change = lambda t, s, at: changes.append((t, s, at))

        # Someone penny jumps at 48
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[48, 5], [47, 10]]))
        tracker.check("MKT-A")

        assert changes == [("MKT-A", "no", False)]

    def test_callback_fires_on_regain(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[48, 5], [47, 10]]))
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])
        tracker.check("MKT-A")  # initial: not at top

        changes: list[tuple[str, str, bool]] = []
        tracker.on_change = lambda t, s, at: changes.append((t, s, at))

        # 48 level gets consumed
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 10]]))
        tracker.check("MKT-A")

        assert changes == [("MKT-A", "no", True)]

    def test_no_duplicate_callbacks(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 10]]))
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])
        tracker.check("MKT-A")  # initial: at top

        changes: list[tuple[str, str, bool]] = []
        tracker.on_change = lambda t, s, at: changes.append((t, s, at))

        # Book updates but top doesn't change
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 15]]))
        tracker.check("MKT-A")
        tracker.check("MKT-A")

        assert changes == []

    def test_first_check_at_top_no_callback(self) -> None:
        """First observation at top — no notification needed."""
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 10]]))
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])

        changes: list[tuple[str, str, bool]] = []
        tracker.on_change = lambda t, s, at: changes.append((t, s, at))

        tracker.check("MKT-A")

        assert changes == []
        assert tracker.is_at_top("MKT-A") is True

    def test_first_check_jumped_fires_callback(self) -> None:
        """First observation already jumped — must notify (P20)."""
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[48, 5], [47, 10]]))
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])

        changes: list[tuple[str, str, bool]] = []
        tracker.on_change = lambda t, s, at: changes.append((t, s, at))

        tracker.check("MKT-A")

        assert changes == [("MKT-A", "no", False)]
        assert tracker.is_at_top("MKT-A") is False

    def test_order_removed_clears_state(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 10]]))
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])
        tracker.check("MKT-A")

        # Orders cleared
        tracker.update_orders([], [PAIR])
        assert tracker.is_at_top("MKT-A") is None
        assert tracker.resting_price("MKT-A") is None


class TestTableIntegration:
    def test_warning_prefix_in_q_column(self) -> None:
        """Q column shows !! prefix when not at top of market."""
        books = OrderBookManager()
        scanner = ArbitrageScanner(books)
        tracker = TopOfMarketTracker(books)

        scanner.add_pair("EVT-A", "MKT-A", "MKT-B")

        # Set up orderbook: MKT-A has been jumped, MKT-B is at top
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[48, 5], [47, 10]]))
        books.apply_snapshot("MKT-B", _snapshot(yes=[], no=[[45, 10]]))
        scanner.scan("MKT-A")
        scanner.scan("MKT-B")

        # Set resting orders
        orders = [_order("MKT-A", 47), _order("MKT-B", 45)]
        tracker.update_orders(orders, scanner.pairs)
        tracker.check("MKT-A")
        tracker.check("MKT-B")

        assert tracker.is_at_top("MKT-A") is False
        assert tracker.is_at_top("MKT-B") is True


class TestYesNoTracking:
    """Tests for same-ticker YES/NO pair jump detection."""

    def test_tracks_yes_and_no_orders_separately(self) -> None:
        books, tracker = _make_tracker()
        pair = ArbPair(
            event_ticker="MKT-1",
            ticker_a="MKT-1",
            ticker_b="MKT-1",
            side_a="yes",
            side_b="no",
        )
        yes_order = Order(
            order_id="yes-1",
            ticker="MKT-1",
            action="buy",
            side="yes",
            no_price_bps=0,
            yes_price_bps=4800,
            initial_count_fp100=1000,
            remaining_count_fp100=1000,
            fill_count_fp100=0,
            status="resting",
        )
        no_order = Order(
            order_id="no-1",
            ticker="MKT-1",
            action="buy",
            side="no",
            no_price_bps=4500,
            yes_price_bps=0,
            initial_count_fp100=1000,
            remaining_count_fp100=1000,
            fill_count_fp100=0,
            status="resting",
        )
        tracker.update_orders([yes_order, no_order], [pair])
        assert tracker.resting_price("MKT-1", "yes") == 48
        assert tracker.resting_price("MKT-1", "no") == 45

    def test_yes_side_jumped(self) -> None:
        books, tracker = _make_tracker()
        pair = ArbPair(
            event_ticker="MKT-1",
            ticker_a="MKT-1",
            ticker_b="MKT-1",
            side_a="yes",
            side_b="no",
        )
        yes_order = Order(
            order_id="yes-1",
            ticker="MKT-1",
            action="buy",
            side="yes",
            no_price_bps=0,
            yes_price_bps=4800,
            initial_count_fp100=1000,
            remaining_count_fp100=1000,
            fill_count_fp100=0,
            status="resting",
        )
        tracker.update_orders([yes_order], [pair])
        # Book YES top is 50 (someone bid higher) -> we got jumped
        books.apply_snapshot("MKT-1", _snapshot(yes=[[50, 5], [48, 10]], no=[[45, 20]]))
        tracker.check("MKT-1", side="yes")
        assert tracker.is_at_top("MKT-1", "yes") is False

    def test_no_side_at_top_while_yes_jumped(self) -> None:
        """NO side can be at top while YES side is jumped on same ticker."""
        books, tracker = _make_tracker()
        pair = ArbPair(
            event_ticker="MKT-1",
            ticker_a="MKT-1",
            ticker_b="MKT-1",
            side_a="yes",
            side_b="no",
        )
        yes_order = Order(
            order_id="yes-1",
            ticker="MKT-1",
            action="buy",
            side="yes",
            no_price_bps=0,
            yes_price_bps=4800,
            initial_count_fp100=1000,
            remaining_count_fp100=1000,
            fill_count_fp100=0,
            status="resting",
        )
        no_order = Order(
            order_id="no-1",
            ticker="MKT-1",
            action="buy",
            side="no",
            no_price_bps=4500,
            yes_price_bps=0,
            initial_count_fp100=1000,
            remaining_count_fp100=1000,
            fill_count_fp100=0,
            status="resting",
        )
        tracker.update_orders([yes_order, no_order], [pair])
        # YES jumped (50 > 48), NO at top (45 is best)
        books.apply_snapshot("MKT-1", _snapshot(yes=[[50, 5], [48, 10]], no=[[45, 20]]))
        tracker.check("MKT-1", side="yes")
        tracker.check("MKT-1", side="no")
        assert tracker.is_at_top("MKT-1", "yes") is False
        assert tracker.is_at_top("MKT-1", "no") is True
